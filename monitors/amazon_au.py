"""
Amazon AU stock monitor.

Scrapes amazon.com.au product pages for:
  - Stock availability (#availability span)
  - Price (#priceblock_ourprice, .a-price .a-offscreen, etc.)
  - Product title (#productTitle)
  - Product image (#landingImage)

Amazon is aggressive with anti-bot measures. This monitor:
  - Rotates User-Agents
  - Adds random delays
  - Uses retry with backoff
  - Keeps sessions for cookie continuity

For high-volume monitoring, consider:
  - Rotating residential proxies
  - Using Amazon's Product Advertising API (PA-API) instead
"""
import re
import logging
from typing import Optional

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class AmazonAUMonitor(BaseMonitor):
    retailer_key = "amazon_au"
    retailer_name = "Amazon AU"

    def __init__(self, db: Database):
        super().__init__(db)
        # Amazon-specific headers
        self.session.headers.update({
            "Accept-Language": "en-AU,en;q=0.9",
        })

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape an Amazon AU product page."""
        soup = self.fetch_page(url)
        if soup is None:
            return None

        # ── Product Title ────────────────────────────────────────────
        name = "Unknown Product"
        title_el = soup.find("span", {"id": "productTitle"})
        if title_el:
            name = title_el.get_text(strip=True)

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

        # Try multiple price selectors (Amazon changes these frequently)
        price_selectors = [
            ("span", {"class": "a-offscreen"}),           # Main price
            ("span", {"id": "priceblock_ourprice"}),       # Legacy
            ("span", {"id": "priceblock_dealprice"}),      # Deal price
            ("span", {"class": "a-price-whole"}),          # Split price
        ]

        for tag, attrs in price_selectors:
            el = soup.find(tag, attrs)
            if el:
                raw = el.get_text(strip=True)
                price_str = raw
                # Extract numeric price
                match = re.search(r"[\d,]+\.?\d*", raw.replace(",", ""))
                if match:
                    try:
                        price = float(match.group())
                        break
                    except ValueError:
                        continue

        # ── Stock Status ─────────────────────────────────────────────
        in_stock = False
        stock_text = "Unknown"
        is_preorder = False

        avail_div = soup.find("div", {"id": "availability"})
        if avail_div:
            avail_text = avail_div.get_text(strip=True).lower()
            stock_text = avail_div.get_text(strip=True)

            if any(kw in avail_text for kw in ["in stock", "left in stock", "available"]):
                in_stock = True
            elif "pre-order" in avail_text or "preorder" in avail_text:
                in_stock = True
                is_preorder = True
                stock_text = "Pre-order"
            elif any(kw in avail_text for kw in [
                "currently unavailable", "out of stock",
                "unavailable", "not available"
            ]):
                in_stock = False

        # Also check the Add to Cart button as a signal
        add_to_cart = soup.find("input", {"id": "add-to-cart-button"})
        if add_to_cart and not in_stock:
            in_stock = True
            if stock_text == "Unknown":
                stock_text = "In Stock"

        # ── Product Image ────────────────────────────────────────────
        image_url = None
        img_el = soup.find("img", {"id": "landingImage"})
        if img_el:
            # Try data-old-hires first (high res), then src
            image_url = img_el.get("data-old-hires") or img_el.get("src")

        # Try hiRes from script tags if no image found
        if not image_url:
            scripts = soup.find_all("script")
            for script in scripts:
                if script.string and "hiRes" in (script.string or ""):
                    match = re.search(r'"hiRes"\s*:\s*"([^"]+)"', script.string)
                    if match:
                        image_url = match.group(1)
                        break

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
