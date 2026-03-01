"""
JB Hi-Fi AU — Product Discovery
=================================
Discovers TCG product URLs from JB Hi-Fi AU search/category pages.

Strategy (two-pass):
  1. Parse raw HTML with BeautifulSoup — JB Hi-Fi server-side renders
     product tiles so this often works without JS.
  2. Playwright with persistent context — for when JS rendering is needed.
     First run use --headed if any bot challenge appears.

Usage:
    python discovery/jbhifi_discovery.py --tcg pokemon --dry-run
    python discovery/jbhifi_discovery.py --tcg pokemon --dry-run --headed

Setup:
    pip install playwright beautifulsoup4 lxml requests
    playwright install chromium
"""

import os
import re
import sys
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

# JB Hi-Fi search URLs per TCG
JBHIFI_CATEGORY_URLS = {
    "pokemon": [
        "https://www.jbhifi.com.au/search?type=product&q=pokemon+trading+card",
    ],
    "one-piece": [
        "https://www.jbhifi.com.au/search?type=product&q=one+piece+trading+card",
    ],
    "mtg": [
        "https://www.jbhifi.com.au/search?type=product&q=magic+the+gathering",
    ],
    "dragon-ball-z": [
        "https://www.jbhifi.com.au/search?type=product&q=dragon+ball+super+card",
    ],
    "lorcana": [
        "https://www.jbhifi.com.au/search?type=product&q=lorcana+trading+card",
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
    Parse product tiles from JB Hi-Fi HTML.

    JB Hi-Fi renders product tiles with various class patterns:
        <div class="ProductItem ...">
            <a class="ProductItem__ImageWrapper" href="/products/SLUG">
                <img src="...">
            </a>
            <h2 class="ProductItem__Title"><a href="/products/SLUG">NAME</a></h2>
            <span class="ProductItem__Price price">$XX.XX</span>
        </div>

    Also handles Shopify-style grid items and search result layouts.
    """
    soup = BeautifulSoup(html, "lxml")

    seen = set()
    products = []

    # Try multiple product tile selectors (JB Hi-Fi uses different layouts)
    tile_selectors = [
        ".ProductItem",
        ".product-block",
        "[data-product-title]",
        ".Grid__Cell .ProductItem",
        ".SearchPage__ProductGrid .ProductItem",
    ]

    tiles = []
    for selector in tile_selectors:
        found = soup.select(selector)
        if found:
            tiles = found
            break

    for tile in tiles:
        # Get product link
        link = (
            tile.select_one('a[href*="/products/"]')
            or tile.select_one('a.ProductItem__ImageWrapper')
        )
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.jbhifi.com.au" + href
        # Normalise — drop query params
        href = href.split("?")[0]

        if href in seen:
            continue
        seen.add(href)

        # Get product name
        name = ""
        name_el = (
            tile.select_one(".ProductItem__Title")
            or tile.select_one("h2")
            or tile.select_one("h3")
            or tile.get("data-product-title")
        )
        if isinstance(name_el, str):
            name = name_el.strip()
        elif name_el:
            name = name_el.get_text(strip=True)

        if not name:
            name = tile.get("data-product-title", "").strip()

        # Get price
        price_str = ""
        price_num = None
        price_el = (
            tile.select_one(".ProductItem__Price")
            or tile.select_one(".price")
            or tile.select_one("[class*='price']")
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
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")
            if src and not src.startswith("data:"):
                image_url = src if src.startswith("http") else ("https:" + src if src.startswith("//") else src)

        if name and href:
            products.append({
                "name": name,
                "url": href,
                "price": price_str,
                "price_raw": price_num,
                "sku": "",
                "image": image_url,
                "is_preorder": "pre-order" in name.lower() or "preorder" in name.lower(),
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
    for (let i = 0; i < 15; i++) {
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

    const selectors = ['.ProductItem', '.product-block', '[data-product-title]'];
    let tiles = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { tiles = Array.from(found); break; }
    }

    tiles.forEach(tile => {
        const link = tile.querySelector('a[href*="/products/"]');
        if (!link) return;
        const href = link.href.split('?')[0];
        if (!href || seen.has(href)) return;
        seen.add(href);

        const nameEl = tile.querySelector('.ProductItem__Title, h2, h3, [data-product-title]');
        const name = nameEl ? nameEl.textContent.trim() : (tile.getAttribute('data-product-title') || '');

        const priceEl = tile.querySelector('.ProductItem__Price, .price, [class*="price"]');
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
            const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
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
                    '.ProductItem, .product-block, [data-product-title]',
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
    if "jbhifi.com.au" not in url or "/products/" not in url:
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
        "retailer": "jbhifi_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price", "") or None,
        "image": raw.get("image", "") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "discovered_at": datetime.now().isoformat(),
        "source": "jbhifi_discovery",
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

def discover_jbhifi(tcg_filter: Optional[str] = None, dry_run: bool = False,
                    fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full JB Hi-Fi product discovery flow."""
    all_products = []
    seen_urls = set()

    categories = JBHIFI_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Valid: {list(JBHIFI_CATEGORY_URLS.keys())}")
            return []

    logger.info(f"🔍 Starting JB Hi-Fi AU discovery")
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

    parser = argparse.ArgumentParser(description="JB Hi-Fi AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", type=str, default=None,
                        help=f"TCG to discover. Options: {', '.join(JBHIFI_CATEGORY_URLS.keys())}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    args = parser.parse_args()

    discover_jbhifi(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
