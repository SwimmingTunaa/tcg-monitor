"""
JB Hi-Fi AU — Product Discovery
=================================
Discovers TCG product URLs from JB Hi-Fi AU.

Strategy (four-pass):
  1. Algolia browse API — JB Hi-Fi uses Algolia with category filters to
     power their collection pages. Uses the same credentials and filters
     as the frontend (index: shopify_products_families, browse endpoint).
  2. Shopify Storefront GraphQL API — fallback search via Shopify Hydrogen.
  3. Raw HTTP + BeautifulSoup — parses server-rendered product tiles.
  4. Playwright with persistent context — full JS rendering as last resort.

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

# Shopify Storefront GraphQL API config
SHOPIFY_STOREFRONT_TOKEN = os.getenv("JBHIFI_STOREFRONT_TOKEN", "")

SHOPIFY_SEARCH_QUERIES = {
    "pokemon": "pokemon trading card",
    "one-piece": "one piece trading card",
    "mtg": "magic the gathering",
    "dragon-ball-z": "dragon ball super card",
    "lorcana": "lorcana trading card",
}

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

# ─── Strategy 1: Algolia Browse API ───────────────────────────────

ALGOLIA_APP_ID = os.getenv("JBHIFI_ALGOLIA_APP_ID", "VTVKM5URPX")
ALGOLIA_API_KEY = os.getenv("JBHIFI_ALGOLIA_API_KEY", "1d989f0839a992bbece9099e1b091f07")
ALGOLIA_INDEX = "shopify_products_families"

# Filter strings per TCG — extracted from JB Hi-Fi's frontend JS.
# These match exactly what the browser sends to Algolia on each collection page.
ALGOLIA_TCG_FILTERS = {
    "pokemon": (
        '("facets.Game type": "Trading card games" OR "category_hierarchy":"Trading card games")'
        ' AND "facets.Brands": "Pokemon TCG"'
    ),
    "one-piece": (
        '("facets.Game type": "Trading card games" OR "category_hierarchy":"Trading card games")'
        ' AND "facets.Brands": "One Piece Card Game"'
    ),
    "mtg": (
        '("facets.Game type": "Trading card games" OR "category_hierarchy":"Trading card games")'
        ' AND "facets.Brands": "Magic The Gathering"'
    ),
    "dragon-ball-z": (
        '("facets.Game type": "Trading card games" OR "category_hierarchy":"Trading card games")'
        ' AND "facets.Brands": "Dragon Ball Super Card Game"'
    ),
    "lorcana": (
        '("facets.Game type": "Trading card games" OR "category_hierarchy":"Trading card games")'
        ' AND "facets.Brands": "Disney Lorcana"'
    ),
}


def scrape_algolia_browse(tcg: str) -> list[dict]:
    """
    Browse JB Hi-Fi products via the Algolia browse endpoint with filters.

    This replicates the exact API call the JB Hi-Fi frontend makes on
    collection pages. Uses filter-based browsing (not free-text search)
    against the shopify_products_families index.

    Returns a flat list of raw product dicts ready for enrich_product().
    """
    filters = ALGOLIA_TCG_FILTERS.get(tcg)
    if not filters:
        logger.info(f"  No Algolia filter configured for TCG: {tcg}")
        return []

    url = (
        f"https://{ALGOLIA_APP_ID}-dsn.algolia.net"
        f"/1/indexes/{ALGOLIA_INDEX}/browse"
        f"?x-algolia-api-key={ALGOLIA_API_KEY}"
        f"&x-algolia-application-id={ALGOLIA_APP_ID}"
    )

    products = []
    cursor = None
    page = 0
    max_pages = 20

    while page < max_pages:
        body: dict = {
            "filters": filters,
            "hitsPerPage": 100,
        }
        if cursor:
            body["cursor"] = cursor

        try:
            resp = SESSION.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            logger.info(f"  Algolia browse response: {resp.status_code} (page {page + 1})")

            if resp.status_code in (401, 403):
                logger.warning(f"  Algolia {resp.status_code} — credentials may be invalid")
                break

            resp.raise_for_status()
            data = resp.json()

            hits = data.get("hits", [])
            if not hits:
                if page == 0:
                    logger.info("  Algolia browse returned 0 hits")
                break

            for hit in hits:
                product = _parse_algolia_hit(hit)
                if product:
                    products.append(product)

            # Algolia browse uses cursor-based pagination
            cursor = data.get("cursor")
            if not cursor:
                break

            page += 1
            time.sleep(0.5)

        except Exception as e:
            logger.warning(f"  Algolia browse failed: {e}")
            break

    if products:
        logger.info(f"  ✅ Found {len(products)} via Algolia browse")
        for i, p in enumerate(products):
            logger.info(f"    [{i+1}] {p['name']} | ${p.get('price_raw') or '?'} | {p['url']}")

    return products


def _parse_algolia_hit(hit: dict) -> dict | None:
    """Parse a single Algolia hit into our standard product dict."""
    title = hit.get("title") or hit.get("name", "")
    handle = hit.get("handle", "")

    if not title or not handle:
        return None

    full_url = f"https://www.jbhifi.com.au/products/{handle}"

    # Price
    price_raw = None
    if "price" in hit:
        price_raw = hit["price"]
    elif "variants" in hit and hit["variants"]:
        first_variant = hit["variants"][0] if isinstance(hit["variants"], list) else {}
        price_raw = first_variant.get("price")
    elif "price_range" in hit:
        price_raw = hit["price_range"].get("min") or hit["price_range"].get("minimum_price")

    if isinstance(price_raw, str):
        try:
            price_raw = float(price_raw)
        except ValueError:
            price_raw = None

    image = hit.get("image") or hit.get("featured_image", "")
    if isinstance(image, dict):
        image = image.get("src") or image.get("url") or ""

    sku = str(hit.get("sku") or hit.get("objectID") or hit.get("id", ""))

    name_lower = title.lower()
    is_preorder = "pre-order" in name_lower or "preorder" in name_lower

    return {
        "name": title,
        "url": full_url,
        "price": f"${price_raw:.2f}" if price_raw else "",
        "price_raw": price_raw,
        "sku": sku,
        "image": image if isinstance(image, str) else "",
        "is_preorder": is_preorder,
        "promo": "",
    }


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

# ─── Strategy 2: Shopify Storefront GraphQL API ──────────────────────

SHOPIFY_GRAPHQL_URL = "https://prod-jbhifi.myshopify.com/api/2025-01/graphql.json"

GRAPHQL_SEARCH_QUERY = """
query searchProducts($query: String!, $first: Int!, $after: String) {
  search(query: $query, types: PRODUCT, first: $first, after: $after) {
    edges {
      node {
        ... on Product {
          id
          title
          handle
          tags
          featuredImage {
            url
          }
          variants(first: 1) {
            edges {
              node {
                sku
                price {
                  amount
                  currencyCode
                }
              }
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

GRAPHQL_COLLECTION_QUERY = """
query collectionProducts($handle: String!, $first: Int!, $after: String) {
  collection(handle: $handle) {
    title
    products(first: $first, after: $after) {
      edges {
        node {
          id
          title
          handle
          tags
          featuredImage {
            url
          }
          variants(first: 1) {
            edges {
              node {
                sku
                price {
                  amount
                  currencyCode
                }
              }
            }
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

# Collection handles per TCG — try multiple variations since Hydrogen
# may use different handle formats than the URL path suggests
SHOPIFY_COLLECTION_HANDLES = {
    "pokemon": [
        "pokemon-trading-cards",
        "collectibles-merchandise-pokemon-trading-cards",
        "pokemon-tcg",
        "trading-card-games",
    ],
    "one-piece": ["trading-card-games"],
    "mtg": ["trading-card-games"],
    "dragon-ball-z": ["trading-card-games"],
    "lorcana": ["trading-card-games"],
}


def scrape_shopify_graphql(tcg: str) -> list[dict]:
    """
    Fetch products via Shopify's Storefront GraphQL API.

    JB Hi-Fi uses Shopify Hydrogen (headless), so the traditional REST
    JSON endpoints are disabled. The Storefront GraphQL API is the
    actual data source their frontend uses.

    Strategy:
      a) Collection query — fetch all products from known collection handles.
      b) Search query — fallback for TCGs without a collection handle.

    Requires JBHIFI_STOREFRONT_TOKEN in .env (public read-only token).

    Returns a flat list of raw product dicts ready for enrich_product().
    """
    token = SHOPIFY_STOREFRONT_TOKEN
    if not token:
        logger.info("  JBHIFI_STOREFRONT_TOKEN not set in .env — skipping GraphQL")
        return []

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-Shopify-Storefront-Access-Token": token,
    }

    products = []
    seen_urls: set[str] = set()

    # (a) Try collection handles first (stop at the first one that works)
    handles = SHOPIFY_COLLECTION_HANDLES.get(tcg, [])
    for handle in handles:
        collection_products = _graphql_collection(handle, headers, seen_urls)
        if collection_products:
            products.extend(collection_products)
            break

    # (b) Fallback: search query
    if not products:
        query_str = SHOPIFY_SEARCH_QUERIES.get(tcg, f"{tcg} trading card")
        search_products = _graphql_search(query_str, headers, seen_urls)
        products.extend(search_products)

    if products:
        logger.info(f"  ✅ Found {len(products)} via Storefront GraphQL")
        for i, p in enumerate(products):
            logger.info(f"    [{i+1}] {p['name']} | ${p.get('price_raw') or '?'} | {p['url']}")

    return products


def _graphql_collection(handle: str, headers: dict, seen_urls: set[str]) -> list[dict]:
    """Fetch all products from a Shopify collection via GraphQL."""
    products = []
    after_cursor = None
    page = 0
    max_pages = 10

    logger.info(f"  Querying collection: {handle}")

    while page < max_pages:
        variables = {"handle": handle, "first": 50}
        if after_cursor:
            variables["after"] = after_cursor

        try:
            resp = SESSION.post(
                SHOPIFY_GRAPHQL_URL,
                headers=headers,
                json={"query": GRAPHQL_COLLECTION_QUERY, "variables": variables},
                timeout=20,
            )
            logger.info(f"  Collection GraphQL response: {resp.status_code} (page {page + 1})")

            if resp.status_code in (401, 403):
                logger.info(f"  Collection GraphQL {resp.status_code} — token issue")
                break

            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                logger.info(f"  Collection GraphQL errors: {str(data['errors'])[:200]}")
                break

            collection = data.get("data", {}).get("collection")
            if not collection:
                logger.info(f"  Collection '{handle}' not found")
                break

            edges = collection.get("products", {}).get("edges", [])
            if not edges:
                if page == 0:
                    logger.info(f"  Collection '{handle}' has 0 products")
                break

            for edge in edges:
                node = edge.get("node", {})
                product = _parse_graphql_product(node)
                if product and product["url"] not in seen_urls:
                    seen_urls.add(product["url"])
                    products.append(product)

            page_info = collection.get("products", {}).get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                after_cursor = page_info["endCursor"]
                page += 1
                time.sleep(1)
            else:
                break

        except Exception as e:
            logger.info(f"  Collection GraphQL failed: {e}")
            break

    return products


def _graphql_search(query_str: str, headers: dict, seen_urls: set[str]) -> list[dict]:
    """Search products via Shopify Storefront GraphQL search query."""
    products = []
    after_cursor = None
    page = 0
    max_pages = 10

    logger.info(f"  Searching GraphQL: '{query_str}'")

    while page < max_pages:
        variables = {"query": query_str, "first": 50}
        if after_cursor:
            variables["after"] = after_cursor

        try:
            resp = SESSION.post(
                SHOPIFY_GRAPHQL_URL,
                headers=headers,
                json={"query": GRAPHQL_SEARCH_QUERY, "variables": variables},
                timeout=20,
            )

            if resp.status_code in (401, 403):
                logger.info(f"  Search GraphQL {resp.status_code} — token issue")
                break

            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                logger.info(f"  Search GraphQL errors: {str(data['errors'])[:200]}")
                break

            search_data = data.get("data", {}).get("search", {})
            edges = search_data.get("edges", [])

            if not edges:
                if page == 0:
                    logger.info(f"  Search returned 0 products")
                break

            for edge in edges:
                node = edge.get("node", {})
                product = _parse_graphql_product(node)
                if product and product["url"] not in seen_urls:
                    seen_urls.add(product["url"])
                    products.append(product)

            page_info = search_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                after_cursor = page_info["endCursor"]
                page += 1
                time.sleep(1)
            else:
                break

        except Exception as e:
            logger.info(f"  Search GraphQL failed: {e}")
            break

    return products


def _parse_graphql_product(node: dict) -> dict | None:
    """Parse a product node from the Storefront GraphQL search response."""
    title = node.get("title", "").strip()
    handle = node.get("handle", "")
    if not title or not handle:
        return None

    url = f"https://www.jbhifi.com.au/products/{handle}"

    # Price from first variant
    price_raw = None
    price_str = ""
    sku = ""
    variant_edges = node.get("variants", {}).get("edges", [])
    if variant_edges:
        variant = variant_edges[0].get("node", {})
        sku = variant.get("sku", "")
        price_obj = variant.get("price", {})
        if price_obj:
            try:
                price_raw = float(price_obj.get("amount", 0))
                price_str = f"${price_raw:.2f}"
            except (ValueError, TypeError):
                pass

    # Image
    image = ""
    featured = node.get("featuredImage") or {}
    image = featured.get("url", "")

    # Pre-order detection
    tags = node.get("tags", [])
    is_preorder = (
        "pre-order" in title.lower()
        or "preorder" in title.lower()
        or any("pre-order" in t.lower() for t in tags if isinstance(t, str))
    )

    return {
        "name": title,
        "url": url,
        "price": price_str,
        "price_raw": price_raw,
        "sku": sku,
        "image": image,
        "is_preorder": is_preorder,
        "promo": "",
    }


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

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        raw_products: list[dict] = []

        # Strategy 1: Algolia browse with filters
        raw_products = scrape_algolia_browse(tcg)

        # Strategy 2: Shopify Storefront GraphQL API
        if not raw_products:
            logger.info("  Trying Shopify Storefront GraphQL API...")
            raw_products = scrape_shopify_graphql(tcg)
            if not raw_products:
                logger.info("  Storefront GraphQL returned no products")

        # Strategy 3 & 4: HTML scraping fallback
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
