"""
Discovery Scheduler
===================
Runs all retailer discovery jobs on a weekly schedule.
Integrates cleanly with the main monitor loop.

Run alongside main.py:
    python discovery/scheduler.py

Or add to your crontab for fully automated scheduling:
    0 3 * * 1 cd /home/ubuntu/tcg-monitor && python discovery/scheduler.py --once
    (Runs every Monday at 3am)
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import all retailer discovery functions
from discovery.ebgames_discovery import discover_ebgames
from discovery.jbhifi_discovery import discover_jbhifi
from discovery.bigw_discovery import discover_bigw
from discovery.kmart_discovery import discover_kmart
from discovery.target_discovery import discover_target
from discovery.amazon_discovery import discover_amazon

logger = logging.getLogger(__name__)

# How often to run discovery (in seconds) — default: once per week
DISCOVERY_INTERVAL = 7 * 24 * 60 * 60

# Run at this hour (24h) to avoid peak times
PREFERRED_HOUR = 3  # 3am


def run_all_discovery(dry_run: bool = False) -> dict:
    """Run all discovery jobs in sequence. Returns {retailer: product_count}."""
    logger.info("=" * 60)
    logger.info(f"🔍 Running discovery jobs — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    jobs = [
        ("1/6", "EB Games AU",  discover_ebgames, "ebgames_au"),
        ("2/6", "JB Hi-Fi AU",  discover_jbhifi,  "jbhifi_au"),
        ("3/6", "Big W AU",     discover_bigw,    "bigw_au"),
        ("4/6", "Kmart AU",     discover_kmart,   "kmart_au"),
        ("5/6", "Target AU",    discover_target,  "target_au"),
        ("6/6", "Amazon AU",    discover_amazon,  "amazon_au"),
    ]

    results = {}

    for step, name, fn, key in jobs:
        logger.info(f"\n[{step}] {name} Discovery")
        try:
            products = fn(dry_run=dry_run)
            results[key] = len(products)
        except Exception as e:
            logger.error(f"{name} discovery failed: {e}", exc_info=True)
            results[key] = 0

    logger.info("\n" + "=" * 60)
    logger.info("📊 Discovery Summary:")
    total = 0
    for retailer, count in results.items():
        logger.info(f"   {retailer:<20} {count} products found")
        total += count
    logger.info(f"   {'TOTAL':<20} {total}")
    logger.info("=" * 60)

    return results


def seconds_until_next_run(preferred_hour: int = 3) -> int:
    """Calculate seconds until next preferred run time."""
    now = datetime.now()
    next_run = now.replace(hour=preferred_hour, minute=0, second=0, microsecond=0)

    # If we've passed today's preferred time, schedule for next week
    if next_run <= now:
        next_run += timedelta(weeks=1)

    return int((next_run - now).total_seconds())


def scheduler_loop(dry_run: bool = False):
    """Run discovery on a weekly schedule."""
    logger.info("🗓️  Discovery Scheduler Started")
    logger.info(f"   Interval: Weekly (every 7 days)")
    logger.info(f"   Preferred time: {PREFERRED_HOUR:02d}:00")

    # Run immediately on first start
    logger.info("   Running initial discovery now...")
    run_all_discovery(dry_run=dry_run)

    while True:
        wait_seconds = seconds_until_next_run(PREFERRED_HOUR)
        next_run_time = datetime.now() + timedelta(seconds=wait_seconds)

        logger.info(f"\n⏳ Next discovery run: {next_run_time.strftime('%Y-%m-%d %H:%M')}")
        logger.info(f"   Sleeping for {wait_seconds // 3600:.1f} hours...")

        time.sleep(wait_seconds)
        run_all_discovery(dry_run=dry_run)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="TCG Product Discovery Scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run discovery once and exit (for cron use)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print found products without saving to DB",
    )
    args = parser.parse_args()

    if args.once:
        run_all_discovery(dry_run=args.dry_run)
    else:
        scheduler_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
