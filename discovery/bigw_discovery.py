"""
Big W AU — Product Discovery
==============================
Discovers TCG product URLs from Big W AU category pages.

Strategy:
  1. Parse raw HTML with BeautifulSoup — Big W uses Next.js but
     server-renders initial product data with JSON-LD and data attributes.
  2. Playwright with persistent context — for full JS-rendered content.
     Use --headed on first run if any challenge appears.

Usage:
    python discovery/bigw_discovery.py --tcg pokemon --dry-run
    python discovery/bigw_discovery.py --tcg pokemon --dry-run --headed

Setup:
    pip install playwright beautifulsoup4 lxml requests
    playwright install chromium
"""

import os
import re
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from utils.database import Database
    from canonical.matcher import match_product
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    match_product = None
    print("⚠️  Could not import project utils — running in standalone mode")

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────

BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")

# Big W category/search URLs per TCG
BIGW_CATEGORY_URLS = {
    "pokemon": [
        "https://www.bigw.com.au/toys-outdoor-sport/toys/games-puzzles-cards/trading-card-games/cat/cat_17680",
        "https://www.bigw.com.au/search?q=pokemon+trading+card",
    ],
    "one-piece": [
        "https://www.bigw.com.au/search?q=one+piece+trading+card",
    ],
    "mtg": [
        "https://www.bigw.com.au/search?q=magic+the+gathering+trading+card",
    ],
    "dragon-ball-z": [
        "https://www.bigw.com.au/search?q=dragon+ball+super+card+game",
    ],
    "lorcana": [
        "https://www.bigw.com.au/search?q=disney+lorcana+trading+card",
    ],
}

TCG_NAME_KEYWORDS = {
    "pokemon": ["pokemon"],
    "one-piece": ["one piece", "op-"],
    "mtg": ["magic: the gathering", "magic gathering", "commander"],
    "dragon-ball-z": ["dragon ball", "dbz"],
    "lorcana": ["lorcana"],
}

PRODUCT_ALLOWLIST = [
    "booster box", "booster bundle", "booster pack", "booster",
    "elite trainer box", "etb", "collection box", "premium collection",
    "tin", "blister", "starter deck", "theme deck",
    "build & battle", "trainer kit", "league battle deck", "knock out collection",
]

PRODUCT_BLOCKLIST = [
    "portfolio", "binder", "card sleeve", "sleeves", "deck box", "playmat",
    "card case", "display case", "storage box", "mini portfolio",
    "9-pocket", "4-pocket", "card divider", "damage counter",
    "coin", "dice", "figure", "plush", "squishmallow",
    "t-shirt", "poster", "card game mat", "checklane",
    "jersey", "hoodie", "cap", "hat", "backpack", "bag",
    "wallet", "keychain", "lanyard", "mug", "water bottle",
]

POKEMON_SETS = {
    "journey together": "journey-together",
    "prismatic evolutions": "prismatic-evolutions",
    "surging sparks": "surging-sparks",
    "paldean fates": "paldean-fates",
    "pokemon 151": "pokemon-151",
    "pokémon 151": "pokemon-151",
    "destined rivals": "destined-rivals",
    "perfect order": "perfect-order",
    "ascended heroes": "ascended-heroes",
    "phantasmal flames": "phantasmal-flames",
    "mega evolutions": "mega-evolutions",
    "mega evolution": "mega-evolutions",
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ─── HTML Parsing ────────────────────────────────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product tiles from Big W HTML.

    Big W (Next.js) renders product tiles as:
        <article data-testid="product-tile" ...>
            <a href="/product/NAME/p/SKU">
                <img src="..." alt="NAME">
            </a>
            <div data-testid="product-title">NAME</div>
            <div data-testid="product-price">$XX.XX</div>
        </article>

    Also tries to parse embedded JSON product data from __NEXT_DATA__.
    """
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    products = []

    # Try __NEXT_DATA__ JSON first (most reliable for Next.js sites)
    next_data_script = soup.select_one("script#__NEXT_DATA__")
    if next_data_script and next_data_script.string:
        try:
            next_data = json.loads(next_data_script.string)
            # Navigate the Next.js data structure to find products
            page_props = (
                next_data.get("props", {})
                .get("pageProps", {})
            )
            # Products may be in various locations within pageProps
            raw_items = (
                page_props.get("products", [])
                or page_props.get("searchResults", {}).get("products", [])
                or page_props.get("categoryData", {}).get("products", [])
                or page_props.get("data", {}).get("products", [])
            )
            for item in raw_items:
                name = item.get("name", "") or item.get("title", "")
                slug = item.get("slug", "") or item.get("urlPath", "")
                sku = str(item.get("id", "") or item.get("sku", ""))
                price_raw = item.get("price", {})
                if isinstance(price_raw, dict):
                    price_num = price_raw.get("current", {}).get("value") or price_raw.get("amount")
                else:
                    price_num = price_raw
                image_url = ""
                images = item.get("images", [])
                if images and isinstance(images, list):
                    first = images[0]
                    image_url = first.get("url", "") if isinstance(first, dict) else first

                if not name:
                    continue

                if slug:
                    href = f"https://www.bigw.com.au{slug}" if slug.startswith("/") else slug
                elif sku:
                    href = f"https://www.bigw.com.au/product/p/{sku}"
                else:
                    continue

                href = href.split("?")[0]
                if href in seen:
                    continue
                seen.add(href)

                price_str = f"${float(price_num):.2f}" if price_num else ""
                products.append({
                    "name": name,
                    "url": href,
                    "price": price_str,
                    "price_raw": float(price_num) if price_num else None,
                    "sku": sku,
                    "image": image_url,
                    "is_preorder": False,
                    "promo": "",
                })
            if products:
                return products
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    # Fall back to HTML tile parsing
    tile_selectors = [
        "article[data-testid='product-tile']",
        "[data-testid='product-tile']",
        "article",
        ".product-tile",
        ".ProductTile",
    ]

    tiles = []
    for selector in tile_selectors:
        found = soup.select(selector)
        if found:
            tiles = found
            break

    for tile in tiles:
        # Get link
        link = tile.select_one('a[href*="/product/"]') or tile.select_one("a")
        if not link:
            continue
        href = link.get("href", "")
        if not href or "/product/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.bigw.com.au" + href
        href = href.split("?")[0]

        if href in seen:
            continue
        seen.add(href)

        # Get name
        name = ""
        name_el = (
            tile.select_one("[data-testid='product-title']")
            or tile.select_one("h3")
            or tile.select_one("h2")
            or tile.select_one("[class*='title']")
            or tile.select_one("[class*='name']")
        )
        if name_el:
            name = name_el.get_text(strip=True)
        if not name:
            # Try alt text of first image
            img = tile.select_one("img")
            if img:
                name = img.get("alt", "").strip()

        # Get price
        price_str = ""
        price_num = None
        price_el = (
            tile.select_one("[data-testid='product-price']")
            or tile.select_one("[class*='price']")
            or tile.select_one("[class*='Price']")
        )
        if price_el:
            raw = price_el.get_text(strip=True)
            match = re.search(r"\$?([\d,]+\.?\d*)", raw)
            if match:
                try:
                    price_num = float(match.group(1).replace(",", ""))
                    price_str = f"${price_num:.2f}"
                except ValueError:
                    pass

        # Get image
        image_url = ""
        img = tile.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:"):
                image_url = "https:" + src if src.startswith("//") else src

        if name and href:
            products.append({
                "name": name,
                "url": href,
                "price": price_str,
                "price_raw": price_num,
                "sku": "",
                "image": image_url,
                "is_preorder": "pre-order" in name.lower(),
                "promo": "",
            })

    return products


# ─── Strategy 1: Raw HTTP ────────────────────────────────────────────

def scrape_category_raw(url: str) -> list[dict]:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"  Raw fetch failed: {e}")
        return []
    return parse_products_from_html(resp.text)


# ─── Strategy 2: Playwright ──────────────────────────────────────────

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-AU', 'en'] });
    delete window.__playwright;
}
"""

SCROLL_JS = """
async () => {
    let last = 0;
    for (let i = 0; i < 20; i++) {
        window.scrollBy(0, 800);
        await new Promise(r => setTimeout(r, 400));
        if (document.body.scrollHeight === last && i > 3) break;
        last = document.body.scrollHeight;
    }
}
"""

EXTRACT_JS = """
() => {
    const seen = new Set();
    const products = [];

    // Try multiple selectors for Big W product tiles
    const selectors = [
        "article[data-testid='product-tile']",
        "[data-testid='product-tile']",
        "article",
        ".product-tile",
    ];
    let tiles = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { tiles = Array.from(found); break; }
    }

    tiles.forEach(tile => {
        const link = tile.querySelector('a[href*="/product/"]') || tile.querySelector('a');
        if (!link || !link.href.includes('/product/')) return;
        const href = link.href.split('?')[0];
        if (!href || seen.has(href)) return;
        seen.add(href);

        const nameEl = tile.querySelector("[data-testid='product-title'], h3, h2, [class*='title'], [class*='name']");
        let name = nameEl ? nameEl.textContent.trim() : '';
        if (!name) {
            const img = tile.querySelector('img');
            name = img ? (img.alt || '') : '';
        }

        const priceEl = tile.querySelector("[data-testid='product-price'], [class*='price'], [class*='Price']");
        let priceStr = '';
        let priceNum = null;
        if (priceEl) {
            const raw = priceEl.textContent.trim();
            const m = raw.match(/\\$?([\\d,]+\\.?\\d*)/);
            if (m) {
                priceNum = parseFloat(m[1].replace(',', ''));
                priceStr = '$' + priceNum.toFixed(2);
            }
        }

        const img = tile.querySelector('img');
        let imageUrl = '';
        if (img) {
            const src = img.src || img.getAttribute('data-src') || '';
            imageUrl = src.startsWith('//') ? 'https:' + src : src;
        }

        if (name && href) {
            products.push({ name, url: href, price: priceStr, price_raw: priceNum,
                           sku: '', image: imageUrl,
                           is_preorder: name.toLowerCase().includes('pre-order'),
                           promo: '' });
        }
    });

    return products;
}
"""


def scrape_category_playwright(url: str, headed: bool = False) -> list[dict]:
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("  Playwright not available")
        return []

    profile_dir = os.path.abspath(BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-AU",
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            logger.info(f"  [{'headed' if headed else 'headless'}] Loading: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            try:
                page.wait_for_selector(
                    "article[data-testid='product-tile'], [data-testid='product-tile'], article",
                    timeout=15000
                )
            except PlaywrightTimeout:
                pass

            page.wait_for_timeout(2000)
            page.evaluate(SCROLL_JS)
            page.wait_for_timeout(2000)

            products = page.evaluate(EXTRACT_JS)
            if products:
                logger.info(f"  Found {len(products)} products via JS extraction")
                return products

            html = page.content()
            products = parse_products_from_html(html)
            if products:
                logger.info(f"  Found {len(products)} products via DOM HTML parse")
            else:
                logger.warning(f"  No products found on {url}")
            return products

        except Exception as e:
            logger.error(f"  Playwright error on {url}: {e}")
            return []
        finally:
            page.close()
            context.close()


# ─── Combined Scraping ───────────────────────────────────────────────

def scrape_category_page(url: str, headed: bool = False) -> list[dict]:
    if not headed:
        logger.info(f"  Loading (raw): {url}")
        products = scrape_category_raw(url)
        if products:
            logger.info(f"  ✅ Found {len(products)} products via raw HTML")
            return products
        logger.info(f"  Raw HTML had no products — falling back to Playwright")

    return scrape_category_playwright(url, headed=headed)


# ─── Product Filtering & Enrichment ─────────────────────────────────

def infer_set(name: str) -> Optional[str]:
    name_lower = name.lower()
    for set_name, set_key in POKEMON_SETS.items():
        if set_name in name_lower:
            return set_key
    return None


def parse_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    match = re.search(r"\$?([\d,]+\.?\d*)", price_str)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def enrich_product(raw: dict, tcg: str) -> Optional[dict]:
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()

    if not name or not url:
        return None
    if "bigw.com.au" not in url or "/product/" not in url:
        return None

    name_lower = name.lower()

    keywords = TCG_NAME_KEYWORDS.get(tcg, [tcg.lower()])
    if not any(kw in name_lower for kw in keywords):
        return None

    for blocked in PRODUCT_BLOCKLIST:
        if blocked in name_lower:
            logger.debug(f"  Blocked '{blocked}': {name}")
            return None

    if not any(allowed in name_lower for allowed in PRODUCT_ALLOWLIST):
        logger.debug(f"  Not in allowlist: {name}")
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "bigw_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price", "") or None,
        "image": raw.get("image", "") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "discovered_at": datetime.now().isoformat(),
        "source": "bigw_discovery",
    }


# ─── Database Integration ────────────────────────────────────────────

def save_new_products(products: list[dict], db: "Database") -> tuple[int, int]:
    added = 0
    skipped = 0

    for product in products:
        url = product["url"]

        if db.get_last_status(url):
            skipped += 1
            continue

        db.update_status(
            url=url,
            name=product["name"],
            retailer=product["retailer"],
            in_stock=False,
            price=product.get("price"),
            price_str=product.get("price_str"),
            image_url=product.get("image") or None,
            status_changed=False,
        )

        if match_product:
            set_key = product.get("set") if product.get("tcg") == "pokemon" else None
            match = match_product(
                product["name"], db,
                tcg=product.get("tcg", "pokemon"),
                set_key=set_key,
            )
            db.set_canonical_match(url, match["canonical_id"], match["status"])
            match_label = (
                f" → {match['canonical_id']} ({match['score']:.0%})"
                if match["canonical_id"]
                else f" (unmatched, {match['score']:.0%})"
            )
            logger.info(f"  ✅ Added: {product['name']}{match_label}")
        else:
            logger.info(f"  ✅ Added: {product['name']}")

        added += 1

    return added, skipped


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_bigw(tcg_filter: Optional[str] = None, dry_run: bool = False,
                  fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full Big W product discovery flow."""
    all_products = []
    seen_urls = set()

    categories = BIGW_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Valid: {list(BIGW_CATEGORY_URLS.keys())}")
            return []

    logger.info(f"🔍 Starting Big W AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        for url in urls:
            raw_products = scrape_category_page(url, headed=headed)

            for raw in raw_products:
                enriched = enrich_product(raw, tcg)
                if not enriched:
                    continue
                if enriched["url"] in seen_urls:
                    continue
                seen_urls.add(enriched["url"])
                all_products.append(enriched)

            time.sleep(2)

    logger.info(f"\n📦 Total unique products after filtering: {len(all_products)}")

    if dry_run:
        logger.info("── DRY RUN — Would add these products ────────")
        for p in all_products:
            set_label = f" [{p['set']}]" if p.get("set") else ""
            price_label = f" {p['price_str']}" if p.get("price_str") else ""
            preorder_label = " ⏳PREORDER" if p.get("is_preorder") else ""
            logger.info(f"  {p['name']}{set_label}{price_label}{preorder_label}")
            logger.info(f"    {p['url']}")
    else:
        if DB_AVAILABLE:
            db = Database()
            added, skipped = save_new_products(all_products, db)
            logger.info(f"✅ Done: {added} added, {skipped} already tracked")
        else:
            logger.warning("DB not available — printing results")
            for p in all_products:
                logger.info(f"  {p['name']} — {p['url']}")

    return all_products


# ─── Entry Point ─────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Big W AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", type=str, default=None,
                        help=f"TCG to discover. Options: {', '.join(BIGW_CATEGORY_URLS.keys())}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    args = parser.parse_args()

    discover_bigw(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
