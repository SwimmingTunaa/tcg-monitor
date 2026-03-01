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
    "amazon_au": "https://discord.com/api/webhooks/1477637680450769067/JZhSZYVMr4rpaGgCsZGClb1cr9B_-MlRrte-k9-Db8RScrpjYvkWBY2euDhbIk76Lzpn",
    "ebgames_au": "https://discord.com/api/webhooks/1477634852202348787/lLWL8FW88wqtse7w4-gUkZfS5KMpI2x7I08hn9ablFGH5D25wVkduzG1SxpcOJR2jybH",
    "jbhifi_au": "hhttps://discord.com/api/webhooks/1477637926031593564/KJU3LkBhZ0qTDJgNFbdxKtlY78ZsKUIItFtM-FaXTbbg0dmBx5aGDIAJQAwIBVJCy2-A",
    "bigw_au": "https://discord.com/api/webhooks/1477638209159565423/stJkSYWzdcj9ungboyx_OgvUvhcZqHcmMns_WC9_v4TmAlCEpnI__So7lVjnQouzxeJr",
    "kmart_au": "https://discord.com/api/webhooks/1477637245702901832/SvoC1yxoIu-DSqzB2LERQg48ozu6niFYNZcfBSvVkpYwNk_xRRx2bsYHABmGDtuMYA52",
    "target_au": "https://discord.com/api/webhooks/1477637518253097082/44CQ29sIPm4ozqBeajd7jFjzwBvW171XxzbEu2LMsKsIWSMeNEdrz9dlGcBSQ93c6Fxf",
    "myer_au": "https://discord.com/api/webhooks/1477638219997774020/LKsm9mkQXd8WQuqZPVeDcg9W4fQSh6OqJOPSB0LMyOTLlPMmMa3oE7vjWmGkEa5AsBr6",
    "toymate_au": "https://discord.com/api/webhooks/1477639125128577197/v_GNnUnepsV2zEsoBTr3SIR14sESF6c7HVWVHxezYJrXuCQUfC-ynSi2sTUZ3TQ8QW9x",
    "costco_au": "https://discord.com/api/webhooks/1477639111878643824/lm5LsGstZFarP-FosNbN6Uv_M0CnTRYSFsoYdYAxxzTWxLO0deH9Jj8Or41wbezzIwg0",
    "pokemon_centre_au": "https://discord.com/api/webhooks/1477639126139408516/BCoNcTSkMoFuG0abdJMpvxtxf9hNYx1rhyWX1om58K5MVLBoWOydSs6e0H8fDAICoJrX",
    "mrtoys_au": "https://discord.com/api/webhooks/1477639126668021872/lcawXu0IzcZI2FxfDiAGtxI6XWykunBVqDT71-INrbqFnSxPr4Fh4F-NusLjO5lqyZoj",

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
