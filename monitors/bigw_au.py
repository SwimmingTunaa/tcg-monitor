"""
Big W AU stock monitor.

Big W (bigw.com.au) product pages. Big W's site can be JS-heavy,
so some products may need Playwright/Selenium for full rendering.
This monitor attempts requests-based scraping first.
"""
import re
import json
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class BigWAUMonitor(BaseMonitor):
    retailer_key = "bigw_au"
    retailer_name = "Big W AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def _try_json_ld(self, soup) -> dict:
        """Try to extract product data from JSON-LD structured data."""
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
        return {}

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a Big W AU product page."""
        soup = self.fetch_page(url)
        if soup is None:
            return None

        # Try structured data first (most reliable)
        ld = self._try_json_ld(soup)

        # ── Product Title ────────────────────────────────────────────
        name = ld.get("name", "")
        if not name:
            title_el = soup.find("h1")
            name = title_el.get_text(strip=True) if title_el else "Unknown Product"

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

        # From JSON-LD
        offers = ld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if offers.get("price"):
            try:
                price = float(offers["price"])
                price_str = f"${price:.2f}"
            except (ValueError, TypeError):
                pass

        # Fallback to HTML
        if not price:
            price_el = soup.find("span", class_=re.compile(r"price|amount", re.I))
            if price_el:
                raw = price_el.get_text(strip=True)
                match = re.search(r"\$?([\d,]+\.?\d*)", raw)
                if match:
                    try:
                        price = float(match.group(1).replace(",", ""))
                        price_str = f"${price:.2f}"
                    except ValueError:
                        pass

        # ── Stock Status ─────────────────────────────────────────────
        in_stock = False
        stock_text = "Unknown"
        is_preorder = False

        # From JSON-LD
        availability = offers.get("availability", "").lower()
        if "instock" in availability:
            in_stock = True
            stock_text = "In Stock"
        elif "outofstock" in availability:
            in_stock = False
            stock_text = "Out of Stock"
        elif "preorder" in availability:
            in_stock = True
            stock_text = "Pre-order"
            is_preorder = True

        # Fallback to HTML elements
        if stock_text == "Unknown":
            add_btn = soup.find("button", string=re.compile(r"add to (cart|trolley|bag)", re.I))
            oos_el = soup.find(string=re.compile(r"out of stock|sold out|unavailable", re.I))

            if add_btn and not oos_el:
                in_stock = True
                stock_text = "In Stock"
            elif oos_el:
                in_stock = False
                stock_text = "Out of Stock"

        # ── Product Image ────────────────────────────────────────────
        image_url = ld.get("image", None)
        if isinstance(image_url, list):
            image_url = image_url[0] if image_url else None
        if not image_url:
            og_img = soup.find("meta", property="og:image")
            if og_img:
                image_url = og_img.get("content")

        return ProductStatus(
            url=url,
            name=name,
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=price,
            price_str=price_str,
            stock_text=stock_text,
            preorder=is_preorder,
            image_url=image_url,
        )
