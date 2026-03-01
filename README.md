# 🎴 TCG Stock Monitor — Australian Restock Alerts for Discord

A self-hosted stock monitoring system for Australian TCG retailers.
Monitors product pages for stock changes and sends real-time alerts to Discord.

## Features

- **Multi-retailer monitoring** — Amazon AU, EB Games, JB Hi-Fi, Big W, Kmart, Target AU
- **Rich Discord embeds** — product name, price, stock status, direct buy link, image
- **Price tracking** — alerts on price drops, tracks price history in SQLite
- **Configurable polling** — per retailer intervals to respect rate limits
- **Extensible** — easy to add new retailers or TCG types (One Piece, MTG, etc.)
- **Deduplication** — won't spam the same alert twice

## Quick Start

```bash
pip install -r requirements.txt
# Edit config/webhooks.py with your Discord webhook URLs
# Edit config/products.py with products to track
python main.py
```

## Architecture

```
tcg-monitor/
├── main.py                  # Entry point
├── config/
│   ├── settings.py          # Global config
│   ├── products.py          # Products to monitor
│   └── webhooks.py          # Discord webhook URLs
├── monitors/
│   ├── base_monitor.py      # Abstract base monitor
│   ├── amazon_au.py         # Amazon AU
│   ├── ebgames_au.py        # EB Games AU
│   ├── jbhifi_au.py         # JB Hi-Fi AU
│   ├── bigw_au.py           # Big W AU
│   ├── kmart_au.py          # Kmart AU
│   └── target_au.py         # Target AU
├── utils/
│   ├── discord.py           # Webhook helpers
│   ├── database.py          # SQLite layer
│   └── helpers.py           # Shared utilities
└── data/
    └── monitor.db           # Auto-created SQLite DB
```

## Adding a New Retailer

1. Create `monitors/newretailer.py` subclassing `BaseMonitor`
2. Implement `scrape_product(url)` returning a `ProductStatus`
3. Add products to `config/products.py`
4. Register in `main.py`
