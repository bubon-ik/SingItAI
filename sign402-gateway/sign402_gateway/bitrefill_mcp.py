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

from .bankr_swap import BASE_USDC_MAINNET
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
            mcp_country = "" if country_code == "XI" else country_code
            arguments: dict[str, Any] = {
                "query": "*",
                "country": mcp_country,
                "include_test_products": bool(include_test_products),
                "per_page": 100,
            }
            payload = self._call_tool("search-products", arguments)
            products.extend(
                self._normalize_product_rows(
                    payload,
                    fallback_country=country_code,
                )
            )
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
        products = self._normalize_product_rows(
            payload,
            fallback_country=arguments["country"],
        )
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
        price_usd = Decimal(str(quote["priceUsd"]))
        if price_usd > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill quote exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        item: dict[str, Any] = {
            "product_id": str(quote["productId"]),
            "package_id": str(quote["packageValue"]),
        }
        refill_input = self._recipient_value(
            recipient,
            str(quote.get("recipientType", "")),
        )
        if refill_input:
            item["refill_input"] = refill_input
        invoice = self._normalize_invoice(
            self._call_tool(
                "buy-products",
                {
                    "cart_items": [item],
                    "payment_method": self.payment_method,
                    "return_payment_link": False,
                },
            )
        )
        invoice_id = self._invoice_id(invoice)
        if not invoice_id:
            raise ValueError("Bitrefill MCP invoice id is missing")
        treasury_payment = None
        self._checkpoint(
            checkpoint_callback,
            invoice=invoice,
            treasury_payment=None,
        )
        if self.payment_method == "usdc_base":
            treasury_payment = self._pay_usdc_invoice(invoice, quote=quote)
            self._checkpoint(
                checkpoint_callback,
                invoice=invoice,
                treasury_payment=treasury_payment,
            )
        invoice = self._poll_invoice(invoice_id)
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            treasury_payment=treasury_payment,
        )

    def refresh_purchase(
        self,
        provider_result: dict[str, Any],
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        invoice_id = str(provider_result.get("invoiceId", "")).strip()
        if not invoice_id:
            raise ValueError("Bitrefill provider result is missing invoiceId")
        invoice = self._normalize_invoice(
            self._call_tool("get-invoice-by-id", {"invoice_id": invoice_id})
        )
        treasury_payment = (
            deepcopy(provider_result["treasuryPayment"])
            if isinstance(provider_result.get("treasuryPayment"), dict)
            else None
        )
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            treasury_payment=treasury_payment,
        )

    def _recipient_value(
        self,
        recipient: dict[str, Any],
        recipient_type: str,
    ) -> str:
        normalized = str(recipient_type).strip().lower()
        keys = {
            "phone": ("phone",),
            "phone_number": ("phone",),
            "mobile_number": ("phone",),
            "email": ("email",),
            "account": ("account",),
            "username": ("username",),
        }.get(normalized, ())
        for key in keys:
            value = str(recipient.get(key, "")).strip()
            if value:
                return value
        return ""

    def _pay_usdc_invoice(
        self,
        invoice: dict[str, Any],
        *,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        if self.treasury_client is None:
            raise ValueError("treasury_client is required for usdc_base")
        payment = invoice.get("payment_info")
        if not isinstance(payment, dict):
            payment = invoice.get("paymentInfo")
        if not isinstance(payment, dict):
            raise ValueError("Bitrefill MCP payment info is missing")
        address = str(payment.get("address") or "").strip()
        if not address:
            raise ValueError("Bitrefill MCP payment address is missing")
        currency = str(
            payment.get("currency") or payment.get("asset") or ""
        ).strip().upper()
        if currency != "USDC":
            raise ValueError(f"Bitrefill MCP expected USDC, got {currency or 'unknown'}")
        network = str(
            payment.get("network")
            or payment.get("chain")
            or payment.get("chain_id")
            or payment.get("chainId")
            or ""
        ).strip().lower()
        if network and network not in {"base", "base-mainnet", "8453"}:
            raise ValueError("Bitrefill MCP payment network is not Base Mainnet")
        contract_address = str(
            payment.get("contract_address")
            or payment.get("contractAddress")
            or ""
        ).strip()
        if (
            contract_address
            and contract_address.casefold() != BASE_USDC_MAINNET.casefold()
        ):
            raise ValueError("Bitrefill MCP payment token is not Base USDC")
        raw_amount = (
            payment.get("amount")
            if payment.get("amount") is not None
            else payment.get("altcoinPrice", payment.get("altcoin_price"))
        )
        if raw_amount is None:
            raise ValueError("Bitrefill MCP payment amount is missing")
        payment_amount = Decimal(str(raw_amount))
        if payment_amount <= 0:
            raise ValueError("Bitrefill MCP payment amount must be positive")
        if payment_amount > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill invoice exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        quoted_price = Decimal(str(quote["priceUsd"]))
        invoice_cap = quoted_price * (
            Decimal(10_000 + self.max_invoice_overage_bps) / Decimal(10_000)
        )
        if payment_amount > invoice_cap:
            raise ValueError(
                f"Bitrefill invoice ${format(payment_amount, 'f')} exceeds quoted price "
                f"${format(quoted_price, 'f')} by more than "
                f"{self.max_invoice_overage_bps} bps"
            )
        return self.treasury_client.transfer_usdc(
            to_address=address,
            amount=format(payment_amount, "f"),
            chain="base",
        )

    def _poll_invoice(self, invoice_id: str) -> dict[str, Any]:
        last_invoice: dict[str, Any] = {"invoice_id": invoice_id}
        terminal_errors = {"blocked", "denied", "payment_error"}
        for attempt in range(self.invoice_poll_attempts):
            invoice = self._normalize_invoice(
                self._call_tool(
                    "get-invoice-by-id",
                    {"invoice_id": invoice_id},
                )
            )
            last_invoice = invoice
            status = self._invoice_status(invoice)
            if status in terminal_errors:
                raise ValueError(f"Bitrefill invoice {invoice_id} failed ({status})")
            if status in {"complete", "completed", "delivered", "all_delivered"}:
                return invoice
            if attempt < self.invoice_poll_attempts - 1 and self.invoice_poll_interval_seconds:
                self.sleeper(self.invoice_poll_interval_seconds)
        status = self._invoice_status(last_invoice) or "unknown"
        raise ValueError(
            f"Bitrefill invoice {invoice_id} was not completed (status: {status})"
        )

    def _normalize_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("invoice", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                return deepcopy(nested)
        return deepcopy(payload)

    def _invoice_id(self, invoice: dict[str, Any]) -> str:
        return str(
            invoice.get("invoice_id")
            or invoice.get("invoiceId")
            or invoice.get("id")
            or ""
        ).strip()

    def _invoice_status(self, invoice: dict[str, Any]) -> str:
        return str(invoice.get("status") or "").strip().lower()

    def _orders(self, invoice: dict[str, Any]) -> list[dict[str, Any]]:
        orders = invoice.get("orders")
        return [order for order in orders if isinstance(order, dict)] if isinstance(orders, list) else []

    def _provider_result(
        self,
        *,
        quote: dict[str, Any],
        invoice: dict[str, Any],
        treasury_payment: dict[str, Any] | None,
    ) -> dict[str, Any]:
        orders = self._orders(invoice)
        if not orders:
            raise ValueError("Bitrefill MCP invoice did not return an order")
        order = orders[0]
        order_id = str(
            order.get("order_id") or order.get("orderId") or order.get("id") or ""
        ).strip()
        if not order_id:
            raise ValueError("Bitrefill MCP order id is missing")
        status = str(order.get("status") or self._invoice_status(invoice)).strip().lower()
        if status in {"complete", "completed", "all_delivered"}:
            status = "delivered"
        redemption = order.get("redemption_info")
        if redemption is None:
            redemption = order.get("redemptionInfo")
        if redemption is None and order.get("esim_install_link"):
            redemption = {"esimInstallLink": order["esim_install_link"]}
        result: dict[str, Any] = {
            "ok": True,
            "provider": "bitrefill-mcp",
            "orderId": order_id,
            "invoiceId": self._invoice_id(invoice),
            "status": status,
            "redemption": {
                "type": "bitrefill",
                "label": "Bitrefill redemption",
                "value": deepcopy(redemption),
            },
        }
        if treasury_payment is not None:
            result["treasuryPayment"] = deepcopy(treasury_payment)
        return result

    def _checkpoint(
        self,
        callback: Any | None,
        *,
        invoice: dict[str, Any],
        treasury_payment: dict[str, Any] | None,
    ) -> None:
        if callback is None:
            return
        checkpoint: dict[str, Any] = {
            "invoiceId": self._invoice_id(invoice),
            "status": self._invoice_status(invoice),
            "orderIds": [
                str(order.get("order_id") or order.get("orderId") or order.get("id") or "")
                for order in self._orders(invoice)
                if order.get("order_id") or order.get("orderId") or order.get("id")
            ],
        }
        payment = invoice.get("payment_info")
        if not isinstance(payment, dict):
            payment = invoice.get("paymentInfo")
        if isinstance(payment, dict):
            checkpoint["paymentInfo"] = {
                key: deepcopy(payment[key])
                for key in (
                    "address",
                    "amount",
                    "currency",
                    "network",
                    "contract_address",
                    "contractAddress",
                )
                if key in payment
            }
        if treasury_payment is not None:
            checkpoint["treasuryPayment"] = deepcopy(treasury_payment)
        callback(checkpoint)

    def _normalize_product_rows(
        self,
        payload: dict[str, Any],
        *,
        fallback_country: str = "",
    ) -> list[dict[str, Any]]:
        rows = payload.get("products")
        if rows is None:
            rows = payload.get("data")
        if isinstance(rows, dict):
            rows = rows.get("products", [])
        if not isinstance(rows, list):
            raise ValueError("Bitrefill MCP product list is missing")
        return [
            self._normalize_product(raw, fallback_country=fallback_country)
            for raw in rows
            if isinstance(raw, dict)
        ]

    def _normalize_product(
        self,
        raw: dict[str, Any],
        *,
        fallback_country: str = "",
    ) -> dict[str, Any]:
        product_id = str(
            raw.get("product_id")
            or raw.get("productId")
            or raw.get("id")
            or raw.get("_id")
            or raw.get("slug")
            or ""
        ).strip()
        if not product_id:
            raise ValueError("Bitrefill product id is missing")
        name = str(raw.get("name") or product_id)
        recipient_type = str(
            raw.get("recipient_type") or raw.get("recipientType") or "none"
        ).strip().lower()
        raw_product_type = str(
            raw.get("product_type")
            or raw.get("productType")
            or raw.get("type")
            or ""
        ).strip().lower()
        product_type = {
            "giftcard": "gift_card",
            "giftcards": "gift_card",
            "gift_card": "gift_card",
            "refill": "phone_refill",
            "refills": "phone_refill",
            "esims": "esim",
        }.get(raw_product_type, raw_product_type)
        if not product_type:
            product_type = _infer_product_type(product_id, name, recipient_type)
        raw_categories = raw.get("categories")
        categories = (
            [
                str(value).strip().lower()
                for value in raw_categories
                if str(value).strip()
            ]
            if isinstance(raw_categories, list)
            else []
        )
        category = str(raw.get("category") or "").strip().lower()
        if not category and categories:
            category = categories[0]
        if not category:
            category = "refill" if product_type == "phone_refill" else product_type
        currency = str(raw.get("currency") or "USD").strip().upper()
        country = str(
            raw.get("country")
            or raw.get("country_code")
            or raw.get("countryCode")
            or ""
        ).strip().upper()
        raw_countries = raw.get("countries")
        countries = (
            [
                str(value).strip().upper()
                for value in raw_countries
                if str(value).strip()
            ]
            if isinstance(raw_countries, list)
            else []
        )
        fallback_country_text = str(fallback_country).strip().upper()
        if not country and fallback_country_text in countries:
            country = fallback_country_text
        elif not country and countries:
            country = countries[0]
        elif not country:
            country = fallback_country_text
        return {
            "productId": product_id,
            "name": name,
            "country": country,
            "currency": currency,
            "category": category,
            "categories": categories or [category],
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
                or package.get("payment_price")
                or package.get("paymentPrice")
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
            and (
                not category_set
                or bool(
                    category_set.intersection(
                        product.get("categories", [product["category"]])
                    )
                )
            )
            and (not type_text or product["productType"] == type_text)
        ]
