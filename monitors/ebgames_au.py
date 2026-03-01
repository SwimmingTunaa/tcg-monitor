"""
EB Games AU stock monitor.

EB Games Australia (ebgames.com.au) product pages.
Looks for stock status in the add-to-cart area and price elements.
"""
import re
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class EBGamesAUMonitor(BaseMonitor):
    retailer_key = "ebgames_au"
    retailer_name = "EB Games AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape an EB Games AU product page."""
        soup = self.fetch_page(url)
        if soup is None:
            return None

        # ── Product Title ────────────────────────────────────────────
        name = "Unknown Product"
        # EB Games uses h1 for product title
        title_el = soup.find("h1", class_=re.compile(r"product[-_]?title|product[-_]?name", re.I))
        if not title_el:
            title_el = soup.find("h1")
        if title_el:
            name = title_el.get_text(strip=True)

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

        # EB Games price selectors
        price_selectors = [
            ("span", {"class": re.compile(r"price|product-price|selling-price", re.I)}),
            ("div", {"class": re.compile(r"price", re.I)}),
            ("span", {"class": "amount"}),
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

        # ── Stock Status ─────────────────────────────────────────────
        in_stock = False
        stock_text = "Unknown"

        # Look for "Add to Cart" button
        add_btn = soup.find("button", string=re.compile(r"add to (cart|bag)", re.I))
        if not add_btn:
            add_btn = soup.find("button", class_=re.compile(r"add-to-cart|addtocart", re.I))
        if not add_btn:
            add_btn = soup.find("input", {"value": re.compile(r"add to cart", re.I)})

        # Look for "Pre-Order" button
        preorder_btn = soup.find("button", string=re.compile(r"pre.?order", re.I))

        # Look for "Out of Stock" / "Sold Out" indicators
        oos_el = soup.find(string=re.compile(r"out of stock|sold out|unavailable|coming soon", re.I))

        # Look for "Click & Collect" availability
        cnc_el = soup.find(string=re.compile(r"click.?&?.?collect|available in.store", re.I))

        if preorder_btn:
            in_stock = True
            stock_text = "Pre-order"
        elif add_btn and not oos_el:
            in_stock = True
            stock_text = "In Stock"
            if cnc_el:
                stock_text = "In Stock (Click & Collect Available)"
        elif oos_el:
            in_stock = False
            stock_text = "Out of Stock"

        # ── Product Image ────────────────────────────────────────────
        image_url = None
        img_el = soup.find("img", class_=re.compile(r"product[-_]?image|gallery", re.I))
        if not img_el:
            # Try og:image meta tag
            og_img = soup.find("meta", property="og:image")
            if og_img:
                image_url = og_img.get("content")
        else:
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
