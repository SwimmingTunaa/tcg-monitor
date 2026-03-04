"""
Target AU stock monitor.

Target Australia (target.com.au) product pages.
Similar approach to Big W / Kmart — JSON-LD first, HTML fallback.
"""
import re
import json
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus, infer_availability_scope_from_text
from utils.database import Database

logger = logging.getLogger(__name__)


class TargetAUMonitor(BaseMonitor):
    retailer_key = "target_au"
    retailer_name = "Target AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a Target AU product page."""
        soup = self.fetch_page(url)
        if soup is None:
            return None

        # ── Try JSON-LD structured data ──────────────────────────────
        product_data = {}
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "Product":
                    product_data = data
                    break
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            product_data = item
                            break
            except (json.JSONDecodeError, TypeError):
                continue

        # Static metadata (name/image) is hydrated centrally in BaseMonitor.prepare_status.
        name = "Unknown Product"

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

        offers = product_data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        if offers.get("price"):
            try:
                price = float(offers["price"])
                price_str = f"${price:.2f}"
            except (ValueError, TypeError):
                pass

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

        if stock_text == "Unknown":
            add_btn = soup.find("button", string=re.compile(r"add to (cart|trolley|bag)", re.I))
            oos_el = soup.find(string=re.compile(r"out of stock|sold out|unavailable", re.I))
            if add_btn and not oos_el:
                in_stock = True
                stock_text = "In Stock"
            elif oos_el:
                in_stock = False
                stock_text = "Out of Stock"

        image_url = None

        availability_scope = infer_availability_scope_from_text(
            soup.get_text(" ", strip=True)
        )

        return ProductStatus(
            url=url,
            name=name,
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=price,
            price_str=price_str,
            stock_text=stock_text,
            preorder=is_preorder,
            availability_scope=availability_scope,
            image_url=image_url,
        )
