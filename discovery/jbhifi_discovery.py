"""
JB Hi-Fi AU — Product Discovery
=================================
Discovers TCG product URLs from JB Hi-Fi AU.

Strategy (three-pass):
  1. Algolia search API — JB Hi-Fi uses Algolia for product search.
     App ID and search-only API key are auto-fetched from the page
     source or loaded from .env (JBHIFI_ALGOLIA_APP_ID / JBHIFI_ALGOLIA_API_KEY).
     Returns structured JSON — no scraping needed.
  2. Raw HTTP + BeautifulSoup — parses server-rendered product tiles.
  3. Playwright with persistent context — full JS rendering as last resort.

Usage:
    python discovery/jbhifi_discovery.py --tcg pokemon --dry-run
    python discovery/jbhifi_discovery.py --tcg pokemon --dry-run --headed

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

# Algolia credentials — loaded from .env, auto-fetched if blank
JBHIFI_ALGOLIA_APP_ID = os.getenv("JBHIFI_ALGOLIA_APP_ID", "")
JBHIFI_ALGOLIA_API_KEY = os.getenv("JBHIFI_ALGOLIA_API_KEY", "")

SESSION = make_session()

# JB Hi-Fi search/category URLs per TCG (fallback if Algolia unavailable)
JBHIFI_CATEGORY_URLS = {
    "pokemon": [
        "https://www.jbhifi.com.au/collections/collectibles-merchandise/pokemon-trading-cards",
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

# Algolia search queries per TCG
ALGOLIA_TCG_QUERIES = {
    "pokemon": "pokemon trading card",
    "one-piece": "one piece trading card",
    "mtg": "magic the gathering",
    "dragon-ball-z": "dragon ball super card",
    "lorcana": "lorcana trading card",
}


# ─── Strategy 1: Algolia API ─────────────────────────────────────────

def fetch_algolia_credentials() -> tuple[str, str]:
    """
    Auto-fetch Algolia App ID and search API key from JB Hi-Fi's page source.

    JB Hi-Fi embeds Algolia config in their JS bundle. The credentials are
    search-only (public read) so it's safe to extract and use them.

    Returns (app_id, api_key) or ("", "") if not found.
    """
    try:
        resp = SESSION.get(
            "https://www.jbhifi.com.au/",
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text

        # Pattern 1: Algolia config object in inline JS
        # e.g. {"appId":"XXXXXXXX","apiKey":"xxxxxxxxxxxxxxxxxxxxxxxx","indexName":"prod_..."}
        app_id_match = re.search(r'"appId"\s*:\s*"([A-Z0-9]{8,12})"', html)
        api_key_match = re.search(r'"apiKey"\s*:\s*"([a-f0-9]{32})"', html)

        if app_id_match and api_key_match:
            return app_id_match.group(1), api_key_match.group(1)

        # Pattern 2: Algolia credentials in script src attributes or window config
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script"):
            text = script.string or ""
            app_id_m = re.search(r'(?:ALGOLIA_APP_ID|algoliaAppId|appId)["\s:=]+([A-Z0-9]{8,12})', text)
            api_key_m = re.search(r'(?:ALGOLIA_API_KEY|algoliaApiKey|apiKey)["\s:=]+([a-f0-9]{32})', text)
            if app_id_m and api_key_m:
                return app_id_m.group(1), api_key_m.group(1)

    except Exception as e:
        logger.debug(f"  Algolia credential fetch failed: {e}")

    return "", ""


def scrape_algolia(tcg: str, app_id: str, api_key: str) -> list[dict]:
    """
    Search JB Hi-Fi products via the Algolia API.

    Returns a flat list of raw product dicts ready for enrich_product().
    """
    query = ALGOLIA_TCG_QUERIES.get(tcg, f"{tcg} trading card")
    url = f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries"

    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
    }

    # Try common JB Hi-Fi Algolia index names
    index_names = ["prod_products", "products", "jbhifi_products", "au_products"]
    params_str = f"query={requests.utils.quote(query)}&hitsPerPage=60&page=0"

    body = {
        "requests": [
            {"indexName": idx, "params": params_str}
            for idx in index_names
        ]
    }

    products = []
    try:
        resp = SESSION.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        for result_set in data.get("results", []):
            hits = result_set.get("hits", [])
            for hit in hits:
                name = hit.get("name") or hit.get("title") or hit.get("productName", "")
                url_path = hit.get("url") or hit.get("handle") or hit.get("productUrl", "")
                price_raw = hit.get("price") or hit.get("variants", [{}])[0].get("price", 0) if hit.get("variants") else 0
                image = hit.get("image") or hit.get("imageUrl") or hit.get("featured_image", "")
                sku = str(hit.get("id") or hit.get("sku") or hit.get("objectID", ""))

                if not name or not url_path:
                    continue

                # Build full URL
                if url_path.startswith("/"):
                    full_url = "https://www.jbhifi.com.au" + url_path
                elif url_path.startswith("http"):
                    full_url = url_path
                else:
                    full_url = f"https://www.jbhifi.com.au/products/{url_path}"

                full_url = full_url.split("?")[0]

                price_num = float(price_raw) if price_raw else None
                products.append({
                    "name": name,
                    "url": full_url,
                    "price": f"${price_num:.2f}" if price_num else "",
                    "price_raw": price_num,
                    "sku": sku,
                    "image": image if isinstance(image, str) else "",
                    "is_preorder": "pre-order" in name.lower() or "preorder" in name.lower(),
                    "promo": "",
                })

        if products:
            logger.info(f"  ✅ Found {len(products)} via Algolia")

    except Exception as e:
        logger.warning(f"  Algolia search failed: {e}")

    return products


# ─── Strategy 2: Raw HTTP ─────────────────────────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse JB Hi-Fi product tiles from raw HTML.

    JB Hi-Fi renders product tiles with various class patterns:
        <div class="ProductItem ...">
            <a href="/products/SLUG">...</a>
            <h2 class="ProductItem__Title">NAME</h2>
            <span class="ProductItem__Price">$XX.XX</span>
        </div>
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    products = []

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
        link = tile.select_one('a[href*="/products/"]')
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.jbhifi.com.au" + href
        href = href.split("?")[0]

        if href in seen:
            continue
        seen.add(href)

        # Name
        name = ""
        name_el = (
            tile.select_one(".ProductItem__Title")
            or tile.select_one("h2") or tile.select_one("h3")
        )
        if name_el:
            name = name_el.get_text(strip=True)
        if not name:
            name = tile.get("data-product-title", "").strip()

        # Price
        price_str = ""
        price_num = None
        price_el = (
            tile.select_one(".ProductItem__Price")
            or tile.select_one(".price")
            or tile.select_one("[class*='price']")
        )
        if price_el:
            raw = price_el.get_text(strip=True)
            price_num = parse_price(raw)
            if price_num:
                price_str = f"${price_num:.2f}"

        # Image
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

        const nameEl = tile.querySelector('.ProductItem__Title, h2, h3');
        const name = nameEl ? nameEl.textContent.trim()
                            : (tile.getAttribute('data-product-title') || '');

        const priceEl = tile.querySelector('.ProductItem__Price, .price, [class*="price"]');
        let priceStr = '';
        let priceNum = null;
        if (priceEl) {
            const m = priceEl.textContent.trim().match(/\\$?([\\d,]+\\.?\\d*)/);
            if (m) { priceNum = parseFloat(m[1].replace(',', '')); priceStr = '$' + priceNum.toFixed(2); }
        }

        const img = tile.querySelector('img');
        let imageUrl = '';
        if (img) {
            const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
            imageUrl = src.startsWith('//') ? 'https:' + src : src;
        }

        if (name && href)
            products.push({ name, url: href, price: priceStr, price_raw: priceNum,
                            sku: '', image: imageUrl, is_preorder: name.toLowerCase().includes('pre-order'), promo: '' });
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
                page.wait_for_selector(".ProductItem, .product-block, [data-product-title]", timeout=15000)
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

    if not apply_filters(name, url, "jbhifi.com.au", "/products/", tcg):
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "jbhifi_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price") or None,
        "image": raw.get("image") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "in_stock": False,
        "discovered_at": datetime.now().isoformat(),
        "source": "jbhifi_discovery",
    }


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_jbhifi(tcg_filter: Optional[str] = None, dry_run: bool = False,
                    fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full JB Hi-Fi AU product discovery flow."""
    all_products: list[dict] = []
    seen_urls: set[str] = set()

    categories = JBHIFI_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Options: {list(JBHIFI_CATEGORY_URLS)}")
            return []

    logger.info("🔍 Starting JB Hi-Fi AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    # Resolve Algolia credentials once
    app_id = JBHIFI_ALGOLIA_APP_ID
    api_key = JBHIFI_ALGOLIA_API_KEY
    if not app_id or not api_key:
        logger.info("  Algolia credentials not in .env — auto-fetching from page source...")
        app_id, api_key = fetch_algolia_credentials()
        if app_id and api_key:
            logger.info(f"  ✅ Algolia: app_id={app_id}")
        else:
            logger.info("  Algolia credentials not found — will use HTML scraping")

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        raw_products: list[dict] = []

        # Strategy 1: Algolia
        if app_id and api_key:
            raw_products = scrape_algolia(tcg, app_id, api_key)

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

    parser = argparse.ArgumentParser(description="JB Hi-Fi AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", default=None,
                        help=f"TCG to discover. Options: {', '.join(JBHIFI_CATEGORY_URLS)}")
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
