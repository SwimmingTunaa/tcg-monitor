"""
Canonical Product Matcher
===========================
Fuzzy-matches discovered product names (from retailer scraping)
against canonical products in the DB.

Matching strategy:
  - Normalize both strings (lowercase, strip punctuation, expand abbreviations)
  - Use difflib.SequenceMatcher for similarity scoring
  - >85%  → auto-match  (status = "matched")
  - 60-85% → needs review (status = "review")
  - <60%  → unmatched   (status = "unmatched")

Never loses a product — everything gets saved, just with different match_status.

Usage:
    from canonical.matcher import match_product, run_bulk_match

    # Match a single scraped name against the DB
    result = match_product("Pokemon TCG Mega Evolution Perfect Order ETB", db, tcg="pokemon")
    # → {"canonical_id": "perfect-order-elite-trainer-box", "score": 0.91, "status": "matched"}

    # Re-run matching for all unmatched/review rows in product_status
    run_bulk_match(db)
"""

import re
import sys
import os
import logging
from difflib import SequenceMatcher
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)

# ─── Thresholds ──────────────────────────────────────────────────────
MATCH_THRESHOLD = 0.85   # auto-match
REVIEW_THRESHOLD = 0.60  # flag for review

# ─── Abbreviation expansion ──────────────────────────────────────────
ABBREVIATIONS = {
    r"\betb\b": "elite trainer box",
    r"\bbb\b": "booster bundle",
    r"\bbooster box\b": "booster box",
    r"\bbooster bundle\b": "booster bundle",
    r"\bbuild & battle\b": "build and battle",
    r"\bbuild and battle box\b": "build and battle",
    r"\bpokemon center\b": "pokemon center",
    r"\bpokemon centre\b": "pokemon center",
    r"\bpokémon\b": "pokemon",
    r"\bsv\b": "scarlet violet",
    r"\bs&v\b": "scarlet violet",
    r"\bscarlet & violet\b": "scarlet violet",
    r"\bmega evo\b": "mega evolution",
    r"\bperfect order\b": "perfect order",
    r"\bascended heroes\b": "ascended heroes",
    r"\bprismatic evolutions\b": "prismatic evolutions",
    r"\bjourney together\b": "journey together",
    r"\bdestined rivals\b": "destined rivals",
    r"\bsurging sparks\b": "surging sparks",
    r"\bphantasmal flames\b": "phantasmal flames",
}

# Words to strip entirely (noise)
NOISE_WORDS = {
    "the", "a", "an", "and", "or", "for", "of", "in", "at", "to",
    "with", "trading", "card", "game", "tcg", "official", "sealed",
    "new", "pack", "cards", "pokemon", "pokémon", "scarlet", "violet",
    "series", "expansion", "products", "product",
}


def normalize(text: str) -> str:
    """
    Normalize a product name for fuzzy matching.
    Lowercases, strips punctuation, expands abbreviations, removes noise.
    """
    t = text.lower()

    # Expand abbreviations
    for pattern, replacement in ABBREVIATIONS.items():
        t = re.sub(pattern, replacement, t, flags=re.IGNORECASE)

    # Strip punctuation except hyphens
    t = re.sub(r"[^\w\s-]", " ", t)
    t = re.sub(r"-", " ", t)

    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # Remove noise words
    tokens = [w for w in t.split() if w not in NOISE_WORDS]

    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def match_product(
    scraped_name: str,
    db,
    tcg: str = "pokemon",
    set_key: Optional[str] = None,
) -> dict:
    """
    Match a scraped product name against canonical products in the DB.

    Args:
        scraped_name: Raw product name from retailer scraping
        db: Database instance
        tcg: TCG type to filter canonicals (default "pokemon")
        set_key: Optional set to restrict matching to (speeds up + improves accuracy)

    Returns:
        {
            "canonical_id": str or None,
            "canonical_name": str or None,
            "score": float,
            "status": "matched" | "review" | "unmatched",
        }
    """
    canonicals = db.get_all_canonical(tcg=tcg, active_only=True)

    if not canonicals:
        logger.warning("No canonical products in DB — run seed_pokemon.py first")
        return {
            "canonical_id": None,
            "canonical_name": None,
            "score": 0.0,
            "status": "unmatched",
        }

    # Optionally filter by set
    if set_key:
        filtered = [c for c in canonicals if c["set_key"] == set_key]
        if filtered:
            canonicals = filtered

    normalized_scraped = normalize(scraped_name)
    logger.debug(f"  Matching: '{scraped_name}' → normalized: '{normalized_scraped}'")

    best_score = 0.0
    best_match = None

    for canonical in canonicals:
        normalized_canonical = normalize(canonical["name"])
        score = similarity(normalized_scraped, normalized_canonical)

        logger.debug(f"    vs '{canonical['name']}' → {score:.3f}")

        if score > best_score:
            best_score = score
            best_match = canonical

    if best_score >= MATCH_THRESHOLD:
        status = "matched"
    elif best_score >= REVIEW_THRESHOLD:
        status = "review"
    else:
        status = "unmatched"

    result = {
        "canonical_id": best_match["id"] if best_match and status != "unmatched" else None,
        "canonical_name": best_match["name"] if best_match and status != "unmatched" else None,
        "score": round(best_score, 4),
        "status": status,
    }

    logger.debug(
        f"  → {status} | score={best_score:.3f} | "
        f"canonical={result['canonical_id']}"
    )
    return result


def run_bulk_match(db, tcg: str = "pokemon", retailer: str = None, dry_run: bool = False):
    """
    Re-run matching for all unmatched/review rows in product_status.

    Args:
        db: Database instance
        tcg: Restrict to this TCG (default pokemon)
        retailer: Restrict to this retailer key
        dry_run: Print results without writing to DB
    """
    rows = db.get_unmatched(retailer=retailer)

    if not rows:
        logger.info("No unmatched products found")
        return

    logger.info(f"Running bulk match on {len(rows)} products...")

    stats = {"matched": 0, "review": 0, "unmatched": 0}

    for row in rows:
        name = row["name"]
        url = row["url"]

        result = match_product(name, db, tcg=tcg)
        status = result["status"]
        stats[status] += 1

        if dry_run:
            score_str = f"{result['score']:.0%}"
            canonical_str = result["canonical_id"] or "—"
            flag = "✅" if status == "matched" else ("⚠️ " if status == "review" else "❌")
            print(f"  {flag} [{score_str}] {name}")
            print(f"       → {canonical_str}")
        else:
            db.set_canonical_match(url, result["canonical_id"], status)
            logger.info(
                f"  [{status}] {name[:60]} "
                f"→ {result['canonical_id'] or '(none)'} "
                f"({result['score']:.0%})"
            )

    logger.info(
        f"\nBulk match complete: "
        f"{stats['matched']} matched, "
        f"{stats['review']} review, "
        f"{stats['unmatched']} unmatched"
    )
    return stats


# ─── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Canonical product matcher")
    subparsers = parser.add_subparsers(dest="command")

    # Test a single name
    test_p = subparsers.add_parser("test", help="Test matching a single product name")
    test_p.add_argument("name", help="Product name to match")
    test_p.add_argument("--set", dest="set_key", default=None)

    # Bulk re-match all unmatched rows
    bulk_p = subparsers.add_parser("bulk", help="Re-match all unmatched products in DB")
    bulk_p.add_argument("--retailer", default=None)
    bulk_p.add_argument("--dry-run", action="store_true")

    # Show normalize output (debug)
    norm_p = subparsers.add_parser("normalize", help="Show normalized form of a name")
    norm_p.add_argument("name")

    args = parser.parse_args()

    if args.command == "normalize":
        print(f"Input:      {args.name}")
        print(f"Normalized: {normalize(args.name)}")

    elif args.command == "test":
        from utils.database import Database
        db = Database()
        result = match_product(args.name, db, set_key=args.set_key)
        print(f"\nInput:     {args.name}")
        print(f"Status:    {result['status']}")
        print(f"Score:     {result['score']:.0%}")
        print(f"Matched:   {result['canonical_name'] or '(none)'}")
        print(f"ID:        {result['canonical_id'] or '(none)'}")

    elif args.command == "bulk":
        from utils.database import Database
        db = Database()
        run_bulk_match(db, retailer=args.retailer, dry_run=args.dry_run)

    else:
        parser.print_help()
