"""
Target AU — Product Discovery
===============================
Discovers TCG product URLs from Target Australia.

Strategy (three-pass):
  1. Target AU search API — Target exposes a JSON search endpoint used
     by their Next.js frontend. Returns structured product data without JS.
  2. Raw HTTP + BeautifulSoup — parses __NEXT_DATA__ and product tiles.
  3. Playwright with persistent context — full JS rendering as last resort.

Usage:
    python discovery/target_discovery.py --tcg pokemon --dry-run
    python discovery/target_discovery.py --tcg pokemon --dry-run --headed

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

# Target category + search URLs per TCG (fallback if API unavailable)
TARGET_CATEGORY_URLS = {
    "pokemon": [
        "https://www.target.com.au/c/toys-and-games/games-and-puzzles/trading-card-games",
        "https://www.target.com.au/search?q=pokemon+trading+card",
    ],
    "one-piece": [
        "https://www.target.com.au/search?q=one+piece+trading+card",
    ],
    "mtg": [
        "https://www.target.com.au/search?q=magic+the+gathering+trading+card",
    ],
    "dragon-ball-z": [
        "https://www.target.com.au/search?q=dragon+ball+super+card+game",
    ],
    "lorcana": [
        "https://www.target.com.au/search?q=disney+lorcana+trading+card",
    ],
}

# Search query strings per TCG for the API
TARGET_TCG_QUERIES = {
    "pokemon": "pokemon trading card",
    "one-piece": "one piece trading card",
    "mtg": "magic the gathering trading card",
    "dragon-ball-z": "dragon ball super card game",
    "lorcana": "lorcana trading card",
}


# ─── Strategy 1: Target Search API ───────────────────────────────────

def scrape_target_api(tcg: str) -> list[dict]:
    """
    Query Target AU's internal search API (used by their Next.js frontend).

    Target exposes a public JSON endpoint. Tries several known endpoint
    patterns and falls back gracefully.
    """
    query = TARGET_TCG_QUERIES.get(tcg, f"{tcg} trading card")

    # Target AU API endpoints — discovered by inspecting XHR on their search page
    endpoints = [
        "https://www.target.com.au/api/2.0/page/search",
        "https://www.target.com.au/api/page/search",
        "https://api.target.com.au/v2/search",
    ]

    products = []
    for base_url in endpoints:
        try:
            page = 1
            while True:
                params = {
                    "q": query,
                    "pageSize": 48,
                    "page": page,
                    "sortby": "Relevance",
                }
                headers = {
                    **REQUEST_HEADERS,
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                }
                resp = SESSION.get(base_url, params=params, headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                items = (
                    data.get("products", [])
                    or data.get("results", {}).get("products", [])
                    or data.get("searchResults", {}).get("products", [])
                    or data.get("data", {}).get("products", [])
                    or []
                )

                if not items:
                    break

                for item in items:
                    name = item.get("name") or item.get("displayName") or item.get("title", "")
                    url_path = item.get("url") or item.get("productUrl") or item.get("urlPath", "")
                    sku = str(item.get("productId") or item.get("id") or item.get("sku", ""))
                    price_raw = (
                        item.get("price", {}).get("current") or
                        item.get("pricingInfo", {}).get("priceValue") or
                        item.get("price")
                    )
                    image_url = (
                        item.get("image") or item.get("imageUrl") or
                        (item.get("media", [{}])[0].get("url", "") if item.get("media") else "")
                    )

                    if not name or not url_path:
                        continue

                    full_url = (
                        "https://www.target.com.au" + url_path
                        if url_path.startswith("/") else url_path
                    )
                    full_url = full_url.split("?")[0]

                    price_num = float(price_raw) if price_raw else None
                    products.append({
                        "name": name,
                        "url": full_url,
                        "price": f"${price_num:.2f}" if price_num else "",
                        "price_raw": price_num,
                        "sku": sku,
                        "image": image_url if isinstance(image_url, str) else "",
                        "is_preorder": "pre-order" in name.lower(),
                        "promo": "",
                    })

                total_pages = (
                    data.get("totalPages") or
                    data.get("pagination", {}).get("totalPages") or 1
                )
                logger.info(f"  Target API page {page}/{total_pages}")

                if page >= total_pages or page >= 5:
                    break
                page += 1
                time.sleep(0.5)

            if products:
                logger.info(f"  ✅ Found {len(products)} via Target API")
                return products

        except requests.HTTPError as e:
            if e.response.status_code == 404:
                continue  # Try next endpoint
            logger.warning(f"  Target API error: {e}")
            break
        except Exception as e:
            logger.debug(f"  Target API ({base_url}) failed: {e}")
            continue

    return products


# ─── Strategy 2: Raw HTML Parsing ────────────────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product tiles from Target AU HTML.

    Tries __NEXT_DATA__ JSON first, then article-based tile scraping.
    Product URL pattern: target.com.au/p/NAME/XXXXXXXX
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
                or page_props.get("categoryData", {}).get("products", [])
                or page_props.get("data", {}).get("products", [])
            )
            for item in items:
                name = item.get("name") or item.get("title", "")
                url_path = item.get("url") or item.get("urlPath") or item.get("slug", "")
                sku = str(item.get("id") or item.get("productId") or item.get("sku", ""))
                price_raw = item.get("price", {})
                price_num = (
                    price_raw.get("current", {}).get("value") or price_raw.get("amount")
                    if isinstance(price_raw, dict) else price_raw
                )
                images = item.get("images", [])
                image_url = (images[0].get("url", "") if isinstance(images[0], dict) else str(images[0])) if images else ""

                if not name:
                    continue
                href = (
                    "https://www.target.com.au" + url_path if url_path.startswith("/") else url_path
                ) if url_path else (f"https://www.target.com.au/p/{sku}" if sku else "")
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
                })
            if products:
                return products
        except (json.JSONDecodeError, AttributeError, TypeError, IndexError):
            pass

    # HTML tile fallback — Target uses similar patterns to Big W/Kmart
    tile_selectors = [
        "article[data-testid='product-tile']",
        "[data-testid='product-tile']",
        "article[data-testid='product-card']",
        "[data-testid='product-card']",
        "article",
        ".product-tile",
    ]
    tiles = []
    for selector in tile_selectors:
        found = soup.select(selector)
        if found:
            tiles = found
            break

    for tile in tiles:
        # Target product URLs use /p/ path
        link = (
            tile.select_one('a[href*="/p/"]')
            or tile.select_one('a[href*="/product/"]')
            or tile.select_one("a")
        )
        if not link:
            continue
        href = link.get("href", "")
        if not href or ("/p/" not in href and "/product/" not in href):
            continue
        if href.startswith("/"):
            href = "https://www.target.com.au" + href
        href = href.split("?")[0]
        if href in seen:
            continue
        seen.add(href)

        name = ""
        name_el = (
            tile.select_one("[data-testid='product-title']") or tile.select_one("h3")
            or tile.select_one("h2") or tile.select_one("[class*='title']")
            or tile.select_one("[class*='name']")
        )
        if name_el:
            name = name_el.get_text(strip=True)
        if not name:
            img = tile.select_one("img")
            if img:
                name = img.get("alt", "").strip()

        price_str, price_num = "", None
        price_el = (
            tile.select_one("[data-testid='product-price']")
            or tile.select_one("[class*='price']") or tile.select_one("[class*='Price']")
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
    const selectors = [
        "article[data-testid='product-tile']", "[data-testid='product-tile']",
        "article[data-testid='product-card']", "[data-testid='product-card']",
        "article", ".product-tile",
    ];
    let tiles = [];
    for (const sel of selectors) {
        const found = document.querySelectorAll(sel);
        if (found.length > 0) { tiles = Array.from(found); break; }
    }
    tiles.forEach(tile => {
        const link = tile.querySelector('a[href*="/p/"]') || tile.querySelector('a[href*="/product/"]') || tile.querySelector('a');
        if (!link) return;
        const href_raw = link.href || '';
        if (!href_raw.includes('/p/') && !href_raw.includes('/product/')) return;
        const href = href_raw.split('?')[0];
        if (!href || seen.has(href)) return;
        seen.add(href);
        const nameEl = tile.querySelector("[data-testid='product-title'], h3, h2, [class*='title'], [class*='name']");
        let name = nameEl ? nameEl.textContent.trim() : '';
        if (!name) { const img = tile.querySelector('img'); name = img ? (img.alt || '') : ''; }
        const priceEl = tile.querySelector("[data-testid='product-price'], [class*='price'], [class*='Price']");
        let priceStr = ''; let priceNum = null;
        if (priceEl) {
            const m = priceEl.textContent.trim().match(/\\$?([\\d,]+\\.?\\d*)/);
            if (m) { priceNum = parseFloat(m[1].replace(',','')); priceStr = '$' + priceNum.toFixed(2); }
        }
        const img = tile.querySelector('img');
        let imageUrl = '';
        if (img) { const src = img.src || img.getAttribute('data-src') || ''; imageUrl = src.startsWith('//') ? 'https:'+src : src; }
        if (name && href) products.push({ name, url: href, price: priceStr, price_raw: priceNum, sku: '', image: imageUrl, is_preorder: name.toLowerCase().includes('pre-order'), promo: '' });
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

    # Target uses /p/ path segments
    if not name or not url:
        return None
    if "target.com.au" not in url:
        return None
    if "/p/" not in url and "/product/" not in url:
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
        "retailer": "target_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price") or None,
        "image": raw.get("image") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "in_stock": False,
        "discovered_at": datetime.now().isoformat(),
        "source": "target_discovery",
    }


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_target(tcg_filter: Optional[str] = None, dry_run: bool = False,
                    fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full Target AU product discovery flow."""
    all_products: list[dict] = []
    seen_urls: set[str] = set()

    categories = TARGET_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Options: {list(TARGET_CATEGORY_URLS)}")
            return []

    logger.info("🔍 Starting Target AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        raw_products: list[dict] = []

        # Strategy 1: Target API
        raw_products = scrape_target_api(tcg)

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

    parser = argparse.ArgumentParser(description="Target AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", default=None,
                        help=f"TCG to discover. Options: {', '.join(TARGET_CATEGORY_URLS)}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    args = parser.parse_args()

    discover_target(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
