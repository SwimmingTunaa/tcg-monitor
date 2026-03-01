"""
Discord webhook URLs for each alert channel.

To create a webhook:
  1. Discord → Channel → Edit → Integrations → Webhooks
  2. New Webhook → Copy URL
  3. Paste below

You can send the same alert to multiple channels by providing a list.
For example, a "Journey Together" restock at Amazon AU can go to both
the #journey-together set channel AND the #amazon-au retailer channel.
"""

# ─── Per-retailer channels ───────────────────────────────────────────
# These fire for ANY product restock at that retailer
RETAILER_WEBHOOKS = {
    "amazon_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "ebgames_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "jbhifi_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "bigw_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "kmart_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "target_au": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
}

# ─── Per-set channels ────────────────────────────────────────────────
# These fire when a product from that set restocks at ANY retailer
SET_WEBHOOKS = {
    "perfect-order": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "ascended-heroes": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "phantasmal-flames": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "journey-together": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "prismatic-evolutions": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "surging-sparks": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "destined-rivals": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "paldean-fates": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "pokemon-151": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
}

# ─── Other TCG channels ─────────────────────────────────────────────
OTHER_TCG_WEBHOOKS = {
    "one-piece": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "mtg": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "dragon-ball-z": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
    "lorcana": "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE",
}

# ─── Special channels ───────────────────────────────────────────────
# Price drop alerts (any retailer, any set)
PRICE_DROP_WEBHOOK = "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE"

# Error/status channel for monitor health
STATUS_WEBHOOK = "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE"
