"""
Abstract base class for all retailer monitors.

Each retailer monitor must implement `scrape_product(url)` which returns
a ProductStatus object. The base class handles:
  - Polling loop with jitter
  - Change detection (stock status, price)
  - Delegating alerts to the Discord webhook system
  - Database updates
"""
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config.settings as settings
from config.settings import DEFAULT_POLL_INTERVAL, POLL_INTERVALS, PRICE_DROP_THRESHOLD
from utils.helpers import (
    ProductStatus, StockChange,
    get_random_headers, jitter, retry_with_backoff,
)
from utils.database import Database
from utils.discord import send_stock_alert

logger = logging.getLogger(__name__)


class BaseMonitor(ABC):
    """Base class for retailer-specific product monitors."""

    retailer_key: str = "unknown"  # Override in subclass
    retailer_name: str = "Unknown"  # Override in subclass

    def __init__(self, db: Database):
        self.db = db
        self.session = requests.Session()
        self.session.headers.update(get_random_headers())

    @property
    def poll_interval(self) -> int:
        return POLL_INTERVALS.get(self.retailer_key, DEFAULT_POLL_INTERVAL)

    def fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        """
        Fetch a URL and return parsed BeautifulSoup, or None on failure.
        Rotates headers each request.
        """
        self.session.headers.update(get_random_headers())

        def _fetch():
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")

        return retry_with_backoff(_fetch, max_retries=2)

    def fetch_page_playwright(
        self,
        url: str,
        wait_for_selector: Optional[str] = None,
        timeout: int = 30_000,
        headed: bool = False,
    ) -> Optional[str]:
        """
        Fetch a URL using Playwright with the shared persistent browser profile.
        Returns the page HTML as a string, or None on failure / missing Playwright.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
        except ImportError:
            logger.debug("Playwright not installed — skipping browser fallback")
            return None

        from discovery.base_discovery import STEALTH_JS, make_playwright_context

        try:
            with sync_playwright() as p:
                context = make_playwright_context(p, headed=headed)
                context.add_init_script(STEALTH_JS)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                    if wait_for_selector:
                        try:
                            page.wait_for_selector(wait_for_selector, timeout=timeout)
                        except PlaywrightTimeout:
                            logger.warning(
                                f"Playwright: selector '{wait_for_selector}' not found on {url}"
                            )
                    return page.content()
                finally:
                    page.close()
                    context.close()
        except Exception as e:
            logger.warning(f"Playwright fetch failed for {url}: {e}")
            return None

    @abstractmethod
    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """
        Scrape a product page and return its current status.

        Must be implemented by each retailer monitor.
        Returns None if the page couldn't be scraped.
        """
        pass

    def prepare_status(self, product: dict, status: ProductStatus) -> ProductStatus:
        """
        Hydrate static metadata from config/DB.
        Dynamic fields (stock/price/channel) remain scraper-sourced.
        """
        url = product.get("url", "")
        last = self.db.get_last_status(url) if url else None
        canonical = self.db.get_canonical_for_url(url) if url else None

        product_name = (product.get("name") or "").strip()
        canonical_name = (canonical or {}).get("name")
        last_name = (last or {}).get("name")
        if product_name:
            status.name = product_name
        elif canonical_name:
            status.name = canonical_name
        elif (not status.name) or status.name.lower().startswith("unknown"):
            if last_name:
                status.name = last_name

        if not status.image_url:
            product_image = (product.get("image") or "").strip()
            canonical_image = ((canonical or {}).get("image") or "").strip()
            last_image = ((last or {}).get("image_url") or "").strip()
            status.image_url = product_image or canonical_image or last_image or None

        return status

    def detect_change(self, product: dict, new_status: ProductStatus) -> Optional[StockChange]:
        """
        Compare new status against the last known status in the DB.
        Returns a StockChange if something alertable happened.
        """
        url = product["url"]
        last = self.db.get_last_status(url)

        if last is None:
            # First time seeing this product
            if new_status.in_stock:
                return StockChange(
                    product=product,
                    old_status=None,
                    new_status=new_status,
                    change_type="new_listing",
                )
            return None  # First seen but out of stock — nothing to alert

        was_in_stock = bool(last["in_stock"])
        is_now_in_stock = new_status.in_stock

        # Restock: was out of stock, now in stock
        if not was_in_stock and is_now_in_stock:
            old_status = ProductStatus(
                url=url,
                name=last["name"],
                retailer=last["retailer"],
                in_stock=False,
                price=last.get("price"),
                price_str=last.get("price_str"),
            )
            change_type = "preorder" if new_status.is_preorder else "restock"
            return StockChange(
                product=product,
                old_status=old_status,
                new_status=new_status,
                change_type=change_type,
            )

        # Price drop: was in stock, still in stock, price decreased
        if was_in_stock and is_now_in_stock:
            old_price = last.get("price")
            new_price = new_status.price
            if old_price and new_price and old_price > 0:
                drop_pct = ((old_price - new_price) / old_price) * 100
                if drop_pct >= PRICE_DROP_THRESHOLD:
                    old_status = ProductStatus(
                        url=url,
                        name=last["name"],
                        retailer=last["retailer"],
                        in_stock=True,
                        price=old_price,
                        price_str=last.get("price_str"),
                    )
                    return StockChange(
                        product=product,
                        old_status=old_status,
                        new_status=new_status,
                        change_type="price_drop",
                    )

        # Out of stock: was in stock, now out
        if was_in_stock and not is_now_in_stock:
            return StockChange(
                product=product,
                old_status=None,
                new_status=new_status,
                change_type="out_of_stock",
            )

        return None  # No change

    def check_product(self, product: dict):
        """Check a single product and alert if status changed."""
        raw_url = product.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        name = product.get("name", "Unknown Product")

        if not url:
            logger.debug(f"Skipping product with missing URL: {name}")
            return

        logger.debug(f"Checking: {name} @ {self.retailer_name}")

        status = self.scrape_product(url)
        if status is None:
            logger.warning(f"Failed to scrape: {name} ({url})")
            return
        status = self.prepare_status(product, status)

        # Detect changes
        change = self.detect_change(product, status)
        if change and change.is_alertable:
            logger.info(f"🔔 ALERT: {change.change_type} — {name}")
            send_stock_alert(change, db=self.db)

        # Update database
        status_changed = change is not None
        self.db.update_status(
            url=url,
            name=name,
            retailer=self.retailer_key,
            in_stock=status.in_stock,
            price=status.price,
            price_str=status.price_str,
            stock_text=status.stock_text,
            image_url=status.image_url,
            status_changed=status_changed,
        )

        # Record price if available
        if status.price:
            self.db.record_price(url, status.price)

    def run_cycle(self, products: list[dict]):
        """Run one check cycle across all products for this retailer."""
        total = len(products)
        for idx, product in enumerate(products, 1):
            if settings.TEST_MODE:
                name = product.get("name", "Unknown Product")
                logger.info(f"[{self.retailer_key}] Progress {idx}/{total}: {name}")
            try:
                self.check_product(product)
            except Exception as e:
                name = product.get("name", "Unknown Product")
                logger.error(f"Error checking {name}: {e}", exc_info=True)

            # Small delay between products to avoid hammering
            if not settings.TEST_MODE:
                time.sleep(jitter(2.0))
