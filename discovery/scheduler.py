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
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import discovery modules (add more retailers here as you build them)
from discovery.ebgames_discovery import discover_ebgames

logger = logging.getLogger(__name__)

# How often to run discovery (in seconds)
# Default: once per week
DISCOVERY_INTERVAL = 7 * 24 * 60 * 60  # 1 week

# Run at this hour (24h) to avoid peak times
PREFERRED_HOUR = 3  # 3am


def run_all_discovery(dry_run: bool = False):
    """Run all discovery jobs."""
    logger.info("=" * 50)
    logger.info(f"🔍 Running discovery jobs — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 50)

    results = {}

    # ── EB Games ───────────────────────────────────────────
    try:
        logger.info("\n[1/1] EB Games AU Discovery")
        products = discover_ebgames(dry_run=dry_run)
        results["ebgames_au"] = len(products)
    except Exception as e:
        logger.error(f"EB Games discovery failed: {e}", exc_info=True)
        results["ebgames_au"] = 0

    # ── Add more retailers here as you build them ──────────
    # try:
    #     logger.info("\n[2/N] JB Hi-Fi AU Discovery")
    #     products = discover_jbhifi(dry_run=dry_run)
    #     results["jbhifi_au"] = len(products)
    # except Exception as e:
    #     logger.error(f"JB Hi-Fi discovery failed: {e}")

    logger.info("\n" + "=" * 50)
    logger.info("📊 Discovery Summary:")
    for retailer, count in results.items():
        logger.info(f"   {retailer}: {count} products found")
    logger.info("=" * 50)

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
        help="Print found products without saving",
    )
    args = parser.parse_args()

    if args.once:
        run_all_discovery(dry_run=args.dry_run)
    else:
        scheduler_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
