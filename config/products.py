"""
Products to monitor.

Each product is a dict with:
  - url: Product page URL
  - name: Human-readable product name
  - set: TCG set name (must match a key in webhooks.SET_WEBHOOKS)
  - tcg: "pokemon" | "one-piece" | "mtg" | "dbz" | "lorcana"
  - retailer: Retailer key (must match a key in webhooks.RETAILER_WEBHOOKS)
  - image: (optional) Product image URL for embed thumbnail

Add as many products as you like. The monitor will cycle through them
at the interval configured for each retailer.
"""

PRODUCTS = [
    # ─── Amazon AU — Pokémon ─────────────────────────────────────────
    {
        "url": "https://www.amazon.com.au/dp/B0DFD1VG2F",
        "name": "Pokémon TCG: Journey Together Booster Bundle",
        "set": "journey-together",
        "tcg": "pokemon",
        "retailer": "amazon_au",
        "image": "",
    },
    {
        "url": "https://www.amazon.com.au/dp/B0DFCZ5HGZ",
        "name": "Pokémon TCG: Journey Together Elite Trainer Box",
        "set": "journey-together",
        "tcg": "pokemon",
        "retailer": "amazon_au",
        "image": "",
    },
    {
        "url": "https://www.amazon.com.au/dp/B0CPRHZ81K",
        "name": "Pokémon TCG: Prismatic Evolutions Elite Trainer Box",
        "set": "prismatic-evolutions",
        "tcg": "pokemon",
        "retailer": "amazon_au",
        "image": "",
    },

    # ─── EB Games AU — Pokémon ───────────────────────────────────────
    {
        "url": "https://www.ebgames.com.au/product/trading-cards/example-journey-together-etb",
        "name": "Pokémon TCG: Journey Together ETB",
        "set": "journey-together",
        "tcg": "pokemon",
        "retailer": "ebgames_au",
        "image": "",
    },

    # ─── JB Hi-Fi AU — Pokémon ───────────────────────────────────────
    {
        "url": "https://www.jbhifi.com.au/products/pokemon-tcg-example",
        "name": "Pokémon TCG: Journey Together Booster Box",
        "set": "journey-together",
        "tcg": "pokemon",
        "retailer": "jbhifi_au",
        "image": "",
    },

    # ─── Big W AU — Pokémon ──────────────────────────────────────────
    {
        "url": "https://www.bigw.com.au/product/pokemon-tcg-example/p/example123",
        "name": "Pokémon TCG: Prismatic Evolutions Booster Bundle",
        "set": "prismatic-evolutions",
        "tcg": "pokemon",
        "retailer": "bigw_au",
        "image": "",
    },

    # ─── Kmart AU — Pokémon ──────────────────────────────────────────
    {
        "url": "https://www.kmart.com.au/product/pokemon-tcg-example/",
        "name": "Pokémon TCG: Journey Together Collection Box",
        "set": "journey-together",
        "tcg": "pokemon",
        "retailer": "kmart_au",
        "image": "",
    },

    # ─── Amazon AU — One Piece ───────────────────────────────────────
    # Uncomment and add real URLs when expanding
    # {
    #     "url": "https://www.amazon.com.au/dp/BXXXXXXXXXX",
    #     "name": "One Piece TCG: OP-09 Booster Box",
    #     "set": "one-piece",
    #     "tcg": "one-piece",
    #     "retailer": "amazon_au",
    #     "image": "",
    # },
]


def get_products_by_retailer(retailer: str) -> list[dict]:
    """Get all products for a specific retailer."""
    return [p for p in PRODUCTS if p["retailer"] == retailer]


def get_products_by_set(set_name: str) -> list[dict]:
    """Get all products for a specific TCG set."""
    return [p for p in PRODUCTS if p["set"] == set_name]


def get_products_by_tcg(tcg: str) -> list[dict]:
    """Get all products for a specific TCG game."""
    return [p for p in PRODUCTS if p["tcg"] == tcg]
