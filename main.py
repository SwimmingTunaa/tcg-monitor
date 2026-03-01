#!/usr/bin/env python3
"""
TCG Stock Monitor — Main Entry Point

Runs all retailer monitors in parallel threads, each on their own
polling interval. Monitors check product pages for stock changes
and send alerts to Discord via webhooks.

Usage:
    python main.py              # Run all monitors
    python main.py --test       # Test mode: single check cycle, no loop
    python main.py --retailers amazon_au jbhifi_au   # Run specific retailers only
"""
import argparse
import logging
import signal
import sys
import time
import threading
from datetime import datetime

from config.settings import LOG_LEVEL, LOG_FORMAT
from config.products import get_products_by_retailer, PRODUCTS
from utils.database import Database
from utils.discord import send_status_message
from utils.helpers import jitter

# Import all monitors
from monitors.amazon_au import AmazonAUMonitor
from monitors.ebgames_au import EBGamesAUMonitor
from monitors.jbhifi_au import JBHiFiAUMonitor
from monitors.bigw_au import BigWAUMonitor
from monitors.kmart_au import KmartAUMonitor
from monitors.target_au import TargetAUMonitor

# ─── Monitor Registry ────────────────────────────────────────────────
MONITOR_CLASSES = {
    "amazon_au": AmazonAUMonitor,
    "ebgames_au": EBGamesAUMonitor,
    "jbhifi_au": JBHiFiAUMonitor,
    "bigw_au": BigWAUMonitor,
    "kmart_au": KmartAUMonitor,
    "target_au": TargetAUMonitor,
}

# ─── Logging Setup ───────────────────────────────────────────────────
logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("tcg-monitor")

# ─── Shutdown Flag ───────────────────────────────────────────────────
shutdown_event = threading.Event()


def signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    logger.info("Shutdown signal received. Stopping monitors...")
    shutdown_event.set()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def monitor_loop(monitor, products: list[dict]):
    """
    Continuous monitoring loop for a single retailer.
    Runs in its own thread.
    """
    retailer = monitor.retailer_key
    interval = monitor.poll_interval
    logger.info(f"[{retailer}] Starting monitor loop ({len(products)} products, {interval}s interval)")

    while not shutdown_event.is_set():
        try:
            cycle_start = time.time()
            monitor.run_cycle(products)
            elapsed = time.time() - cycle_start

            logger.info(f"[{retailer}] Cycle complete ({elapsed:.1f}s). "
                        f"Next check in {interval}s")

            # Wait with jitter, checking shutdown flag periodically
            wait_time = jitter(interval)
            wait_until = time.time() + wait_time
            while time.time() < wait_until and not shutdown_event.is_set():
                time.sleep(1)

        except Exception as e:
            logger.error(f"[{retailer}] Unexpected error in monitor loop: {e}", exc_info=True)
            # Wait before retrying
            for _ in range(30):
                if shutdown_event.is_set():
                    break
                time.sleep(1)

    logger.info(f"[{retailer}] Monitor loop stopped")


def run_test_mode(db: Database, retailers: list[str] = None):
    """Run a single check cycle for testing (no loop)."""
    logger.info("=== TEST MODE — Single check cycle ===")

    for retailer_key, monitor_class in MONITOR_CLASSES.items():
        if retailers and retailer_key not in retailers:
            continue

        products = get_products_by_retailer(retailer_key)
        if not products:
            logger.info(f"[{retailer_key}] No products configured, skipping")
            continue

        monitor = monitor_class(db)
        logger.info(f"[{retailer_key}] Checking {len(products)} products...")

        for product in products:
            status = monitor.scrape_product(product["url"])
            if status:
                logger.info(f"  {status}")
            else:
                logger.warning(f"  Failed to scrape: {product['name']}")

    logger.info("=== Test cycle complete ===")


def main():
    parser = argparse.ArgumentParser(description="TCG Stock Monitor")
    parser.add_argument("--test", action="store_true", help="Run single check cycle (no loop)")
    parser.add_argument("--retailers", nargs="+", help="Only run specific retailers")
    args = parser.parse_args()

    # Initialize database
    db = Database()

    # Clean up old data on startup
    db.cleanup_old_data(days=90)

    if args.test:
        run_test_mode(db, args.retailers)
        return

    # ─── Start monitor threads ───────────────────────────────────────
    threads = []
    active_retailers = []

    for retailer_key, monitor_class in MONITOR_CLASSES.items():
        if args.retailers and retailer_key not in args.retailers:
            continue

        products = get_products_by_retailer(retailer_key)
        if not products:
            logger.info(f"[{retailer_key}] No products configured, skipping")
            continue

        monitor = monitor_class(db)
        thread = threading.Thread(
            target=monitor_loop,
            args=(monitor, products),
            name=f"monitor-{retailer_key}",
            daemon=True,
        )
        threads.append(thread)
        active_retailers.append(retailer_key)

    if not threads:
        logger.error("No monitors to start! Add products to config/products.py")
        sys.exit(1)

    # Count total products
    total_products = sum(
        len(get_products_by_retailer(r)) for r in active_retailers
    )

    logger.info("=" * 60)
    logger.info("🎴 TCG Stock Monitor Starting")
    logger.info(f"   Retailers: {', '.join(active_retailers)}")
    logger.info(f"   Products:  {total_products}")
    logger.info(f"   Started:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Send startup notification to Discord
    send_status_message(
        f"🟢 **TCG Monitor Started**\n"
        f"Retailers: {', '.join(active_retailers)}\n"
        f"Products: {total_products}\n"
        f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color=0x00FF00,
    )

    # Start all threads
    for thread in threads:
        thread.start()

    # Wait for shutdown signal
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    logger.info("Waiting for monitor threads to stop...")
    shutdown_event.set()

    for thread in threads:
        thread.join(timeout=10)

    send_status_message("🔴 **TCG Monitor Stopped**", color=0xFF0000)
    logger.info("All monitors stopped. Goodbye! 👋")


if __name__ == "__main__":
    main()
