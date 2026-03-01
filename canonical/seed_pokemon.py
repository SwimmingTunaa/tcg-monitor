"""
Pokémon TCG Canonical Product Seeder
======================================
Builds the canonical_products table from two sources:

  1. A static SETS registry defined in this file — set names, keys,
     release dates. You add a new entry here when a set is announced.

  2. Pokémon Center AU product pages — scraped per set to get the
     actual product list (ETB, Booster Bundle, etc.) with images.
     Claude cleans up names and infers product types.

Why this approach instead of scraping Pokemon.com?
  - Pokemon.com blocks scrapers (Incapsula WAF)
  - Pokémon Center AU has clean product pages per set with images and prices
  - Static set registry means you control what gets tracked

Usage:
    python canonical/seed_pokemon.py                    # seed all active sets
    python canonical/seed_pokemon.py --set perfect-order  # seed one set
    python canonical/seed_pokemon.py --dry-run          # print without saving
    python canonical/seed_pokemon.py --list             # list known sets
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    print("❌ Missing dependency: pip install anthropic")
    sys.exit(1)

try:
    from utils.database import Database
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    print("⚠️  Could not import database — running in standalone mode")

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Set Registry ────────────────────────────────────────────────────
# This is your source of truth for which sets exist.
# Add a new entry here when a new set is announced.
#
# pokebeach_url: PokéBeach product lineup article (primary source)
# pokemon_center_url: Pokémon Center AU category page (kept for future use)
# active: False to stop monitoring without deleting canonical records

POKEMON_SETS = {
    "perfect-order": {
        "name": "Pokémon TCG: Mega Evolution—Perfect Order",
        "series": "Mega Evolution",
        "release_date": "2026-03-27",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2026/01/perfect-order-product-lineup-revealed",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/mega-evolution-perfect-order",
    },
    "ascended-heroes": {
        "name": "Pokémon TCG: Mega Evolution—Ascended Heroes",
        "series": "Mega Evolution",
        "release_date": "2026-01-30",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2025/11/ascended-heroes-special-set-revealed-for-january",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/ascended-heroes",
    },
    "phantasmal-flames": {
        "name": "Pokémon TCG: Mega Evolution—Phantasmal Flames",
        "series": "Mega Evolution",
        "release_date": "2025-11-07",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2025/09/phantasmal-flames-english-set-officially-revealed-for-november-smallest-english-set-in-years",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/phantasmal-flames",
    },
    "mega-evolutions": {
        "name": "Pokémon TCG: Mega Evolution",
        "series": "Mega Evolution",
        "release_date": "2025-09-05",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2025/07/mega-evolution-set-product-lineup-revealed-for-september",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/mega-evolution",
    },
    "journey-together": {
        "name": "Pokémon TCG: Scarlet & Violet—Journey Together",
        "series": "Scarlet & Violet",
        "release_date": "2025-03-28",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2025/01/journey-together-set-officially-revealed-for-march-featuring-owners-pokemon-other-sets-to-expect-in-2025",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/journey-together",
    },
    "destined-rivals": {
        "name": "Pokémon TCG: Scarlet & Violet—Destined Rivals",
        "series": "Scarlet & Violet",
        "release_date": "2025-05-30",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2025/03/destined-rivals-tcg-set-officially-revealed",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/destined-rivals",
    },
    "prismatic-evolutions": {
        "name": "Pokémon TCG: Scarlet & Violet—Prismatic Evolutions",
        "series": "Scarlet & Violet",
        "release_date": "2025-01-17",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2024/11/prismatic-evolution-special-set-officially-revealed-for-january",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/prismatic-evolutions",
    },
    "surging-sparks": {
        "name": "Pokémon TCG: Scarlet & Violet—Surging Sparks",
        "series": "Scarlet & Violet",
        "release_date": "2024-11-08",
        "active": True,
        "pokebeach_url": "https://www.pokebeach.com/2024/11/surging-sparks-set-guide-card-list-secret-rares-products-and-store-promotions",
        "pokemon_center_url": "https://www.pokemoncenter.com/category/surging-sparks",
    },
}

# Product types we care about — Claude will map retailer names to these
CANONICAL_TYPES = [
    "booster-box",
    "booster-bundle",
    "booster-pack",
    "elite-trainer-box",
    "pokemon-center-etb",
    "collection-box",
    "premium-collection",
    "tin",
    "blister",
    "three-pack-blister",
    "build-and-battle",
    "starter-deck",
    "league-battle-deck",
]

# Australian MSRP by product type (best estimates — update as needed)
AU_MSRP = {
    "booster-box": 189.95,
    "booster-bundle": 59.95,
    "booster-pack": 9.95,
    "elite-trainer-box": 89.95,
    "pokemon-center-etb": 99.95,
    "collection-box": 69.95,
    "premium-collection": 89.95,
    "tin": 34.95,
    "blister": 24.95,
    "three-pack-blister": 24.95,
    "build-and-battle": 34.95,
    "starter-deck": 19.95,
    "league-battle-deck": 49.95,
}

# ─── Name normalization ───────────────────────────────────────────────────
# Fixes inconsistent name generation from Claude (e.g. "3-Pack" vs "Three-Pack").
# Keys are substrings to find (case-insensitive), values are replacements.
TYPE_NAME_OVERRIDES = {
    "3-pack blister": "Three-Pack Blister",
    "3 pack blister": "Three-Pack Blister",
    "three pack blister": "Three-Pack Blister",
    "3-pack": "Three-Pack",
}


def normalize_product_name(name: str) -> str:
    """Apply TYPE_NAME_OVERRIDES to fix Claude generation inconsistencies."""
    for pattern, replacement in TYPE_NAME_OVERRIDES.items():
        name = re.sub(re.escape(pattern), replacement, name, flags=re.IGNORECASE)
    return name


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─── Scraping ────────────────────────────────────────────────────────

def fetch_pokemon_center_page(url: str) -> Optional[str]:
    """Fetch a Pokémon Center category page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        logger.info(f"  Fetched {url} ({len(resp.text):,} chars)")
        return resp.text
    except Exception as e:
        logger.warning(f"  Could not fetch {url}: {e}")
        return None


def fetch_pokebeach_article(url: str) -> Optional[str]:
    """
    Fetch a PokéBeach product lineup article and return the article body text.
    PokéBeach is openly scrapeable and lists product names in prose/bullet form.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # PokéBeach article body is in <div class="entry-content"> or similar
        article = (
            soup.find("div", class_="entry-content")
            or soup.find("article")
            or soup.find("main")
        )
        if not article:
            return None

        # Remove noisy elements
        for tag in article.find_all(["script", "style", "aside", "figure"]):
            tag.decompose()

        text = article.get_text(separator="\n", strip=True)
        logger.info(f"  Fetched PokéBeach article ({len(text):,} chars)")
        return text[:8000]  # Claude context limit
    except Exception as e:
        logger.warning(f"  Could not fetch PokéBeach {url}: {e}")
        return None


def extract_page_content(html: str) -> str:
    """Strip HTML down to product listings for Claude."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()

    # Extract product links + names
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        name = a.get_text(strip=True)
        if name and len(name) > 8 and any(
            kw in href.lower() for kw in ("/product/", "/p/", "trainer-box", "booster", "bundle")
        ):
            # Make absolute
            if href.startswith("/"):
                href = "https://www.pokemoncenter.com" + href
            links.append(f"{name} → {href}")

    text = soup.get_text(separator="\n", strip=True)

    return f"=== PAGE TEXT ===\n{text[:6000]}\n\n=== PRODUCT LINKS ===\n" + "\n".join(links[:80])


# ─── Claude Extraction ───────────────────────────────────────────────

def extract_products_with_claude(
    page_content: str,
    set_key: str,
    set_info: dict,
) -> list[dict]:
    """
    Use Claude Haiku to extract canonical product definitions from
    a Pokémon Center category page.

    Returns list of dicts with: name, type, image, msrp
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    type_list = "\n".join(f"  - {t}" for t in CANONICAL_TYPES)

    prompt = f"""You are building a canonical product database for Pokémon TCG set: "{set_info['name']}"
Set key: {set_key}
Release date: {set_info['release_date']}

From the page content below, extract all purchasable TCG products for this set.

For each product return:
- name: Clean canonical name (e.g. "Pokémon TCG: Mega Evolution—Perfect Order Elite Trainer Box")
- type: One of these exact type keys:
{type_list}
- image: Image URL if found, else null
- msrp_au: Australian price in dollars if found (number only, e.g. 89.95), else null

Rules:
- Only include card products — no accessories, portfolios, sleeves, playmats
- Use the set's full official name in the product name
- Normalise names to be clean and consistent
- If you see a Pokémon Center exclusive ETB, use type "pokemon-center-etb"

Return ONLY a JSON array, no markdown, no explanation:
[
  {{"name": "...", "type": "elite-trainer-box", "image": "https://...", "msrp_au": 89.95}},
  ...
]

If no products found: []

PAGE CONTENT:
{page_content}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        # Claude sometimes returns multiple JSON blocks — take the first valid array
        try:
            products = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"(\[.*?\])", raw, re.DOTALL)
            if match:
                products = json.loads(match.group(1))
            else:
                return []

        if not isinstance(products, list):
            return []

        logger.info(f"  Claude extracted {len(products)} canonical products")
        return products

    except Exception as e:
        logger.error(f"  Claude error: {e}")
        return []


def build_from_known_types(set_key: str, set_info: dict) -> list[dict]:
    """
    Fallback: if Pokémon Center page fails, build canonical products
    from known standard product types using Claude to generate names.
    """
    if not ANTHROPIC_API_KEY:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Standard products released for most sets
    standard_types = [
        "booster-box", "booster-bundle", "booster-pack",
        "elite-trainer-box", "three-pack-blister",
    ]

    prompt = f"""Generate canonical product names for the Pokémon TCG set: "{set_info['name']}"

For each of these product types, give the official full product name:
{json.dumps(standard_types)}

Use the format: "Pokémon TCG: [Set Name] [Product Type]"
For example: "Pokémon TCG: Mega Evolution—Perfect Order Elite Trainer Box"

Return ONLY a JSON array:
[
  {{"name": "...", "type": "booster-box", "image": null, "msrp_au": null}},
  ...
]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        products = json.loads(raw)
        logger.info(f"  Claude generated {len(products)} fallback products")
        return products if isinstance(products, list) else []
    except Exception as e:
        logger.error(f"  Claude fallback error: {e}")
        return []


# ─── ID Generation ───────────────────────────────────────────────────

def make_canonical_id(set_key: str, product_type: str) -> str:
    """
    Generate a stable, human-readable canonical ID.
    e.g. "perfect-order-elite-trainer-box"
    """
    return f"{set_key}-{product_type}"


# ─── Seeding ─────────────────────────────────────────────────────────

def seed_set(set_key: str, set_info: dict, db: Optional["Database"],
             dry_run: bool = False) -> list[dict]:
    """
    Seed canonical products for a single set.
    Returns list of canonical product dicts.
    """
    logger.info(f"\n── {set_info['name']} ({'active' if set_info['active'] else 'inactive'}) ──")
    logger.info(f"   Key: {set_key} | Release: {set_info['release_date']}")

    # Tier 1: PokéBeach article (real product list from official coverage)
    raw_products = []
    pokebeach_url = set_info.get("pokebeach_url")
    if pokebeach_url:
        logger.info(f"  Trying PokéBeach: {pokebeach_url}")
        article_text = fetch_pokebeach_article(pokebeach_url)
        if article_text and len(article_text) > 300:
            raw_products = extract_products_with_claude(
                f"=== POKEBEACH ARTICLE ===\n{article_text}",
                set_key, set_info,
            )
            if raw_products:
                logger.info(f"  ✅ PokéBeach source: {len(raw_products)} products")

    # Tier 2: Claude generator from known standard types
    if not raw_products:
        logger.info("  PokéBeach unavailable — generating from known types")
        raw_products = build_from_known_types(set_key, set_info)

    # Build canonical records
    canonical = []
    for p in raw_products:
        product_type = p.get("type", "").strip()
        name = normalize_product_name(p.get("name", "").strip())
        image = p.get("image") or None
        msrp = p.get("msrp_au") or AU_MSRP.get(product_type)

        if not product_type or product_type not in CANONICAL_TYPES:
            logger.debug(f"  Skipping unknown type '{product_type}': {name}")
            continue

        if not name:
            continue

        canonical_id = make_canonical_id(set_key, product_type)

        record = {
            "id": canonical_id,
            "name": name,
            "set_key": set_key,
            "type": product_type,
            "tcg": "pokemon",
            "msrp": msrp,
            "image": image,
            "release_date": set_info["release_date"],
        }
        canonical.append(record)

        if dry_run:
            msrp_str = f"  AU${msrp:.2f}" if msrp else ""
            logger.info(f"  [{product_type}] {name}{msrp_str}")
        elif db:
            is_new = db.upsert_canonical(
                id=canonical_id,
                name=name,
                set_key=set_key,
                type=product_type,
                tcg="pokemon",
                msrp=msrp,
                image=image,
                release_date=set_info["release_date"],
            )
            status = "✅ Added" if is_new else "↩️  Updated"
            logger.info(f"  {status}: {name}")

    return canonical


def seed_all(set_filter: Optional[str] = None, dry_run: bool = False) -> dict:
    """Seed canonical products for all active sets."""
    db = Database() if DB_AVAILABLE and not dry_run else None

    sets_to_seed = {
        k: v for k, v in POKEMON_SETS.items()
        if (set_filter is None or k == set_filter) and v.get("active", True)
    }

    if not sets_to_seed:
        logger.error(f"No matching sets found. Use --list to see available sets.")
        return {}

    logger.info(f"🌱 Seeding canonical products")
    logger.info(f"   Sets: {len(sets_to_seed)} | Mode: {'DRY RUN' if dry_run else 'LIVE'}")

    all_results = {}
    for set_key, set_info in sets_to_seed.items():
        results = seed_set(set_key, set_info, db, dry_run=dry_run)
        all_results[set_key] = results
        time.sleep(2)  # Polite delay between sets

    total = sum(len(v) for v in all_results.values())
    logger.info(f"\n✅ Done — {total} canonical products across {len(all_results)} sets")
    return all_results


# ─── Entry Point ─────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Pokémon TCG canonical product seeder")
    parser.add_argument("--set", type=str, default=None,
                        help="Only seed this set key (e.g. perfect-order)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print products without saving to DB")
    parser.add_argument("--list", action="store_true",
                        help="List all known sets and exit")
    args = parser.parse_args()

    if args.list:
        print("\nKnown Pokémon TCG sets:\n")
        for key, info in POKEMON_SETS.items():
            status = "✅" if info["active"] else "⏸️ "
            print(f"  {status} {key:<25} {info['release_date']}  {info['name']}")
        print()
        return

    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY not set — see .env file")
        sys.exit(1)

    seed_all(set_filter=args.set, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
