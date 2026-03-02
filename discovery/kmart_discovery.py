"""
Kmart AU — Product Discovery
==============================
Discovers TCG product URLs from Kmart AU.

Strategy (three-pass):
  1. Constructor.io search API — Kmart uses Constructor.io for product search.
     The API key is loaded from .env (KMART_CONSTRUCTOR_KEY) or auto-fetched
     from a script tag on the Kmart website. Returns structured JSON including
     per-state stock availability (stateOOS field).
  2. Raw HTTP + BeautifulSoup — parses __NEXT_DATA__ and product cards.
  3. Playwright with persistent context — full JS rendering as last resort.

Usage:
    python discovery/kmart_discovery.py --tcg pokemon --dry-run
    python discovery/kmart_discovery.py --tcg pokemon --dry-run --headed

Setup:
    pip install playwright beautifulsoup4 lxml requests python-dotenv
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
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    print("⚠️  Could not import project utils — running in standalone mode")

from discovery.base_discovery import (
    PRODUCT_ALLOWLIST, PRODUCT_BLOCKLIST, TCG_NAME_KEYWORDS, POKEMON_SETS,
    REQUEST_HEADERS, BROWSER_PROFILE_DIR, STEALTH_JS, SCROLL_JS,
    infer_set, parse_price, apply_filters, make_session,
    make_playwright_context, save_new_products, log_dry_run,
)

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────

SESSION = make_session()

# Constructor.io credentials — from .env, auto-fetched if blank
KMART_CONSTRUCTOR_KEY = os.getenv("KMART_CONSTRUCTOR_KEY", "")
CONSTRUCTOR_FALLBACK_KEY = "key_GZTqlLr41FS2p7AY"
CONSTRUCTOR_SEARCH_BASE = "https://ac.cnstrc.com/search/"

# Kmart category + search URLs per TCG (fallback if API unavailable)
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


# ─── Strategy 1: Constructor.io API ──────────────────────────────────

def fetch_constructor_key() -> str:
    """
    Auto-fetch the Constructor.io API key from a Kmart page's script tags.

    Kmart loads Constructor via a CDN script whose src contains the key:
        <script src="https://ac.cnstrc.com/...?key=KEY_HERE&...">
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
        logger.debug(f"  Constructor key auto-fetch failed: {e}")

    return ""


def resolve_constructor_key() -> str:
    """Return the best available Constructor.io key."""
    # 1. Prefer .env value
    if KMART_CONSTRUCTOR_KEY:
        logger.info(f"  Constructor key: from .env")
        return KMART_CONSTRUCTOR_KEY

    # 2. Try auto-fetching from page source
    logger.info("  Constructor key not in .env — auto-fetching from page...")
    key = fetch_constructor_key()
    if key:
        logger.info(f"  Constructor key: auto-fetched ({key[:12]}...)")
        return key

    # 3. Known fallback key
    logger.warning(f"  Constructor key: using hardcoded fallback")
    return CONSTRUCTOR_FALLBACK_KEY


def scrape_constructor(tcg: str, key: str) -> list[dict]:
    """
    Search Kmart products via the Constructor.io API.

    Returns a flat list of raw product dicts. Each item includes
    `state_oos` (list of AU state codes out of stock) and
    `is_oos_all_states` (True if all ~8 states are OOS).
    """
    query = f"{tcg.replace('-', ' ')} trading card"
    url = f"{CONSTRUCTOR_SEARCH_BASE}{requests.utils.quote(query)}"

    params = {
        "c": "ciojs-client-2.71.1",
        "key": key,
        "num_results_per_page": 60,
        "filters[Seller]": "Kmart",
        "sort_by": "relevance",
        "sort_order": "descending",
    }
    headers = {**REQUEST_HEADERS, "Accept": "application/json"}

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
            pd = item.get("data", {})
            name = item.get("value", "")
            url_path = pd.get("url", "")
            price_num = pd.get("price")
            state_oos = pd.get("stateOOS", []) or []

            if not name or not url_path:
                continue

            full_url = (
                f"https://www.kmart.com.au{url_path}"
                if url_path.startswith("/") else url_path
            )

            products.append({
                "name": name,
                "url": full_url,
                "price": f"${float(price_num):.2f}" if price_num else "",
                "price_raw": float(price_num) if price_num else None,
                "sku": pd.get("id", ""),
                "image": pd.get("image_url", ""),
                "is_preorder": False,
                "promo": "",
                "state_oos": state_oos,
                "is_oos_all_states": len(state_oos) >= 8,
            })

        total_pages = data.get("response", {}).get("total_pages", 1)
        logger.info(f"  Constructor page {page}/{total_pages}")

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.4)

    if products:
        logger.info(f"  ✅ Found {len(products)} via Constructor Search")

    return products


# ─── Strategy 2: Raw HTML Parsing ────────────────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product cards from Kmart HTML.

    Kmart is React-rendered so raw HTML is often sparse. Tries __NEXT_DATA__
    first, then article card selectors.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    products = []

    # Try __NEXT_DATA__
    next_data_script = soup.select_one("script#__NEXT_DATA__")
    if next_data_script and next_data_script.string:
        try:
            nd = json.loads(next_data_script.string)
            page_props = nd.get("props", {}).get("pageProps", {})
            items = (
                page_props.get("products", [])
                or page_props.get("searchResults", {}).get("products", [])
                or page_props.get("category", {}).get("products", [])
                or page_props.get("data", {}).get("products", [])
            )
            for item in items:
                name = item.get("name") or item.get("title", "")
                url_path = item.get("urlPath") or item.get("slug", "")
                sku = str(item.get("id") or item.get("productId", ""))
                price_raw = item.get("price", {})
                price_num = (
                    price_raw.get("amount") or price_raw.get("current", {}).get("value")
                    if isinstance(price_raw, dict) else price_raw
                )
                images = item.get("images", [])
                image_url = (images[0].get("url", "") if isinstance(images[0], dict) else str(images[0])) if images else ""

                if not name:
                    continue
                href = (
                    f"https://www.kmart.com.au{url_path}" if url_path.startswith("/") else url_path
                ) if url_path else (f"https://www.kmart.com.au/product/{sku}" if sku else "")
                if not href:
                    continue

                href = href.split("?")[0]
                if href in seen:
                    continue
                seen.add(href)
                price_num_f = float(price_num) if price_num else None
                products.append({
                    "name": name, "url": href,
                    "price": f"${price_num_f:.2f}" if price_num_f else "",
                    "price_raw": price_num_f, "sku": sku, "image": image_url,
                    "is_preorder": False, "promo": "",
                    "state_oos": [], "is_oos_all_states": False,
                })
            if products:
                return products
        except (json.JSONDecodeError, AttributeError, TypeError, IndexError):
            pass

    # HTML card fallback
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
            or tile.select_one("[class*='title']") or tile.select_one("[class*='name']")
            or tile.select_one("[data-testid*='title']")
        )
        if name_el:
            name = name_el.get_text(strip=True)
        if not name:
            img = tile.select_one("img")
            if img:
                name = img.get("alt", "").strip()

        price_str, price_num = "", None
        price_el = (
            tile.select_one("[class*='price']") or tile.select_one("[class*='Price']")
            or tile.select_one("[data-testid*='price']")
        )
        if price_el:
            price_num = parse_price(price_el.get_text(strip=True))
            if price_num:
                price_str = f"${price_num:.2f}"

        image_url = ""
        img = tile.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and not src.startswith("data:"):
                image_url = "https:" + src if src.startswith("//") else src

        if name and href:
            products.append({
                "name": name, "url": href, "price": price_str, "price_raw": price_num,
                "sku": "", "image": image_url, "is_preorder": "pre-order" in name.lower(), "promo": "",
                "state_oos": [], "is_oos_all_states": False,
            })

    return products


def scrape_category_raw(url: str) -> list[dict]:
    try:
        resp = SESSION.get(url, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"  Raw fetch failed: {e}")
        return []
    return parse_products_from_html(resp.text)


# ─── Strategy 3: Playwright ───────────────────────────────────────────

EXTRACT_JS = """
() => {
    const seen = new Set();
    const products = [];
    const selectors = ["article[data-testid='product-card']", "[data-testid='product-card']", "article", ".product-tile", ".ProductCard"];
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
        if (!name) { const img = tile.querySelector('img'); name = img ? (img.alt || '') : ''; }
        const priceEl = tile.querySelector('[class*="price"], [class*="Price"], [data-testid*="price"]');
        let priceStr = ''; let priceNum = null;
        if (priceEl) {
            const m = priceEl.textContent.trim().match(/\\$?([\\d,]+\\.?\\d*)/);
            if (m) { priceNum = parseFloat(m[1].replace(',','')); priceStr = '$' + priceNum.toFixed(2); }
        }
        const img = tile.querySelector('img');
        let imageUrl = '';
        if (img) { const src = img.src || img.getAttribute('data-src') || ''; imageUrl = src.startsWith('//') ? 'https:'+src : src; }
        if (name && href) products.push({ name, url: href, price: priceStr, price_raw: priceNum, sku: '', image: imageUrl, is_preorder: name.toLowerCase().includes('pre-order'), promo: '', state_oos: [], is_oos_all_states: false });
    });
    return products;
}
"""


def scrape_category_playwright(url: str, headed: bool = False) -> list[dict]:
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("  Playwright not available")
        return []

    with sync_playwright() as p:
        context = make_playwright_context(p, headed=headed)
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            logger.info(f"  [{'headed' if headed else 'headless'}] Loading: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
                logger.info(f"  Found {len(products)} via JS extraction")
                return products

            html = page.content()
            products = parse_products_from_html(html)
            logger.info(f"  Found {len(products)} via DOM HTML parse")
            return products

        except Exception as e:
            logger.error(f"  Playwright error on {url}: {e}")
            return []
        finally:
            page.close()
            context.close()


def scrape_category_page(url: str, headed: bool = False) -> list[dict]:
    if not headed:
        logger.info(f"  Loading (raw): {url}")
        products = scrape_category_raw(url)
        if products:
            logger.info(f"  ✅ Found {len(products)} via raw HTML")
            return products
        logger.info(f"  Raw had no products — trying Playwright")
    return scrape_category_playwright(url, headed=headed)


# ─── Product Enrichment ───────────────────────────────────────────────

def enrich_product(raw: dict, tcg: str) -> Optional[dict]:
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()

    if not apply_filters(name, url, "kmart.com.au", "/product/", tcg):
        return None

    # Skip products that are out of stock in every Australian state
    if raw.get("is_oos_all_states"):
        logger.debug(f"  Skipping OOS (all states): {name}")
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "kmart_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price") or None,
        "image": raw.get("image") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "in_stock": not raw.get("is_oos_all_states", False),
        "state_oos": raw.get("state_oos", []),
        "discovered_at": datetime.now().isoformat(),
        "source": "kmart_discovery",
    }


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_kmart(tcg_filter: Optional[str] = None, dry_run: bool = False,
                   fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full Kmart AU product discovery flow."""
    all_products: list[dict] = []
    seen_urls: set[str] = set()

    categories = KMART_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Options: {list(KMART_CATEGORY_URLS)}")
            return []

    logger.info("🔍 Starting Kmart AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    # Resolve Constructor key once (shared across all TCGs)
    constructor_key = resolve_constructor_key()

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        raw_products: list[dict] = []

        # Strategy 1: Constructor.io API
        if constructor_key:
            raw_products = scrape_constructor(tcg, constructor_key)

        # Strategy 2 & 3: Scraping fallback
        if not raw_products:
            for url in urls:
                raw_products += scrape_category_page(url, headed=headed)
                time.sleep(2)

        for raw in raw_products:
            enriched = enrich_product(raw, tcg)
            if not enriched or enriched["url"] in seen_urls:
                continue
            seen_urls.add(enriched["url"])
            all_products.append(enriched)

    logger.info(f"\n📦 Total unique products after filtering: {len(all_products)}")

    if dry_run:
        log_dry_run(all_products)
    else:
        if DB_AVAILABLE:
            db = Database()
            added, skipped = save_new_products(all_products, db)
            logger.info(f"✅ Done: {added} added, {skipped} already tracked")
        else:
            logger.warning("DB not available — printing results only")
            for p in all_products:
                logger.info(f"  {p['name']} — {p['url']}")

    return all_products


# ─── Entry Point ─────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Kmart AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", default=None,
                        help=f"TCG to discover. Options: {', '.join(KMART_CATEGORY_URLS)}")
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
