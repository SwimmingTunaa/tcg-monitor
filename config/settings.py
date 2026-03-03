"""
Global configuration for the TCG Stock Monitor.
"""
import os

# ─── Database ────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "monitor.db")

# ─── Polling intervals (seconds) per retailer ────────────────────────
# Be respectful — don't hammer retailers. 60s minimum recommended.
POLL_INTERVALS = {
    "amazon_au": 60,
    "ebgames_au": 90,
    "jbhifi_au": 90,
    "bigw_au": 120,
    "kmart_au": 120,
    "target_au": 120,
}

# Default interval if retailer not specified above
DEFAULT_POLL_INTERVAL = 90

# ─── Request settings ────────────────────────────────────────────────
REQUEST_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

# ─── Alert settings ─────────────────────────────────────────────────
# Don't re-alert for the same product within this window (seconds)
ALERT_COOLDOWN = 1800  # 30 minutes

# Alert on price drops greater than this percentage
PRICE_DROP_THRESHOLD = 5.0  # percent

# ─── Test mode ────────────────────────────────────────────────────
# When True, all alerts route to TEST_WEBHOOK instead of real channels
TEST_MODE = False

# ─── Logging ─────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
