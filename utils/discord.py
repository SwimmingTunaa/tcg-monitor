"""
Discord webhook helpers for sending rich stock alerts.

Sends alerts to:
  1. The retailer-specific channel (e.g. #amazon-au)
  2. The set-specific channel (e.g. #journey-together)
  3. The price-drop channel (if applicable)
"""
import requests
import logging
from datetime import datetime
from typing import Optional

from config.webhooks import (
    RETAILER_WEBHOOKS, SET_WEBHOOKS, OTHER_TCG_WEBHOOKS,
    PRICE_DROP_WEBHOOK, STATUS_WEBHOOK,
)
from utils.helpers import (
    ProductStatus, StockChange,
    RETAILER_NAMES, RETAILER_COLORS, SET_DISPLAY_NAMES,
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


def build_restock_embed(change: StockChange) -> dict:
    """Build a rich Discord embed for a restock alert."""
    product = change.product
    status = change.new_status
    retailer = product["retailer"]
    retailer_name = RETAILER_NAMES.get(retailer, retailer)
    color = RETAILER_COLORS.get(retailer, 0x00FF00)

    # Title based on change type
    if change.change_type == "restock":
        title = "🟢 BACK IN STOCK"
    elif change.change_type == "preorder":
        title = "🔵 PRE-ORDER AVAILABLE"
    elif change.change_type == "price_drop":
        title = "💰 PRICE DROP"
    elif change.change_type == "new_listing":
        title = "🆕 NEW LISTING"
    else:
        title = "📦 STOCK UPDATE"

    # Build description
    desc_parts = []
    if status.price_str:
        desc_parts.append(f"**Price:** {status.price_str}")
    if status.stock_text:
        desc_parts.append(f"**Status:** {status.stock_text}")

    # Add price context if we have history
    if change.change_type == "price_drop" and change.old_status and change.old_status.price:
        old_price = change.old_status.price
        new_price = status.price
        if old_price and new_price:
            savings = old_price - new_price
            pct = (savings / old_price) * 100
            desc_parts.append(f"**Was:** ${old_price:.2f} → **Now:** ${new_price:.2f} ({pct:.0f}% off)")

    desc_parts.append(f"\n**[🛒 BUY NOW]({status.url})**")

    embed = {
        "title": f"{title} — {retailer_name}",
        "description": "\n".join(desc_parts),
        "color": color,
        "fields": [
            {
                "name": "Product",
                "value": status.name,
                "inline": True,
            },
            {
                "name": "Set",
                "value": SET_DISPLAY_NAMES.get(product.get("set", ""), product.get("set", "N/A")),
                "inline": True,
            },
            {
                "name": "Retailer",
                "value": retailer_name,
                "inline": True,
            },
        ],
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {
            "text": "TCG Stock Monitor • Alerts are not guaranteed — verify stock before purchasing",
        },
    }

    # Add thumbnail if we have a product image
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
