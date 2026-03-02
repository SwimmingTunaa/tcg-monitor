"""
EB Games AU — Product Discovery
=================================
Discovers TCG product URLs from EB Games AU category pages.

Strategy (three-pass):
  1. Parse raw HTML with BeautifulSoup — EB Games server-side renders
     product tiles with data-sku, data-name, data-price attributes.
     Fast but often blocked by Cloudflare bot detection.
  2. Playwright with persistent context — uses a stored browser profile
     (cookies, fingerprint) so Cloudflare remembers us as a real browser.
     First run must be --headed to pass the challenge visually.
  3. Falls back to parsing the raw DOM HTML if JS hydration doesn't fire.

Usage:
    # First run: use headed mode to pass Cloudflare challenge once
    python discovery/ebgames_discovery.py --tcg pokemon --dry-run --headed

    # Subsequent runs: headless works using saved cookies
    python discovery/ebgames_discovery.py --tcg pokemon --dry-run

    # Skip image fetching for speed
    python discovery/ebgames_discovery.py --tcg pokemon --dry-run --no-images

Setup:
    pip install playwright beautifulsoup4 lxml
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

# Load .env file from project root
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

# Add project root to path
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

# Persistent browser profile directory (survives between runs)
BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")

# EB Games category pages per TCG
EB_CATEGORY_URLS = {
    "pokemon": [
        "https://www.ebgames.com.au/featured/pokemon-trading-card-game",
        "https://www.ebgames.com.au/featured/trading-cards",
    ],
    "one-piece": [
        "https://www.ebgames.com.au/featured/trading-cards",
    ],
    "mtg": [
        "https://www.ebgames.com.au/featured/trading-cards",
    ],
    "dragon-ball-z": [
        "https://www.ebgames.com.au/featured/trading-cards",
    ],
    "lorcana": [
        "https://www.ebgames.com.au/featured/trading-cards",
    ],
}

# Keywords that must appear in the product name for each TCG
TCG_NAME_KEYWORDS = {
    "pokemon": ["pokemon"],
    "one-piece": ["one piece", "op-"],
    "mtg": ["magic: the gathering", "magic gathering", "commander"],
    "dragon-ball-z": ["dragon ball", "dbz"],
    "lorcana": ["lorcana"],
}

# Product types we WANT to track
PRODUCT_ALLOWLIST = [
    "booster box",
    "booster bundle",
    "booster pack",
    "booster",
    "elite trainer box",
    "etb",
    "collection box",
    "premium collection",
    "tin",
    "blister",
    "starter deck",
    "theme deck",
    "build & battle",
    "trainer kit",
    "league battle deck",
    "knock out collection",
]

# Product types to skip
PRODUCT_BLOCKLIST = [
    "portfolio",
    "binder",
    "card sleeve",
    "sleeves",
    "deck box",
    "playmat",
    "card case",
    "display case",
    "storage box",
    "mini portfolio",
    "9-pocket",
    "4-pocket",
    "card divider",
    "damage counter",
    "coin",
    "dice",
    "figure",
    "plush",
    "squishmallow",
    "t-shirt",
    "poster",
    "card game mat",
    "checklane",
    "jersey",
    "hoodie",
    "cap",
    "hat",
    "backpack",
    "bag",
    "wallet",
    "keychain",
    "lanyard",
    "mug",
    "water bottle",
]

# Known Pokémon sets for auto-tagging
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

# Headers for raw HTTP requests
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
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


# ─── HTML Parsing (shared by all strategies) ─────────────────────────

def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product tiles from HTML using BeautifulSoup.

    EB Games server-side renders product tiles as:
        <div class="product-tile ..." data-sku="339236"
             data-name="Pokemon - TCG - ..." data-price="115" ...>
            <a href="/product/..." class="product-link details" ...>
                <img class="packshot-image" src="//c1-ebgames.eb-cdn.com.au/..." ...>
                <div class="release-date-info">...</div>  (if preorder)
            </a>
        </div>

    Skeleton placeholders lack data-sku and have class "skeleton-loader".
    """
    soup = BeautifulSoup(html, "lxml")
    tiles = soup.select(".product-tile[data-sku]")

    seen = set()
    products = []

    for tile in tiles:
        name = tile.get("data-name", "").strip()
        price_raw = tile.get("data-price", "")
        sku = tile.get("data-sku", "")

        # Get product link
        link = tile.select_one('a.product-link[href*="/product/"]')
        if not link:
            continue
        href = link.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.ebgames.com.au" + href

        if href in seen:
            continue
        seen.add(href)

        # Get product image (skip skeleton SVG placeholders)
        image_url = ""
        img = tile.select_one("img.packshot-image")
        if img:
            src = img.get("src", "")
            if src and not src.startswith("data:"):
                image_url = "https:" + src if src.startswith("//") else src

        # Check for preorder
        is_preorder = bool(tile.select_one(".release-date-info, .icon-preorder"))

        # Get promo badge
        promo_badge = tile.select_one(".promo-badge")
        promo = promo_badge.get_text(strip=True) if promo_badge else ""

        if name and href:
            try:
                price_num = float(price_raw)
            except (ValueError, TypeError):
                price_num = None

            products.append({
                "name": name,
                "url": href,
                "price": f"${price_num:.2f}" if price_num else "",
                "price_raw": price_num,
                "sku": sku,
                "image": image_url,
                "is_preorder": is_preorder,
                "promo": promo,
            })

    return products


# ─── Strategy 1: Raw HTTP (fast, often blocked by Cloudflare) ────────

def scrape_category_raw(url: str) -> list[dict]:
    """
    Fetch an EB Games category page via raw HTTP and parse product tiles.
    Fast when it works, but Cloudflare usually blocks it.
    """
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f"  Raw fetch failed: {e}")
        return []

    products = parse_products_from_html(resp.text)
    return products


# ─── Strategy 2: Playwright with persistent context ──────────────────

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-AU', 'en'] });
    delete window.__playwright;
    delete window.__pw_manual;
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
    return document.body.scrollHeight;
}
"""

EXTRACT_JS = """
() => {
    const seen = new Set();
    const products = [];

    document.querySelectorAll('.product-tile[data-sku]').forEach(tile => {
        const name = tile.getAttribute('data-name') || '';
        const price = tile.getAttribute('data-price') || '';
        const sku = tile.getAttribute('data-sku') || '';

        const link = tile.querySelector('a.product-link[href*="/product/"]');
        const href = link ? link.href : '';
        if (!href || seen.has(href)) return;
        seen.add(href);

        const img = tile.querySelector('img.packshot-image:not(.skeleton-loader)');
        const imageUrl = img ? (img.getAttribute('src') || '') : '';

        const preorderEl = tile.querySelector('.release-date-info, .icon-preorder');
        const isPreorder = !!preorderEl;

        const promoBadge = tile.querySelector('.promo-badge');
        const promoText = promoBadge ? promoBadge.textContent.trim() : '';

        if (name && href) {
            const priceNum = parseFloat(price) || null;
            products.push({
                name: name,
                url: href,
                price: priceNum ? ('$' + priceNum.toFixed(2)) : '',
                price_raw: priceNum,
                sku: sku,
                image: imageUrl.startsWith('//') ? ('https:' + imageUrl) : imageUrl,
                is_preorder: isPreorder,
                promo: promoText,
            });
        }
    });

    return products;
}
"""


def scrape_category_playwright(url: str, headed: bool = False) -> list[dict]:
    """
    Use Playwright with a persistent browser context to scrape EB Games.

    The persistent context stores cookies/localStorage between runs.
    On first run, use --headed so you can see (and pass) the Cloudflare
    challenge. After that, headless runs reuse the saved cookies.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("  Playwright not available")
        return []

    profile_dir = os.path.abspath(BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    with sync_playwright() as p:
        # launch_persistent_context gives us a real browser profile
        # that persists cookies, localStorage, etc. across runs
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-AU",
            viewport={"width": 1280, "height": 900},
        )

        # Inject stealth patches before navigation
        context.add_init_script(STEALTH_JS)
        page = context.new_page()

        try:
            mode = "headed" if headed else "headless"
            logger.info(f"  [{mode}] Loading: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # If headed, give user time to solve any Cloudflare challenge
            if headed:
                logger.info(f"  [headed] Waiting for page to fully load (solve any challenges)...")
                try:
                    page.wait_for_selector('.product-tile[data-sku]', timeout=60000)
                except PlaywrightTimeout:
                    logger.info(f"  [headed] Still waiting — trying DOM parse...")
            else:
                # Headless: wait for tiles to hydrate (JS adds data-sku)
                try:
                    page.wait_for_selector('.product-tile[data-sku]', timeout=30000)
                except PlaywrightTimeout:
                    # Log what we see so we can diagnose
                    counts = page.evaluate("""
                        () => {
                            const all = document.querySelectorAll('.product-tile');
                            const hydrated = document.querySelectorAll('.product-tile[data-sku]');
                            return { total: all.length, hydrated: hydrated.length };
                        }
                    """)
                    logger.warning(
                        f"  Timed out waiting for hydrated tiles. "
                        f"Total tiles: {counts['total']}, hydrated: {counts['hydrated']}"
                    )

            # Extra wait for hydration
            page.wait_for_timeout(3000)

            # Scroll to trigger lazy-loaded carousels
            logger.info(f"  Scrolling to load all products...")
            page.evaluate(SCROLL_JS)
            page.wait_for_timeout(2000)

            # Try JS extraction first (works if tiles are hydrated in DOM)
            products = page.evaluate(EXTRACT_JS)
            if products:
                logger.info(f"  Found {len(products)} products via JS extraction")
                return products

            # Fallback: parse the raw HTML that Playwright received
            logger.info(f"  JS extraction found 0 — trying raw HTML parse of Playwright DOM...")
            html = page.content()
            products = parse_products_from_html(html)
            if products:
                logger.info(f"  Found {len(products)} products via DOM HTML parse")
                return products

            logger.warning(f"  No products found on {url}")
            return []

        except Exception as e:
            logger.error(f"  Playwright error on {url}: {e}")
            return []
        finally:
            page.close()
            context.close()


# ─── Combined Scraping ───────────────────────────────────────────────

def scrape_category_page(url: str, tcg: str, headed: bool = False) -> list[dict]:
    """
    Scrape an EB Games category page for product data.

    Strategy:
      1. Try raw HTTP + BeautifulSoup (fast, no JS needed)
      2. If that fails, use Playwright with persistent context
    """
    # Strategy 1: raw HTML (skip if we know it'll fail — e.g. previous 403)
    if not headed:
        logger.info(f"  Loading (raw): {url}")
        products = scrape_category_raw(url)
        if products:
            logger.info(f"  ✅ Found {len(products)} products via raw HTML")
            return products
        logger.info(f"  Raw HTML had no products — falling back to Playwright")

    # Strategy 2: Playwright with persistent context
    products = scrape_category_playwright(url, headed=headed)
    return products


def fetch_product_image_playwright(url: str) -> Optional[str]:
    """
    Use Playwright to fetch a product page and extract its image URL.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None

    profile_dir = os.path.abspath(BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(STEALTH_JS)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)

            image_url = page.evaluate("""
                () => {
                    const og = document.querySelector('meta[property="og:image"]');
                    if (og && og.content) return og.content;
                    const img = document.querySelector('img[src*="eb-cdn"], img[src*="scene7"]');
                    if (img) return img.src.startsWith('//') ? 'https:' + img.src : img.src;
                    return null;
                }
            """)
            return image_url
        except Exception as e:
            logger.debug(f"  Image fetch failed for {url}: {e}")
            return None
        finally:
            page.close()
            context.close()


# ─── Product Filtering & Enrichment ─────────────────────────────────

def infer_set(name: str) -> Optional[str]:
    """Try to infer the TCG set from the product name."""
    name_lower = name.lower()
    for set_name, set_key in POKEMON_SETS.items():
        if set_name in name_lower:
            return set_key
    return None


def parse_price(price_str: str) -> Optional[float]:
    """Parse '$59.00' → 59.0"""
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
    """Filter and enrich a raw product dict. Returns None if it should be skipped."""
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()

    if not name or not url:
        return None
    if "ebgames.com.au" not in url or "/product/" not in url:
        return None

    # Only allow TCG-relevant product categories (blocks clothing, homewares, etc.)
    ALLOWED_URL_PATHS = ["/product/toys-and-collectibles/", "/product/trading-cards/"]
    if not any(path in url for path in ALLOWED_URL_PATHS):
        logger.debug(f"  Wrong category: {url}")
        return None

    name_lower = name.lower()

    # Must match the TCG keywords
    keywords = TCG_NAME_KEYWORDS.get(tcg, [tcg.lower()])
    if not any(kw in name_lower for kw in keywords):
        return None

    # Blocklist check
    for blocked in PRODUCT_BLOCKLIST:
        if blocked in name_lower:
            logger.debug(f"  Blocked '{blocked}': {name}")
            return None

    # Allowlist check
    if not any(allowed in name_lower for allowed in PRODUCT_ALLOWLIST):
        logger.debug(f"  Not in allowlist: {name}")
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "ebgames_au",
        "price": raw.get("price_raw") or parse_price(raw.get("price", "")),
        "price_str": raw.get("price", "") or None,
        "image": raw.get("image", "") or "",
        "sku": raw.get("sku", ""),
        "is_preorder": raw.get("is_preorder", False),
        "discovered_at": datetime.now().isoformat(),
        "source": "playwright_discovery",
    }


# ─── Database Integration ────────────────────────────────────────────

def save_new_products(products: list[dict], db: "Database") -> tuple[int, int]:
    """Save newly discovered products. Returns (added, skipped)."""
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
            sku=product.get("sku") or None,
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


def generate_config_entries(products: list[dict]) -> str:
    """Generate config/products.py entries for discovered products."""
    lines = []
    for p in products:
        set_val = f'"{p["set"]}"' if p.get("set") else "None"
        lines.append(f"""    {{
        "url": "{p["url"]}",
        "name": "{p["name"]}",
        "set": {set_val},
        "tcg": "{p["tcg"]}",
        "retailer": "ebgames_au",
        "image": "{p.get("image", "")}",
    }},""")
    return "\n".join(lines)


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_ebgames(tcg_filter: Optional[str] = None, dry_run: bool = False,
                     fetch_images: bool = True, headed: bool = False) -> list[dict]:
    """
    Run the full EB Games product discovery flow.
    """
    all_products = []
    seen_urls = set()

    categories = EB_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG: {tcg_filter}. Valid: {list(EB_CATEGORY_URLS.keys())}")
            return []

    logger.info(f"🔍 Starting EB Games discovery")
    logger.info(f"   TCG: {tcg_filter or 'all'}")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"   Browser: {'HEADED' if headed else 'headless'}")
    logger.info(f"   Images: {'yes' if fetch_images else 'skip'}")
    logger.info("")

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        for url in urls:
            raw_products = scrape_category_page(url, tcg, headed=headed)

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

    # Fetch images
    if fetch_images and all_products:
        needs_image = [p for p in all_products if not p.get("image")]
        if needs_image:
            logger.info(f"🖼️  Fetching product images ({len(needs_image)} pages)...")
            for i, product in enumerate(needs_image, 1):
                logger.info(f"  [{i}/{len(needs_image)}] {product['name'][:55]}")
                image_url = fetch_product_image_playwright(product["url"])
                if image_url:
                    product["image"] = image_url
                    logger.info(f"    ✅ {image_url[:80]}")
                else:
                    logger.info(f"    ⚠️  No image found")
                time.sleep(1)
            logger.info("")
        else:
            logger.info("🖼️  All products already have images from category tiles")

    # Output
    if dry_run:
        logger.info("── DRY RUN — Would add these products ────────")
        for p in all_products:
            set_label = f" [{p['set']}]" if p.get("set") else ""
            price_label = f" {p['price_str']}" if p.get("price_str") else ""
            img_label = " 🖼️" if p.get("image") else ""
            preorder_label = " ⏳PREORDER" if p.get("is_preorder") else ""
            logger.info(f"  {p['name']}{set_label}{price_label}{img_label}{preorder_label}")
            logger.info(f"    {p['url']}")
        logger.info("")
        logger.info("── config/products.py entries ─────────────────")
        print(generate_config_entries(all_products))
    else:
        if DB_AVAILABLE:
            db = Database()
            added, skipped = save_new_products(all_products, db)
            logger.info(f"✅ Done: {added} added, {skipped} already tracked")
        else:
            logger.warning("DB not available — printing config entries")
            print(generate_config_entries(all_products))

    return all_products


# ─── Entry Point ─────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="EB Games AU — TCG product discovery")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    parser.add_argument("--tcg", type=str, default=None,
                        help=f"TCG to discover. Options: {', '.join(EB_CATEGORY_URLS.keys())}")
    parser.add_argument("--no-images", action="store_true", help="Skip image fetching (faster)")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in headed mode (visible). Use on first run to pass Cloudflare challenge.")
    args = parser.parse_args()

    if not PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright not installed.")
        print("   pip install playwright")
        print("   playwright install chromium")
        sys.exit(1)

    discover_ebgames(
        tcg_filter=args.tcg,
        dry_run=args.dry_run,
        fetch_images=not args.no_images,
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
