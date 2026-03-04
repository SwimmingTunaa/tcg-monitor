"""
JB Hi-Fi AU stock monitor.

JB Hi-Fi (jbhifi.com.au) uses a fairly standard e-commerce layout.
Products have clear price elements and add-to-cart buttons.
"""
import json
import re
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus, infer_availability_scope_from_text
from utils.database import Database

logger = logging.getLogger(__name__)


def _extract_themeconfig_metafields(html: str) -> Optional[dict]:
    """Extract and decode the JSON payload passed to themeConfig('product.metafields', ...)."""
    call_pattern = re.compile(
        r"(?:window\.)?themeConfig\(\s*['\"]product\.metafields['\"]\s*,\s*",
        re.IGNORECASE,
    )

    def _scan_quoted_string(src: str, start_idx: int) -> tuple[Optional[str], int]:
        quote = src[start_idx]
        out = []
        escaped = False
        i = start_idx + 1
        while i < len(src):
            ch = src[i]
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == quote:
                return "".join(out), i + 1
            else:
                out.append(ch)
            i += 1
        return None, i

    def _scan_balanced_object(src: str, start_idx: int) -> Optional[str]:
        depth = 0
        in_string = False
        escaped = False
        string_quote = ""
        for idx in range(start_idx, len(src)):
            ch = src[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == string_quote:
                    in_string = False
                continue
            if ch in ("'", "\""):
                in_string = True
                string_quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start_idx:idx + 1]
        return None

    for match in call_pattern.finditer(html):
        i = match.end()
        while i < len(html) and html[i].isspace():
            i += 1
        if i >= len(html):
            continue

        # Pattern: themeConfig(..., { ... });
        if html[i] == "{":
            obj_str = _scan_balanced_object(html, i)
            if not obj_str:
                continue
            try:
                return json.loads(obj_str)
            except json.JSONDecodeError:
                continue

        # Pattern: themeConfig(..., JSON.parse('...'));
        if html.startswith("JSON.parse", i):
            open_paren = html.find("(", i)
            if open_paren == -1:
                continue
            j = open_paren + 1
            while j < len(html) and html[j].isspace():
                j += 1
            if j >= len(html) or html[j] not in ("'", "\""):
                continue
            encoded_json, _ = _scan_quoted_string(html, j)
            if not encoded_json:
                continue
            # Decode JS-style escapes then parse the JSON payload.
            try:
                decoded = bytes(encoded_json, "utf-8").decode("unicode_escape")
                return json.loads(decoded)
            except Exception:
                continue

    return None


def _extract_availability_fields_fallback(html: str) -> tuple[Optional[str], Optional[str]]:
    """Fallback extractor when metafields payload is JS-like (not strict JSON)."""
    idx = html.find("product.metafields")
    if idx == -1:
        return None, None

    # Limit search window to keep regex cost predictable.
    window = html[idx:idx + 200_000]
    overall_match = re.search(
        r"""(?:['"])?OverallStatus(?:['"])?\s*:\s*['"]([^'"]+)['"]""",
        window,
        re.IGNORECASE,
    )
    lifecycle_match = re.search(
        r"""(?:['"])?ProductLifeCycle(?:['"])?\s*:\s*['"]([^'"]+)['"]""",
        window,
        re.IGNORECASE,
    )
    overall_status = overall_match.group(1) if overall_match else None
    lifecycle = lifecycle_match.group(1) if lifecycle_match else None
    return overall_status, lifecycle


def _extract_availability_from_data(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Get availability fields from known payload shapes."""
    candidates = [
        data.get("online_product", {}).get("value", {}).get("Availability", {}),
        data.get("online_product", {}).get("Availability", {}),
        data.get("value", {}).get("Availability", {}),
        data.get("Availability", {}),
    ]
    for availability in candidates:
        if not isinstance(availability, dict):
            continue
        overall_status = availability.get("OverallStatus")
        lifecycle = availability.get("ProductLifeCycle")
        if overall_status is not None or lifecycle is not None:
            return overall_status, lifecycle
    return None, None


def _normalize_status_token(value: Optional[str]) -> Optional[str]:
    """Normalize stock lifecycle tokens to lowercase alpha keys."""
    if not value:
        return None
    return re.sub(r"[^a-z]", "", str(value).lower())


def _is_actionable_availability(overall_status: Optional[str], lifecycle: Optional[str]) -> bool:
    """Return True when the availability values can be mapped to a stock state."""
    overall_key = _normalize_status_token(overall_status)
    lifecycle_key = _normalize_status_token(lifecycle)
    if lifecycle_key == "preorder":
        return True
    return overall_key in {"preorder", "instock", "outofstock", "limitedavailability"}


def _merge_with_jsonld_hint(
    overall_status: Optional[str],
    lifecycle: Optional[str],
    jsonld_overall: Optional[str],
    jsonld_lifecycle: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    Merge JSON-LD as an enrichment signal.
    Prefer JSON-LD when it adds stronger state (preorder/limited availability).
    """
    current_overall_key = _normalize_status_token(overall_status)
    current_lifecycle_key = _normalize_status_token(lifecycle)
    json_overall_key = _normalize_status_token(jsonld_overall)
    json_lifecycle_key = _normalize_status_token(jsonld_lifecycle)

    # JSON-LD preorder is stronger than plain in-stock.
    if json_lifecycle_key == "preorder" and current_lifecycle_key != "preorder":
        return jsonld_overall or overall_status, "PreOrder"

    # LimitedAvailability is a stronger in-stock signal than generic InStock.
    if json_overall_key == "limitedavailability" and current_overall_key == "instock":
        return "LimitedAvailability", lifecycle

    return overall_status, lifecycle


def _extract_from_themeconfig(html: str) -> tuple[Optional[str], Optional[str]]:
    """Extract availability from window.themeConfig('product.metafields', ...)."""
    data = _extract_themeconfig_metafields(html)
    if data:
        overall_status, lifecycle = _extract_availability_from_data(data)
        if overall_status is not None or lifecycle is not None:
            return overall_status, lifecycle
    return _extract_availability_fields_fallback(html)


def _jsonld_objects_from_html(soup: BeautifulSoup) -> list[dict]:
    """Parse JSON-LD scripts and return dict objects for scanning."""
    objects: list[dict] = []
    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        if isinstance(payload, dict):
            objects.append(payload)
        elif isinstance(payload, list):
            objects.extend([item for item in payload if isinstance(item, dict)])
    return objects


def _walk_json(node):
    """Yield nested dict nodes from a JSON-like object."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def _map_schema_availability(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map schema availability URL/value to monitor status tokens."""
    key = _normalize_status_token(value)
    if not key:
        return None, None
    if "preorder" in key:
        return "InStock", "PreOrder"
    if "limitedavailability" in key:
        return "LimitedAvailability", None
    if "instock" in key:
        return "InStock", None
    if "outofstock" in key:
        return "OutOfStock", None
    return None, None


def _extract_from_jsonld(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """Extract availability from application/ld+json offers.availability."""
    for payload in _jsonld_objects_from_html(soup):
        for obj in _walk_json(payload):
            availability = obj.get("availability")
            if not isinstance(availability, str):
                continue
            overall_status, lifecycle = _map_schema_availability(availability)
            if overall_status is not None or lifecycle is not None:
                return overall_status, lifecycle
    return None, None


def _extract_from_cta(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """Fallback availability parser using CTA/banner text."""
    banner = soup.find(attrs={"data-testid": "pdp-banner-tag"})
    if banner:
        text = banner.get_text(" ", strip=True).lower()
        if "pre-order" in text or "preorder" in text:
            return "InStock", "PreOrder"

    # Explicit CTA control identifiers are a strong server-rendered signal.
    add_to_cart_btn = soup.find("button", attrs={"data-testid": "add-to-cart-button"})
    if add_to_cart_btn is not None:
        return "InStock", None

    cta_scope = soup.find(id="pdp-addtocart-cta") or soup.find(id="pdp-call-to-action-wrapper") or soup
    cta_text = cta_scope.get_text(" ", strip=True).lower()
    if "pre-order" in cta_text or "preorder" in cta_text:
        return "InStock", "PreOrder"
    if "add to cart" in cta_text:
        return "InStock", None
    if "notify me" in cta_text:
        return "OutOfStock", None

    # If CTA container has a button but no readable text in SSR HTML, treat as in-stock fallback.
    if cta_scope.find("button") is not None:
        return "InStock", None

    return None, None


def _extract_from_page_text(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """Broad text fallback when structured and CTA signals are weak."""
    scope = soup.find(id="pdp-right-panel") or soup
    text = scope.get_text(" ", strip=True).lower()
    text = re.sub(r"\s+", " ", text)

    if "pre-order" in text or "preorder" in text:
        return "InStock", "PreOrder"
    if "notify me" in text or "out of stock" in text or "sold out" in text:
        return "OutOfStock", None
    if "add to cart" in text:
        return "InStock", None
    return None, None


def _extract_release_date(soup: BeautifulSoup, html: str) -> Optional[str]:
    """Extract PDP release date text if present."""
    label = soup.find("span", string=re.compile(r"^\s*Release date\s*$", re.I))
    if label and label.parent:
        text = label.parent.get_text(" ", strip=True)
        text = re.sub(r"(?i)^release date\s*", "", text).strip(" :-")
        if text:
            return text

    match = re.search(r"Release date</span>\s*([^<]+)<", html, re.I)
    if match:
        value = match.group(1).strip()
        if value:
            return value

    return None


def _extract_price_from_jsonld(soup: BeautifulSoup) -> Optional[float]:
    """Extract offer price from JSON-LD if available."""
    for payload in _jsonld_objects_from_html(soup):
        for obj in _walk_json(payload):
            if not isinstance(obj, dict):
                continue
            obj_type = str(obj.get("@type", "")).lower()
            if "offer" not in obj_type and "availability" not in obj:
                continue
            raw_price = obj.get("price")
            if raw_price is None:
                continue
            try:
                return float(str(raw_price).replace(",", "").replace("$", "").strip())
            except ValueError:
                continue
    return None


def _has_nearly_gone_tag(soup: BeautifulSoup, html: str) -> bool:
    """Detect JB's low-stock banner text."""
    banner_root = soup.find(id="pdp-banner-label")
    if banner_root:
        text = banner_root.get_text(" ", strip=True).lower()
        if "nearly gone" in text:
            return True
    if soup.find(string=re.compile(r"\bnearly\s+gone\b", re.I)):
        return True
    # Raw HTML fallback for cases where text nodes are not preserved as expected.
    if re.search(r"\bnearly\s*gone\b", html, re.I):
        return True
    return False


class JBHiFiAUMonitor(BaseMonitor):
    retailer_key = "jbhifi_au"
    retailer_name = "JB Hi-Fi AU"

    def __init__(self, db: Database):
        super().__init__(db)

    def _unavailable_status(self, url: str) -> ProductStatus:
        """Return a synthetic status for hard-miss product URLs."""
        return ProductStatus(
            url=url,
            name="Unavailable Product",
            retailer=self.retailer_key,
            in_stock=False,
            price=None,
            price_str=None,
            stock_text="Unavailable/Removed",
            preorder=False,
            image_url=None,
        )

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a JB Hi-Fi AU product page."""
        html = None

        # Primary path: plain requests fetch (matches the successful local diagnostic path).
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            html = resp.text
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                return self._unavailable_status(url)
            logger.debug(f"JB Hi-Fi plain fetch HTTP error for {url}: {e}")
        except requests.exceptions.RequestException as e:
            logger.debug(f"JB Hi-Fi plain fetch failed for {url}: {e}")
        except Exception as e:
            logger.debug(f"JB Hi-Fi plain fetch failed for {url}: {e}")

        # Fallback: monitor session fetch (keeps compatibility with existing behavior/cookies).
        if not html:
            try:
                resp = self.session.get(url, timeout=20)
                resp.raise_for_status()
                html = resp.text
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                if status_code in (404, 410):
                    return self._unavailable_status(url)
                logger.debug(f"JB Hi-Fi session fetch HTTP error for {url}: {e}")
                return None
            except requests.exceptions.RequestException as e:
                logger.debug(f"JB Hi-Fi session fetch failed for {url}: {e}")
                return None
            except Exception as e:
                logger.debug(f"Failed to fetch JB Hi-Fi page {url}: {e}")
                return None

        soup = BeautifulSoup(html, "lxml")

        # Availability precedence:
        # 1) themeConfig product metafields
        # 2) JSON-LD offers.availability
        # 3) CTA/banner text fallback
        overall_status, lifecycle = _extract_from_themeconfig(html)
        jsonld_overall, jsonld_lifecycle = _extract_from_jsonld(soup)
        if not _is_actionable_availability(overall_status, lifecycle):
            overall_status, lifecycle = jsonld_overall, jsonld_lifecycle
        else:
            overall_status, lifecycle = _merge_with_jsonld_hint(
                overall_status, lifecycle, jsonld_overall, jsonld_lifecycle
            )
        if not _is_actionable_availability(overall_status, lifecycle):
            overall_status, lifecycle = _extract_from_cta(soup)
        if not _is_actionable_availability(overall_status, lifecycle):
            overall_status, lifecycle = _extract_from_page_text(soup)

        overall_key = _normalize_status_token(overall_status)
        lifecycle_key = _normalize_status_token(lifecycle)
        is_preorder = lifecycle_key == "preorder" or overall_key == "preorder"
        in_stock = is_preorder or overall_key in ("instock", "limitedavailability")
        is_nearly_gone = _has_nearly_gone_tag(soup, html)

        # Static metadata (name/image) is hydrated centrally in BaseMonitor.prepare_status.
        name = "Unknown Product"

        # ── Price ────────────────────────────────────────────────────
        # JB Hi-Fi uses a stable partial class name: PriceTag_priceTag
        price = None
        price_str = None

        price_el = soup.find(lambda tag: tag.name in ("span", "div", "p")
                             and any("PriceTag_priceTag" in c for c in tag.get("class", [])))
        if price_el:
            raw = price_el.get_text(strip=True)
            match = re.search(r"\$?([\d,]+\.?\d*)", raw)
            if match:
                try:
                    price = float(match.group(1).replace(",", ""))
                    price_str = f"${price:.2f}"
                except ValueError:
                    pass

        # Fallback: product:price:amount meta tag
        if not price:
            meta_price = soup.find("meta", {"property": "product:price:amount"})
            if meta_price:
                try:
                    price = float(meta_price.get("content", "0"))
                    price_str = f"${price:.2f}"
                except (ValueError, TypeError):
                    pass

        # Fallback: JSON-LD offers.price
        if not price:
            jsonld_price = _extract_price_from_jsonld(soup)
            if jsonld_price is not None:
                price = jsonld_price
                price_str = f"${price:.2f}"

        if is_preorder:
            release_date = _extract_release_date(soup, html)
            stock_text = f"Pre-order — {release_date}" if release_date else "Pre-order"
        elif overall_key in ("instock", "limitedavailability"):
            if is_nearly_gone:
                stock_text = "Nearly gone"
            elif overall_key == "limitedavailability":
                stock_text = "Nearly gone"
            else:
                stock_text = "In Stock"
        elif is_nearly_gone:
            in_stock = True
            stock_text = "Nearly gone"
        elif overall_key == "outofstock":
            stock_text = "Out of Stock"
        else:
            stock_text = "Unknown"

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
