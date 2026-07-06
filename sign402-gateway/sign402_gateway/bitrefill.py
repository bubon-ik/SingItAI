import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from decimal import Decimal
from typing import Any, Callable, Protocol


class BitrefillClient(Protocol):
    def list_products(
        self,
        *,
        country: str,
        category: str,
        start: int,
        limit: int,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        ...

    def search_products(
        self,
        *,
        query: str,
        country: str,
        category: str,
        product_type: str,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        ...

    def get_product_details(self, *, product_id: str, country: str) -> dict[str, Any]:
        ...

    def quote_product(
        self,
        *,
        product_id: str,
        package_id: str,
        country: str,
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def buy_product(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        ...

    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        ...


TEST_PRODUCTS: dict[str, dict[str, Any]] = {
    "test-gift-card-link": {
        "productId": "test-gift-card-link",
        "name": "Test Gift Card Link",
        "country": "US",
        "currency": "USD",
        "category": "gift_card",
        "productType": "gift_card",
        "recipientType": "none",
        "requiredRecipientFields": [],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
    "test-gift-card-code": {
        "productId": "test-gift-card-code",
        "name": "Test Gift Card Code",
        "country": "US",
        "currency": "USD",
        "category": "gift_card",
        "productType": "gift_card",
        "recipientType": "none",
        "requiredRecipientFields": [],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
    "test-phone-refill": {
        "productId": "test-phone-refill",
        "name": "Test Phone Refill",
        "country": "US",
        "currency": "USD",
        "category": "refill",
        "productType": "phone_refill",
        "recipientType": "phone",
        "requiredRecipientFields": ["phone"],
        "packages": [{"packageId": "1", "value": "1", "priceUsd": "1.00"}],
    },
}


class TestBitrefillClient:
    __test__ = False

    def list_products(
        self,
        *,
        country: str,
        category: str,
        start: int,
        limit: int,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        if not include_test_products:
            return []
        country_filters = {
            value.strip().casefold() for value in str(country).split(",") if value.strip()
        }
        category_filters = {
            value.strip().casefold() for value in str(category).split(",") if value.strip()
        }
        matches = [
            deepcopy(product)
            for product in TEST_PRODUCTS.values()
            if (
                not country_filters
                or str(product["country"]).casefold() in country_filters
            )
            and (
                not category_filters
                or str(product["category"]).casefold() in category_filters
            )
        ]
        return matches[start : start + limit]

    def search_products(
        self,
        *,
        query: str,
        country: str,
        category: str,
        product_type: str,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        if not include_test_products:
            return []
        query_text = str(query).strip().lower()
        country_text = str(country).strip().upper()
        category_text = str(category).strip().lower()
        product_type_text = str(product_type).strip().lower()
        matches = []
        for product in TEST_PRODUCTS.values():
            searchable = " ".join(
                str(product[key]).lower()
                for key in ("productId", "name", "category", "productType")
            )
            if query_text and query_text not in searchable:
                continue
            if country_text and country_text != product["country"]:
                continue
            if category_text and category_text != product["category"]:
                continue
            if product_type_text and product_type_text != product["productType"]:
                continue
            matches.append(deepcopy(product))
        return matches

    def get_product_details(self, *, product_id: str, country: str) -> dict[str, Any]:
        product = TEST_PRODUCTS.get(str(product_id).strip())
        if product is None:
            raise ValueError("unknown Bitrefill product")
        requested_country = str(country).strip().upper()
        if requested_country and requested_country != product["country"]:
            raise ValueError("Bitrefill product is not available in the requested country")
        return deepcopy(product)

    def quote_product(
        self,
        *,
        product_id: str,
        package_id: str,
        country: str,
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        product = self.get_product_details(product_id=product_id, country=country)
        selected_package = next(
            (
                package
                for package in product["packages"]
                if str(package["packageId"]) == str(package_id).strip()
            ),
            None,
        )
        if selected_package is None:
            raise ValueError("unknown Bitrefill package")
        for field in product["requiredRecipientFields"]:
            if not str(recipient.get(field, "")).strip():
                raise ValueError(f"recipient.{field} is required")
        return {
            "productId": product["productId"],
            "name": product["name"],
            "productType": product["productType"],
            "packageId": selected_package["packageId"],
            "packageValue": selected_package["value"],
            "country": product["country"],
            "currency": product["currency"],
            "priceUsd": selected_package["priceUsd"],
            "recipientType": product["recipientType"],
            "requiredRecipientFields": deepcopy(product["requiredRecipientFields"]),
        }

    def buy_product(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        order_seed = f"{quote['quoteId']}:{quote['productId']}:{quote['packageId']}"
        order_id = "test_bitrefill_" + hashlib.sha256(order_seed.encode("utf-8")).hexdigest()[:16]
        return {
            "ok": True,
            "provider": "bitrefill-test",
            "orderId": order_id,
            "invoiceId": "test_invoice_" + order_id[-8:],
            "status": "delivered",
            "redemption": {
                "type": "test",
                "label": "Bitrefill test fulfillment",
                "value": "TEST-REDEMPTION-NO-VALUE",
            },
        }

    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        return deepcopy(provider_result)


DryRunBitrefillClient = TestBitrefillClient


class LiveBitrefillClient:
    __test__ = False

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.bitrefill.com/v2",
        max_purchase_usd: str = "5.00",
        max_invoice_overage_bps: int = 500,
        payment_method: str = "balance",
        refund_address: str = "",
        treasury_client: Any | None = None,
        invoice_poll_attempts: int = 12,
        invoice_poll_interval_seconds: float = 5.0,
        sleeper: Any | None = None,
        request_json: Any | None = None,
    ):
        key = str(api_key).strip()
        if not key:
            raise ValueError("BITREFILL_API_KEY is required")
        self.api_key = key
        self.base_url = str(base_url).strip().rstrip("/")
        self.max_purchase_usd = Decimal(str(max_purchase_usd))
        if self.max_purchase_usd <= 0:
            raise ValueError("SIGN402_BITREFILL_LIVE_MAX_USD must be positive")
        self.max_invoice_overage_bps = int(max_invoice_overage_bps)
        if self.max_invoice_overage_bps < 0:
            raise ValueError("max_invoice_overage_bps must be non-negative")
        self.payment_method = str(payment_method).strip().lower() or "balance"
        if self.payment_method not in {"balance", "usdc_base"}:
            raise ValueError("unsupported Bitrefill payment method")
        self.refund_address = str(refund_address).strip()
        self.treasury_client = treasury_client
        self.invoice_poll_attempts = int(invoice_poll_attempts)
        self.invoice_poll_interval_seconds = float(invoice_poll_interval_seconds)
        self.sleeper = sleeper or time.sleep
        self._request_json = request_json or self._default_request_json

    def list_products(
        self,
        *,
        country: str,
        category: str,
        start: int,
        limit: int,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            "/products",
            query={
                "country": str(country).strip(),
                "category": str(category).strip(),
                "start": str(start),
                "limit": str(limit),
                "include_test_products": "true" if include_test_products else "false",
            },
            body=None,
        )
        return [self._normalize_product(raw) for raw in payload.get("data", [])]

    def search_products(
        self,
        *,
        query: str,
        country: str,
        category: str,
        product_type: str,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            "/products/search",
            query={
                "q": str(query).strip() or "gift card",
                "limit": "50",
                "include_test_products": "true" if include_test_products else "false",
            },
            body=None,
        )
        country_filter = str(country).strip().upper()
        category_filter = str(category).strip().lower()
        type_filter = str(product_type).strip().lower()
        products = []
        for raw in payload.get("data", []):
            product = self._normalize_product(raw)
            if country_filter and product["country"] != country_filter:
                continue
            if category_filter and product["category"] != category_filter:
                continue
            if type_filter and product["productType"] != type_filter:
                continue
            products.append(product)
        return products

    def get_product_details(self, *, product_id: str, country: str) -> dict[str, Any]:
        product_id_text = str(product_id).strip()
        if not product_id_text:
            raise ValueError("productId is required")
        payload = self._request_json(
            "GET",
            f"/products/{urllib.parse.quote(product_id_text, safe='')}",
            query={},
            body=None,
        )
        product = self._normalize_product(payload.get("data", {}))
        requested_country = str(country).strip().upper()
        if requested_country and product["country"] != requested_country:
            raise ValueError("Bitrefill product is not available in the requested country")
        return product

    def quote_product(
        self,
        *,
        product_id: str,
        package_id: str,
        country: str,
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        product = self.get_product_details(product_id=product_id, country=country)
        selected_package = self._select_package(product=product, package_id=package_id)
        for field in product["requiredRecipientFields"]:
            if not str(recipient.get(field, "")).strip():
                raise ValueError(f"recipient.{field} is required")
        price_usd = Decimal(str(selected_package["priceUsd"]))
        if price_usd > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill quote exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        return {
            "productId": product["productId"],
            "name": product["name"],
            "productType": product["productType"],
            "packageId": selected_package["packageId"],
            "packageValue": selected_package["value"],
            "country": product["country"],
            "currency": product["currency"],
            "priceUsd": selected_package["priceUsd"],
            "recipientType": product["recipientType"],
            "requiredRecipientFields": deepcopy(product["requiredRecipientFields"]),
        }

    def buy_product(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        price_usd = Decimal(str(quote["priceUsd"]))
        if price_usd > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill quote exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        item: dict[str, Any] = {
            "product_id": str(quote["productId"]),
            "quantity": 1,
        }
        package_id = str(quote["packageId"])
        if "<&>" in package_id:
            item["package_id"] = package_id
        else:
            item["value"] = _decimal_to_json_number(str(quote["packageValue"]))
        phone = str(recipient.get("phone", "")).strip()
        if phone:
            item["phone_number"] = phone

        if self.payment_method == "usdc_base":
            return self._buy_product_with_usdc_base_invoice(
                item=item,
                quote=quote,
                checkpoint_callback=checkpoint_callback,
            )
        return self._buy_product_with_balance_invoice(
            item=item,
            quote=quote,
            checkpoint_callback=checkpoint_callback,
        )

    def _buy_product_with_balance_invoice(
        self,
        *,
        item: dict[str, Any],
        quote: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        invoice_payload = self._request_json(
            "POST",
            "/invoices",
            query={},
            body={
                "products": [item],
                "payment_method": "balance",
                "auto_pay": True,
            },
        )
        invoice = invoice_payload.get("data", {})
        self._checkpoint(
            checkpoint_callback,
            invoice=invoice,
            treasury_payment=None,
        )
        order = self._fetch_first_order(invoice, product_type=str(quote.get("productType", "")))
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            order=order,
            treasury_payment=None,
        )

    def _buy_product_with_usdc_base_invoice(
        self,
        *,
        item: dict[str, Any],
        quote: dict[str, Any],
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not self.refund_address:
            raise ValueError("SIGN402_BITREFILL_REFUND_ADDRESS is required for usdc_base")
        if self.treasury_client is None:
            raise ValueError("treasury_client is required for usdc_base")
        invoice_payload = self._request_json(
            "POST",
            "/invoices",
            query={},
            body={
                "products": [item],
                "payment_method": "usdc_base",
                "refund_address": self.refund_address,
            },
        )
        invoice = invoice_payload.get("data", {})
        self._checkpoint(
            checkpoint_callback,
            invoice=invoice,
            treasury_payment=None,
        )
        invoice_id = str(invoice.get("id", "")).strip()
        if not invoice_id:
            raise ValueError("Bitrefill invoice id is missing")
        payment = invoice.get("payment") if isinstance(invoice.get("payment"), dict) else {}
        payment_address = str(payment.get("address", "")).strip()
        if not payment_address:
            raise ValueError("Bitrefill invoice payment address is missing")
        payment_currency = str(payment.get("currency", "")).strip().upper()
        if payment_currency and payment_currency not in {"USDC", "USD"}:
            raise ValueError(f"Bitrefill invoice expected USDC, got {payment_currency}")
        if payment.get("price") is None:
            raise ValueError("Bitrefill invoice payment price is missing")
        payment_amount = _payment_amount(payment["price"], currency=payment_currency)
        if Decimal(payment_amount) > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill invoice exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        quoted_price = Decimal(str(quote["priceUsd"]))
        invoice_cap = quoted_price * (
            Decimal(10_000 + self.max_invoice_overage_bps) / Decimal(10_000)
        )
        if Decimal(payment_amount) > invoice_cap:
            raise ValueError(
                f"Bitrefill invoice ${payment_amount} exceeds quoted price ${quoted_price} "
                f"by more than {self.max_invoice_overage_bps} bps"
            )
        treasury_payment = self.treasury_client.transfer_usdc(
            to_address=payment_address,
            amount=payment_amount,
            chain="base",
        )
        self._checkpoint(
            checkpoint_callback,
            invoice=invoice,
            treasury_payment=treasury_payment,
        )
        invoice = self._poll_invoice_until_orders(invoice_id)
        order = self._fetch_first_order(invoice, product_type=str(quote.get("productType", "")))
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            order=order,
            treasury_payment=treasury_payment,
        )

    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        invoice_id = str(provider_result.get("invoiceId", "")).strip()
        order_id = str(provider_result.get("orderId", "")).strip()
        invoice: dict[str, Any] = {"id": invoice_id, "orders": [{"id": order_id}] if order_id else []}
        if invoice_id:
            payload = self._request_json(
                "GET",
                f"/invoices/{urllib.parse.quote(invoice_id, safe='')}",
                query={},
                body=None,
            )
            fetched_invoice = payload.get("data", {})
            if isinstance(fetched_invoice, dict):
                invoice = fetched_invoice
        if invoice.get("orders"):
            order = self._fetch_first_order(invoice, product_type=str(quote.get("productType", "")))
        elif order_id:
            order = self._poll_order_until_usable(order_id, product_type=str(quote.get("productType", "")))
        else:
            raise ValueError("Bitrefill provider result is missing orderId")
        treasury_payment = (
            deepcopy(provider_result["treasuryPayment"])
            if isinstance(provider_result.get("treasuryPayment"), dict)
            else None
        )
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            order=order,
            treasury_payment=treasury_payment,
        )

    def _checkpoint(
        self,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None,
        *,
        invoice: dict[str, Any],
        treasury_payment: dict[str, Any] | None,
    ) -> None:
        if checkpoint_callback is None:
            return
        checkpoint = {
            "invoiceId": str(invoice.get("id", "")),
            "status": str(invoice.get("status", "")),
        }
        payment = invoice.get("payment")
        if isinstance(payment, dict):
            checkpoint["payment"] = deepcopy(payment)
        orders = invoice.get("orders")
        if isinstance(orders, list):
            checkpoint["orders"] = deepcopy(orders)
        if treasury_payment is not None:
            checkpoint["treasuryPayment"] = deepcopy(treasury_payment)
        checkpoint_callback(checkpoint)

    def _fetch_first_order(self, invoice: dict[str, Any], *, product_type: str) -> dict[str, Any]:
        orders = invoice.get("orders") if isinstance(invoice.get("orders"), list) else []
        if not orders:
            raise ValueError("Bitrefill invoice did not return an order")
        order_id = str(orders[0].get("id", "")).strip()
        if not order_id:
            raise ValueError("Bitrefill invoice order id is missing")
        return self._poll_order_until_usable(order_id, product_type=product_type)

    def _poll_order_until_usable(self, order_id: str, *, product_type: str) -> dict[str, Any]:
        attempts = max(1, self.invoice_poll_attempts)
        last_order: dict[str, Any] = {}
        for attempt in range(attempts):
            order_payload = self._request_json(
                "GET",
                f"/orders/{urllib.parse.quote(order_id, safe='')}",
                query={},
                body=None,
            )
            order = order_payload.get("data", {})
            if isinstance(order, dict):
                last_order = order
                status = str(order.get("status", "")).strip().lower()
                requires_redemption = product_type in {"gift_card", "esim"}
                if status == "delivered" and (
                    not requires_redemption or order.get("redemption_info") is not None
                ):
                    return order
            if attempt < attempts - 1 and self.invoice_poll_interval_seconds > 0:
                self.sleeper(self.invoice_poll_interval_seconds)
        return last_order

    def _poll_invoice_until_orders(self, invoice_id: str) -> dict[str, Any]:
        attempts = max(1, self.invoice_poll_attempts)
        last_invoice: dict[str, Any] = {}
        for attempt in range(attempts):
            payload = self._request_json(
                "GET",
                f"/invoices/{urllib.parse.quote(invoice_id, safe='')}",
                query={},
                body=None,
            )
            invoice = payload.get("data", {})
            if isinstance(invoice, dict):
                last_invoice = invoice
                status = str(invoice.get("status", "")).strip().lower()
                orders = invoice.get("orders") if isinstance(invoice.get("orders"), list) else []
                if orders and status in {
                    "complete",
                    "completed",
                    "delivered",
                    "all_delivered",
                    "not_delivered",
                }:
                    return invoice
            if attempt < attempts - 1 and self.invoice_poll_interval_seconds > 0:
                self.sleeper(self.invoice_poll_interval_seconds)
        status = str(last_invoice.get("status", "unknown"))
        raise ValueError(f"Bitrefill invoice {invoice_id} was not completed (status: {status})")

    def _provider_result(
        self,
        *,
        quote: dict[str, Any],
        invoice: dict[str, Any],
        order: dict[str, Any],
        treasury_payment: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result = {
            "ok": True,
            "provider": "bitrefill-live",
            "orderId": str(order.get("id") or ""),
            "invoiceId": str(invoice.get("id", "")),
            "status": str(order.get("status") or invoice.get("status") or "delivered"),
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": deepcopy(order.get("redemption_info")),
            },
        }
        if treasury_payment is not None:
            result["treasuryPayment"] = deepcopy(treasury_payment)
        return result

    def _default_request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query_text = urllib.parse.urlencode(query or {})
        url = f"{self.base_url}{path}"
        if query_text:
            url = f"{url}?{query_text}"
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded_body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Hermes-Sign402/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", "replace")
            raise ValueError(f"Bitrefill API error {exc.code}: {error_text}") from exc
        data = json.loads(text or "{}")
        if not isinstance(data, dict):
            raise ValueError("Bitrefill API returned a non-object response")
        return data

    def _normalize_product(self, raw: dict[str, Any]) -> dict[str, Any]:
        product_id = str(raw.get("id", "")).strip()
        if not product_id:
            raise ValueError("Bitrefill product id is missing")
        recipient_type = str(raw.get("recipient_type") or "none").strip().lower()
        product_type = _infer_product_type(product_id, str(raw.get("name", "")), recipient_type)
        currency = str(raw.get("currency") or "USD").upper()
        return {
            "productId": product_id,
            "name": str(raw.get("name") or product_id),
            "country": str(raw.get("country_code") or "").upper(),
            "currency": currency,
            "category": "refill" if product_type == "phone_refill" else product_type,
            "productType": product_type,
            "recipientType": recipient_type,
            "requiredRecipientFields": _recipient_fields(recipient_type),
            "packages": self._normalize_packages(raw, currency=currency),
            "inStock": bool(raw.get("in_stock", True)),
        }

    def _normalize_packages(
        self,
        raw: dict[str, Any],
        *,
        currency: str,
    ) -> list[dict[str, str]]:
        product_id = str(raw.get("id", "")).strip()
        packages = raw.get("packages")
        product_range = raw.get("range")
        is_usd_range = currency == "USD" and isinstance(product_range, dict)
        normalized = []
        package_items = packages if isinstance(packages, list) else []
        if isinstance(packages, dict) and packages.get("id"):
            package_items = [packages]
        for package in package_items:
            value = str(package.get("value") or package.get("package_value") or "").strip()
            package_id = str(
                package.get("id") or package.get("package_id") or f"{product_id}<&>{value}"
            ).strip()
            if not value and "<&>" in package_id:
                value = package_id.rsplit("<&>", 1)[1]
            if is_usd_range and package.get("amount") is not None:
                price = package["amount"]
            else:
                price = package.get("price", value)
            normalized.append(
                {
                    "packageId": package_id,
                    "value": value,
                    "priceUsd": _money(price),
                }
            )
        if is_usd_range and product_range.get("min") is not None:
            min_value = format(Decimal(str(product_range["min"])).normalize(), "f")
            if all(package["value"] != min_value for package in normalized):
                normalized.insert(
                    0,
                    {
                        "packageId": min_value,
                        "value": min_value,
                        "priceUsd": _money(min_value),
                    },
                )
        if normalized:
            return normalized
        if isinstance(product_range, dict) and product_range.get("min") is not None:
            min_value = str(product_range["min"])
            price_rate = Decimal(str(product_range.get("price_rate", "1")))
            price = Decimal(min_value) * price_rate
            return [{"packageId": min_value, "value": min_value, "priceUsd": _money(price)}]
        return []

    def _select_package(self, *, product: dict[str, Any], package_id: str) -> dict[str, str]:
        package_id_text = str(package_id).strip()
        for package in product["packages"]:
            if package["packageId"] == package_id_text or package["value"] == package_id_text:
                return package
        raise ValueError("unknown Bitrefill package")


def _infer_product_type(product_id: str, name: str, recipient_type: str) -> str:
    haystack = f"{product_id} {name}".lower()
    if recipient_type in {"phone", "phone_number", "mobile_number"}:
        return "phone_refill"
    if "esim" in haystack or "e-sim" in haystack:
        return "esim"
    return "gift_card"


def _recipient_fields(recipient_type: str) -> list[str]:
    if recipient_type in {"phone", "phone_number", "mobile_number"}:
        return ["phone"]
    if recipient_type in {"email", "account", "username"}:
        return [recipient_type]
    return []


def _money(value: Any) -> str:
    return f"{Decimal(str(value)):.2f}"


def _payment_amount(value: Any, *, currency: str) -> str:
    decimal = Decimal(str(value))
    if currency == "USDC" and decimal == decimal.to_integral_value() and abs(decimal) >= 1000:
        return _decimal_amount(decimal / Decimal("1000000"), decimal_places=6)
    if currency == "USDC":
        return _decimal_amount(decimal)
    return _money(value)


def _decimal_amount(value: Decimal, *, decimal_places: int | None = None) -> str:
    if decimal_places is not None:
        quantum = Decimal("1").scaleb(-decimal_places)
        return format(value.quantize(quantum), "f")
    return format(value.normalize(), "f")


def _decimal_to_json_number(value: str) -> int | float:
    decimal = Decimal(str(value))
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal)
