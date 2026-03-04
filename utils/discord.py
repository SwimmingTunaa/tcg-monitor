"""
Discord webhook helpers for sending rich stock alerts.

Sends alerts to:
  1. The retailer-specific channel (e.g. #amazon-au)
  2. The set-specific channel (e.g. #journey-together)
  3. The price-drop channel (if applicable)
"""
import requests
import logging
import re
from datetime import datetime
from typing import Optional

from config.webhooks import (
    RETAILER_WEBHOOKS, SET_WEBHOOKS, OTHER_TCG_WEBHOOKS,
    PRICE_DROP_WEBHOOK, STATUS_WEBHOOK, TEST_WEBHOOK,
)
from config.settings import TEST_MODE
from utils.helpers import (
    ProductStatus, StockChange,
    RETAILER_NAMES, SET_DISPLAY_NAMES, availability_scope_label,
)

logger = logging.getLogger(__name__)


def send_webhook(webhook_url: str, payload: dict) -> bool:
    """Send a payload to a Discord webhook. Returns True if successful."""
    if "YOUR_WEBHOOK_HERE" in webhook_url:
        logger.debug(f"Skipping unconfigured webhook: {webhook_url[:50]}...")
        return False

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 204:
            return True
        elif resp.status_code == 429:
            # Rate limited — log and skip
            retry_after = resp.json().get("retry_after", 1)
            logger.warning(f"Discord rate limited. Retry after {retry_after}s")
            return False
        else:
            logger.error(f"Discord webhook error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")
        return False


# ── Embed colors per change type ───────────────────────────────────────────
_CHANGE_COLORS = {
    "restock":      0x57F287,  # green
    "preorder":     0x5865F2,  # blurple
    "new_listing":  0xFEE75C,  # yellow
    "price_drop":   0xEB459E,  # pink
    "out_of_stock": 0xED4245,  # red
}

# ── Retailer favicon URLs ────────────────────────────────────────────
_RETAILER_ICONS = {
    "ebgames_au": "https://www.ebgames.com.au/favicon.ico",
    "amazon_au":  "https://www.amazon.com.au/favicon.ico",
    "jbhifi_au":  "https://www.jbhifi.com.au/favicon.ico",
    "bigw_au":    "https://www.bigw.com.au/favicon.ico",
    "kmart_au":   "https://www.kmart.com.au/favicon.ico",
    "target_au":  "https://www.target.com.au/favicon.ico",
}


def build_restock_embed(change: StockChange) -> dict:
    """Build a Discord embed for a stock alert."""
    product = change.product
    status = change.new_status
    retailer = product["retailer"]
    retailer_name = RETAILER_NAMES.get(retailer, retailer)
    color = _CHANGE_COLORS.get(change.change_type, 0x57F287)

    # ── Type label & stock indicator ────────────────────────────────
    type_map = {
        "restock":      ("Restock",      "🟢 In Stock"),
        "preorder":     ("Pre-order",    "🔵 Pre-order Open"),
        "new_listing":  ("New Listing",  "🟢 In Stock"),
        "price_drop":   ("Price Drop",   "🟢 In Stock"),
        "out_of_stock": ("Out of Stock", "🔴 Out of Stock"),
    }
    type_label, stock_indicator = type_map.get(
        change.change_type, ("Stock Update", "🟡 Unknown")
    )

    # ── Price ───────────────────────────────────────────────────────
    if change.change_type == "price_drop" and change.old_status and change.old_status.price and status.price:
        old = change.old_status.price
        new = status.price
        pct = ((old - new) / old) * 100
        price_display = f"~~${old:.2f}~~ → **${new:.2f} AUD** ({pct:.0f}% off)"
    else:
        price_display = f"${status.price:.2f} AUD" if status.price else "N/A"

    # ── Set ──────────────────────────────────────────────────────────
    set_key = product.get("set", "")
    set_display = SET_DISPLAY_NAMES.get(
        set_key,
        "General" if set_key in ("general", "pokemon", "", None)
        else set_key.replace("-", " ").title()
    )

    # ── Pre-order release date (parsed from stock_text "Pre-order — Fri, 27 Mar 2026") ─
    preorder_date = None
    if status.stock_text and "—" in status.stock_text:
        preorder_date = status.stock_text.split("—", 1)[1].strip()
        preorder_date = re.sub(
            r"\s+\((?:Online only|In-store only|Online \+ In-store|Unknown channel)\)\s*$",
            "",
            preorder_date,
            flags=re.I,
        )

    # ── Product title (with [TEST] prefix if needed) ──────────────────────
    product_title = f"[TEST] {status.name}" if TEST_MODE else status.name

    # ── Fields ─────────────────────────────────────────────────────────
    fields = [
        {"name": "Price",    "value": price_display,   "inline": True},
        {"name": "Type",     "value": type_label,       "inline": True},
        {"name": "Set",      "value": set_display,      "inline": True},
        {"name": "Stock",    "value": stock_indicator,  "inline": True},
        {"name": "Channel",  "value": availability_scope_label(status.availability_scope), "inline": True},
        {"name": "Retailer", "value": retailer_name,    "inline": True},
    ]
    if preorder_date:
        fields.append({"name": "Release Date", "value": preorder_date, "inline": True})

    # ── Assemble ──────────────────────────────────────────────────────
    embed = {
        "author": {
            "name": retailer_name,
            "icon_url": _RETAILER_ICONS.get(retailer, ""),
        },
        "title": product_title,
        "url": status.url,
        "color": color,
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {
            "text": "TCG Monitor AU • Verify stock before purchasing",
        },
    }

    image_url = status.image_url or product.get("image", "")
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return embed


def send_stock_alert(change: StockChange, db=None):
    """
    Send a stock change alert to all relevant Discord channels.

    Sends to:
      1. Retailer channel (e.g. #amazon-au)
      2. Set channel (e.g. #journey-together) — for Pokémon
      3. TCG channel (e.g. #one-piece) — for non-Pokémon
      4. Price drop channel — if it's a price drop
    """
    if not change.is_alertable:
        return

    product = change.product
    retailer = product["retailer"]
    set_name = product.get("set", "")
    tcg = product.get("tcg", "pokemon")

    # Check cooldown
    if db and not db.can_alert(product["url"], change.change_type):
        logger.info(f"Alert cooldown active for {product['name']}, skipping")
        return

    embed = build_restock_embed(change)
    payload = {"embeds": [embed]}
    sent_any = False

    # In test mode, send only to the test channel
    if TEST_MODE:
        if send_webhook(TEST_WEBHOOK, payload):
            logger.info(f"[TEST] Alert sent to test channel for {change.change_type} — {change.product['name']}")
            if db:
                db.log_alert(product["url"], change.change_type)
        return

    # 1. Retailer channel
    if retailer in RETAILER_WEBHOOKS:
        if send_webhook(RETAILER_WEBHOOKS[retailer], payload):
            sent_any = True
            logger.info(f"Alert sent to retailer channel: {retailer}")

    # 2. Set channel (Pokémon sets)
    if set_name in SET_WEBHOOKS:
        if send_webhook(SET_WEBHOOKS[set_name], payload):
            sent_any = True
            logger.info(f"Alert sent to set channel: {set_name}")

    # 3. Other TCG channel
    if tcg != "pokemon" and tcg in OTHER_TCG_WEBHOOKS:
        if send_webhook(OTHER_TCG_WEBHOOKS[tcg], payload):
            sent_any = True
            logger.info(f"Alert sent to TCG channel: {tcg}")

    # 4. Price drop channel
    if change.change_type == "price_drop":
        if send_webhook(PRICE_DROP_WEBHOOK, payload):
            sent_any = True
            logger.info("Alert sent to price drop channel")

    # Log the alert for cooldown tracking
    if sent_any and db:
        db.log_alert(product["url"], change.change_type)


def send_status_message(message: str, color: int = 0x808080):
    """Send a status/health message to the status channel."""
    payload = {
        "embeds": [{
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "TCG Monitor Status"},
        }]
    }
    send_webhook(STATUS_WEBHOOK, payload)
