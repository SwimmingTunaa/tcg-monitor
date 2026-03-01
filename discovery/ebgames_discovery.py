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

# Load .env file from project root
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass  # dotenv not installed, rely on environment variables
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
    from canonical.matcher import match_product
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    match_product = None
    print("⚠️  Could not import project utils — running in standalone mode")

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# EB Games category pages to crawl
# Add/remove categories as needed
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

# Product types we WANT to track (must match at least one)
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

# Product types we NEVER want to track
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
]

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


def fetch_product_image(url: str) -> Optional[str]:
    """
    Fetch a product page and extract its image URL.

    EB Games renders images via JS so og:image is often missing.
    We try multiple sources in order of reliability:
      1. __NEXT_DATA__ JSON blob (Next.js SSR data, always present)
      2. JSON-LD structured data
      3. og:image meta tag
      4. Any img tag with a CDN URL
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # ── 1. Next.js __NEXT_DATA__ (most reliable for EB Games) ────
        next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data_tag and next_data_tag.string:
            try:
                next_data = json.loads(next_data_tag.string)
                # Walk the props tree to find image URLs
                # Path varies but images are usually in props.pageProps.product
                props = next_data.get("props", {})
                page_props = props.get("pageProps", {})
                product = page_props.get("product", page_props.get("data", {}))

                # Try common image field names
                for field in ("imageUrl", "image", "images", "primaryImage", "thumbnail"):
                    val = product.get(field)
                    if isinstance(val, str) and val.startswith("http"):
                        return val
                    if isinstance(val, list) and val:
                        first = val[0]
                        if isinstance(first, str) and first.startswith("http"):
                            return first
                        if isinstance(first, dict):
                            for k in ("url", "src", "href"):
                                if first.get(k, "").startswith("http"):
                                    return first[k]

                # Broader search: find any CDN image URL in the JSON
                raw_json = next_data_tag.string
                cdn_matches = re.findall(
                    r'https://[^"]+(?:ebgames|scene7|cloudinary|cdn)[^"]+\.(?:jpg|jpeg|png|webp)',
                    raw_json, re.I
                )
                if cdn_matches:
                    return cdn_matches[0]

            except (json.JSONDecodeError, AttributeError):
                pass

        # ── 2. JSON-LD structured data ───────────────────────────────
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
                items = [data] if isinstance(data, dict) else data
                for item in items:
                    if item.get("@type") == "Product":
                        img = item.get("image")
                        if isinstance(img, str) and img.startswith("http"):
                            return img
                        if isinstance(img, list) and img:
                            return img[0]
            except (json.JSONDecodeError, TypeError):
                continue

        # ── 3. <link itemprop="image"> — schema.org microdata ────────
        link_img = soup.find("link", {"itemprop": "image"})
        if link_img and link_img.get("href"):
            href = link_img["href"]
            if href.startswith("//"):
                href = "https:" + href
            return href

        # ── 4. <img class="gallery-img"> — product gallery image ──────
        gallery_img = soup.find("img", class_="gallery-img")
        if gallery_img and gallery_img.get("src"):
            src = gallery_img["src"]
            if src.startswith("//"):
                src = "https:" + src
            return src

        # ── 5. og:image meta tag ─────────────────────────────────────
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            return og_img["content"]

        # ── 6. Any img tag pointing to EB CDN ────────────────────────
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if "eb-cdn.com.au" in src and any(ext in src.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                if src.startswith("//"):
                    src = "https:" + src
                return src

    except Exception as e:
        logger.debug(f"  Could not fetch image for {url}: {e}")

    return None


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

    # Filter by product type
    name_lower = name.lower()

    # Blocklist check — skip accessories, storage, merch
    for blocked in PRODUCT_BLOCKLIST:
        if blocked in name_lower:
            logger.debug(f"  Skipping (blocklisted '{blocked}'): {name}")
            return None

    # Allowlist check — must be a card product we care about
    if not any(allowed in name_lower for allowed in PRODUCT_ALLOWLIST):
        logger.debug(f"  Skipping (not in allowlist): {name}")
        return None

    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "ebgames_au",
        "image": "",  # Filled in by fetch_product_image later
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
            image_url=product.get("image") or None,
            status_changed=False,
        )

        # Attempt canonical matching
        if match_product:
            set_key = product.get("set") if product.get("tcg") == "pokemon" else None
            match = match_product(
                product["name"], db,
                tcg=product.get("tcg", "pokemon"),
                set_key=set_key,
            )
            db.set_canonical_match(url, match["canonical_id"], match["status"])
            match_label = f" → {match['canonical_id']} ({match['score']:.0%})" if match["canonical_id"] else f" (unmatched, {match['score']:.0%})"
            logger.info(f"  ✅ Added: {product['name']}{match_label}")
        else:
            logger.info(f"  ✅ Added: {product['name']}")

        added += 1

    return added, skipped


def update_products_config(products: list[dict], dry_run: bool = False) -> str:
    """
    Generate the Python config entries for new products.
    Used to update config/products.py with discovered products.
    """
    lines = []
    for p in products:
        set_val = f'"{p["set"]}"' if p.get("set") else "None"
        image_val = p.get("image", "")
        lines.append(f"""    {{
        "url": "{p["url"]}",
        "name": "{p["name"]}",
        "set": {set_val},
        "tcg": "{p["tcg"]}",
        "retailer": "ebgames_au",
        "image": "{image_val}",
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

    # Step 5: Fetch images for all discovered products
    if all_products:
        logger.info(f"🖼️  Fetching images for {len(all_products)} products...")
        for product in all_products:
            image_url = fetch_product_image(product["url"])
            if image_url:
                product["image"] = image_url
                logger.info(f"  ✅ {product['name'][:50]}")
            else:
                logger.info(f"  ⚠️  No image: {product['name'][:50]}")
            time.sleep(1)  # Be polite between product page fetches
        logger.info("")

        logger.info("")

    logger.info(f"📦 Total unique products found: {len(all_products)}")
    logger.info("")

    # Step 5: Save or display
    if dry_run:
        logger.info("── DRY RUN — Products that would be added ─────")
        for p in all_products:
            set_label = f" [{p['set']}]" if p.get("set") else ""
            image_label = " 🖼️" if p.get("image") else " (no image)"
            logger.info(f"  {p['name']}{set_label}{image_label}")
            logger.info(f"    {p['url']}")
            if p.get("image"):
                logger.info(f"    {p['image']}")
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
