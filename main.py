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
    python main.py --test --url https://www.amazon.com.au/dp/B0XXXXXXX
"""
import argparse
import logging
import re
import signal
import sys
import time
import threading
from datetime import datetime

from config.settings import LOG_LEVEL, LOG_FORMAT
from config.products import get_products_by_retailer, PRODUCTS
from utils.database import Database
from utils.discord import send_status_message, send_stock_alert
from utils.helpers import jitter, StockChange

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


def _has_preorder_hint(text: str) -> bool:
    return bool(re.search(r"\bpre[\s-]?order\b", text, re.I))


def _is_monitorable_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    value = url.strip()
    if not value:
        return False
    low = value.lower()
    # Skip known placeholder URLs that only slow down checks.
    if "pokemon-tcg-example" in low or "example.com" in low:
        return False
    return True


def _infer_retailer_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    low = url.lower()
    domain_map = [
        ("amazon.com.au", "amazon_au"),
        ("ebgames.com.au", "ebgames_au"),
        ("jbhifi.com.au", "jbhifi_au"),
        ("bigw.com.au", "bigw_au"),
        ("kmart.com.au", "kmart_au"),
        ("target.com.au", "target_au"),
    ]
    for domain, retailer_key in domain_map:
        if domain in low:
            return retailer_key
    return None


def _build_manual_test_product(url: str, retailer_key: str) -> dict:
    """Build a minimal product dict for direct URL test mode."""
    return {
        "url": url,
        "retailer": retailer_key,
        # Intentionally omit a fixed name so scraper-derived product name is preserved.
        "set": "general",
        "tcg": "pokemon",
        "image": "",
    }


def _infer_forced_change_type(db: Database, product: dict, status) -> str | None:
    """Infer force-alert type with preorder hints from status/product/last DB row."""
    if status.is_preorder:
        return "preorder"

    hints = []
    if isinstance(status.stock_text, str):
        hints.append(status.stock_text)
    name = product.get("name")
    if isinstance(name, str):
        hints.append(name)

    # For blocked fallback statuses, inspect last known stock_text as a hint.
    if isinstance(status.stock_text, str) and "blocked" in status.stock_text.lower():
        last = db.get_last_status(status.url)
        if last and isinstance(last.get("stock_text"), str):
            hints.append(last["stock_text"])

    if any(_has_preorder_hint(h) for h in hints):
        return "preorder"
    if status.in_stock:
        return "restock"
    return None


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


def run_test_mode(
    db: Database,
    retailers: list[str] = None,
    force_alert_test: bool = False,
    force_alert_limit: int = 1,
    test_url: str | None = None,
):
    """Run a single check cycle for testing (no loop).
    Uses check_product so change detection and Discord alerts fire normally,
    but all alerts are routed to the test channel via TEST_MODE.
    """
    logger.info("=== TEST MODE — Single check cycle ===")

    for retailer_key, monitor_class in MONITOR_CLASSES.items():
        if retailers and retailer_key not in retailers:
            continue

        products = get_products_by_retailer(retailer_key)
        products = [p for p in products if _is_monitorable_url(p.get("url", ""))]

        if test_url:
            match = [
                p for p in products
                if (p.get("url") or "").strip().rstrip("/") == test_url.rstrip("/")
            ]
            if match:
                products = match
                logger.info(f"[{retailer_key}] URL test mode: using configured product for {test_url}")
            else:
                products = [_build_manual_test_product(test_url, retailer_key)]
                logger.info(f"[{retailer_key}] URL test mode: using ad-hoc product for {test_url}")

        if not products:
            logger.info(f"[{retailer_key}] No products configured, skipping")
            continue

        monitor = monitor_class(db)
        logger.info(f"[{retailer_key}] Checking {len(products)} products...")
        if not force_alert_test:
            monitor.run_cycle(products)
            continue

        logger.info(
            f"[{retailer_key}] FORCE ALERT TEST enabled "
            f"(max alerts: {force_alert_limit if force_alert_limit > 0 else 'unlimited'})"
        )
        sent = 0
        for idx, product in enumerate(products, 1):
            name = product.get("name", "Unknown Product")
            url = (product.get("url") or "").strip()
            if not url:
                logger.debug(f"[{retailer_key}] Skipping missing URL: {name}")
                continue

            logger.info(f"[{retailer_key}] Force alert progress {idx}/{len(products)}: {name}")
            status = monitor.scrape_product(url)
            if status is None:
                logger.warning(f"Failed to scrape: {name} ({url})")
                continue
            status = monitor.prepare_status(product, status)

            # Force-send only meaningful stock alerts.
            change_type = _infer_forced_change_type(db, product, status)
            if change_type is None:
                logger.debug(f"[{retailer_key}] Skipping forced alert for out-of-stock item: {name}")
                continue

            change = StockChange(
                product=product,
                old_status=None,
                new_status=status,
                change_type=change_type,
            )
            # db=None bypasses cooldown checks so this can always test webhooks.
            send_stock_alert(change, db=None)
            sent += 1
            if force_alert_limit > 0 and sent >= force_alert_limit:
                logger.info(f"[{retailer_key}] Reached forced alert limit ({force_alert_limit})")
                break

    logger.info("=== Test cycle complete ===")


def main():
    parser = argparse.ArgumentParser(description="TCG Stock Monitor")
    parser.add_argument("--test", action="store_true", help="Run single check cycle (no loop)")
    parser.add_argument("--retailers", nargs="+", help="Only run specific retailers")
    parser.add_argument(
        "--url",
        help="In --test mode, run only this product URL (retailer auto-detected if --retailers omitted)",
    )
    parser.add_argument(
        "--force-alert-test",
        action="store_true",
        help="In --test mode, send alerts even when no status change is detected",
    )
    parser.add_argument(
        "--force-alert-limit",
        type=int,
        default=1,
        help="Max forced alerts per retailer in --force-alert-test mode (<=0 means unlimited)",
    )
    args = parser.parse_args()

    if args.force_alert_test and not args.test:
        parser.error("--force-alert-test requires --test")
    if args.url and not args.test:
        parser.error("--url requires --test")

    if args.url:
        url = args.url.strip()
        if not _is_monitorable_url(url):
            parser.error("--url is empty or not monitorable")
        args.url = url

        inferred_retailer = _infer_retailer_from_url(url)
        if not args.retailers:
            if not inferred_retailer:
                parser.error("--url retailer could not be inferred; pass --retailers explicitly")
            args.retailers = [inferred_retailer]
        elif inferred_retailer and inferred_retailer not in args.retailers:
            parser.error(
                f"--url appears to be {inferred_retailer}, but --retailers is {args.retailers}"
            )

    # Initialize database
    db = Database()

    # Clean up old data on startup
    db.cleanup_old_data(days=90)

    if args.test:
        import config.settings as _settings
        _settings.TEST_MODE = True
        import utils.discord as _discord
        _discord.TEST_MODE = True
        logger.info("🧪 TEST MODE — alerts will fire to the test Discord channel")
        run_test_mode(
            db,
            args.retailers,
            force_alert_test=args.force_alert_test,
            force_alert_limit=args.force_alert_limit,
            test_url=args.url,
        )
        return

    # ─── Start monitor threads ───────────────────────────────────────
    threads = []
    active_retailers = []

    for retailer_key, monitor_class in MONITOR_CLASSES.items():
        if args.retailers and retailer_key not in args.retailers:
            continue

        products = get_products_by_retailer(retailer_key)
        products = [p for p in products if _is_monitorable_url(p.get("url", ""))]
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
        len([p for p in get_products_by_retailer(r) if _is_monitorable_url(p.get("url", ""))])
        for r in active_retailers
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
