"""
Kmart AU stock monitor.

Kmart Australia (kmart.com.au) product pages.
Kmart's site is heavily JS-rendered, so JSON-LD and meta tags
are the most reliable data sources via requests-based scraping.
"""
import re
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor
from utils.helpers import ProductStatus, infer_availability_scope_from_text
from utils.database import Database

logger = logging.getLogger(__name__)


class KmartAUMonitor(BaseMonitor):
    retailer_key = "kmart_au"
    retailer_name = "Kmart AU"
    _constructor_fallback_key = "key_GZTqlLr41FS2p7AY"
    _find_in_store_api = "https://api.kmart.com.au/gateway/graphql"

    def __init__(self, db: Database):
        super().__init__(db)

    @staticmethod
    def _normalize_status_token(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        return re.sub(r"[^a-z]", "", str(value).lower())

    @staticmethod
    def _extract_jsonld_product_data(soup: BeautifulSoup) -> dict:
        """Extract first Product JSON-LD payload."""
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            raw = script.string or script.get_text() or ""
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return item
        return {}

    @staticmethod
    def _extract_offer(product_data: dict) -> dict:
        offers = product_data.get("offers", {})
        if isinstance(offers, list):
            return offers[0] if offers else {}
        return offers if isinstance(offers, dict) else {}

    @staticmethod
    def _map_availability(availability: str) -> tuple[Optional[str], Optional[str]]:
        key = KmartAUMonitor._normalize_status_token(availability)
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

    @staticmethod
    def _has_preorder_text(text: str) -> bool:
        return bool(re.search(r"\bpre[\s-]?order\b", text, re.I))

    @staticmethod
    def _extract_from_text(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
        text = soup.get_text(" ", strip=True).lower()
        text = re.sub(r"\s+", " ", text)
        if KmartAUMonitor._has_preorder_text(text):
            return "InStock", "PreOrder"
        if "notify me" in text or "out of stock" in text or "sold out" in text or "unavailable" in text:
            return "OutOfStock", None
        if "add to cart" in text or "add to trolley" in text or "add to bag" in text:
            return "InStock", None
        return None, None

    @staticmethod
    def _extract_price_from_soup(soup: BeautifulSoup) -> Optional[float]:
        """Extract price from Kmart DOM variants when JSON-LD/meta are missing."""
        candidate_selectors = [
            '[data-testid="product-price-discount"]',
            '[data-testid="price"]',
            'span[class*="product-price"]',
            'p[class*="save-price"]',
            'span[class*="price"]',
        ]
        for selector in candidate_selectors:
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                if not text:
                    continue
                match = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text)
                if not match:
                    continue
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    continue
        return None

    @staticmethod
    def _extract_from_status_badges(
        soup: BeautifulSoup,
    ) -> tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        Extract stock signals from Kmart status badges.
        Example labels:
          - "Pre-order (27/03/26)"
          - "In store only"
          - "Online only"
        """
        labels = []
        for el in soup.select('[role="status"][aria-label]'):
            label = (el.get("aria-label") or "").strip()
            if label:
                labels.append(label)

        # Fallback: visible badge text (covers cases where aria-label is absent/stripped).
        if not labels:
            for el in soup.select("span,div,p"):
                txt = el.get_text(" ", strip=True)
                if not txt:
                    continue
                low = txt.lower()
                if (
                    "online only" in low
                    or "in store only" in low
                    or "instore only" in low
                    or "pre-order" in low
                    or "preorder" in low
                ):
                    labels.append(txt)

        if not labels:
            return None, None, None, "unknown"

        normalized = [
            re.sub(r"\s+", " ", l.replace("\u00a0", " ").replace("-", " ")).strip().lower()
            for l in labels
        ]
        has_online_only = any(re.search(r"\bonline\s+only\b", l) for l in normalized)
        has_instore_only = any(
            re.search(r"\bin\s*store\s+only\b", l) or re.search(r"\binstore\s+only\b", l)
            for l in normalized
        )
        if has_online_only and has_instore_only:
            availability_scope = "both"
        elif has_online_only:
            availability_scope = "online"
        elif has_instore_only:
            availability_scope = "instore_only"
        else:
            availability_scope = "unknown"

        if has_instore_only and not has_online_only:
            return "OutOfStock", None, None, availability_scope

        for label, low in zip(labels, normalized):
            if KmartAUMonitor._has_preorder_text(low):
                match = re.search(r"\(([^)]+)\)", label)
                release_date = match.group(1).strip() if match else None
                return "InStock", "PreOrder", release_date, availability_scope

        return None, None, None, availability_scope

    @staticmethod
    def _extract_from_callout_notifications(
        soup: BeautifulSoup,
    ) -> tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        Extract stock/channel from Kmart callout notification blocks.
        Common pattern:
          <div data-testid="calloutMsgNotification"> ... In Store Only ... </div>
        """
        callouts = soup.select(
            '[data-testid="calloutMsgNotification"], #ItemLimitNotification, .MuiAlert-root[role="alert"]'
        )
        if not callouts:
            return None, None, None, "unknown"

        labels = []
        for el in callouts:
            text = el.get_text(" ", strip=True)
            if text:
                labels.append(text)

        if not labels:
            return None, None, None, "unknown"

        normalized = [
            re.sub(r"\s+", " ", l.replace("\u00a0", " ").replace("-", " ")).strip().lower()
            for l in labels
        ]
        has_online_only = any(re.search(r"\bonline\s+only\b", l) for l in normalized)
        has_instore_only = any(
            re.search(r"\bin\s*store\s+only\b", l) or re.search(r"\binstore\s+only\b", l)
            for l in normalized
        )

        if has_online_only and has_instore_only:
            availability_scope = "both"
        elif has_online_only:
            availability_scope = "online"
        elif has_instore_only:
            availability_scope = "instore_only"
        else:
            availability_scope = "unknown"

        if has_instore_only and not has_online_only:
            return "OutOfStock", None, None, availability_scope

        for label, low in zip(labels, normalized):
            if KmartAUMonitor._has_preorder_text(low):
                match = re.search(r"\(([^)]+)\)", label)
                release_date = match.group(1).strip() if match else None
                return "InStock", "PreOrder", release_date, availability_scope

        if any("out of stock" in l or "sold out" in l or "unavailable" in l for l in normalized):
            return "OutOfStock", None, None, availability_scope

        return None, None, None, availability_scope

    @staticmethod
    def _merge_scopes(*scopes: Optional[str]) -> str:
        """
        Merge multiple channel scope hints into one.
        """
        normalized = []
        for scope in scopes:
            if not scope:
                continue
            s = str(scope).strip().lower()
            if s in {"online", "instore_only", "both", "unknown"}:
                normalized.append(s)

        if not normalized:
            return "unknown"
        if "both" in normalized:
            return "both"
        has_online = "online" in normalized
        has_instore = "instore_only" in normalized
        if has_online and has_instore:
            return "both"
        if has_online:
            return "online"
        if has_instore:
            return "instore_only"
        return "unknown"

    @staticmethod
    def _looks_access_denied(html: str) -> bool:
        text = html.lower()
        return (
            "access denied" in text
            and "you don't have permission to access" in text
        )

    @staticmethod
    def _extract_product_id_from_url(url: str) -> Optional[str]:
        """
        Extract trailing numeric Kmart product id from product URL.
        Example:
          ...-43252695/ -> 43252695
        """
        match = re.search(r"-([0-9]{6,})/?(?:\?.*)?$", (url or "").strip())
        if match:
            return match.group(1)
        return None

    def _fetch_constructor_snapshot(self, url: str) -> Optional[dict]:
        """
        Lightweight fallback against Constructor search API.
        Used only when PDP is Akamai-blocked.
        """
        product_id = self._extract_product_id_from_url(url)
        if not product_id:
            return None

        key = os.getenv("KMART_CONSTRUCTOR_KEY", "").strip() or self._constructor_fallback_key
        search_url = f"https://ac.cnstrc.com/search/{quote(product_id)}"
        params = {
            "c": "ciojs-client-2.71.1",
            "key": key,
            "num_results_per_page": 60,
            "filters[Seller]": "Kmart",
            "sort_by": "relevance",
            "sort_order": "descending",
        }

        try:
            resp = self.session.get(search_url, params=params, timeout=8)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.debug(f"Kmart Constructor fallback failed for {url}: {e}")
            return None

        results = payload.get("response", {}).get("results", []) or []
        def _truthy_flag(value) -> Optional[bool]:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                low = value.strip().lower()
                if low in {"true", "yes", "1", "y"}:
                    return True
                if low in {"false", "no", "0", "n"}:
                    return False
            return None

        def _iter_pairs(obj, prefix: str = ""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = f"{prefix}.{k}" if prefix else str(k)
                    yield key, v
                    yield from _iter_pairs(v, key)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    key = f"{prefix}[{i}]"
                    yield key, v
                    yield from _iter_pairs(v, key)

        def _infer_scope_from_constructor_data(data: dict) -> str:
            explicit_online = False
            explicit_instore = False
            text_parts = []

            for key, value in _iter_pairs(data):
                k = str(key).lower()
                v_flag = _truthy_flag(value)

                if any(tok in k for tok in ("instoreonly", "in_store_only", "storeonly")):
                    if v_flag is True:
                        explicit_instore = True
                    elif v_flag is False:
                        pass
                if any(tok in k for tok in ("onlineonly", "online_only", "deliveryonly", "delivery_only")):
                    if v_flag is True:
                        explicit_online = True
                    elif v_flag is False:
                        pass

                if isinstance(value, str):
                    text_parts.append(value)
                elif isinstance(value, (dict, list)):
                    # include compact json for textual hints inside nested structures
                    try:
                        text_parts.append(json.dumps(value, ensure_ascii=False))
                    except Exception:
                        pass

            if explicit_online and explicit_instore:
                return "both"
            if explicit_online:
                return "online"
            if explicit_instore:
                return "instore_only"

            corpus = " ".join(text_parts).lower()
            has_instore_only = bool(
                re.search(
                    r"\bin\s*store\s+only\b|\binstore\s+only\b|\bpick\s*up\s+only\b|\bclick\s*(?:&|and)\s*collect\s+only\b",
                    corpus,
                )
            )
            has_online_only = bool(
                re.search(
                    r"\bonline\s+only\b|\bdelivery\s+only\b|\bshipping\s+only\b|\bship\s+only\b",
                    corpus,
                )
            )
            if has_instore_only and has_online_only:
                return "both"
            if has_instore_only:
                return "instore_only"
            if has_online_only:
                return "online"
            return "unknown"

        for item in results:
            data = item.get("data", {}) if isinstance(item, dict) else {}
            candidate_id = str(data.get("id") or "")
            candidate_url = str(data.get("url") or "")
            if (
                candidate_id != product_id
                and f"-{product_id}" not in candidate_url
            ):
                continue

            state_oos = data.get("stateOOS", []) or []
            is_oos_all_states = len(state_oos) >= 8
            price_value = data.get("price")
            try:
                price_float = float(price_value) if price_value is not None else None
            except (TypeError, ValueError):
                price_float = None

            # Explicit channel hints if present in constructor payload.
            in_store_only_keys = (
                "inStoreOnly", "isInStoreOnly", "instoreOnly", "isStoreOnly", "storeOnly"
            )
            online_only_keys = (
                "onlineOnly", "isOnlineOnly", "isDeliveryOnly", "deliveryOnly"
            )
            in_store_only = any(
                _truthy_flag(data.get(k)) is True for k in in_store_only_keys
            )
            online_only = any(
                _truthy_flag(data.get(k)) is True for k in online_only_keys
            )

            # Textual hint fallback from optional fields if flags are absent.
            if not in_store_only and not online_only:
                hint_parts = []
                for key in (
                    "availability", "availabilityStatus", "inventoryStatus",
                    "fulfilment", "fulfillment", "shipping", "deliveryMessage",
                    "badges", "tags",
                ):
                    value = data.get(key)
                    if value is None:
                        continue
                    if isinstance(value, (dict, list)):
                        hint_parts.append(json.dumps(value))
                    else:
                        hint_parts.append(str(value))
                hint_text = " ".join(hint_parts).lower()
                if re.search(r"\bin\s*store\s+only\b|\binstore\s+only\b", hint_text):
                    in_store_only = True
                elif re.search(r"\bonline\s+only\b|\bdelivery\s+only\b", hint_text):
                    online_only = True
            inferred_scope = _infer_scope_from_constructor_data(data)

            return {
                "id": product_id,
                "price": price_float,
                "state_oos": state_oos,
                "is_oos_all_states": is_oos_all_states,
                "name": item.get("value") if isinstance(item, dict) else None,
                "in_store_only": in_store_only,
                "online_only": online_only,
                "inferred_scope": inferred_scope,
            }

        return None

    @staticmethod
    def _format_iso_release_date(iso_date: str) -> Optional[str]:
        try:
            parsed = datetime.strptime(iso_date, "%Y-%m-%d")
            return parsed.strftime("%a, %d %b %Y")
        except Exception:
            return None

    @staticmethod
    def _is_future_iso_date(iso_date: str) -> bool:
        try:
            return datetime.strptime(iso_date, "%Y-%m-%d").date() > datetime.now().date()
        except Exception:
            return False

    def _fetch_find_in_store_snapshot(self, url: str) -> Optional[dict]:
        """
        Query Kmart GraphQL `getFindInStore` using keycode + postcode.
        Used as a blocked-page fallback to infer in-store-only channel.
        """
        keycode = self._extract_product_id_from_url(url)
        if not keycode:
            return None

        postcode = os.getenv("KMART_POSTCODE", "3000").strip()
        if not postcode:
            return None

        query = (
            "query getFindInStore($input: FindInStoreQueryInput!) {"
            "  findInStoreQuery(input: $input) {"
            "    keycode "
            "    inventory { locationName locationId stockLevel phoneNumber __typename } "
            "    __typename "
            "  }"
            "}"
        )
        keycode_candidates = [keycode]
        if not keycode.startswith("P_"):
            keycode_candidates.append(f"P_{keycode}")

        headers = {
            "accept": "*/*",
            "accept-language": "en-AU,en-US;q=0.9,en-GB;q=0.8,en;q=0.7",
            "content-type": "application/json",
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "referer": "https://www.kmart.com.au/",
            "origin": "https://www.kmart.com.au",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
        }
        debug_mode = os.getenv("KMART_DEBUG_FIND_STORE", "").lower() in {"1", "true", "yes"}

        rows = []
        for kc in keycode_candidates:
            payload = {
                "operationName": "getFindInStore",
                "variables": {
                    "input": {
                        "postcode": postcode,
                        "country": "AU",
                        "keycodes": [kc],
                    }
                },
                "query": query,
            }
            try:
                # Use requests.post directly (not session) to avoid polluted cookies.
                resp = requests.post(
                    self._find_in_store_api,
                    headers=headers,
                    json=payload,
                    timeout=10,
                )
                status_code = resp.status_code
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                if debug_mode:
                    logger.warning(f"Kmart find-in-store request failed for keycode {kc}: {e}")
                continue

            if debug_mode and data.get("errors"):
                logger.warning(f"Kmart find-in-store GraphQL errors for keycode {kc}: {data.get('errors')}")
            if debug_mode:
                logger.info(f"Kmart find-in-store HTTP {status_code} for keycode {kc}")

            rows = data.get("data", {}).get("findInStoreQuery", []) or []
            if rows:
                keycode = kc
                break

        if not rows:
            return None

        inventory = rows[0].get("inventory", []) or []
        if not inventory:
            return {
                "postcode": postcode,
                "keycode": keycode,
                "inventory_count": 0,
                "available_store_count": 0,
                "has_instore_stock": False,
                "stock_levels": [],
            }

        positive_tokens = ("high", "medium", "low", "limited", "instock", "available")
        negative_tokens = ("none", "no stock", "out", "unavailable", "sold")

        stock_levels = []
        available_store_count = 0
        for store in inventory:
            level_raw = str(store.get("stockLevel") or "").strip()
            level = level_raw.lower()
            stock_levels.append(level_raw)
            if not level:
                continue
            if any(tok in level for tok in negative_tokens):
                continue
            if any(tok in level for tok in positive_tokens):
                available_store_count += 1

        return {
            "postcode": postcode,
            "keycode": keycode,
            "inventory_count": len(inventory),
            "available_store_count": available_store_count,
            "has_instore_stock": available_store_count > 0,
            "stock_levels": stock_levels,
        }

    def _unavailable_status(self, url: str) -> ProductStatus:
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

    def _blocked_status_from_db(self, url: str) -> Optional[ProductStatus]:
        """
        Use last-known status when Kmart blocks scraping (Akamai 403).
        Prevents false unavailable/out-of-stock state changes.
        """
        last = self.db.get_last_status(url)
        if not last:
            return None
        last_stock_text = (last.get("stock_text") or "").strip()
        keep_last_text = bool(last_stock_text) and "blocked (akamai)" not in last_stock_text.lower()
        canonical_release_date = self.db.get_canonical_release_date_for_url(url)
        has_future_release = (
            bool(canonical_release_date)
            and self._is_future_iso_date(canonical_release_date)
        )
        formatted_release = (
            self._format_iso_release_date(canonical_release_date)
            if canonical_release_date else None
        )
        is_preorder = (
            "pre-order" in last_stock_text.lower()
            or "preorder" in last_stock_text.lower()
            or has_future_release
        )
        if is_preorder:
            stock_text = f"Pre-order — {formatted_release}" if formatted_release else "Pre-order"
        else:
            stock_text = last_stock_text if keep_last_text else "Blocked (Akamai)"
        availability_scope = infer_availability_scope_from_text(last_stock_text)
        # Kmart pre-orders are typically online-only; preserve a useful channel signal
        # when blocked pages prevent live badge extraction.
        if is_preorder and availability_scope == "unknown":
            availability_scope = "online"

        in_stock = True if is_preorder else bool(last.get("in_stock"))
        if availability_scope == "instore_only" and not is_preorder:
            in_stock = False
            if not keep_last_text or "blocked (akamai)" in (stock_text or "").lower():
                stock_text = "Out of Stock"

        return ProductStatus(
            url=url,
            name=last.get("name") or "Unknown Product",
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=last.get("price"),
            price_str=last.get("price_str"),
            # Preserve last meaningful stock text so blocked checks don't erase useful state.
            stock_text=stock_text,
            preorder=is_preorder,
            availability_scope=availability_scope,
            image_url=last.get("image_url"),
        )

    def _blocked_status_from_constructor(
        self,
        url: str,
        base_status: Optional[ProductStatus],
    ) -> Optional[ProductStatus]:
        """
        Improve blocked status using Constructor API signals.
        """
        snap = self._fetch_constructor_snapshot(url)
        if not snap:
            return None

        base_name = (
            (base_status.name if base_status else None)
            or snap.get("name")
            or "Unknown Product"
        )
        base_price = base_status.price if base_status else None
        base_price_str = base_status.price_str if base_status else None
        base_image = base_status.image_url if base_status else None
        base_preorder = bool(base_status.is_preorder) if base_status else False
        base_stock_text = (base_status.stock_text if base_status else None) or "Blocked (Akamai)"

        price = base_price
        price_str = base_price_str
        if price is None and snap.get("price") is not None:
            price = snap["price"]
            price_str = f"${price:.2f}"

        is_oos_all_states = bool(snap.get("is_oos_all_states"))
        explicit_instore_only = bool(snap.get("in_store_only"))
        explicit_online_only = bool(snap.get("online_only"))
        inferred_scope = str(snap.get("inferred_scope") or "unknown").lower()

        if explicit_instore_only or is_oos_all_states or inferred_scope == "instore_only":
            availability_scope = "instore_only"
        elif explicit_online_only or inferred_scope == "online":
            availability_scope = "online"
        elif inferred_scope == "both":
            availability_scope = "both"
        else:
            availability_scope = (
                base_status.availability_scope if base_status else "unknown"
            )

        if base_preorder:
            in_stock = True
            stock_text = base_stock_text if "pre-order" in base_stock_text.lower() else "Pre-order"
        elif availability_scope == "instore_only":
            in_stock = False
            stock_text = "Out of Stock"
        elif availability_scope == "online":
            in_stock = True
            stock_text = "In Stock"
        else:
            in_stock = base_status.in_stock if base_status else True
            stock_text = base_stock_text if base_status else "Blocked (Akamai)"

        return ProductStatus(
            url=url,
            name=base_name,
            retailer=self.retailer_key,
            in_stock=in_stock,
            price=price,
            price_str=price_str,
            stock_text=stock_text,
            preorder=base_preorder,
            availability_scope=availability_scope,
            image_url=base_image,
        )

    def _blocked_status_from_find_in_store(
        self,
        url: str,
        base_status: Optional[ProductStatus],
    ) -> Optional[ProductStatus]:
        """
        When channel is still unknown on blocked pages, use find-in-store
        inventory as a location-scoped signal for `instore_only`.
        """
        if not base_status:
            return None
        if base_status.is_preorder:
            return None
        if base_status.availability_scope != "unknown":
            return None

        snap = self._fetch_find_in_store_snapshot(url)
        if not snap:
            return None
        if not snap.get("has_instore_stock"):
            return None

        return ProductStatus(
            url=url,
            name=base_status.name,
            retailer=self.retailer_key,
            # In-store availability only means not purchasable online.
            in_stock=False,
            price=base_status.price,
            price_str=base_status.price_str,
            stock_text="In Store Only",
            preorder=False,
            availability_scope="instore_only",
            image_url=base_status.image_url,
        )

    def _fetch_with_playwright_fallback(self, url: str) -> Optional[str]:
        """
        Try multiple Playwright strategies to bypass Akamai bot blocks.
        Returns page HTML when a real product page is captured.
        """
        # Keep fallback bounded and quick, but allow late Kmart callouts to render.
        attempts = [{"headed": False, "timeout": 7_500}]
        if os.getenv("KMART_PLAYWRIGHT_NO_HEADED", "").lower() not in {"1", "true", "yes"}:
            attempts.append({"headed": True, "timeout": 9_000})

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.debug("Playwright not installed — skipping browser fallback")
            return None

        from discovery.base_discovery import STEALTH_JS, make_playwright_context

        settle_ms = int(os.getenv("KMART_PLAYWRIGHT_SETTLE_MS", "2200"))
        settle_step_ms = 250
        signal_patterns = (
            "in store only",
            "instore only",
            "online only",
            "pre-order",
            "preorder",
            "calloutmsgnotification",
        )

        for attempt in attempts:
            try:
                with sync_playwright() as p:
                    context = make_playwright_context(
                        p,
                        headed=attempt["headed"],
                    )
                    context.add_init_script(STEALTH_JS)
                    page = context.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=attempt["timeout"])
                        try:
                            page.wait_for_selector(
                                '[data-testid="product-content-container"], [data-testid="product-title"], [data-testid="product-button"]',
                                timeout=3_500,
                            )
                        except Exception:
                            pass

                        end_ts = time.time() + (settle_ms / 1000.0)
                        latest_html = page.content()
                        while time.time() < end_ts:
                            low = latest_html.lower()
                            if any(token in low for token in signal_patterns):
                                break
                            page.wait_for_timeout(settle_step_ms)
                            latest_html = page.content()

                        if latest_html and not self._looks_access_denied(latest_html):
                            return latest_html
                    finally:
                        page.close()
                        context.close()
            except Exception as e:
                logger.warning(f"Playwright fetch failed for {url}: {e}")
                continue
        return None

    def scrape_product(self, url: str) -> Optional[ProductStatus]:
        """Scrape a Kmart AU product page."""
        if "pokemon-tcg-example" in url.lower():
            return None

        html = None
        blocked_like = False
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            html = resp.text
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                return self._unavailable_status(url)
            if status_code in (401, 403):
                blocked_like = True
                if e.response is not None and e.response.text:
                    html = e.response.text
            logger.debug(f"Kmart plain fetch HTTP error for {url}: {e}")
        except requests.exceptions.RequestException as e:
            logger.debug(f"Kmart plain fetch failed for {url}: {e}")
        except Exception as e:
            logger.debug(f"Kmart plain fetch failed for {url}: {e}")

        if not html:
            try:
                resp = self.session.get(url, timeout=20)
                resp.raise_for_status()
                html = resp.text
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                if status_code in (404, 410):
                    return self._unavailable_status(url)
                if status_code in (401, 403):
                    blocked_like = True
                    if e.response is not None and e.response.text:
                        html = e.response.text
                logger.debug(f"Kmart session fetch HTTP error for {url}: {e}")
            except requests.exceptions.RequestException as e:
                logger.debug(f"Kmart session fetch failed for {url}: {e}")
                return None
            except Exception as e:
                logger.debug(f"Kmart session fetch failed for {url}: {e}")
                return None

        # Kmart frequently serves Akamai Access Denied to raw HTTP clients.
        # Fall back to Playwright browser rendering when blocked.
        if not html or blocked_like or self._looks_access_denied(html):
            # Fast path: reuse last-known status to avoid expensive browser fetches.
            blocked_status = self._blocked_status_from_db(url)
            constructor_status = self._blocked_status_from_constructor(url, blocked_status)
            if constructor_status is not None:
                blocked_status = constructor_status
            find_store_status = self._blocked_status_from_find_in_store(url, blocked_status)
            if find_store_status is not None:
                blocked_status = find_store_status
            needs_scope_refresh = bool(
                blocked_status is not None and blocked_status.availability_scope == "unknown"
            )
            if blocked_status is not None and not needs_scope_refresh:
                return blocked_status

            if os.getenv("KMART_SKIP_PLAYWRIGHT", "").lower() in {"1", "true", "yes"}:
                return blocked_status

            browser_html = self._fetch_with_playwright_fallback(url)
            if browser_html:
                html = browser_html
            elif blocked_status is not None:
                return blocked_status
            elif blocked_like:
                # 403/Akamai block is not a delisted product.
                # Return last-known status if available; otherwise keep scrape failure.
                try:
                    import playwright  # noqa: F401
                except Exception:
                    logger.warning(
                        "Kmart blocked and Playwright not available in this Python env. "
                        "Install in venv: pip install playwright && python -m playwright install chromium"
                    )
                return self._blocked_status_from_db(url)
            elif not html:
                return None
            elif self._looks_access_denied(html):
                return self._blocked_status_from_db(url)

        soup = BeautifulSoup(html, "lxml")
        product_data = self._extract_jsonld_product_data(soup)
        offers = self._extract_offer(product_data)
        overall_status, lifecycle = self._map_availability(str(offers.get("availability", "")))
        release_date = None
        availability_scope = "unknown"
        callout_status, callout_lifecycle, callout_release_date, callout_scope = (
            self._extract_from_callout_notifications(soup)
        )
        badge_status, badge_lifecycle, badge_release_date, badge_scope = self._extract_from_status_badges(soup)
        availability_scope = self._merge_scopes(callout_scope, badge_scope)
        if callout_status is not None or callout_lifecycle is not None:
            overall_status = callout_status or overall_status
            lifecycle = callout_lifecycle or lifecycle
            release_date = callout_release_date
        if badge_status is not None or badge_lifecycle is not None:
            overall_status = badge_status or overall_status
            lifecycle = badge_lifecycle or lifecycle
            release_date = badge_release_date or release_date
        if overall_status is None and lifecycle is None:
            overall_status, lifecycle = self._extract_from_text(soup)
        if availability_scope == "unknown":
            availability_scope = infer_availability_scope_from_text(
                soup.get_text(" ", strip=True)
            )

        overall_key = self._normalize_status_token(overall_status)
        lifecycle_key = self._normalize_status_token(lifecycle)
        is_preorder = lifecycle_key == "preorder" or overall_key == "preorder"
        in_stock = is_preorder or overall_key in ("instock", "limitedavailability")

        # Static metadata (name/image) is hydrated centrally in BaseMonitor.prepare_status.
        name = "Unknown Product"

        # ── Price ────────────────────────────────────────────────────
        price = None
        price_str = None

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
            dom_price = self._extract_price_from_soup(soup)
            if dom_price is not None:
                price = dom_price
                price_str = f"${price:.2f}"

        # ── Stock Status ─────────────────────────────────────────────
        if is_preorder:
            stock_text = f"Pre-order — {release_date}" if release_date else "Pre-order"
        elif overall_key == "limitedavailability":
            stock_text = "Limited stock"
        elif overall_key == "instock":
            stock_text = "In Stock"
        elif overall_key == "outofstock":
            stock_text = "Out of Stock"
        else:
            stock_text = "Unknown"

        # If the static snapshot is too weak, do one browser retry and re-resolve
        # availability/channel from late-rendered DOM callouts.
        should_retry_dynamic = (
            availability_scope == "unknown"
            and overall_key is None
            and os.getenv("KMART_SKIP_PLAYWRIGHT", "").lower() not in {"1", "true", "yes"}
        )
        if should_retry_dynamic:
            browser_html = self._fetch_with_playwright_fallback(url)
            if browser_html and browser_html != html:
                browser_soup = BeautifulSoup(browser_html, "lxml")
                c_status, c_lifecycle, c_release_date, c_scope = (
                    self._extract_from_callout_notifications(browser_soup)
                )
                b_status, b_lifecycle, b_release_date, b_scope = (
                    self._extract_from_status_badges(browser_soup)
                )
                availability_scope = self._merge_scopes(c_scope, b_scope)

                if c_status is not None or c_lifecycle is not None:
                    overall_status = c_status or overall_status
                    lifecycle = c_lifecycle or lifecycle
                    release_date = c_release_date or release_date
                if b_status is not None or b_lifecycle is not None:
                    overall_status = b_status or overall_status
                    lifecycle = b_lifecycle or lifecycle
                    release_date = b_release_date or release_date
                if (overall_status is None and lifecycle is None) or availability_scope == "unknown":
                    t_status, t_lifecycle = self._extract_from_text(browser_soup)
                    overall_status = overall_status or t_status
                    lifecycle = lifecycle or t_lifecycle
                    if availability_scope == "unknown":
                        availability_scope = infer_availability_scope_from_text(
                            browser_soup.get_text(" ", strip=True)
                        )

                overall_key = self._normalize_status_token(overall_status)
                lifecycle_key = self._normalize_status_token(lifecycle)
                is_preorder = lifecycle_key == "preorder" or overall_key == "preorder"
                in_stock = is_preorder or overall_key in ("instock", "limitedavailability")

                if is_preorder:
                    stock_text = f"Pre-order — {release_date}" if release_date else "Pre-order"
                elif overall_key == "limitedavailability":
                    stock_text = "Limited stock"
                elif overall_key == "instock":
                    stock_text = "In Stock"
                elif overall_key == "outofstock":
                    stock_text = "Out of Stock"
                else:
                    stock_text = "Unknown"

                if not price:
                    browser_price = self._extract_price_from_soup(browser_soup)
                    if browser_price is not None:
                        price = browser_price
                        price_str = f"${price:.2f}"

        image_url = None

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
