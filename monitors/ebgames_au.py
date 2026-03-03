"""
EB Games AU stock monitor.

EB Games Australia (ebgames.com.au) product pages.

Strategy:
  1. Raw HTTP via self.fetch_page() — parse with BeautifulSoup using real selectors.
  2. If Cloudflare blocks the raw request (detected by challenge page markers or
     missing product content), fall back to self.fetch_page_playwright() with
     wait_for_selector=".product-detail".

Parsing is handled by a shared _parse_product_page() helper used by both paths.
"""
import re
import logging
from typing import Optional

from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class EBGamesAUMonitor(BaseMonitor):
    retailer_key = "ebgames_au"
    retailer_name = "EB Games AU"

    def __init__(self, db: Database):
        super().__init__(db)

    # ── Cloudflare detection ──────────────────────────────────────────

    @staticmethod
    def _is_cloudflare_page(soup: BeautifulSoup) -> bool:
        """Return True if the soup looks like a Cloudflare challenge page."""
        if soup.find(id="cf-browser-verification"):
            return True
        if soup.find(id="challenge-form"):
            return True
        # No product content at all — almost certainly a bot-wall
        if not soup.find("h1") and not soup.find(class_=re.compile(r"product", re.I)):
            return True
        return False

    # ── Shared parsing ────────────────────────────────────────────────

    def _parse_product_page(self, soup: BeautifulSoup, url: str) -> ProductStatus:
        """
        Parse an EB Games product detail page and return a ProductStatus.
        Works on soup from either raw HTTP or Playwright.
        """

        # ── Title ────────────────────────────────────────────────────
        name = "Unknown Product"
        title_el = soup.find("h1", itemprop="name") or soup.find("h1")
        if title_el:
            name = title_el.get_text(strip=True)

        # ── Price ────────────────────────────────────────────────────
        # EB Games embeds the price in itemprop="price" content attribute
        price: Optional[float] = None
        price_str: Optional[str] = None

        price_el = soup.find("span", itemprop="price")
        if price_el:
            raw = price_el.get("content") or price_el.get_text(strip=True)
            try:
                price = float(raw.replace(",", "").replace("$", ""))
                price_str = f"${price:.2f}"
            except (ValueError, AttributeError):
                pass

        # ── Stock status ─────────────────────────────────────────────
        in_stock = False
        stock_text = "Unknown"

        # Schema.org availability meta tag — most reliable signal
        availability_meta = soup.find("meta", itemprop="availability")
        availability = availability_meta.get("content", "") if availability_meta else ""

        is_preorder_schema = "PreOrder" in availability
        is_instock_schema = "InStock" in availability
        is_outofstock_schema = "OutOfStock" in availability

        # Home delivery stock status div
        delivery_available = bool(soup.select_one(".option .col2.stock-status.available"))

        # Add-to-cart / preorder button text
        add_btn = soup.select_one("button.add-product")
        btn_text = add_btn.get_text(strip=True).lower() if add_btn else ""
        is_add_to_cart = "add to cart" in btn_text
        is_preorder_btn = bool(soup.select_one("div.product-preorder"))

        if is_preorder_schema or is_preorder_btn:
            in_stock = True
            release_el = soup.find("div", itemprop="releaseDate")
            release_date = release_el.get_text(strip=True) if release_el else None
            stock_text = f"Pre-order — {release_date}" if release_date else "Pre-order"
        elif is_instock_schema or is_add_to_cart or delivery_available:
            in_stock = True
            stock_text = "In Stock"
        elif is_outofstock_schema:
            in_stock = False
            stock_text = "Out of Stock"

        # ── Image ────────────────────────────────────────────────────
        image_url: Optional[str] = None
        # First non-skeleton gallery image
        img_el = soup.select_one("#product-media-gallery img.gallery-img:not(.skeleton-loader)")
        if img_el:
            src = img_el.get("src", "")
            if src and not src.startswith("data:"):
                image_url = "https:" + src if src.startswith("//") else src
        if not image_url:
            og_img = soup.find("meta", property="og:image:url") or soup.find("meta", property="og:image")
            if og_img:
                src = og_img.get("content", "")
                image_url = "https:" + src if src.startswith("//") else src

        return ProductStatus(
            url=url,
            name=name,
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=price,
            price_str=price_str,
            stock_text=stock_text,
            preorder=(is_preorder_schema or is_preorder_btn),
            image_url=image_url,
        )

    # ── Public entry point ────────────────────────────────────────────

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """
        Scrape an EB Games AU product page.

        Strategy 1: raw HTTP + BeautifulSoup.
        Strategy 2: Playwright fallback when Cloudflare blocks strategy 1.
        """
        # Strategy 1 — raw HTTP
        soup = self.fetch_page(url)
        if soup is not None and not self._is_cloudflare_page(soup):
            logger.debug(f"EBGames: raw HTTP succeeded for {url}")
            return self._parse_product_page(soup, url)

        if soup is not None:
            logger.info(f"EBGames: Cloudflare detected — falling back to Playwright for {url}")
        else:
            logger.info(f"EBGames: raw fetch returned None — falling back to Playwright for {url}")

        # Strategy 2 — Playwright
        html = self.fetch_page_playwright(url, wait_for_selector="h1[itemprop=name]", headed=True)
        if html is None:
            return None

        soup = BeautifulSoup(html, "lxml")
        return self._parse_product_page(soup, url)
