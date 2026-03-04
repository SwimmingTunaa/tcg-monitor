"""
Amazon AU stock monitor.

Amazon frequently changes buy-box and availability markup and can serve
bot-challenge pages to plain HTTP clients. Strategy:
  1. Raw HTTP scrape via BaseMonitor.fetch_page()
  2. Playwright fallback when blocked/ambiguous
"""
import re
import logging
from typing import Optional

from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus
from utils.database import Database

logger = logging.getLogger(__name__)


class AmazonAUMonitor(BaseMonitor):
    retailer_key = "amazon_au"
    retailer_name = "Amazon AU"
    availability_scope = "online"

    def __init__(self, db: Database):
        super().__init__(db)
        self.session.headers.update({
            "Accept-Language": "en-AU,en;q=0.9",
        })

    @staticmethod
    def _is_blocked_page(soup: BeautifulSoup) -> bool:
        text = soup.get_text(" ", strip=True).lower()
        blockers = [
            "enter the characters you see below",
            "sorry, we just need to make sure you're not a robot",
            "automated access to amazon data",
            "/errors/validatecaptcha",
            "api-services-support@amazon.com",
        ]
        if any(marker in text for marker in blockers):
            return True
        if soup.find("form", attrs={"action": re.compile(r"validatecaptcha", re.I)}):
            return True
        return False

    @staticmethod
    def _is_removed_page(soup: BeautifulSoup) -> bool:
        text = soup.get_text(" ", strip=True).lower()
        return (
            "sorry! we couldn't find that page" in text
            or "dogs of amazon" in text
            or ("404" in text and "amazon.com.au" in text and "page" in text)
        )

    @staticmethod
    def _parse_price(raw: str) -> Optional[float]:
        if not raw:
            return None
        match = re.search(r"(\d[\d,]*\.?\d{0,2})", raw)
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    @classmethod
    def _extract_price(cls, soup: BeautifulSoup) -> tuple[Optional[float], Optional[str]]:
        selectors = [
            "#corePrice_feature_div .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
            "#tp_price_block_total_price_ww .a-offscreen",
            "span.a-price.aok-align-center .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#price_inside_buybox",
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if not el:
                continue
            raw = el.get_text(" ", strip=True)
            value = cls._parse_price(raw)
            if value is not None:
                price_str = raw if "$" in raw else f"${value:.2f}"
                return value, price_str

        hidden_inputs = [
            "input[id='items[0.base][customerVisiblePrice][amount]']",
            "input[name='items[0.base][customerVisiblePrice][amount]']",
            "input#twister-plus-price-data-price",
        ]
        for selector in hidden_inputs:
            el = soup.select_one(selector)
            if not el:
                continue
            raw = (el.get("value") or "").strip()
            value = cls._parse_price(raw)
            if value is not None:
                return value, f"${value:.2f}"

        fallback = soup.select_one(".a-price .a-offscreen")
        if fallback:
            raw = fallback.get_text(" ", strip=True)
            value = cls._parse_price(raw)
            if value is not None:
                price_str = raw if "$" in raw else f"${value:.2f}"
                return value, price_str

        return None, None

    @staticmethod
    def _normalize_stock_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    @classmethod
    def _extract_stock_state(cls, soup: BeautifulSoup) -> tuple[Optional[bool], str, bool]:
        out_tokens = (
            "currently unavailable",
            "temporarily out of stock",
            "out of stock",
            "not available",
            "unavailable",
            "we don't know when or if this item will be back in stock",
        )
        preorder_tokens = (
            "pre-order",
            "preorder",
            "released on",
            "release date",
            "this item will be released",
        )
        in_tokens = (
            "in stock",
            "left in stock",
            "available to ship",
            "ships from",
            "usually dispatched",
            "usually ships within",
        )

        selectors = [
            "#availability .primary-availability-message",
            "#availability span.a-size-medium",
            "#availability span",
            "#availabilityInsideBuyBox_feature_div #availability span",
            "#availabilityInsideBuyBox_feature_div .primary-availability-message",
            "#outOfStock",
            "#outOfStock_feature_div",
        ]
        seen: set[str] = set()
        for selector in selectors:
            for el in soup.select(selector):
                raw_text = cls._normalize_stock_text(el.get_text(" ", strip=True))
                if not raw_text:
                    continue
                key = raw_text.lower()
                if key in seen:
                    continue
                seen.add(key)

                if any(token in key for token in preorder_tokens):
                    return True, raw_text, True
                if any(token in key for token in out_tokens):
                    return False, raw_text, False
                if any(token in key for token in in_tokens):
                    return True, raw_text, False

        # CTA fallback: presence of active buy box controls usually means purchasable.
        add_to_cart = soup.select_one("#add-to-cart-button, input[name='submit.add-to-cart']")
        if add_to_cart and not add_to_cart.has_attr("disabled"):
            return True, "In stock", False

        buy_now = soup.select_one("#buy-now-button")
        if buy_now and not buy_now.has_attr("disabled"):
            return True, "In stock", False

        offer_listing = soup.select_one("input[name='offerListingID'], #offerListingID")
        if offer_listing and str(offer_listing.get("value", "")).strip():
            return True, "In stock", False

        page_text = soup.get_text(" ", strip=True).lower()
        if any(token in page_text for token in preorder_tokens):
            return True, "Pre-order", True
        if any(token in page_text for token in out_tokens):
            return False, "Out of stock", False
        if "in stock" in page_text:
            return True, "In stock", False

        return None, "Unknown", False

    def _parse_product_page(self, soup: BeautifulSoup, url: str) -> Optional[ProductStatus]:
        if self._is_removed_page(soup):
            return ProductStatus(
                url=url,
                name="Unknown Product",
                retailer=self.retailer_key,
                in_stock=False,
                stock_text="Unavailable/Removed",
                preorder=False,
                availability_scope=self.availability_scope,
                image_url=None,
            )

        title_el = soup.select_one("#productTitle")
        name = self._normalize_stock_text(title_el.get_text(" ", strip=True)) if title_el else "Unknown Product"

        price, price_str = self._extract_price(soup)
        in_stock, stock_text, is_preorder = self._extract_stock_state(soup)
        if in_stock is None:
            return None

        image_url = None
        img = soup.select_one("#landingImage, #imgTagWrapperId img")
        if img:
            image_url = (
                img.get("data-old-hires")
                or img.get("src")
                or img.get("data-a-dynamic-image")
                or None
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
            availability_scope=self.availability_scope,
            image_url=image_url,
        )

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape an Amazon AU product page with Playwright fallback."""
        soup = self.fetch_page(url)
        if soup is not None and not self._is_blocked_page(soup):
            status = self._parse_product_page(soup, url)
            if status is not None:
                return status
            logger.info(f"Amazon: ambiguous raw parse, retrying via Playwright: {url}")
        elif soup is not None:
            logger.info(f"Amazon: bot challenge detected, retrying via Playwright: {url}")
        else:
            logger.info(f"Amazon: raw fetch failed, retrying via Playwright: {url}")

        html = self.fetch_page_playwright(
            url,
            wait_for_selector="#ppd, #buybox, #productTitle",
            timeout=45_000,
            headed=False,
        )
        if html is None:
            return None

        soup = BeautifulSoup(html, "lxml")
        if self._is_blocked_page(soup):
            logger.warning(f"Amazon: Playwright still blocked by anti-bot page: {url}")
            return None

        status = self._parse_product_page(soup, url)
        if status is None:
            logger.warning(f"Amazon: could not determine stock state from page markup: {url}")
        return status
