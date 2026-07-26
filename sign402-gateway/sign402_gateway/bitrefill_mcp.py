import asyncio
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.parse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import toons
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .bankr_swap import BASE_USDC_MAINNET
from .bitrefill import _infer_product_type, _money, _recipient_fields
from .diagnostics import (
    log_hidden_detail,
    log_swallowed_failure,
    safe_provider_diagnostic,
)


logger = logging.getLogger(__name__)

MAX_MCP_RESPONSE_BYTES = 1024 * 1024
MAX_CATALOG_CACHE_BYTES = 5 * 1024 * 1024
MAX_CATALOG_CACHE_ENTRIES = 256
MAX_CATALOG_PRODUCTS_PER_ENTRY = 200


class _CatalogFlight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.error: Exception | None = None


def decode_mcp_tool_result(
    result: Any,
    *,
    max_bytes: int = MAX_MCP_RESPONSE_BYTES,
) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)):
        raw_detail = "\n".join(
            str(block.text)
            for block in getattr(result, "content", [])
            if hasattr(block, "text")
        ).strip()
        log_hidden_detail(
            logger,
            "Bitrefill MCP tool returned an error",
            json.dumps(
                safe_provider_diagnostic(raw_detail),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
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
            log_swallowed_failure(
                logger,
                "Bitrefill MCP request failed",
                exc,
                tool=tool_name,
            )
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
        catalog_call_tool: Any | None = None,
        catalog_cache_ttl_seconds: float | None = None,
        catalog_timeout_seconds: float | None = None,
        catalog_cache_path: str | Path | None = None,
        catalog_refresh_runner: Any | None = None,
        now_provider: Any | None = None,
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
        cache_ttl = (
            catalog_cache_ttl_seconds
            if catalog_cache_ttl_seconds is not None
            else os.environ.get(
                "SIGN402_BITREFILL_CATALOG_CACHE_TTL_SECONDS",
                "600",
            )
        )
        self.catalog_cache_ttl_seconds = float(cache_ttl)
        if self.catalog_cache_ttl_seconds <= 0:
            raise ValueError("catalog_cache_ttl_seconds must be positive")
        catalog_timeout = (
            catalog_timeout_seconds
            if catalog_timeout_seconds is not None
            else os.environ.get("SIGN402_BITREFILL_CATALOG_TIMEOUT_SECONDS", "8")
        )
        self.catalog_timeout_seconds = float(catalog_timeout)
        if self.catalog_timeout_seconds <= 0:
            raise ValueError("catalog_timeout_seconds must be positive")
        configured_cache_path = catalog_cache_path
        if configured_cache_path is None:
            configured_cache_path = os.environ.get(
                "SIGN402_BITREFILL_CATALOG_CACHE_PATH"
            )
        if (
            configured_cache_path is None
            and call_tool is None
            and catalog_call_tool is None
        ):
            configured_cache_path = "~/.sign402/bitrefill-catalog-cache.json"
        self.catalog_cache_path = (
            Path(configured_cache_path).expanduser()
            if configured_cache_path is not None
            else None
        )
        self._now = now_provider or time.time
        self._catalog_lock = threading.RLock()
        self._catalog_cache: dict[tuple[str, bool], dict[str, Any]] = {}
        self._catalog_flights: dict[tuple[str, bool], _CatalogFlight] = {}
        self._catalog_refresh_runner = (
            catalog_refresh_runner or self._default_catalog_refresh_runner
        )
        self._load_catalog_cache()
        self.sleeper = sleeper or time.sleep
        server_url = f"{base_url}/{urllib.parse.quote(key, safe='')}"
        self._call_tool = call_tool or McpToolCaller(server_url)
        if catalog_call_tool is not None:
            self._catalog_call_tool = catalog_call_tool
        elif call_tool is not None:
            self._catalog_call_tool = call_tool
        else:
            self._catalog_call_tool = McpToolCaller(
                server_url,
                timeout_seconds=self.catalog_timeout_seconds,
            )

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
        local_countries = [item for item in countries if item != "XI"]
        mcp_countries = local_countries or [""]
        products: list[dict[str, Any]] = []
        for mcp_country in mcp_countries:
            products.extend(
                self._catalog_snapshot(
                    mcp_country,
                    include_test_products=include_test_products,
                )
            )
        products = self._filter_products(
            products,
            countries=countries,
            category=category,
            product_type="",
        )
        return products[int(start) : int(start) + int(limit)]

    def _catalog_snapshot(
        self,
        country: str,
        *,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        normalized_country = str(country).strip().upper()
        key = (normalized_country, bool(include_test_products))
        stale_products: list[dict[str, Any]] | None = None
        refresh_flight: _CatalogFlight | None = None
        cold_flight: _CatalogFlight | None = None
        cold_owner = False
        with self._catalog_lock:
            cached = self._catalog_cache.get(key)
            if cached is not None:
                cached_products = deepcopy(cached["products"])
                if (
                    self._now() - float(cached["storedAt"])
                    <= self.catalog_cache_ttl_seconds
                ):
                    return cached_products
                stale_products = cached_products
                if key not in self._catalog_flights:
                    refresh_flight = _CatalogFlight()
                    self._catalog_flights[key] = refresh_flight
            else:
                cold_flight = self._catalog_flights.get(key)
                if cold_flight is None:
                    cold_flight = _CatalogFlight()
                    self._catalog_flights[key] = cold_flight
                    cold_owner = True
        if stale_products is not None:
            if refresh_flight is not None:
                try:
                    self._catalog_refresh_runner(
                        lambda: self._refresh_catalog_snapshot(key, refresh_flight)
                    )
                except Exception:
                    self._finish_catalog_flight(key, refresh_flight)
            return stale_products

        if cold_flight is None:
            raise RuntimeError("catalog flight was not initialized")
        if not cold_owner:
            if not cold_flight.event.wait(self.catalog_timeout_seconds + 1.0):
                raise ValueError("Bitrefill catalog request timed out")
            if cold_flight.error is not None:
                raise cold_flight.error
            with self._catalog_lock:
                completed = self._catalog_cache.get(key)
                if completed is None:
                    raise ValueError("Bitrefill catalog request failed")
                return deepcopy(completed["products"])
        try:
            products = self._fetch_catalog_snapshot(
                normalized_country,
                include_test_products=include_test_products,
            )
            self._store_catalog_snapshot(key, products)
            return products
        except Exception as exc:
            cold_flight.error = exc
            raise
        finally:
            self._finish_catalog_flight(key, cold_flight)

    def _fetch_catalog_snapshot(
        self,
        country: str,
        *,
        include_test_products: bool,
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "query": "*",
            "country": country,
            "include_test_products": bool(include_test_products),
            "per_page": 100,
        }
        payload = self._catalog_call_tool("search-products", arguments)
        products = self._normalize_product_rows(
            payload,
            fallback_country=country or "XI",
        )
        for product in products:
            product["packages"] = []
        return products

    def _store_catalog_snapshot(
        self,
        key: tuple[str, bool],
        products: list[dict[str, Any]],
    ) -> None:
        with self._catalog_lock:
            self._catalog_cache[key] = {
                "storedAt": float(self._now()),
                "products": deepcopy(products),
            }
            self._persist_catalog_cache()

    def _refresh_catalog_snapshot(
        self,
        key: tuple[str, bool],
        flight: _CatalogFlight,
    ) -> None:
        try:
            products = self._fetch_catalog_snapshot(
                key[0],
                include_test_products=key[1],
            )
            self._store_catalog_snapshot(key, products)
        except Exception as exc:
            flight.error = exc
        finally:
            self._finish_catalog_flight(key, flight)

    def _finish_catalog_flight(
        self,
        key: tuple[str, bool],
        flight: _CatalogFlight,
    ) -> None:
        with self._catalog_lock:
            if self._catalog_flights.get(key) is flight:
                self._catalog_flights.pop(key, None)
            flight.event.set()

    @staticmethod
    def _default_catalog_refresh_runner(callback: Any) -> None:
        threading.Thread(
            target=callback,
            name="bitrefill-catalog-refresh",
            daemon=True,
        ).start()

    def _load_catalog_cache(self) -> None:
        if self.catalog_cache_path is None or not self.catalog_cache_path.exists():
            return
        try:
            if self.catalog_cache_path.stat().st_size > MAX_CATALOG_CACHE_BYTES:
                return
            payload = json.loads(self.catalog_cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return
        entries = payload.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_CATALOG_CACHE_ENTRIES:
            return
        now = float(self._now())
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            country = str(entry.get("country") or "").strip().upper()
            if country and (
                len(country) != 2 or not country.isascii() or not country.isalpha()
            ):
                continue
            include_test_products = entry.get("includeTestProducts")
            if not isinstance(include_test_products, bool):
                continue
            stored_at = entry.get("storedAt")
            if isinstance(stored_at, bool) or not isinstance(stored_at, (int, float)):
                continue
            stored_at = float(stored_at)
            if not math.isfinite(stored_at) or stored_at < 0 or stored_at > now:
                continue
            products = entry.get("products")
            if (
                not isinstance(products, list)
                or len(products) > MAX_CATALOG_PRODUCTS_PER_ENTRY
                or any(not self._valid_cached_product(product) for product in products)
            ):
                continue
            self._catalog_cache[(country, include_test_products)] = {
                "storedAt": stored_at,
                "products": deepcopy(products),
            }

    @staticmethod
    def _valid_cached_product(product: Any) -> bool:
        if not isinstance(product, dict):
            return False
        if not str(product.get("productId") or "").strip():
            return False
        if not isinstance(product.get("categories"), list):
            return False
        return all(
            isinstance(product.get(key), expected_type)
            for key, expected_type in (
                ("name", str),
                ("country", str),
                ("currency", str),
                ("category", str),
                ("productType", str),
                ("recipientType", str),
                ("requiredRecipientFields", list),
                ("packages", list),
                ("inStock", bool),
                ("requiresPrepayment", bool),
            )
        )

    def _persist_catalog_cache(self) -> None:
        if self.catalog_cache_path is None:
            return
        try:
            self.catalog_cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        entries = [
            {
                "country": country,
                "includeTestProducts": include_test_products,
                "storedAt": entry["storedAt"],
                "products": entry["products"],
            }
            for (country, include_test_products), entry in self._catalog_cache.items()
        ]
        if len(entries) > MAX_CATALOG_CACHE_ENTRIES:
            return
        try:
            encoded = json.dumps(
                {"version": 1, "entries": entries},
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(encoded) > MAX_CATALOG_CACHE_BYTES:
            return
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.catalog_cache_path.parent,
                prefix=f".{self.catalog_cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.catalog_cache_path)
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

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

    def prepare_purchase(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        price_usd = Decimal(str(quote["priceUsd"]))
        if price_usd > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill quote exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        # `package_id` is deprecated in the Bitrefill MCP schema; the cart is
        # keyed by the denomination `get-product-details` reports as
        # `package_value`, which is exactly what the quote carries.
        item: dict[str, Any] = {
            "product_id": str(quote["productId"]),
            "package_value": str(quote["packageValue"]),
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
        return self._validated_invoice_snapshot(
            invoice,
            quote=quote,
        )

    def complete_purchase(
        self,
        *,
        quote: dict[str, Any],
        prepared: dict[str, Any],
        checkpoint_callback: Any | None = None,
    ) -> dict[str, Any]:
        validated_prepared = self._validate_prepared_purchase(
            prepared,
            quote=quote,
        )
        invoice_id = validated_prepared["invoiceId"]
        invoice = self._normalize_invoice(
            self._call_tool(
                "get-invoice-by-id",
                {"invoice_id": invoice_id},
            )
        )
        if self._invoice_id(invoice) != invoice_id:
            raise ValueError("Bitrefill MCP invoice id changed")
        reloaded = self._validated_invoice_snapshot(
            invoice,
            quote=quote,
            fallback=validated_prepared,
        )
        for key in (
            "invoiceId",
            "productId",
            "packageValue",
            "paymentMethod",
            "paymentAmount",
            "paymentAsset",
            "paymentNetwork",
        ):
            if key in validated_prepared or key in reloaded:
                if str(validated_prepared.get(key, "")) != str(
                    reloaded.get(key, "")
                ):
                    raise ValueError(
                        f"Bitrefill MCP prepared invoice {key} changed"
                    )

        treasury_payment = (
            deepcopy(validated_prepared["treasuryPayment"])
            if isinstance(validated_prepared.get("treasuryPayment"), dict)
            else None
        )
        invoice_status = self._invoice_status(invoice)
        if invoice_status in {"blocked", "denied", "payment_error"}:
            raise ValueError(
                f"Bitrefill invoice {invoice_id} failed ({invoice_status})"
            )
        if invoice_status in {
            "complete",
            "completed",
            "delivered",
            "all_delivered",
        }:
            return self._provider_result(
                quote=quote,
                invoice=invoice,
                treasury_payment=treasury_payment,
            )
        if self.payment_method == "usdc_base":
            treasury_payment = self._pay_usdc_invoice(invoice, quote=quote)
            if checkpoint_callback is not None:
                checkpoint_callback(
                    {
                        **validated_prepared,
                        "status": self._invoice_status(invoice),
                        "treasuryPayment": deepcopy(treasury_payment),
                    }
                )
        invoice = self._poll_invoice(invoice_id)
        return self._provider_result(
            quote=quote,
            invoice=invoice,
            treasury_payment=treasury_payment,
        )

    def buy_product(
        self,
        *,
        quote: dict[str, Any],
        recipient: dict[str, Any],
        checkpoint_callback: Any | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_purchase(
            quote=quote,
            recipient=recipient,
        )
        if checkpoint_callback is not None:
            checkpoint_callback(deepcopy(prepared))
        return self.complete_purchase(
            quote=quote,
            prepared=prepared,
            checkpoint_callback=checkpoint_callback,
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

    def _invoice_payment(self, invoice: dict[str, Any]) -> dict[str, Any]:
        payment = invoice.get("payment_info")
        if not isinstance(payment, dict):
            payment = invoice.get("paymentInfo")
        if not isinstance(payment, dict):
            raise ValueError("Bitrefill MCP payment info is missing")
        return payment

    def _invoice_item(self, invoice: dict[str, Any]) -> dict[str, Any] | None:
        for key in ("cart_items", "cartItems", "items"):
            items = invoice.get(key)
            if isinstance(items, list):
                return next(
                    (item for item in items if isinstance(item, dict)),
                    None,
                )
        cart = invoice.get("cart")
        if isinstance(cart, dict):
            for key in ("items", "cart_items", "cartItems"):
                items = cart.get(key)
                if isinstance(items, list):
                    return next(
                        (item for item in items if isinstance(item, dict)),
                        None,
                    )
        return None

    def _validated_payment_requirements(
        self,
        invoice: dict[str, Any],
        *,
        quote: dict[str, Any],
    ) -> dict[str, str]:
        payment = self._invoice_payment(invoice)
        address = str(payment.get("address") or "").strip()
        if not address:
            raise ValueError("Bitrefill MCP payment address is missing")
        currency = str(
            payment.get("currency") or payment.get("asset") or ""
        ).strip().upper()
        if currency != "USDC":
            raise ValueError(
                f"Bitrefill MCP expected USDC, got {currency or 'unknown'}"
            )
        network = str(
            payment.get("network")
            or payment.get("chain")
            or payment.get("chain_id")
            or payment.get("chainId")
            or "base"
        ).strip().lower()
        if network not in {"base", "base-mainnet", "8453"}:
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
        if not payment_amount.is_finite() or payment_amount <= 0:
            raise ValueError("Bitrefill MCP payment amount must be positive")
        if payment_amount > self.max_purchase_usd:
            raise ValueError(
                f"Bitrefill invoice exceeds live Bitrefill max ${self.max_purchase_usd}"
            )
        approved_total = Decimal(
            str(quote.get("totalUsd") or quote["priceUsd"])
        )
        if payment_amount > approved_total:
            raise ValueError(
                f"Bitrefill invoice ${format(payment_amount, 'f')} exceeds "
                f"approved total ${format(approved_total, 'f')}"
            )
        return {
            "address": address,
            "amount": format(payment_amount, "f"),
            "asset": "USDC",
            "network": "base",
        }

    def _validated_invoice_snapshot(
        self,
        invoice: dict[str, Any],
        *,
        quote: dict[str, Any],
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        invoice_id = self._invoice_id(invoice)
        if not invoice_id:
            raise ValueError("Bitrefill MCP invoice id is missing")
        provider_method = str(
            invoice.get("payment_method")
            or invoice.get("paymentMethod")
            or (fallback or {}).get("paymentMethod")
            or self.payment_method
        ).strip().lower()
        if provider_method != self.payment_method:
            raise ValueError("Bitrefill MCP payment method changed")

        item = self._invoice_item(invoice)
        if any(key in invoice for key in ("cart_items", "cartItems", "items")):
            if item is None:
                raise ValueError("Bitrefill MCP invoice cart item is missing")
        if item is not None:
            provider_product = str(
                item.get("product_id")
                or item.get("productId")
                or ""
            ).strip()
            provider_package = str(
                item.get("package_value")
                or item.get("packageValue")
                or ""
            ).strip()
            if provider_product and provider_product != str(quote["productId"]):
                raise ValueError("Bitrefill MCP invoice product changed")
            if provider_package and provider_package != str(quote["packageValue"]):
                raise ValueError("Bitrefill MCP invoice package changed")

        snapshot: dict[str, Any] = {
            "invoiceId": invoice_id,
            "status": self._invoice_status(invoice),
            "productId": str(quote["productId"]),
            "packageValue": str(quote["packageValue"]),
            "paymentMethod": self.payment_method,
        }
        if self.payment_method == "usdc_base":
            if isinstance(
                invoice.get("payment_info") or invoice.get("paymentInfo"),
                dict,
            ):
                payment = self._validated_payment_requirements(
                    invoice,
                    quote=quote,
                )
                snapshot.update(
                    {
                        "paymentAmount": payment["amount"],
                        "paymentAsset": payment["asset"],
                        "paymentNetwork": payment["network"],
                    }
                )
            elif fallback is not None:
                snapshot.update(
                    {
                        "paymentAmount": str(fallback["paymentAmount"]),
                        "paymentAsset": str(fallback["paymentAsset"]),
                        "paymentNetwork": str(fallback["paymentNetwork"]),
                    }
                )
            else:
                raise ValueError("Bitrefill MCP payment info is missing")
        return snapshot

    def _validate_prepared_purchase(
        self,
        prepared: dict[str, Any],
        *,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(prepared, dict):
            raise ValueError("prepared Bitrefill invoice is invalid")
        invoice_id = str(prepared.get("invoiceId", "")).strip()
        if not invoice_id:
            raise ValueError("prepared Bitrefill invoice id is missing")
        expected = {
            "productId": str(quote["productId"]),
            "packageValue": str(quote["packageValue"]),
            "paymentMethod": self.payment_method,
        }
        for key, value in expected.items():
            if str(prepared.get(key, "")).strip() != value:
                raise ValueError(f"prepared Bitrefill invoice {key} changed")
        if self.payment_method == "usdc_base":
            amount = Decimal(str(prepared.get("paymentAmount", "")))
            approved_total = Decimal(
                str(quote.get("totalUsd") or quote["priceUsd"])
            )
            if not amount.is_finite() or amount <= 0 or amount > approved_total:
                raise ValueError("prepared Bitrefill invoice amount is invalid")
            if str(prepared.get("paymentAsset", "")).upper() != "USDC":
                raise ValueError("prepared Bitrefill invoice asset changed")
            if str(prepared.get("paymentNetwork", "")).lower() != "base":
                raise ValueError("prepared Bitrefill invoice network changed")
        return deepcopy(prepared)

    def _pay_usdc_invoice(
        self,
        invoice: dict[str, Any],
        *,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        if self.treasury_client is None:
            raise ValueError("treasury_client is required for usdc_base")
        payment = self._validated_payment_requirements(invoice, quote=quote)
        transfer = self.treasury_client.transfer_usdc(
            to_address=payment["address"],
            amount=payment["amount"],
            chain="base",
        )
        if not isinstance(transfer, dict):
            raise ValueError("Bitrefill treasury transfer result is invalid")
        transaction_hash = str(
            transfer.get("txId")
            or transfer.get("transactionHash")
            or transfer.get("hash")
            or ""
        ).strip()
        if not transaction_hash:
            raise ValueError("Bitrefill treasury transfer hash is missing")
        return {
            "txId": transaction_hash,
            "network": "base",
            "asset": "USDC",
            "amount": payment["amount"],
        }

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
            "paymentMethod": self.payment_method,
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
            checkpoint["paymentInfo"] = {}
            if "amount" in payment:
                checkpoint["paymentInfo"]["amount"] = deepcopy(payment["amount"])
            asset = payment.get("asset", payment.get("currency"))
            if asset is not None:
                checkpoint["paymentInfo"]["asset"] = deepcopy(asset)
            if "network" in payment:
                checkpoint["paymentInfo"]["network"] = deepcopy(payment["network"])
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
