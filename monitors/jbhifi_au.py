"""
JB Hi-Fi AU stock monitor.

JB Hi-Fi (jbhifi.com.au) uses a fairly standard e-commerce layout.
Products have clear price elements and add-to-cart buttons.
"""
import re
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class JBHiFiAUMonitor(BaseMonitor):
    retailer_key = "jbhifi_au"
    retailer_name = "JB Hi-Fi AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a JB Hi-Fi AU product page."""
        soup = self.fetch_page(url)
        if soup is None:
            return None

        # ── Product Title ────────────────────────────────────────────
        name = "Unknown Product"
        title_el = soup.find("h1")
        if not title_el:
            og_title = soup.find("meta", property="og:title")
            if og_title:
                name = og_title.get("content", "Unknown Product")
        else:
            name = title_el.get_text(strip=True)

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

        # JB uses various price display elements
        price_selectors = [
            ("span", {"class": re.compile(r"price|sale-price", re.I)}),
            ("div", {"class": re.compile(r"price", re.I)}),
        ]

        for tag, attrs in price_selectors:
            el = soup.find(tag, attrs)
            if el:
                raw = el.get_text(strip=True)
                match = re.search(r"\$?([\d,]+\.?\d*)", raw)
                if match:
                    try:
                        price = float(match.group(1).replace(",", ""))
                        price_str = f"${price:.2f}"
                        break
                    except ValueError:
                        continue

        # Also try meta tag
        if not price:
            meta_price = soup.find("meta", {"property": "product:price:amount"})
            if meta_price:
                try:
                    price = float(meta_price.get("content", "0"))
                    price_str = f"${price:.2f}"
                except (ValueError, TypeError):
                    pass

        # ── Stock Status ─────────────────────────────────────────────
        in_stock = False
        stock_text = "Unknown"

        # Look for add to cart button
        add_btn = soup.find("button", string=re.compile(r"add to (cart|bag)", re.I))
        if not add_btn:
            add_btn = soup.find("button", class_=re.compile(r"add.?to.?cart", re.I))

        # Pre-order check
        preorder_el = soup.find(string=re.compile(r"pre.?order", re.I))

        # Out of stock indicators
        oos_el = soup.find(string=re.compile(r"out of stock|sold out|unavailable|temporarily", re.I))

        # Delivery/stock availability text
        avail_el = soup.find(string=re.compile(r"available|in stock|ships? from", re.I))

        if preorder_el and add_btn:
            in_stock = True
            stock_text = "Pre-order"
        elif add_btn and not oos_el:
            in_stock = True
            stock_text = "In Stock"
        elif oos_el:
            in_stock = False
            stock_text = "Out of Stock"
        elif avail_el:
            in_stock = True
            stock_text = "Available"

        # ── Product Image ────────────────────────────────────────────
        image_url = None
        og_img = soup.find("meta", property="og:image")
        if og_img:
            image_url = og_img.get("content")
        if not image_url:
            img_el = soup.find("img", class_=re.compile(r"product", re.I))
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src")

        return ProductStatus(
            url=url,
            name=name,
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=price,
            price_str=price_str,
            stock_text=stock_text,
            image_url=image_url,
        )
