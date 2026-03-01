"""
Shared utilities for the TCG Stock Monitor.
"""
import random
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ─── User Agent Rotation ────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
]


def get_random_headers() -> dict:
    """Return randomized browser-like headers."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def jitter(base_seconds: float, variance: float = 0.3) -> float:
    """Add random jitter to a delay to avoid detection patterns."""
    return base_seconds * (1 + random.uniform(-variance, variance))


def retry_with_backoff(func, max_retries: int = 3, base_delay: float = 2.0):
    """
    Retry a function with exponential backoff.
    Returns the function result, or None if all retries fail.
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"All {max_retries} retries failed: {e}")
                return None
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)
    return None


# ─── Data Models ─────────────────────────────────────────────────────
@dataclass
class ProductStatus:
    """Represents the current state of a product on a retailer's site."""
    url: str
    name: str
    retailer: str
    in_stock: bool
    price: Optional[float] = None
    price_str: Optional[str] = None
    stock_text: Optional[str] = None  # e.g. "In Stock", "Only 3 left", "Pre-order"
    image_url: Optional[str] = None
    scraped_at: datetime = field(default_factory=datetime.now)

    @property
    def is_preorder(self) -> bool:
        if self.stock_text:
            return "pre-order" in self.stock_text.lower() or "preorder" in self.stock_text.lower()
        return False

    def __str__(self) -> str:
        status = "✅ IN STOCK" if self.in_stock else "❌ OUT OF STOCK"
        price = f" — {self.price_str}" if self.price_str else ""
        return f"[{self.retailer}] {self.name}: {status}{price}"


@dataclass
class StockChange:
    """Represents a change in stock status for alerting."""
    product: dict  # Original product config from products.py
    old_status: Optional[ProductStatus]
    new_status: ProductStatus
    change_type: str  # "restock", "out_of_stock", "price_drop", "new_listing", "preorder"

    @property
    def is_alertable(self) -> bool:
        """Should this change trigger a Discord alert?"""
        return self.change_type in ("restock", "price_drop", "new_listing", "preorder")


# ─── Retailer Display Names ─────────────────────────────────────────
RETAILER_NAMES = {
    "amazon_au": "Amazon AU",
    "ebgames_au": "EB Games AU",
    "jbhifi_au": "JB Hi-Fi AU",
    "bigw_au": "Big W AU",
    "kmart_au": "Kmart AU",
    "target_au": "Target AU",
    "myer_online": "Myer Online",
}

RETAILER_COLORS = {
    "amazon_au": 0xFF9900,      # Amazon orange
    "ebgames_au": 0xE31837,     # EB Games red
    "jbhifi_au": 0xFFD700,      # JB yellow
    "bigw_au": 0x004B87,        # Big W blue
    "kmart_au": 0xE31837,       # Kmart red
    "target_au": 0xCC0000,      # Target red
    "myer_online": 0x000000,    # Myer black
}

SET_DISPLAY_NAMES = {
    "perfect-order": "Perfect Order",
    "ascended-heroes": "Ascended Heroes",
    "phantasmal-flames": "Phantasmal Flames",
    "mega-evolutions": "Mega Evolutions",
    "journey-together": "Journey Together",
    "prismatic-evolutions": "Prismatic Evolutions",
    "surging-sparks": "Surging Sparks",
    "destined-rivals": "Destined Rivals",
    "paldean-fates": "Paldean Fates",
    "pokemon-151": "Pokémon 151",
}
