"""
TCG Stock Monitor — Minimal Test Version
=========================================
Single file. One product. One retailer. One Discord webhook.

Setup:
  1. pip install requests beautifulsoup4 lxml
  2. Set WEBHOOK_URL below (create one in Discord: channel settings > integrations > webhooks)
  3. Set PRODUCT_URL to a real product page
  4. python monitor_test.py

It checks the page every 60s, detects stock status, and sends a Discord
alert when the status changes (e.g. out of stock → in stock).
"""

import time
import re
import requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIG — edit these three things
# ============================================================

WEBHOOK_URL = "https://discord.com/api/webhooks/1477289111231533129/D2BGZpYvQY_k6l1aT-_MUOYDuydJeq_m49vxIZ2k0InefmQnMU0NlwHtwnxCZT7y72W5"  # Your Discord webhook URL
PRODUCT_URL = "https://www.ebgames.com.au/product/toys-and-collectibles/339236-pokemon-tcg-mega-evolution-perfect-order-elite-trainer-box"  # A real product page URL (Amazon AU, EB Games, JB Hi-Fi, etc.)
PRODUCT_NAME = "Pokemon - TCG - Mega Evolution Perfect Order Elite Trainer Box"  # Display name for the alert
CHECK_INTERVAL = 3  # Seconds between checks

# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}

# Keywords to detect stock status (case-insensitive)
IN_STOCK = ["add to cart", "add to bag", "buy now", "in stock"]
OUT_OF_STOCK = ["out of stock", "sold out", "currently unavailable", "unavailable"]
PRE_ORDER = ["pre-order", "pre order", "preorder"]


def detect_status(text: str) -> str:
    t = text.lower()
    for kw in PRE_ORDER:
        if kw in t:
            return "pre_order"
    for kw in IN_STOCK:
        if kw in t:
            return "in_stock"
    for kw in OUT_OF_STOCK:
        if kw in t:
            return "out_of_stock"
    return "unknown"


def extract_price(text: str) -> str | None:
    match = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
    return match.group(0) if match else None


def check_product(url: str) -> dict:
    """Fetch the page and return status + price."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(separator=" ", strip=True)

    # Only look at the first ~3000 chars (product area, not footer/nav)
    status = detect_status(text[:3000])

    # Try to find a price
    price = None
    for sel in [".price", ".a-price .a-offscreen", ".product-price", "[data-price]"]:
        el = soup.select_one(sel)
        if el:
            price = extract_price(el.get_text())
            if price:
                break

    return {"status": status, "price": price}


def send_alert(status: str, price: str | None, url: str):
    """Send a Discord webhook embed."""
    if not WEBHOOK_URL:
        print("  ⚠️  No webhook URL set — skipping Discord alert")
        return

    colours = {
        "in_stock": 0x00FF00,
        "pre_order": 0x3498DB,
        "out_of_stock": 0xFF0000,
        "unknown": 0x808080,
    }
    emojis = {
        "in_stock": "🟢 IN STOCK",
        "pre_order": "🔵 PRE-ORDER",
        "out_of_stock": "🔴 OUT OF STOCK",
        "unknown": "⚪ UNKNOWN",
    }

    embed = {
        "title": f"{emojis.get(status, status)} — {PRODUCT_NAME}",
        "url": url,
        "color": colours.get(status, 0x808080),
        "fields": [],
        "footer": {"text": "TCG Stock Monitor"},
    }

    if price:
        embed["fields"].append({"name": "Price", "value": price, "inline": True})

    embed["fields"].append({
        "name": "🔗 Link",
        "value": f"[Go to product →]({url})",
        "inline": False,
    })

    try:
        resp = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if resp.status_code == 204:
            print("  ✅ Discord alert sent!")
        else:
            print(f"  ❌ Webhook failed: {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Webhook error: {e}")


def main():
    if not PRODUCT_URL:
        print("❌ Set PRODUCT_URL in the script first!")
        return

    print(f"🎴 TCG Stock Monitor — Minimal Test")
    print(f"   Product: {PRODUCT_NAME}")
    print(f"   URL: {PRODUCT_URL}")
    print(f"   Interval: {CHECK_INTERVAL}s")
    print(f"   Webhook: {'✅ Set' if WEBHOOK_URL else '❌ Not set (console only)'}")
    print()

    last_status = None

    while True:
        result = check_product(PRODUCT_URL)
        status = result["status"]
        price = result.get("price")
        now = time.strftime("%H:%M:%S")

        print(f"[{now}] Status: {status}" + (f" | Price: {price}" if price else ""))

        if status == "error":
            print(f"  ⚠️  Error: {result.get('error')}")
        elif status != last_status and last_status is not None:
            print(f"  🔔 STATUS CHANGED: {last_status} → {status}")
            send_alert(status, price, PRODUCT_URL)
        elif last_status is None:
            print(f"  📝 Initial status recorded")

        last_status = status
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
