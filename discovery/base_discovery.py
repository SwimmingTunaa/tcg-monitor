"""
Discovery Base Module
======================
Shared constants, utilities, and helpers for all retailer discovery scripts.

Import from each retailer script:
    from discovery.base_discovery import (
        PRODUCT_ALLOWLIST, PRODUCT_BLOCKLIST, TCG_NAME_KEYWORDS, POKEMON_SETS,
        REQUEST_HEADERS, BROWSER_PROFILE_DIR, STEALTH_JS, SCROLL_JS,
        infer_set, parse_price, make_session, make_playwright_context,
        save_new_products, log_dry_run,
    )
"""

import os
import re
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────

# Persistent browser profile — shared across all retailers so cookies
# and fingerprints survive between runs.
BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")

# ─── Product Filtering ────────────────────────────────────────────────

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

# Product types to skip entirely
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
    "storage case",
    "mini portfolio",
    "9-pocket",
    "4-pocket",
    "12 pocket",
    "zip binder",
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
    # Accessories / protective gear
    "acrylic",
    "toploader",
    "top loader",
    "carrying case",
    "protective box",
    "album",
    "card album",
    "organiser",
    "organizer",
    "commander display",
    "deck case",
    # Third-party loose/random lots
    "card lot",
    "pack lot",
    "random selection",
    "lot of ",
    "mystery",
    "booster packs x",
    # Single cards / promos
    "single card",
    "& 1 random foil",
    "- single",
]

# Keywords that must appear in a product name for each TCG
TCG_NAME_KEYWORDS: dict[str, list[str]] = {
    "pokemon": ["pokemon"],
    "one-piece": ["one piece", "op-"],
    "mtg": ["magic: the gathering", "magic gathering", "commander"],
    "dragon-ball-z": ["dragon ball", "dbz"],
    "lorcana": ["lorcana"],
}

# Known Pokémon sets for set-key inference
# Ordered newest-first so earlier matches win on ambiguous names
POKEMON_SETS: dict[str, str] = {
    # ── Mega Evolution series (2025–2026) ──
    "perfect order": "perfect-order",
    "ascended heroes": "ascended-heroes",
    "phantasmal flames": "phantasmal-flames",
    "mega evolutions 2": "mega-evolutions-2",
    "mega evolutions 1": "mega-evolutions-1",
    "mega evolutions": "mega-evolutions",
    "mega evolution": "mega-evolutions",
    # ── Scarlet & Violet (2023–2026) ──
    "journey together": "journey-together",
    "destined rivals": "destined-rivals",
    "prismatic evolutions": "prismatic-evolutions",
    "surging sparks": "surging-sparks",
    "stellar crown": "stellar-crown",
    "twilight masquerade": "twilight-masquerade",
    "temporal forces": "temporal-forces",
    "paldean fates": "paldean-fates",
    "paradox rift": "paradox-rift",
    "obsidian flames": "obsidian-flames",
    "151": "pokemon-151",
    "pokemon 151": "pokemon-151",
    "pokémon 151": "pokemon-151",
    "paldea evolved": "paldea-evolved",
    "scarlet & violet base": "scarlet-violet-base",
    # ── Sword & Shield (2020–2023) ──
    "crown zenith": "crown-zenith",
    "silver tempest": "silver-tempest",
    "lost origin": "lost-origin",
    "pokemon go": "pokemon-go",
    "astral radiance": "astral-radiance",
    "brilliant stars": "brilliant-stars",
    "fusion strike": "fusion-strike",
    "celebrations": "celebrations",
    "evolving skies": "evolving-skies",
    "chilling reign": "chilling-reign",
    "battle styles": "battle-styles",
    "shining fates": "shining-fates",
    "vivid voltage": "vivid-voltage",
    "champion's path": "champions-path",
    "darkness ablaze": "darkness-ablaze",
    "rebel clash": "rebel-clash",
    "sword & shield base": "sword-shield-base",
    # ── Sun & Moon (2017–2019) ──
    "cosmic eclipse": "cosmic-eclipse",
    "hidden fates": "hidden-fates",
    "unified minds": "unified-minds",
    "unbroken bonds": "unbroken-bonds",
    "team up": "team-up",
    "lost thunder": "lost-thunder",
    "dragon majesty": "dragon-majesty",
    "celestial storm": "celestial-storm",
    "forbidden light": "forbidden-light",
    "ultra prism": "ultra-prism",
    "crimson invasion": "crimson-invasion",
    "shining legends": "shining-legends",
    "burning shadows": "burning-shadows",
    "guardians rising": "guardians-rising",
    "sun & moon base": "sun-moon-base",
}

# ─── HTTP ─────────────────────────────────────────────────────────────

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
    "Cache-Control": "max-age=0",
}


def make_session() -> requests.Session:
    """Create a requests Session pre-loaded with browser-like headers."""
    session = requests.Session()
    session.headers.update({"User-Agent": REQUEST_HEADERS["User-Agent"]})
    return session


# ─── Playwright JS Snippets ───────────────────────────────────────────

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


def make_playwright_context(p, headed: bool = False, profile_dir: Optional[str] = None):
    """
    Create a persistent Playwright browser context.

    Uses a stored browser profile so cookies and fingerprints survive
    between runs. On first run use headed=True to pass any bot challenges.

    Returns a context object (not a page). Caller must open a page and
    close the context when done.
    """
    if profile_dir is None:
        profile_dir = os.path.abspath(BROWSER_PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)

    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=not headed,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
        user_agent=REQUEST_HEADERS["User-Agent"],
        locale="en-AU",
        viewport={"width": 1280, "height": 900},
    )


# ─── Utility Functions ────────────────────────────────────────────────

def infer_set(name: str) -> Optional[str]:
    """Try to infer the Pokémon set slug from a product name."""
    name_lower = name.lower()
    for set_name, set_key in POKEMON_SETS.items():
        if set_name in name_lower:
            return set_key
    return None


def parse_price(price_str: str) -> Optional[float]:
    """Parse '$59.00' or '59.00' → 59.0. Returns None if unparseable."""
    if not price_str:
        return None
    match = re.search(r"\$?([\d,]+\.?\d*)", str(price_str))
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def apply_filters(name: str, url: str, domain: str, url_path_fragment: str,
                  tcg: str) -> bool:
    """
    Return True if the product passes all filters and should be kept.

    Checks:
      - URL is on the expected domain and path
      - Name contains TCG keywords
      - Name is not in the blocklist
      - Name is in the allowlist
    """
    if not name or not url:
        return False
    if domain not in url or url_path_fragment not in url:
        return False

    name_lower = name.lower()

    keywords = TCG_NAME_KEYWORDS.get(tcg, [tcg.lower()])
    if not any(kw in name_lower for kw in keywords):
        return False

    for blocked in PRODUCT_BLOCKLIST:
        if blocked in name_lower:
            logger.debug(f"  Blocked '{blocked}': {name}")
            return False

    if not any(allowed in name_lower for allowed in PRODUCT_ALLOWLIST):
        logger.debug(f"  Not in allowlist: {name}")
        return False

    return True


# ─── Database Helpers ─────────────────────────────────────────────────

def save_new_products(products: list[dict], db) -> tuple[int, int]:
    """
    Save newly discovered products to the database.

    Skips products already tracked (by URL). Runs canonical matching
    if available.

    Returns:
        (added, skipped) counts
    """
    try:
        from canonical.matcher import match_product as _match_product
    except ImportError:
        _match_product = None

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
            in_stock=product.get("in_stock", False),
            price=product.get("price"),
            price_str=product.get("price_str"),
            image_url=product.get("image") or None,
            sku=product.get("sku") or None,
            status_changed=False,
        )

        if _match_product:
            set_key = product.get("set") if product.get("tcg") == "pokemon" else None
            match = _match_product(
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


# ─── Output Helpers ───────────────────────────────────────────────────

def log_dry_run(products: list[dict]) -> None:
    """Pretty-print discovered products for a --dry-run."""
    logger.info("── DRY RUN — Would add these products ────────")
    for p in products:
        set_label = f" [{p['set']}]" if p.get("set") else ""
        price_label = f" {p['price_str']}" if p.get("price_str") else ""
        img_label = " 🖼️" if p.get("image") else ""
        preorder_label = " ⏳PREORDER" if p.get("is_preorder") else ""
        logger.info(f"  {p['name']}{set_label}{price_label}{img_label}{preorder_label}")
        logger.info(f"    {p['url']}")
    logger.info("")
