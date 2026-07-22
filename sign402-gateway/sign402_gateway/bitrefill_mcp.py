import asyncio
import json
import time
import urllib.parse
from copy import deepcopy
from decimal import Decimal
from typing import Any

import httpx
import toons
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .bitrefill import _infer_product_type, _money, _recipient_fields


MAX_MCP_RESPONSE_BYTES = 1024 * 1024


def decode_mcp_tool_result(
    result: Any,
    *,
    max_bytes: int = MAX_MCP_RESPONSE_BYTES,
) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        raise ValueError("Bitrefill MCP tool failed")

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return deepcopy(structured)

    text = "\n".join(
        str(block.text)
        for block in getattr(result, "content", [])
        if hasattr(block, "text")
    ).strip()
    if len(text.encode("utf-8")) > int(max_bytes):
        raise ValueError("Bitrefill MCP response is too large")

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = toons.loads(text)
        except Exception as exc:
            raise ValueError("Bitrefill MCP returned malformed data") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Bitrefill MCP returned a non-object response")
    return decoded


class McpToolCaller:
    def __init__(
        self,
        server_url: str,
        *,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = MAX_MCP_RESPONSE_BYTES,
    ):
        url = str(server_url).strip()
        if not url:
            raise ValueError("Bitrefill MCP server URL is required")
        self._server_url = url
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("Bitrefill MCP timeout must be positive")
        self.max_response_bytes = int(max_response_bytes)
        if self.max_response_bytes <= 0:
            raise ValueError("Bitrefill MCP response limit must be positive")

    def __repr__(self) -> str:
        return (
            "McpToolCaller(server_url='<redacted>', "
            f"timeout_seconds={self.timeout_seconds})"
        )

    def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return asyncio.run(self._call(tool_name, arguments))
        except Exception as exc:
            raise ValueError("Bitrefill MCP request failed") from exc

    async def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
        ) as http_client:
            async with streamable_http_client(
                self._server_url,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if tool_name not in {tool.name for tool in tools.tools}:
                        raise ValueError(
                            f"required Bitrefill MCP tool is unavailable: {tool_name}"
                        )
                    result = await session.call_tool(
                        tool_name,
                        arguments=deepcopy(arguments),
                    )
                    return decode_mcp_tool_result(
                        result,
                        max_bytes=self.max_response_bytes,
                    )


class McpBitrefillClient:
    __test__ = False

    def __init__(
        self,
        *,
        api_key: str,
        mcp_url: str = "https://api.bitrefill.com/mcp",
        max_purchase_usd: str = "5.00",
        max_invoice_overage_bps: int = 500,
        payment_method: str = "balance",
        treasury_client: Any | None = None,
        invoice_poll_attempts: int = 12,
        invoice_poll_interval_seconds: float = 5.0,
        sleeper: Any | None = None,
        call_tool: Any | None = None,
    ):
        key = str(api_key).strip()
        if not key:
            raise ValueError("BITREFILL_API_KEY is required")
        base_url = str(mcp_url).strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("SIGN402_BITREFILL_MCP_URL must use https")
        self.max_purchase_usd = Decimal(str(max_purchase_usd))
        if self.max_purchase_usd <= 0:
            raise ValueError("SIGN402_BITREFILL_LIVE_MAX_USD must be positive")
        self.max_invoice_overage_bps = int(max_invoice_overage_bps)
        if self.max_invoice_overage_bps < 0:
            raise ValueError("max_invoice_overage_bps must be non-negative")
        self.payment_method = str(payment_method).strip().lower() or "balance"
        if self.payment_method not in {"balance", "usdc_base"}:
            raise ValueError("unsupported Bitrefill payment method")
        self.treasury_client = treasury_client
        self.invoice_poll_attempts = int(invoice_poll_attempts)
        if self.invoice_poll_attempts <= 0:
            raise ValueError("invoice_poll_attempts must be positive")
        self.invoice_poll_interval_seconds = float(invoice_poll_interval_seconds)
        if self.invoice_poll_interval_seconds < 0:
            raise ValueError("invoice_poll_interval_seconds must be non-negative")
        self.sleeper = sleeper or time.sleep
        server_url = f"{base_url}/{urllib.parse.quote(key, safe='')}"
        self._call_tool = call_tool or McpToolCaller(server_url)

    def __repr__(self) -> str:
        return (
            "McpBitrefillClient(mcp_url='<redacted>', "
            f"payment_method={self.payment_method!r})"
        )

    def list_products(
        self,
        *,
        country: str,
        category: str,
        start: int,
        limit: int,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        countries = [
            item.strip().upper()
            for item in str(country).split(",")
            if item.strip()
        ] or [""]
        products: list[dict[str, Any]] = []
        for country_code in countries:
            arguments: dict[str, Any] = {
                "country": country_code,
                "include_test_products": bool(include_test_products),
                "per_page": 100,
            }
            payload = self._call_tool("search-products", arguments)
            products.extend(self._normalize_product_rows(payload))
        products = self._filter_products(
            products,
            countries=countries,
            category=category,
            product_type="",
        )
        return products[int(start) : int(start) + int(limit)]

    def search_products(
        self,
        *,
        query: str,
        country: str,
        category: str,
        product_type: str,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "query": str(query).strip(),
            "country": str(country).strip().upper(),
            "include_test_products": bool(include_test_products),
            "per_page": 100,
        }
        category_text = str(category).strip().lower()
        if category_text:
            arguments["category"] = category_text
        type_text = str(product_type).strip().lower()
        if type_text:
            arguments["type"] = type_text
        payload = self._call_tool("search-products", arguments)
        products = self._normalize_product_rows(payload)
        countries = [arguments["country"]] if arguments["country"] else []
        return self._filter_products(
            products,
            countries=countries,
            category=category_text,
            product_type=type_text,
        )

    def get_product_details(
        self,
        *,
        product_id: str,
        country: str,
    ) -> dict[str, Any]:
        product_id_text = str(product_id).strip()
        if not product_id_text:
            raise ValueError("productId is required")
        payload = self._call_tool(
            "get-product-details",
            {"product_id": product_id_text, "currency": "USD"},
        )
        raw = payload.get("product") if isinstance(payload.get("product"), dict) else payload
        product = self._normalize_product(raw)
        requested_country = str(country).strip().upper()
        if requested_country and product["country"] not in {requested_country, "XI", ""}:
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
        if product["requiresPrepayment"]:
            raise ValueError(
                "this Bitrefill product requires a prepayment form and is not supported yet"
            )
        package_id_text = str(package_id).strip()
        selected = next(
            (
                package
                for package in product["packages"]
                if package["packageId"] == package_id_text
                or package["value"] == package_id_text
            ),
            None,
        )
        if selected is None:
            raise ValueError("unknown Bitrefill package")
        for field in product["requiredRecipientFields"]:
            if not str(recipient.get(field, "")).strip():
                raise ValueError(f"recipient.{field} is required")
        price_usd = Decimal(str(selected["priceUsd"]))
        if price_usd > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill quote exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        return {
            "productId": product["productId"],
            "name": product["name"],
            "productType": product["productType"],
            "packageId": selected["packageId"],
            "packageValue": selected["value"],
            "country": product["country"],
            "currency": product["currency"],
            "priceUsd": selected["priceUsd"],
            "recipientType": product["recipientType"],
            "requiredRecipientFields": deepcopy(product["requiredRecipientFields"]),
        }

    def buy_product(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        checkpoint_callback: Any | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("MCP purchase support is not implemented")

    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError("MCP purchase refresh is not implemented")

    def _normalize_product_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("products")
        if rows is None:
            rows = payload.get("data")
        if isinstance(rows, dict):
            rows = rows.get("products", [])
        if not isinstance(rows, list):
            raise ValueError("Bitrefill MCP product list is missing")
        return [self._normalize_product(raw) for raw in rows if isinstance(raw, dict)]

    def _normalize_product(self, raw: dict[str, Any]) -> dict[str, Any]:
        product_id = str(
            raw.get("product_id") or raw.get("productId") or raw.get("id") or ""
        ).strip()
        if not product_id:
            raise ValueError("Bitrefill product id is missing")
        name = str(raw.get("name") or product_id)
        recipient_type = str(
            raw.get("recipient_type") or raw.get("recipientType") or "none"
        ).strip().lower()
        product_type = str(
            raw.get("product_type") or raw.get("productType") or ""
        ).strip().lower() or _infer_product_type(product_id, name, recipient_type)
        category = str(raw.get("category") or "").strip().lower()
        if not category:
            category = "refill" if product_type == "phone_refill" else product_type
        currency = str(raw.get("currency") or "USD").strip().upper()
        country = str(
            raw.get("country") or raw.get("country_code") or raw.get("countryCode") or ""
        ).strip().upper()
        return {
            "productId": product_id,
            "name": name,
            "country": country,
            "currency": currency,
            "category": category,
            "productType": product_type,
            "recipientType": recipient_type,
            "requiredRecipientFields": _recipient_fields(recipient_type),
            "packages": self._normalize_packages(raw, product_id=product_id),
            "inStock": bool(raw.get("in_stock", raw.get("inStock", True))),
            "requiresPrepayment": isinstance(raw.get("prepayment"), dict),
        }

    def _normalize_packages(
        self,
        raw: dict[str, Any],
        *,
        product_id: str,
    ) -> list[dict[str, str]]:
        packages = raw.get("packages")
        rows = packages if isinstance(packages, list) else []
        if isinstance(packages, dict):
            rows = [packages]
        normalized: list[dict[str, str]] = []
        for package in rows:
            if not isinstance(package, dict):
                continue
            value = str(
                package.get("package_value")
                or package.get("packageValue")
                or package.get("value")
                or ""
            ).strip()
            package_id = str(
                package.get("package_id")
                or package.get("packageId")
                or package.get("id")
                or f"{product_id}<&>{value}"
            ).strip()
            if not value and "<&>" in package_id:
                value = package_id.rsplit("<&>", 1)[1]
            price = (
                package.get("price_usd")
                or package.get("priceUsd")
                or package.get("price")
                or package.get("amount")
                or value
            )
            normalized.append(
                {
                    "packageId": package_id,
                    "value": value,
                    "priceUsd": _money(price),
                }
            )
        product_range = raw.get("range")
        if isinstance(product_range, dict) and product_range.get("min") is not None:
            minimum = format(Decimal(str(product_range["min"])).normalize(), "f")
            if all(package["value"] != minimum for package in normalized):
                normalized.insert(
                    0,
                    {
                        "packageId": minimum,
                        "value": minimum,
                        "priceUsd": _money(minimum),
                    },
                )
        return normalized

    def _filter_products(
        self,
        products: list[dict[str, Any]],
        *,
        countries: list[str],
        category: str,
        product_type: str,
    ) -> list[dict[str, Any]]:
        country_set = {item.upper() for item in countries if item}
        category_set = {
            item.strip().lower()
            for item in str(category).split(",")
            if item.strip()
        }
        type_text = str(product_type).strip().lower()
        return [
            product
            for product in products
            if (not country_set or product["country"] in country_set)
            and (not category_set or product["category"] in category_set)
            and (not type_text or product["productType"] == type_text)
        ]
