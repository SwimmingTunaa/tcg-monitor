"""
Amazon AU — Product Discovery
================================
Discovers TCG product URLs from Amazon Australia.

Strategy:
  Playwright with persistent context — Amazon is aggressively anti-bot
  so direct HTTP scraping is unreliable. Playwright with a saved browser
  profile (cookies, fingerprints) gives the best results.

  - Scrapes search result pages (up to 3 pages per query)
  - Scrapes selected Amazon Store pages as an additional discovery source
  - Normalises all product URLs to https://www.amazon.com.au/dp/ASIN
    to avoid duplicates from different URL variations
  - Adds random delays between pages to avoid rate limiting
  - On first run use --headed to build a trusted cookie session

  Amazon Product Advertising API (PA-API v5) is NOT used — it requires
  an active Associates account and product sales. AMAZON_PA_* keys in
  .env are reserved for future use.

Usage:
    # First run: build cookie session
    python discovery/amazon_discovery.py --tcg pokemon --dry-run --headed

    # Subsequent runs: headless works with saved session
    python discovery/amazon_discovery.py --tcg pokemon --dry-run

Setup:
    pip install playwright python-dotenv
    playwright install chromium
"""

import os
import re
import sys
import time
import random
import logging
import argparse
from datetime import datetime
from typing import Optional

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
    infer_set, parse_price, make_playwright_context,
    save_new_products, log_dry_run,
)

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────

# Price bounds — filters out single loose packs (<$25) and scalper-priced
# old listings (>$600) that aren't useful for stock monitoring
MIN_PRICE = 25.0
MAX_PRICE = 600.0

# Max search result pages to scrape per query (3 × ~16 results = ~48 products)
MAX_PAGES = 3

# Amazon search URLs per TCG — multiple queries per TCG to catch more products
AMAZON_SEARCH_URLS: dict[str, list[str]] = {
    "pokemon": [
    ],
    "one-piece": [
        "https://www.amazon.com.au/s?k=one+piece+trading+card+game+booster+box",
    ],
    "mtg": [
        "https://www.amazon.com.au/s?k=magic+the+gathering+booster+box",
        "https://www.amazon.com.au/s?k=magic+the+gathering+commander+deck",
    ],
    "dragon-ball-z": [
        "https://www.amazon.com.au/s?k=dragon+ball+super+card+game+booster+box",
    ],
    "lorcana": [
        "https://www.amazon.com.au/s?k=disney+lorcana+trading+card+booster+box",
    ],
}

# Curated Amazon Store pages to enrich discovery beyond search results.
# These pages are useful when new listings are not yet indexed in normal search.
AMAZON_STORE_URLS: dict[str, list[str]] = {
    "pokemon": [
        "https://www.amazon.com.au/stores/Pok%C3%A9monTradingCardGame/page/49E55811-7F50-4D13-9DAD-D7D6DCA60918?lp_asin=B0G3CY83L5&ref_=ast_bln",
    ],
    "one-piece": [],
    "mtg": [],
    "dragon-ball-z": [],
    "lorcana": [],
}


# ─── JS Snippets ─────────────────────────────────────────────────────

EXTRACT_JS = """
() => {
    const seen = new Set();
    const products = [];

    // Amazon AU new grid layout — tiles are div.s-widget-container with data-csa-c-item-id
    // ASIN lives on the inner div[data-asin] (add-to-cart form), not the outer wrapper
    const tiles = document.querySelectorAll('div.s-widget-container[data-csa-c-item-id]');

    tiles.forEach(tile => {
        // Extract ASIN — prefer inner div[data-asin] (tiles with add-to-cart),
        // fall back to data-csa-c-item-id on the outer tile (e.g. "no featured offers" ETBs)
        const asinEl = tile.querySelector('div[data-asin]');
        let asin = asinEl ? asinEl.getAttribute('data-asin') : '';
        if (!asin || asin.trim() === '') {
            // data-csa-c-item-id format: "MAIN-SEARCH_RESULTS-3|B0XXXX|..." or just the ASIN
            const itemId = tile.getAttribute('data-csa-c-item-id') || '';
            const asinMatch = itemId.match(/\\b([A-Z0-9]{10})\\b/);
            asin = asinMatch ? asinMatch[1] : '';
        }
        if (!asin || asin.trim() === '') return;

        // Normalise URL to /dp/ASIN to avoid duplicate variations
        const url = `https://www.amazon.com.au/dp/${asin}`;
        if (seen.has(url)) return;
        seen.add(url);

        // Product name — h2.a-size-base-plus > span in new layout
        const nameEl = tile.querySelector(
            'h2.a-size-base-plus span, h2 a span, h2 .a-text-normal'
        );
        const name = nameEl ? nameEl.textContent.trim() : '';
        if (!name) return;

        // Price — .a-offscreen inside .a-price (unchanged, still works)
        let priceStr = '';
        let priceNum = null;
        const priceEl = tile.querySelector('.a-price .a-offscreen');
        if (priceEl) {
            const raw = priceEl.textContent.trim();
            const m = raw.match(/[\\d,]+\\.?\\d*/);
            if (m) {
                priceNum = parseFloat(m[0].replace(',', ''));
                priceStr = '$' + priceNum.toFixed(2);
            }
        }

        // Product image (unchanged)
        const img = tile.querySelector('img.s-image');
        const imageUrl = img ? img.src : '';

        // Sponsored label (unchanged)
        const isSponsored = !!tile.querySelector('.s-sponsored-label-info-icon, [data-component-type="s-sponsored-placements"]');

        products.push({
            name,
            url,
            asin,
            price: priceStr,
            price_raw: priceNum,
            sku: asin,
            image: imageUrl,
            is_preorder: name.toLowerCase().includes('pre-order') || name.toLowerCase().includes('coming soon'),
            is_sponsored: isSponsored,
            promo: isSponsored ? 'Sponsored' : '',
        });
    });

    return products;
}
"""

EXTRACT_STORE_JS = """
() => {
    const seen = new Set();
    const products = [];

    const anchors = Array.from(document.querySelectorAll('a[href*="/dp/"], a[href*="/gp/product/"]'));

    anchors.forEach(anchor => {
        const href = anchor.getAttribute('href') || '';
        const asinMatch = href.match(/\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/i);
        if (!asinMatch) return;

        const asin = asinMatch[1].toUpperCase();
        const url = `https://www.amazon.com.au/dp/${asin}`;
        if (seen.has(url)) return;
        seen.add(url);

        const card = anchor.closest('article, li, div');

        let name = '';
        const headingEl = (
            card && card.querySelector('h1, h2, h3, [aria-label], span')
        ) || anchor.querySelector('h1, h2, h3, span');
        if (headingEl) {
            name = (headingEl.textContent || '').trim();
        }
        if (!name) {
            name = (anchor.getAttribute('aria-label') || '').trim();
        }
        if (!name) {
            const img = (card && card.querySelector('img')) || anchor.querySelector('img');
            name = ((img && img.getAttribute('alt')) || '').trim();
        }
        if (!name) return;

        let priceStr = '';
        let priceNum = null;
        const priceEl =
            (card && card.querySelector('.a-price .a-offscreen')) ||
            anchor.querySelector('.a-price .a-offscreen');
        if (priceEl) {
            const raw = (priceEl.textContent || '').trim();
            const m = raw.match(/[\\d,]+\\.?\\d*/);
            if (m) {
                priceNum = parseFloat(m[0].replace(',', ''));
                priceStr = '$' + priceNum.toFixed(2);
            }
        }

        const imageEl = (card && card.querySelector('img')) || anchor.querySelector('img');
        const imageUrl = imageEl
            ? (imageEl.getAttribute('src') || imageEl.getAttribute('data-src') || '')
            : '';

        products.push({
            name,
            url,
            asin,
            price: priceStr,
            price_raw: priceNum,
            sku: asin,
            image: imageUrl,
            is_preorder: name.toLowerCase().includes('pre-order') || name.toLowerCase().includes('coming soon'),
            is_sponsored: false,
            promo: '',
        });
    });

    return products;
}
"""


# ─── Playwright Scraping ─────────────────────────────────────────────

def extract_asin_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).upper()

def scrape_search_page(base_url: str, headed: bool = False) -> list[dict]:
    """
    Scrape up to MAX_PAGES Amazon search result pages for a given query URL.

    Returns a flat list of raw product dicts.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("  Playwright not available — cannot scrape Amazon")
        return []

    all_products: list[dict] = []
    seen_asins: set[str] = set()

    with sync_playwright() as p:
        context = make_playwright_context(p, headed=headed)
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            for page_num in range(1, MAX_PAGES + 1):
                url = f"{base_url}&page={page_num}" if page_num > 1 else base_url
                mode = "headed" if headed else "headless"
                logger.info(f"  [{mode}] Page {page_num}: {url[:80]}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightTimeout:
                    logger.warning(f"  Timeout on page {page_num} — stopping pagination")
                    break

                # Wait for search results to appear (new Amazon AU grid layout)
                try:
                    page.wait_for_selector(
                        'div.s-widget-container[data-csa-c-item-id]',
                        timeout=15000
                    )
                except PlaywrightTimeout:
                    logger.warning(f"  No search results on page {page_num} — may be blocked")
                    break

                # Scroll to trigger lazy-loaded images
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(1500)

                # Extract products
                products = page.evaluate(EXTRACT_JS)
                new_count = 0

                for product in products:
                    asin = product.get("asin", "")
                    if asin and asin not in seen_asins:
                        seen_asins.add(asin)
                        all_products.append(product)
                        new_count += 1

                logger.info(f"  Page {page_num}: {new_count} new products (total: {len(all_products)})")

                if new_count == 0:
                    logger.info(f"  No new products on page {page_num} — stopping pagination")
                    break

                # Random delay between pages to avoid rate limiting
                if page_num < MAX_PAGES:
                    delay = random.uniform(2.0, 4.0)
                    logger.info(f"  Waiting {delay:.1f}s before next page...")
                    time.sleep(delay)

        except Exception as e:
            logger.error(f"  Playwright error: {e}")
        finally:
            page.close()
            context.close()

    return all_products


def scrape_store_page(store_url: str, headed: bool = False) -> list[dict]:
    """
    Scrape a curated Amazon Store page and extract product links containing ASINs.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("  Playwright not available — cannot scrape Amazon store")
        return []

    with sync_playwright() as p:
        context = make_playwright_context(p, headed=headed)
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            mode = "headed" if headed else "headless"
            logger.info(f"  [{mode}] Store: {store_url[:90]}")
            page.goto(store_url, wait_until="domcontentloaded", timeout=35_000)

            # Store pages often lazy-load; scroll before extraction.
            page.evaluate(SCROLL_JS)
            page.wait_for_timeout(1800)

            products = page.evaluate(EXTRACT_STORE_JS)
            logger.info(f"  Store extracted: {len(products)} raw products")
            return products
        except PlaywrightTimeout:
            logger.warning("  Store page timeout")
            return []
        except Exception as e:
            logger.warning(f"  Store scraping failed: {e}")
            return []
        finally:
            page.close()
            context.close()


# ─── Product Enrichment ───────────────────────────────────────────────

def enrich_product(raw: dict, tcg: str) -> Optional[dict]:
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()
    asin = (raw.get("asin", "").strip() or extract_asin_from_url(url) or "")

    if not name or not url:
        return None
    if "amazon.com.au" not in url:
        return None
    if not asin:
        return None
    url = f"https://www.amazon.com.au/dp/{asin}"

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

    price_val = raw.get("price_raw") or parse_price(raw.get("price", ""))
    if price_val is not None:
        if price_val < MIN_PRICE:
            logger.debug(f"  Below min price ${MIN_PRICE}: {name} (${price_val})")
            return None
        if price_val > MAX_PRICE:
            logger.debug(f"  Above max price ${MAX_PRICE}: {name} (${price_val})")
            return None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "amazon_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price") or None,
        "image": raw.get("image") or "",
        "sku": asin,
        "asin": asin,
        "is_preorder": raw.get("is_preorder", False),
        "is_sponsored": raw.get("is_sponsored", False),
        "in_stock": False,
        "discovered_at": datetime.now().isoformat(),
        "source": "amazon_discovery",
    }


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_amazon(tcg_filter: Optional[str] = None, dry_run: bool = False,
                    fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """Run the full Amazon AU product discovery flow."""
    all_products: list[dict] = []
    seen_urls: set[str] = set()

    all_tcg_keys = sorted(set(AMAZON_SEARCH_URLS) | set(AMAZON_STORE_URLS))
    categories = {
        tcg: {"search": AMAZON_SEARCH_URLS.get(tcg, []), "store": AMAZON_STORE_URLS.get(tcg, [])}
        for tcg in all_tcg_keys
    }
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Options: {all_tcg_keys}")
            return []

    logger.info("🔍 Starting Amazon AU discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"   Browser: {'HEADED' if headed else 'headless (use --headed on first run)'}")
    logger.info("")

    if not PLAYWRIGHT_AVAILABLE:
        logger.error("❌ Playwright is required for Amazon AU discovery.")
        logger.error("   pip install playwright && playwright install chromium")
        return []

    for tcg, sources in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        search_urls = sources.get("search", [])
        store_urls = sources.get("store", [])

        for search_url in search_urls:
            raw_products = scrape_search_page(search_url, headed=headed)

            for raw in raw_products:
                enriched = enrich_product(raw, tcg)
                if not enriched or enriched["url"] in seen_urls:
                    continue
                seen_urls.add(enriched["url"])
                all_products.append(enriched)

            # Delay between different search queries
            time.sleep(random.uniform(3.0, 5.0))

        for store_url in store_urls:
            raw_products = scrape_store_page(store_url, headed=headed)

            for raw in raw_products:
                enriched = enrich_product(raw, tcg)
                if not enriched or enriched["url"] in seen_urls:
                    continue
                seen_urls.add(enriched["url"])
                all_products.append(enriched)

            time.sleep(random.uniform(2.0, 3.5))

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

    parser = argparse.ArgumentParser(description="Amazon AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", default=None,
                        help=f"TCG to discover. Options: {', '.join(sorted(set(AMAZON_SEARCH_URLS) | set(AMAZON_STORE_URLS)))}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in headed mode. Use on first run to build a trusted cookie session.")
    args = parser.parse_args()

    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright not installed.")
        print("   pip install playwright")
        print("   playwright install chromium")
        import sys
        sys.exit(1)

    discover_amazon(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
