"""
EB Games AU — AI-Powered Product Discovery
==========================================
Automatically discovers TCG product URLs from EB Games AU category pages
using Claude API (Haiku) to extract product listings from HTML.

Runs as a weekly job. Found products are saved to the database and
automatically picked up by the monitor on the next cycle.

Usage:
    python discovery/ebgames_discovery.py              # Discover all TCG products
    python discovery/ebgames_discovery.py --dry-run    # Print found products, don't save
    python discovery/ebgames_discovery.py --tcg pokemon # Only discover Pokémon products

Setup:
    pip install anthropic
    Set ANTHROPIC_API_KEY in your environment or .env file
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Optional

# Add project root to path so we can import from config/utils
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import anthropic
except ImportError:
    print("❌ Missing dependency: pip install anthropic")
    sys.exit(1)

try:
    from utils.database import Database
    from utils.helpers import get_random_headers, RETAILER_NAMES
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    print("⚠️  Could not import project utils — running in standalone mode")

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# EB Games category pages to crawl
# Add/remove categories as needed
EB_CATEGORY_URLS = {
    "pokemon": [
        "https://www.ebgames.com.au/category/trading-cards?q=pokemon",
        "https://www.ebgames.com.au/category/toys-and-collectibles?q=pokemon+tcg",
    ],
    "one-piece": [
        "https://www.ebgames.com.au/category/trading-cards?q=one+piece",
    ],
    "mtg": [
        "https://www.ebgames.com.au/category/trading-cards?q=magic+gathering",
    ],
    "dragon-ball-z": [
        "https://www.ebgames.com.au/category/trading-cards?q=dragon+ball",
    ],
    "lorcana": [
        "https://www.ebgames.com.au/category/trading-cards?q=lorcana",
    ],
}

# TCG keywords to identify relevant products
TCG_KEYWORDS = {
    "pokemon": [
        "pokemon", "pokémon", "pikachu", "charizard",
        "booster", "elite trainer", "etb", "collection box",
        "tin", "blister", "bundle",
    ],
    "one-piece": ["one piece", "op-"],
    "mtg": ["magic: the gathering", "magic gathering", "commander", "draft booster"],
    "dragon-ball-z": ["dragon ball", "dbz"],
    "lorcana": ["lorcana"],
}

# Product types to capture (helps with set inference)
PRODUCT_TYPES = [
    "elite trainer box", "etb",
    "booster box", "booster bundle", "booster pack",
    "collection box", "collection",
    "tin", "blister",
    "starter deck", "theme deck",
    "premium collection",
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─── HTML Fetching ───────────────────────────────────────────────────

def fetch_category_page(url: str) -> Optional[str]:
    """Fetch an EB Games category page and return the raw HTML."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        logger.info(f"  Fetched {url} ({len(resp.text):,} chars)")
        return resp.text
    except Exception as e:
        logger.error(f"  Failed to fetch {url}: {e}")
        return None


def extract_product_listing_html(html: str) -> str:
    """
    Strip down the full page HTML to just the product listing area.
    This reduces token usage significantly before sending to Claude.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noise: nav, footer, scripts, styles, ads
    for tag in soup(["script", "style", "nav", "footer", "header",
                      "aside", "iframe", "noscript"]):
        tag.decompose()

    # Try to find the product grid/listing container
    # EB Games typically uses a product grid with these classes
    product_container = (
        soup.find("div", class_=re.compile(r"product.?grid|product.?list|search.?results|category.?products", re.I))
        or soup.find("ul", class_=re.compile(r"product", re.I))
        or soup.find("main")
        or soup.find("div", {"id": re.compile(r"main|content|products", re.I)})
    )

    if product_container:
        text = product_container.get_text(separator="\n", strip=True)
    else:
        # Fallback: use full page text but truncate
        text = soup.get_text(separator="\n", strip=True)

    # Also extract all anchor tags with hrefs (to catch product links)
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/product/" in href:
            name = a.get_text(strip=True)
            if name and len(name) > 5:
                links.append(f"{name} → {href}")

    # Combine: text content + extracted links
    combined = f"=== PAGE TEXT ===\n{text[:8000]}\n\n=== PRODUCT LINKS FOUND ===\n"
    combined += "\n".join(links[:100])  # Cap at 100 links

    return combined


# ─── Claude AI Extraction ────────────────────────────────────────────

def extract_products_with_claude(page_content: str, tcg: str, category_url: str) -> list[dict]:
    """
    Use Claude Haiku to extract TCG product listings from page content.
    Returns a list of product dicts with url, name, tcg fields.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set. Get one at console.anthropic.com")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tcg_label = tcg.replace("-", " ").title()
    keywords = ", ".join(TCG_KEYWORDS.get(tcg, [tcg]))

    prompt = f"""You are extracting {tcg_label} TCG product listings from an EB Games Australia page.

The page content below is from: {category_url}

Your task:
1. Find all {tcg_label} TCG products listed on this page
2. For each product, extract:
   - name: The full product name exactly as shown
   - url: The product URL (make it absolute: prefix with https://www.ebgames.com.au if relative)
3. Only include actual TCG products — ignore accessories, consoles, games, etc.
4. Keywords that indicate a relevant product: {keywords}

Return ONLY a JSON array. No explanation, no markdown, just raw JSON like:
[
  {{"name": "Pokémon TCG: Journey Together Elite Trainer Box", "url": "https://www.ebgames.com.au/product/..."}},
  {{"name": "Pokémon TCG: Surging Sparks Booster Bundle", "url": "https://www.ebgames.com.au/product/..."}}
]

If no products found, return: []

PAGE CONTENT:
{page_content}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        products = json.loads(raw)

        if not isinstance(products, list):
            logger.warning("Claude returned non-list response")
            return []

        logger.info(f"  Claude found {len(products)} products")
        return products

    except json.JSONDecodeError as e:
        logger.error(f"  Claude returned invalid JSON: {e}")
        logger.debug(f"  Raw response: {raw[:500]}")
        return []
    except Exception as e:
        logger.error(f"  Claude API error: {e}")
        return []


# ─── Product Enrichment ──────────────────────────────────────────────

def infer_set(name: str) -> Optional[str]:
    """Try to infer the TCG set from the product name."""
    name_lower = name.lower()
    for set_name, set_key in POKEMON_SETS.items():
        if set_name in name_lower:
            return set_key
    return None


def infer_product_type(name: str) -> str:
    """Infer product type from name for display purposes."""
    name_lower = name.lower()
    for pt in PRODUCT_TYPES:
        if pt in name_lower:
            return pt.title()
    return "Product"


def enrich_product(raw: dict, tcg: str) -> Optional[dict]:
    """
    Take a raw product dict from Claude and enrich it with
    set info, retailer key, and other metadata.
    Returns None if the product doesn't look valid.
    """
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()

    if not name or not url:
        return None

    # Ensure URL is absolute
    if url.startswith("/"):
        url = f"https://www.ebgames.com.au{url}"

    # Must be an EB Games URL
    if "ebgames.com.au" not in url:
        return None

    # Must have /product/ in URL (actual product page, not category)
    if "/product/" not in url:
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "ebgames_au",
        "image": "",
        "discovered_at": datetime.now().isoformat(),
        "source": "ai_discovery",
    }


# ─── Database Integration ────────────────────────────────────────────

def save_new_products(products: list[dict], db: "Database") -> tuple[int, int]:
    """
    Save newly discovered products to the database.
    Returns (added, skipped) counts.
    """
    added = 0
    skipped = 0

    for product in products:
        url = product["url"]

        # Check if we already track this URL
        existing = db.get_last_status(url)
        if existing:
            skipped += 1
            continue

        # Insert as a new product with unknown stock status
        # The monitor will pick it up on the next cycle
        db.update_status(
            url=url,
            name=product["name"],
            retailer=product["retailer"],
            in_stock=False,
            status_changed=False,
        )
        added += 1
        logger.info(f"  ✅ Added: {product['name']}")

    return added, skipped


def update_products_config(products: list[dict], dry_run: bool = False) -> str:
    """
    Generate the Python config entries for new products.
    Used to update config/products.py with discovered products.
    """
    lines = []
    for p in products:
        set_val = f'"{p["set"]}"' if p.get("set") else "None"
        lines.append(f"""    {{
        "url": "{p["url"]}",
        "name": "{p["name"]}",
        "set": {set_val},
        "tcg": "{p["tcg"]}",
        "retailer": "ebgames_au",
        "image": "",
    }},""")

    return "\n".join(lines)


# ─── Main Discovery Flow ─────────────────────────────────────────────

def discover_ebgames(tcg_filter: Optional[str] = None, dry_run: bool = False) -> list[dict]:
    """
    Run the full EB Games product discovery flow.

    1. Fetch each category page
    2. Strip HTML down to product listings
    3. Send to Claude to extract product URLs + names
    4. Enrich and deduplicate
    5. Save to DB (unless dry_run)
    """
    all_products = []
    seen_urls = set()

    categories = EBGAMES_CATEGORY_URLS = EB_CATEGORY_URLS
    if tcg_filter:
        categories = {k: v for k, v in categories.items() if k == tcg_filter}
        if not categories:
            logger.error(f"Unknown TCG filter: {tcg_filter}. Valid: {list(EB_CATEGORY_URLS.keys())}")
            return []

    logger.info(f"🔍 Starting EB Games discovery ({len(categories)} TCG types)")
    logger.info(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("")

    for tcg, urls in categories.items():
        logger.info(f"── {tcg.upper()} ──────────────────────────────")

        for url in urls:
            logger.info(f"  Fetching: {url}")

            # Step 1: Fetch the page
            html = fetch_category_page(url)
            if not html:
                continue

            # Step 2: Strip down HTML
            page_content = extract_product_listing_html(html)
            logger.info(f"  Extracted {len(page_content):,} chars of content")

            # Step 3: Claude extraction
            logger.info(f"  Sending to Claude Haiku...")
            raw_products = extract_products_with_claude(page_content, tcg, url)

            # Step 4: Enrich and deduplicate
            for raw in raw_products:
                enriched = enrich_product(raw, tcg)
                if not enriched:
                    continue
                if enriched["url"] in seen_urls:
                    continue
                seen_urls.add(enriched["url"])
                all_products.append(enriched)

            # Be polite between requests
            time.sleep(2)

        logger.info("")

    logger.info(f"📦 Total unique products found: {len(all_products)}")
    logger.info("")

    # Step 5: Save or display
    if dry_run:
        logger.info("── DRY RUN — Products that would be added ─────")
        for p in all_products:
            set_label = f" [{p['set']}]" if p.get("set") else ""
            logger.info(f"  {p['name']}{set_label}")
            logger.info(f"    {p['url']}")
        logger.info("")
        logger.info("── Config entries (copy to config/products.py) ─")
        print(update_products_config(all_products))

    else:
        if DB_AVAILABLE:
            db = Database()
            added, skipped = save_new_products(all_products, db)
            logger.info(f"✅ Discovery complete: {added} added, {skipped} already tracked")
        else:
            logger.warning("Database not available — printing config entries instead")
            print(update_products_config(all_products))

    return all_products


# ─── Entry Point ─────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="EB Games AU — AI-powered TCG product discovery"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print found products without saving to DB",
    )
    parser.add_argument(
        "--tcg",
        type=str,
        default=None,
        help=f"Only discover this TCG. Options: {', '.join(EB_CATEGORY_URLS.keys())}",
    )
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY not set!")
        print("   Get an API key at: https://console.anthropic.com")
        print("   Then: export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    discover_ebgames(tcg_filter=args.tcg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
