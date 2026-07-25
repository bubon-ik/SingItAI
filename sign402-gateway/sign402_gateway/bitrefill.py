import hashlib
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
            "gift_card" if value.strip().casefold() == "giftcard" else value.strip().casefold()
            for value in str(category).split(",")
            if value.strip()
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
        refreshed = deepcopy(provider_result)
        refreshed.update(
            {
                "ok": True,
                "provider": "bitrefill-test",
                "status": "delivered",
                "redemption": {
                    "type": "test",
                    "label": "Bitrefill test fulfillment",
                    "value": "TEST-REDEMPTION-NO-VALUE",
                },
            }
        )
        return refreshed


DryRunBitrefillClient = TestBitrefillClient


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
