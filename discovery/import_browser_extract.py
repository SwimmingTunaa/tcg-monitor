"""
Import EB Games products extracted from your browser into the DB.

The headless scraper gets blocked by EB Games. This script takes JSON
that you extract from your real browser session (via Claude in Chrome)
and imports it into the monitoring database.

Usage:
    # 1. Extract from browser (Claude does this for you via the extension)
    # 2. Save output to a JSON file, e.g. ebgames_products.json
    # 3. Run:
    python discovery/import_browser_extract.py ebgames_products.json --dry-run
    python discovery/import_browser_extract.py ebgames_products.json
"""

import sys
import os
import json
import re
import argparse
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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

TCG_KEYWORDS = {
    "pokemon": ["pokemon"],
    "one-piece": ["one piece", "op-"],
    "mtg": ["magic: the gathering", "magic gathering"],
    "dragon-ball-z": ["dragon ball", "dbz"],
    "lorcana": ["lorcana"],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def infer_tcg(name: str) -> str:
    n = name.lower()
    for tcg, keywords in TCG_KEYWORDS.items():
        if any(kw in n for kw in keywords):
            return tcg
    return "unknown"


def infer_set(name: str) -> Optional[str]:
    n = name.lower()
    for set_name, set_key in POKEMON_SETS.items():
        if set_name in n:
            return set_key
    return None


def parse_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    m = re.search(r"\$?([\d,]+\.?\d*)", price_str)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def filter_product(raw: dict) -> Optional[dict]:
    name = raw.get("name", "").strip()
    url = raw.get("url", "").strip()
    if not name or not url:
        return None
    if "ebgames.com.au" not in url or "/product/" not in url:
        return None

    n = name.lower()

    for blocked in PRODUCT_BLOCKLIST:
        if blocked in n:
            return None

    if not any(allowed in n for allowed in PRODUCT_ALLOWLIST):
        return None

    tcg = infer_tcg(name)
    set_key = infer_set(name) if tcg == "pokemon" else None

    return {
        "url": url,
        "name": name,
        "set": set_key or tcg,
        "tcg": tcg,
        "retailer": "ebgames_au",
        "price": parse_price(raw.get("price", "")),
        "price_str": raw.get("price") or None,
        "image": "",
        "discovered_at": datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Import browser-extracted EB Games products to DB")
    parser.add_argument("input", help="JSON file with browser-extracted products")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    args = parser.parse_args()

    with open(args.input) as f:
        raw_products = json.load(f)

    logger.info(f"Loaded {len(raw_products)} raw products from {args.input}")

    filtered = []
    seen = set()
    for raw in raw_products:
        p = filter_product(raw)
        if p and p["url"] not in seen:
            seen.add(p["url"])
            filtered.append(p)

    by_tcg = defaultdict(list)
    for p in filtered:
        by_tcg[p["tcg"]].append(p)

    logger.info(f"\n── Filtered: {len(filtered)} trackable products ──────")
    for tcg, prods in sorted(by_tcg.items()):
        logger.info(f"  {tcg.upper()}: {len(prods)} products")
        for p in prods:
            set_label = f" [{p['set']}]" if p.get("set") and p["set"] != p["tcg"] else ""
            price_label = f" {p['price_str']}" if p.get("price_str") else ""
            logger.info(f"    {p['name']}{set_label}{price_label}")

    if args.dry_run:
        logger.info(f"\n── DRY RUN — not saving to DB ─────────────────")
        return

    # Import to DB
    try:
        from utils.database import Database
        from canonical.matcher import match_product
    except ImportError as e:
        logger.error(f"Cannot import DB utils: {e}")
        sys.exit(1)

    db = Database()
    added = skipped = 0

    for p in filtered:
        if db.get_last_status(p["url"]):
            skipped += 1
            continue

        db.update_status(
            url=p["url"], name=p["name"], retailer=p["retailer"],
            in_stock=False, price=p.get("price"), price_str=p.get("price_str"),
            status_changed=False,
        )

        set_key = p.get("set") if p.get("tcg") == "pokemon" else None
        match = match_product(p["name"], db, tcg=p.get("tcg", "pokemon"), set_key=set_key)
        db.set_canonical_match(p["url"], match["canonical_id"], match["status"])

        match_label = (
            f" → {match['canonical_id']} ({match['score']:.0%})"
            if match["canonical_id"]
            else f" (unmatched, {match['score']:.0%})"
        )
        logger.info(f"  ✅ {p['name']}{match_label}")
        added += 1

    logger.info(f"\n✅ Done: {added} added, {skipped} already tracked")


if __name__ == "__main__":
    main()
