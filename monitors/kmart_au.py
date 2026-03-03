"""
Kmart AU stock monitor.

Kmart Australia (kmart.com.au) product pages.
Kmart's site is heavily JS-rendered, so JSON-LD and meta tags
are the most reliable data sources via requests-based scraping.
"""
import re
import json
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class KmartAUMonitor(BaseMonitor):
    retailer_key = "kmart_au"
    retailer_name = "Kmart AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a Kmart AU product page."""
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

        # ── Product Title ────────────────────────────────────────────
        name = product_data.get("name", "")
        if not name:
            title_el = soup.find("h1")
            if not title_el:
                og = soup.find("meta", property="og:title")
                name = og.get("content", "Unknown Product") if og else "Unknown Product"
            else:
                name = title_el.get_text(strip=True)

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
            meta_price = soup.find("meta", {"property": "product:price:amount"})
            if meta_price:
                try:
                    price = float(meta_price.get("content", "0"))
                    price_str = f"${price:.2f}"
                except (ValueError, TypeError):
                    pass

        if not price:
            price_el = soup.find("span", class_=re.compile(r"price", re.I))
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
            add_btn = soup.find("button", string=re.compile(r"add to (cart|trolley)", re.I))
            oos_el = soup.find(string=re.compile(r"out of stock|sold out|unavailable", re.I))
            if add_btn and not oos_el:
                in_stock = True
                stock_text = "In Stock"
            elif oos_el:
                in_stock = False
                stock_text = "Out of Stock"

        # ── Product Image ────────────────────────────────────────────
        image_url = product_data.get("image", None)
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
