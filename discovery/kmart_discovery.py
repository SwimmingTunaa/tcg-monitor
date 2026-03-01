"""
Kmart AU — Product Discovery
==============================
Discovers TCG product URLs from Kmart AU category pages.

Strategy:
  1. Parse raw HTML — Kmart uses React so raw HTML may be sparse,
     but server-rendered Next.js data is sometimes present.
  2. Playwright with persistent context — primary strategy for Kmart's
     heavily JS-rendered pages.

Usage:
    python discovery/kmart_discovery.py --tcg pokemon --dry-run
    python discovery/kmart_discovery.py --tcg pokemon --dry-run --headed

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

CONSTRUCTOR_API_KEY = os.getenv(
    "KMART_CONSTRUCTOR_KEY",
)

def fetch_constructor_key() -> Optional[str]:
    """
    Extract Constructor API key from script tag src.
    Much more reliable than searching inline HTML.
    """
    try:
        resp = SESSION.get(
            "https://www.kmart.com.au/search/?q=test",
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        for script in soup.find_all("script", src=True):
            src = script["src"]
            if "ac.cnstrc.com" in src and "key=" in src:
                match = re.search(r'key=([^&"]+)', src)
                if match:
                    return match.group(1)

    except Exception as e:
        logger.debug(f"Constructor key fetch failed: {e}")

    return None

CONSTRUCTOR_FALLBACK_KEY = "key_GZTqlLr41FS2p7AY"


# Kmart category URLs per TCG
KMART_CATEGORY_URLS = {
    "pokemon": [
        "https://www.kmart.com.au/category/toys/pokemon-trading-cards",
        "https://www.kmart.com.au/search/?q=pokemon+trading+card",
    ],
    "one-piece": [
        "https://www.kmart.com.au/search/?q=one+piece+trading+card",
    ],
    "mtg": [
        "https://www.kmart.com.au/search/?q=magic+gathering+trading+card",
    ],
    "dragon-ball-z": [
        "https://www.kmart.com.au/search/?q=dragon+ball+super+card",
    ],
    "lorcana": [
        "https://www.kmart.com.au/search/?q=lorcana+trading+card",
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

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": REQUEST_HEADERS["User-Agent"],
})

# ─── Constructor Search API ─────────────────────────────

CONSTRUCTOR_SEARCH_BASE = "https://ac.cnstrc.com/search/"

# ─── HTML Parsing ────────────────────────────────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product tiles from Kmart HTML.

    Kmart is React-rendered, so server HTML is often sparse.
    Tries to extract from __NEXT_DATA__ or visible product cards.
    Product cards look like:
        <article data-testid="product-card">
            <a href="/product/NAME/XXXXX">...</a>
            <h3>NAME</h3>
            <span>$XX.XX</span>
        </article>
    """
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    products = []

    # Try __NEXT_DATA__ first
    next_data_script = soup.select_one("script#__NEXT_DATA__")
    if next_data_script and next_data_script.string:
        try:
            next_data = json.loads(next_data_script.string)
            page_props = next_data.get("props", {}).get("pageProps", {})
            raw_items = (
                page_props.get("products", [])
                or page_props.get("searchResults", {}).get("products", [])
                or page_props.get("category", {}).get("products", [])
                or page_props.get("data", {}).get("products", [])
            )
            for item in raw_items:
                name = item.get("name", "") or item.get("title", "")
                url_path = item.get("urlPath", "") or item.get("slug", "")
                sku = str(item.get("id", "") or item.get("productId", ""))
                price_raw = item.get("price", {})
                price_num = None
                if isinstance(price_raw, dict):
                    price_num = price_raw.get("amount") or price_raw.get("current", {}).get("value")
                elif isinstance(price_raw, (int, float)):
                    price_num = price_raw

                image_url = ""
                images = item.get("images", [])
                if images and isinstance(images, list):
                    first = images[0]
                    image_url = first.get("url", "") if isinstance(first, dict) else str(first)

                if not name:
                    continue

                if url_path:
                    href = f"https://www.kmart.com.au{url_path}" if url_path.startswith("/") else url_path
                elif sku:
                    href = f"https://www.kmart.com.au/product/{sku}"
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

    # HTML tile fallback
    tile_selectors = [
        "article[data-testid='product-card']",
        "[data-testid='product-card']",
        "article",
        ".product-tile",
        ".ProductCard",
    ]

    tiles = []
    for selector in tile_selectors:
        found = soup.select(selector)
        if found:
            tiles = found
            break

    for tile in tiles:
        link = tile.select_one('a[href*="/product/"]') or tile.select_one("a")
        if not link:
            continue
        href = link.get("href", "")
        if not href or "/product/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.kmart.com.au" + href
        href = href.split("?")[0]

        if href in seen:
            continue
        seen.add(href)

        name = ""
        name_el = (
            tile.select_one("h3") or tile.select_one("h2")
            or tile.select_one("[class*='title']")
            or tile.select_one("[class*='name']")
            or tile.select_one("[data-testid*='title']")
            or tile.select_one("[data-testid*='name']")
        )
        if name_el:
            name = name_el.get_text(strip=True)
        if not name:
            img = tile.select_one("img")
            if img:
                name = img.get("alt", "").strip()

        price_str = ""
        price_num = None
        price_el = (
            tile.select_one("[class*='price']")
            or tile.select_one("[class*='Price']")
            or tile.select_one("[data-testid*='price']")
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
        resp = SESSION.get(url, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"  Raw fetch failed: {e}")
        return []

    return parse_products_from_html(resp.text)

def scrape_category_constructor(tcg: str) -> list[dict]:
    """
    Primary strategy: Constructor.io search endpoint.
    Multi-TCG, no group_id dependency.
    """
    key = CONSTRUCTOR_API_KEY

    if key:
        logger.info("  Constructor key: dynamic")
    else:
        key = CONSTRUCTOR_FALLBACK_KEY
        logger.warning("  Constructor key: using FALLBACK")

    if not key:
        logger.warning("  Could not fetch Constructor key")
        return []

    query = f"{tcg.replace('-', ' ')} trading card"
    url = f"{CONSTRUCTOR_SEARCH_BASE}{query}"

    params = {
        "c": "ciojs-client-2.71.1",
        "key": key,
        "page": 1,
        "num_results_per_page": 60,
        "filters[Seller]": "Kmart",
        "sort_by": "relevance",
        "sort_order": "descending",
    }

    headers = {"Accept": "application/json"}

    products = []
    page = 1

    while True:
        params["page"] = page

        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"  Constructor search failed on page {page}: {e}")
            break

        results = data.get("response", {}).get("results", [])
        if not results:
            break

        for item in results:
            product_data = item.get("data", {})
            name = item.get("value", "")
            url_path = product_data.get("url", "")
            price_num = product_data.get("price")
            state_oos = product_data.get("stateOOS", []) or []

            if not name or not url_path:
                continue

            full_url = (
                f"https://www.kmart.com.au{url_path}"
                if url_path.startswith("/")
                else url_path
            )

            products.append({
                "name": name,
                "url": full_url,
                "price": f"${float(price_num):.2f}" if price_num else "",
                "price_raw": float(price_num) if price_num else None,
                "sku": product_data.get("id", ""),
                "image": product_data.get("image_url", ""),
                "is_preorder": False,
                "promo": "",
                "state_oos": state_oos,
                "is_oos_all_states": len(state_oos) >= 8,
            })

        total_pages = data.get("response", {}).get("total_pages", 1)
        logger.info(f"  Search page {page}/{total_pages}")

        if page >= total_pages:
            break

        page += 1
        time.sleep(0.4)

    if products:
        logger.info(f"  ✅ Found {len(products)} via Constructor Search")

    return products


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

    const selectors = [
        "article[data-testid='product-card']",
        "[data-testid='product-card']",
        "article",
        ".product-tile",
        ".ProductCard",
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

        const nameEl = tile.querySelector('h3, h2, [class*="title"], [class*="name"], [data-testid*="title"]');
        let name = nameEl ? nameEl.textContent.trim() : '';
        if (!name) {
            const img = tile.querySelector('img');
            name = img ? (img.alt || '') : '';
        }

        const priceEl = tile.querySelector('[class*="price"], [class*="Price"], [data-testid*="price"]');
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
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            logger.info(f"  [{'headed' if headed else 'headless'}] Loading: {url}")
            page.goto(url, wait_until="networkidle", timeout=30000)

            try:
                page.wait_for_selector(
                    "article[data-testid='product-card'], article, .ProductCard",
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

def scrape_category_page(url: str, tcg: str, headed: bool = False) -> list[dict]:
    # Strategy 1 — Raw
    if not headed:
        logger.info(f"  Loading (raw): {url}")
        products = scrape_category_raw(url)
        if products:
            logger.info(f"  ✅ Found {len(products)} products via raw HTML")
            return products
        logger.info(f"  Raw HTML had no products — falling back to Playwright")

    # Strategy 2 — Playwright
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
    if "kmart.com.au" not in url or "/product/" not in url:
        return None

    name_lower = name.lower()
    
    # Skip if OOS in all states
    if raw.get("is_oos_all_states"):
        logger.debug(f"  Skipping OOS (all states): {name}")
        return None

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
        "retailer": "kmart_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price", "") or None,
        "image": raw.get("image") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "in_stock": not raw.get("is_oos_all_states", False),
        "state_oos": raw.get("state_oos", []),
        "discovered_at": datetime.now().isoformat(),
        "source": "kmart_discovery",
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
            in_stock=product.get("in_stock", False),
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

def discover_kmart(tcg_filter: Optional[str] = None, dry_run: bool = False,
                   fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full Kmart AU product discovery flow."""
    all_products = []
    seen_urls = set()

    categories = KMART_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Valid: {list(KMART_CATEGORY_URLS.keys())}")
            return []

    logger.info(f"🔍 Starting Kmart AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    for tcg, urls in categories.items():
        # Primary strategy — Constructor Search (once per TCG)
        constructor_products = scrape_category_constructor(tcg)

        for raw in constructor_products:
            enriched = enrich_product(raw, tcg)
            if not enriched:
                continue
            if enriched["url"] in seen_urls:
                continue
            seen_urls.add(enriched["url"])
            all_products.append(enriched)

        # If Constructor worked, skip URL scraping
        if constructor_products:
            continue
        
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        for url in urls:
            raw_products = scrape_category_page(url, tcg=tcg, headed=headed)

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

    parser = argparse.ArgumentParser(description="Kmart AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", type=str, default=None,
                        help=f"TCG to discover. Options: {', '.join(KMART_CATEGORY_URLS.keys())}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    args = parser.parse_args()

    discover_kmart(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
