from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
import urllib.request


ROOT_DIR = Path(__file__).resolve().parents[2]
SIGN402_BRIDGE_DIR = ROOT_DIR / "sign402-bridge"
PAYMENT_EXECUTOR_DIR = ROOT_DIR / "payment-executor"
LIVE_DEMO_DIR = ROOT_DIR / "live-demo"
DEMO_RESOURCE_SERVER_DIR = ROOT_DIR / "demo-resource-server"
DEFAULT_EVENT_STORE_PATH = ROOT_DIR / "demo-dashboard" / "latest-run.json"
DEFAULT_AGENT_STATE_PATH = ROOT_DIR / "demo-dashboard" / "agent-state.json"
DEFAULT_BITREFILL_COMMERCE_STORE_PATH = ROOT_DIR / "demo-dashboard" / "bitrefill-orders.sqlite3"
DEFAULT_CDP_X402_SERVICE_DIR = ROOT_DIR / "cdp-x402-service"
DEFAULT_BASE_REPORT_URL = "http://127.0.0.1:4021/paid/sign402-report"
LOCAL_BANKR_CLI = ROOT_DIR / ".tools" / "bankr-cli" / "node_modules" / ".bin" / "bankr"
DEFAULT_BANKR_CLI = str(LOCAL_BANKR_CLI) if LOCAL_BANKR_CLI.exists() else "bankr"
DEFAULT_SINGIT_RISK_CHECK_URL = os.getenv(
    "SIGN402_SINGIT_RISK_CHECK_URL",
    "https://x402.bankr.bot/0x3b3e349e6cfee692b69d2c63ce86f7d444667d98/paid-risk-check",
)
DEFAULT_SINGIT_TOKEN_ADDRESS = os.getenv(
    "SIGN402_SINGIT_TOKEN_ADDRESS",
    "0xc2c1e0b7C401e6217193732272444D928646eba3",
)
DEFAULT_BANKR_BITREFILL_URL = os.getenv(
    "SIGN402_BANKR_BITREFILL_URL",
    "https://x402.bankr.bot/YOUR_WALLET/buy-bitrefill",
)
BASE_MAINNET_RPC_URL = os.getenv("SIGN402_BASE_RPC_URL", "https://mainnet.base.org")
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SINGIT_RISK_CHECK_REQUEST_BODY = {
    "paymentRequirements": {
        "scheme": "upto",
        "network": "eip155:8453",
        "asset": DEFAULT_SINGIT_TOKEN_ADDRESS,
        "maxAmountRequired": "10000000000000000000",
        "payTo": "0x1111111111111111111111111111111111111111",
        "resource": "https://merchant.example/protected",
        "extra": {
            "nonce": "sign402-singit-risk-check",
            "assetTransferMethod": "permit2",
        },
    }
}
BANKR_LLM_CREDITS_PURPOSE = "bankr_llm_credits_topup"
BANKR_LLM_CREDITS_RESOURCE = "bankr://llm-credits/top-up"
BANKR_LLM_CREDITS_RECEIVER = "bankr.llm"

for package_dir in (SIGN402_BRIDGE_DIR, PAYMENT_EXECUTOR_DIR, LIVE_DEMO_DIR, DEMO_RESOURCE_SERVER_DIR):
    package_path = str(package_dir)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)

from sign402_live.flow import build_payment_commitment
from sign402_live.http_resource import X402ResourceClient
from sign402_bridge.firefly import FireflyClient, find_firefly_port
from sign402_bridge.policy import canonicalize_policy, hash_policy
from sign402_executor.executor import build_x402_avm_payment_signature_header, execute_payment
from x402_demo.core import encode_payment_proof

from .bankr_swap import (
    BASE_USDC_MAINNET,
    BankrSwapClient,
    BankrWalletApiClient,
    load_bankr_api_key,
    usdc_balance_from_portfolio,
)
from .numeric import format_decimal
from .goplausible import fetch_x402_paid_resource, fetch_x402_payment_required, normalize_x402_payment_required
from .real_rate_pricing import RealRateSingitPricer

HEX_32_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BUY_TOOL_DUPLICATE_SUPPRESSION_SECONDS = 120


PAID_TOOLS: dict[str, dict[str, Any]] = {
    "goplausible.weather": {
        "id": "goplausible.weather",
        "name": "GoPlausible Weather",
        "kind": "external_x402_resource",
        "source": "goplausible-x402",
        "description": "Paid weather forecast API exposed through official x402 on Algorand.",
        "resourceUrl": "https://x402.goplausible.xyz/examples/weather",
        "command": "buy goplausible weather",
        "mcpStyleName": "get_weather",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "base.sign402.report": {
        "id": "base.sign402.report",
        "name": "Base Sign402 Report",
        "kind": "local_x402_resource",
        "source": "sign402-cdp-x402-service",
        "description": "Local Sign402 paid report settled through official x402 on Base Mainnet.",
        "resourceUrl": os.getenv("SIGN402_BASE_REPORT_URL", DEFAULT_BASE_REPORT_URL),
        "command": "buy base sign402 report",
        "mcpStyleName": "get_sign402_report",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "bankr.singit.risk_check": {
        "id": "bankr.singit.risk_check",
        "name": "SINGIT Risk Check",
        "kind": "external_x402_resource",
        "source": "bankr-x402-cloud",
        "description": "Sign402 risk analysis endpoint paid with SINGIT on Base through Bankr x402 Cloud.",
        "resourceUrl": DEFAULT_SINGIT_RISK_CHECK_URL,
        "requestBody": SINGIT_RISK_CHECK_REQUEST_BODY,
        "paymentContext": {
            "title": "SINGIT RISK",
            "subject": "Risk Check",
        },
        "command": "buy singit risk check",
        "mcpStyleName": "get_singit_risk_check",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "x402.twitter.profile": {
        "id": "x402.twitter.profile",
        "name": "X/Twitter Profile",
        "kind": "external_x402_resource",
        "source": "x402.twit.sh",
        "description": "Look up a Twitter/X profile by username through a Base USDC x402 endpoint.",
        "resourceUrl": "https://x402.twit.sh/users/by/username",
        "resourceUrlTemplate": "https://x402.twit.sh/users/by/username?username={username}",
        "templateFields": {
            "username": {
                "aliases": ["handle", "screenName"],
                "stripPrefix": "@",
                "required": True,
            },
        },
        "paymentContext": {
            "title": "X PROFILE",
            "subjectField": "username",
            "subjectPrefix": "@",
        },
        "command": "buy x profile <username>",
        "mcpStyleName": "get_x_profile",
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Twitter/X screen name without @.",
                },
            },
            "required": ["username"],
        },
    },
    "otto.crypto_news": {
        "id": "otto.crypto_news",
        "name": "Crypto News",
        "kind": "external_x402_resource",
        "source": "Otto AI",
        "description": "Real-time crypto market news with sentiment analysis and top headlines.",
        "resourceUrl": "https://x402.ottoai.services/crypto-news",
        "paymentContext": {
            "title": "CRYPTO NEWS",
            "subject": "Otto AI",
        },
        "command": "buy crypto news",
        "mcpStyleName": "get_crypto_news",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "otto.hyperliquid_market": {
        "id": "otto.hyperliquid_market",
        "name": "Hyperliquid Market",
        "kind": "external_x402_resource",
        "source": "Otto AI",
        "description": "Hyperliquid perpetuals market data: price, funding, open interest, leverage, and size specs.",
        "resourceUrl": "https://x402.ottoai.services/hyperliquid-market",
        "resourceUrlTemplate": "https://x402.ottoai.services/hyperliquid-market?asset={asset}",
        "templateFields": {
            "asset": {
                "aliases": ["symbol", "ticker"],
                "required": True,
                "transform": "upper",
            },
        },
        "paymentContext": {
            "title": "HYPERLIQUID",
            "subjectField": "asset",
        },
        "command": "buy hyperliquid <asset>",
        "mcpStyleName": "get_hyperliquid_market",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "Perpetual market ticker, e.g. BTC, ETH, SOL.",
                },
            },
            "required": ["asset"],
        },
    },
    "otto.funding_rates": {
        "id": "otto.funding_rates",
        "name": "Funding Rates",
        "kind": "external_x402_resource",
        "source": "Otto AI",
        "description": "Cross-venue funding rates, open interest, long/short ratios, whale positions, and liquidations.",
        "resourceUrl": "https://x402.ottoai.services/funding-rates",
        "resourceUrlTemplate": "https://x402.ottoai.services/funding-rates?symbol={symbol}",
        "templateFields": {
            "symbol": {
                "aliases": ["asset", "ticker"],
                "required": True,
                "transform": "upper",
            },
        },
        "paymentContext": {
            "title": "FUNDING",
            "subjectField": "symbol",
        },
        "command": "buy funding <symbol>",
        "mcpStyleName": "get_funding_rates",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Market ticker, e.g. BTC, ETH, SOL.",
                },
            },
            "required": ["symbol"],
        },
    },
    "onesource.ens": {
        "id": "onesource.ens",
        "name": "ENS Resolve",
        "kind": "external_x402_resource",
        "source": "OneSource",
        "description": "Resolve a .eth name into an address, or an address into its primary .eth name.",
        "resourceUrl": "https://skills.onesource.io/api/chain/ens/:input",
        "resourceUrlTemplate": "https://skills.onesource.io/api/chain/ens/{input}?network={network}",
        "templateFields": {
            "input": {
                "aliases": ["name", "ens", "address"],
                "required": True,
            },
            "network": {
                "default": "ethereum",
            },
        },
        "paymentContext": {
            "title": "ENS RESOLVE",
            "subjectField": "input",
        },
        "command": "buy ens <name>",
        "mcpStyleName": "resolve_ens",
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": ".eth name or Ethereum address.",
                },
                "network": {
                    "type": "string",
                    "description": "Ethereum network, defaults to ethereum.",
                },
            },
            "required": ["input"],
        },
    },
    "anchor.token_price": {
        "id": "anchor.token_price",
        "name": "Token Price",
        "kind": "external_x402_resource",
        "source": "Anchor x402",
        "description": "USD price for a major token by symbol.",
        "resourceUrl": "https://api.anchor-x402.com/v1/price/token",
        "resourceUrlTemplate": "https://api.anchor-x402.com/v1/price/token?symbol={symbol}",
        "templateFields": {
            "symbol": {
                "aliases": ["asset", "ticker"],
                "required": True,
                "transform": "upper",
            },
        },
        "paymentContext": {
            "title": "TOKEN PRICE",
            "subjectField": "symbol",
        },
        "command": "buy token price <symbol>",
        "mcpStyleName": "get_token_price",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Token ticker, e.g. ETH, BTC, SOL.",
                },
            },
            "required": ["symbol"],
        },
    },
}

PAID_TOOL_ALIASES = {
    "weather": "goplausible.weather",
    "goplausible-weather": "goplausible.weather",
    "goplausible_weather": "goplausible.weather",
    "get_weather": "goplausible.weather",
    "base-report": "base.sign402.report",
    "base_report": "base.sign402.report",
    "sign402-report": "base.sign402.report",
    "sign402_report": "base.sign402.report",
    "get_sign402_report": "base.sign402.report",
    "singit-risk": "bankr.singit.risk_check",
    "singit_risk": "bankr.singit.risk_check",
    "singit-risk-check": "bankr.singit.risk_check",
    "singit_risk_check": "bankr.singit.risk_check",
    "risk-check": "bankr.singit.risk_check",
    "risk_check": "bankr.singit.risk_check",
    "get_singit_risk_check": "bankr.singit.risk_check",
    "x-profile": "x402.twitter.profile",
    "x_profile": "x402.twitter.profile",
    "twitter-profile": "x402.twitter.profile",
    "twitter_profile": "x402.twitter.profile",
    "x402-twitter-profile": "x402.twitter.profile",
    "get_x_profile": "x402.twitter.profile",
    "crypto-news": "otto.crypto_news",
    "crypto_news": "otto.crypto_news",
    "news": "otto.crypto_news",
    "get_crypto_news": "otto.crypto_news",
    "hyperliquid": "otto.hyperliquid_market",
    "hyperliquid-market": "otto.hyperliquid_market",
    "hyperliquid_market": "otto.hyperliquid_market",
    "get_hyperliquid_market": "otto.hyperliquid_market",
    "funding": "otto.funding_rates",
    "funding-rates": "otto.funding_rates",
    "funding_rates": "otto.funding_rates",
    "get_funding_rates": "otto.funding_rates",
    "ens": "onesource.ens",
    "ens-resolve": "onesource.ens",
    "ens_resolve": "onesource.ens",
    "resolve_ens": "onesource.ens",
    "token-price": "anchor.token_price",
    "token_price": "anchor.token_price",
    "price": "anchor.token_price",
    "get_token_price": "anchor.token_price",
}


class Sign402GatewayHandler(BaseHTTPRequestHandler):
    server_version = "Sign402Gateway/0.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "sign402-gateway",
                    "endpoints": [
                        "/approve-policy",
                        "/approve-payment",
                        "/execute-payment",
                        "/events/latest",
                        "/agent/buy-probe",
                        "/agent/tools",
                        "/agent/inspect-tool",
                        "/agent/buy-tool",
                        "/agent/inspect-x402",
                        "/agent/buy-x402",
                        "/agent/inspect-llm-credits-topup",
                        "/agent/top-up-llm-credits",
                        "/agent/search-bitrefill",
                        "/agent/get-bitrefill-product",
                        "/agent/quote-bitrefill",
                        "/agent/buy-bitrefill",
                        "/agent/buy-wallet-bitrefill",
                        "/agent/get-bitrefill-order",
                    ],
                }
            )
            return
        if path == "/agent/tools":
            self._handle_agent_tools()
            return
        if path == "/events/latest":
            self._handle_get_latest_event()
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/approve-policy":
            self._handle_approve_policy()
            return
        if path == "/approve-payment":
            self._handle_approve_payment()
            return
        if path == "/execute-payment":
            self._handle_execute_payment()
            return
        if path == "/events/latest":
            self._handle_post_latest_event()
            return
        if path == "/agent/buy-probe":
            self._handle_agent_buy_probe()
            return
        if path == "/agent/inspect-tool":
            self._handle_agent_inspect_tool()
            return
        if path == "/agent/buy-tool":
            self._handle_agent_buy_tool()
            return
        if path == "/agent/inspect-x402":
            self._handle_agent_inspect_x402()
            return
        if path == "/agent/buy-x402":
            self._handle_agent_buy_x402()
            return
        if path == "/agent/inspect-llm-credits-topup":
            self._handle_agent_inspect_llm_credits_topup()
            return
        if path == "/agent/top-up-llm-credits":
            self._handle_agent_top_up_llm_credits()
            return
        if path == "/agent/search-bitrefill":
            self._handle_agent_search_bitrefill()
            return
        if path == "/agent/get-bitrefill-product":
            self._handle_agent_get_bitrefill_product()
            return
        if path == "/agent/quote-bitrefill":
            self._handle_agent_quote_bitrefill()
            return
        if path == "/agent/buy-bitrefill":
            self._handle_agent_buy_bitrefill()
            return
        if path == "/agent/buy-wallet-bitrefill":
            self._handle_agent_buy_wallet_bitrefill()
            return
        if path == "/agent/get-bitrefill-order":
            self._handle_agent_get_bitrefill_order()
            return
        if path == "/internal/fulfill-bitrefill":
            self._handle_internal_fulfill_bitrefill()
            return
        if path == "/internal/prepare-bitrefill-settlement":
            self._handle_internal_prepare_bitrefill_settlement()
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_approve_policy(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            policy = payload["policy"]
            if not isinstance(policy, dict):
                raise ValueError("policy must be an object")

            canonical = canonicalize_policy(policy)
            policy_hash = hash_policy(policy)
            approval = self.server.firefly.approve_payment_hash(policy_hash)

            if approval["approvedHash"] != policy_hash:
                raise ValueError("Firefly approved hash does not match policy hash.")

            response = {
                "approved": True,
                "policy": policy,
                "canonicalPolicy": canonical,
                "policyHash": policy_hash,
                "firefly": approval,
            }
            self.server.agent_state_store.write_policy(response)
            self._send_json(response)
        except Exception as exc:
            self._send_json({"approved": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_approve_payment(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            payment_hash = _read_hash(payload, "paymentHash")
            payment_commitment = payload.get("paymentCommitment")
            context_lines = _payment_context_lines(
                payment_commitment if isinstance(payment_commitment, dict) else None
            )
            approval = self.server.firefly.approve_payment_hash(
                payment_hash,
                context_lines=context_lines,
            )

            if approval.get("approved") and approval["approvedHash"] != payment_hash:
                raise ValueError("Firefly approved hash does not match payment hash.")

            self._send_json(
                {
                    "approved": bool(approval.get("approved")),
                    "paymentHash": payment_hash,
                    "firefly": approval,
                },
                status=200 if approval.get("approved") else 400,
            )
        except Exception as exc:
            self._send_json({"approved": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_execute_payment(self) -> None:
        try:
            payload = self._read_json()
            policy_hash = _read_hash(payload, "policyHash")
            payment_approval_hash = _read_hash(payload, "paymentApprovalHash")
            requirement = payload["paymentRequirements"]
            _validate_payment_requirements(requirement)

            payment = self.server.payment_executor(requirement, policy_hash)
            self._send_json(
                {
                    "ok": True,
                    "policyHash": policy_hash,
                    "paymentApprovalHash": payment_approval_hash,
                    "payment": payment,
                }
            )
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_get_latest_event(self) -> None:
        event = self.server.event_store.read()
        self._send_json({"ok": event is not None, "event": event})

    def _handle_post_latest_event(self) -> None:
        try:
            payload = self._read_json()
            event = payload.get("event", payload)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            saved_event = self.server.event_store.write(event)
            self._send_json({"ok": True, "event": saved_event})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_probe(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            target = str(payload.get("target", "")).strip()
            if not target:
                raise ValueError("target is required")

            result = self.server.agent_buy_probe(target)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_tools(self) -> None:
        self._send_json(
            {
                "ok": True,
                "mode": "paid_tool_catalog",
                "tools": list(PAID_TOOLS.values()),
                "nextStep": "POST /agent/inspect-tool with {\"tool\":\"goplausible.weather\"}, then POST /agent/buy-tool.",
            }
        )

    def _handle_agent_inspect_tool(self) -> None:
        try:
            payload = self._read_json()
            tool = _resolve_paid_tool(payload)
            resource_url = _paid_tool_resource_url(tool, payload)
            policy_hash = self._policy_hash_from_payload_or_state(payload)
            request_body = tool.get("requestBody") if isinstance(tool.get("requestBody"), dict) else None
            if request_body is not None:
                inspection = self.server.x402_inspector(resource_url, policy_hash, request_body=request_body)
            else:
                inspection = self.server.x402_inspector(resource_url, policy_hash)
            result = _tool_result(tool, inspection, resource_url)
            result["nextStep"] = "If acceptable, POST /agent/buy-tool with the same tool id. Firefly approval is required before payment."
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_tool(self) -> None:
        try:
            payload = self._read_json()
            tool = _resolve_paid_tool(payload)
            resource_url = _paid_tool_resource_url(tool, payload)
            cache_key = _buy_tool_cache_key(tool, resource_url)
            cached = self._read_buy_tool_cache(cache_key)
            if cached is not None:
                self._send_json(cached)
                return
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
            return

        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payment_context = _tool_payment_context(tool, payload)
            request_body = tool.get("requestBody") if isinstance(tool.get("requestBody"), dict) else None
            if payment_context:
                kwargs: dict[str, Any] = {"payment_context": payment_context}
                if request_body is not None:
                    kwargs["request_body"] = request_body
                result = self.server.x402_buyer(resource_url, **kwargs)
            else:
                if request_body is not None:
                    result = self.server.x402_buyer(resource_url, request_body=request_body)
                else:
                    result = self.server.x402_buyer(resource_url)
            enriched = _tool_result(tool, result, resource_url)
            enriched["decision"] = result.get("decision", "approved_and_executed")
            enriched["ok"] = bool(result.get("ok", False))
            if enriched.get("ok"):
                self.server.event_store.write(enriched)
            self._store_buy_tool_cache(cache_key, enriched)
            self._send_json(enriched)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_inspect_x402(self) -> None:
        try:
            payload = self._read_json()
            resource_url = str(payload.get("url", "")).strip()
            if not resource_url:
                raise ValueError("url is required")

            policy_hash = self._policy_hash_from_payload_or_state(payload)
            result = self.server.x402_inspector(resource_url, policy_hash)
            _validate_base_usdc_x402_requirement(result.get("paymentRequirements"))
            result["quoteText"] = _base_x402_quote_text(result["paymentRequirements"])
            result["nextStep"] = "If acceptable, POST /agent/buy-x402 with the same url. Firefly approval is required before payment."
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_x402(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            resource_url = str(payload.get("url", "")).strip()
            if not resource_url:
                raise ValueError("url is required")

            result = self.server.x402_buyer(
                resource_url,
                requirement_validator=_validate_base_usdc_x402_requirement,
            )
            if not result.get("telegramText"):
                telegram_text = _x402_telegram_text(result)
                if telegram_text:
                    result["telegramText"] = telegram_text
                    self.server.event_store.write(result)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_inspect_llm_credits_topup(self) -> None:
        try:
            payload = self._read_json()
            policy_hash = self._policy_hash_from_payload_or_state(payload)
            result = self.server.bankr_llm_topup_inspector(payload, policy_hash)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_top_up_llm_credits(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            result = self.server.bankr_llm_topup(payload)
            if result.get("ok"):
                self.server.event_store.write(result)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_quote_bitrefill(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.bitrefill_quote_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_search_bitrefill(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.bitrefill_search_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_get_bitrefill_product(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.bitrefill_product_details_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_bitrefill(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            result = self.server.bitrefill_purchase_runner(payload)
            if result.get("ok"):
                self.server.event_store.write(_without_fulfillment_token(result))
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_buy_wallet_bitrefill(self) -> None:
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            result = self.server.bitrefill_wallet_purchase_runner(payload)
            if result.get("ok"):
                self.server.event_store.write(_without_fulfillment_token(result))
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_get_bitrefill_order(self) -> None:
        try:
            payload = self._read_json()
            quote_id = str(payload.get("quoteId") or payload.get("orderId") or "").strip()
            if not quote_id:
                raise ValueError("quoteId is required")
            result = self.server.bitrefill_order_lookup(
                quote_id,
                include_redemption=bool(payload.get("includeRedemption", False)),
                recipient=payload.get("recipient") if isinstance(payload.get("recipient"), dict) else {},
                fulfillment_token=str(payload.get("fulfillmentToken", "")),
            )
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_internal_fulfill_bitrefill(self) -> None:
        if not self._service_secret_authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return
        if os.getenv("SIGN402_ALLOW_LEGACY_BANKR_FULFILLMENT", "").strip() != "1":
            self._send_json(
                {
                    "ok": False,
                    "error": "legacy fulfillment disabled; use /internal/prepare-bitrefill-settlement",
                },
                status=410,
            )
            return

        try:
            payload = self._read_json()
            result = self.server.bitrefill_fulfillment_runner(payload)
            redacted = {
                "ok": bool(result.get("ok", False)),
                "quoteId": result.get("quoteId"),
                "orderId": result.get("orderId"),
                "status": result.get("status"),
                "settleAmountAtomic": result.get("settleAmountAtomic"),
                "maxSingitAtomic": result.get("maxSingitAtomic"),
            }
            self._send_json(redacted, status=200 if redacted["ok"] else 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_internal_prepare_bitrefill_settlement(self) -> None:
        if not self._service_secret_authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        try:
            payload = self._read_json()
            result = self.server.bitrefill_settlement_preparation_runner(payload)
            redacted = {
                "ok": bool(result.get("ok", False)),
                "quoteId": result.get("quoteId"),
                "status": result.get("status"),
                "pricingMode": result.get("pricingMode"),
                "settleAmountAtomic": result.get("settleAmountAtomic"),
                "maxSingitAtomic": result.get("maxSingitAtomic"),
            }
            self._send_json(redacted, status=200 if redacted["ok"] else 400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _policy_hash_from_payload_or_state(self, payload: dict[str, Any]) -> str:
        if payload.get("policyHash"):
            return _read_hash(payload, "policyHash")

        policy_state = self.server.agent_state_store.read_policy()
        if policy_state is None:
            raise ValueError("policyHash is required when no policy is stored")
        policy_hash = str(policy_state.get("policyHash", "")).lower()
        if not HEX_32_RE.fullmatch(policy_hash):
            raise ValueError("stored policyHash must be 64 hex characters")
        return policy_hash

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if not body:
            raise ValueError("request body is empty")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _service_secret_authorized(self) -> bool:
        expected_secret = os.getenv("SIGN402_BANKR_FULFILLMENT_SECRET", "")
        if not expected_secret:
            return False
        authorization = str(self.headers.get("Authorization", ""))
        return hmac.compare_digest(authorization, f"Bearer {expected_secret}")

    def _acquire_firefly(self) -> bool:
        lock = getattr(self.server, "firefly_lock", None)
        if lock is not None:
            return lock.acquire(blocking=False)

        if getattr(self.server, "firefly_busy", False):
            return False

        self.server.firefly_busy = True
        return True

    def _release_firefly(self) -> None:
        lock = getattr(self.server, "firefly_lock", None)
        if lock is not None:
            lock.release()
            return

        self.server.firefly_busy = False

    def _read_buy_tool_cache(self, cache_key: str) -> dict[str, Any] | None:
        cache = getattr(self.server, "buy_tool_response_cache", None)
        if not isinstance(cache, dict):
            return None

        cached = cache.get(cache_key)
        if not isinstance(cached, dict):
            return None

        if time.time() - float(cached.get("storedAt", 0)) > BUY_TOOL_DUPLICATE_SUPPRESSION_SECONDS:
            cache.pop(cache_key, None)
            return None

        response = dict(cached.get("response") or {})
        response["duplicateSuppressed"] = True
        response["message"] = (
            "Duplicate buy-tool request suppressed. "
            "Returning the previous receipt without asking Firefly again."
        )
        return response

    def _store_buy_tool_cache(self, cache_key: str, response: dict[str, Any]) -> None:
        cache = getattr(self.server, "buy_tool_response_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.server.buy_tool_response_cache = cache

        cache[cache_key] = {
            "storedAt": time.time(),
            "response": dict(response),
        }


class Sign402GatewayServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        handler_class,
        *,
        firefly: FireflyClient,
        payment_executor: Callable[[dict[str, Any], str], dict[str, Any]],
        event_store: "LatestEventStore",
        agent_state_store: "AgentStateStore",
        agent_buy_probe: Callable[[str], dict[str, Any]],
        x402_inspector: Callable[[str, str], dict[str, Any]],
        x402_buyer: Callable[[str], dict[str, Any]],
        bankr_llm_topup_inspector: Callable[[dict[str, Any], str], dict[str, Any]],
        bankr_llm_topup: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_search_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_product_details_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_quote_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_purchase_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_wallet_purchase_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_order_lookup: Callable[..., dict[str, Any]],
        bitrefill_fulfillment_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_settlement_preparation_runner: Callable[[dict[str, Any]], dict[str, Any]],
    ):
        super().__init__(server_address, handler_class)
        self.firefly = firefly
        self.payment_executor = payment_executor
        self.event_store = event_store
        self.agent_state_store = agent_state_store
        self.agent_buy_probe = agent_buy_probe
        self.x402_inspector = x402_inspector
        self.x402_buyer = x402_buyer
        self.bankr_llm_topup_inspector = bankr_llm_topup_inspector
        self.bankr_llm_topup = bankr_llm_topup
        self.bitrefill_search_service = bitrefill_search_service
        self.bitrefill_product_details_service = bitrefill_product_details_service
        self.bitrefill_quote_service = bitrefill_quote_service
        self.bitrefill_purchase_runner = bitrefill_purchase_runner
        self.bitrefill_wallet_purchase_runner = bitrefill_wallet_purchase_runner
        self.bitrefill_order_lookup = bitrefill_order_lookup
        self.bitrefill_fulfillment_runner = bitrefill_fulfillment_runner
        self.bitrefill_settlement_preparation_runner = bitrefill_settlement_preparation_runner
        self.firefly_lock = threading.Lock()
        self.buy_tool_response_cache: dict[str, dict[str, Any]] = {}


def build_bitrefill_client_from_env(env: dict[str, str] | None = None):
    from .bitrefill import LiveBitrefillClient, TestBitrefillClient

    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_MODE", "test").strip().lower()
    if mode == "test":
        return TestBitrefillClient()
    if mode == "live":
        if not values.get("BITREFILL_API_KEY", "").strip():
            raise ValueError("BITREFILL_API_KEY is required in live Bitrefill mode")
        payment_method = values.get("SIGN402_BITREFILL_PAYMENT_METHOD", "balance").strip().lower()
        refund_address = (
            values.get("SIGN402_BITREFILL_REFUND_ADDRESS", "").strip()
            or values.get("SIGN402_TREASURY_REFUND_ADDRESS", "").strip()
        )
        treasury_client = None
        if payment_method == "usdc_base":
            if not refund_address:
                raise ValueError("SIGN402_BITREFILL_REFUND_ADDRESS is required for usdc_base")
            treasury_mode = values.get("SIGN402_BITREFILL_USDC_TREASURY_MODE", "bankr_wallet").strip().lower()
            if treasury_mode == "cdp_wallet":
                treasury_client = CdpWalletClient(
                    service_dir=Path(
                        values.get("SIGN402_CDP_X402_SERVICE_DIR", str(DEFAULT_CDP_X402_SERVICE_DIR))
                    )
                )
            elif treasury_mode == "bankr_wallet":
                treasury_client = BankrTreasuryClient()
            else:
                raise ValueError(f"unsupported SIGN402_BITREFILL_USDC_TREASURY_MODE: {treasury_mode}")
        return LiveBitrefillClient(
            api_key=values["BITREFILL_API_KEY"],
            base_url=values.get(
                "SIGN402_BITREFILL_BASE_URL",
                "https://api.bitrefill.com/v2",
            ),
            max_purchase_usd=values.get("SIGN402_BITREFILL_LIVE_MAX_USD", "5.00"),
            max_invoice_overage_bps=int(
                values.get("SIGN402_BITREFILL_LIVE_MAX_INVOICE_OVERAGE_BPS", "500")
            ),
            payment_method=payment_method,
            refund_address=refund_address,
            treasury_client=treasury_client,
        )
    raise ValueError(f"unsupported SIGN402_BITREFILL_MODE: {mode}")


def build_singit_settlement_verifier_from_env(
    env: dict[str, str] | None = None,
) -> SingitSettlementVerifier | None:
    values = os.environ if env is None else env
    if values.get("SIGN402_DISABLE_BANKR_BITREFILL_SETTLEMENT", "").strip() == "1":
        return None
    mode = values.get("SIGN402_BITREFILL_MODE", "test").strip().lower()
    if mode != "live":
        return SingitSettlementVerifier()
    payer_address = values.get("SIGN402_BANKR_WALLET_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", payer_address):
        raise ValueError("SIGN402_BANKR_WALLET_ADDRESS is required in live Bitrefill mode")
    token_address = values.get("SIGN402_SINGIT_TOKEN_ADDRESS", DEFAULT_SINGIT_TOKEN_ADDRESS).strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", token_address):
        raise ValueError("SIGN402_SINGIT_TOKEN_ADDRESS must be an EVM address")
    return SingitSettlementVerifier(
        transaction_resolver=BaseErc20TransactionResolver(),
        payer_address=payer_address,
        singit_token_address=token_address,
    )


def build_real_rate_pricer_from_env(env: dict[str, str] | None = None):
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_PRICING_MODE", "fixed").strip().lower()
    if mode != "bankr_real_rate":
        return None
    max_singit = values.get("SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER", "").strip()
    if not max_singit:
        raise ValueError(
            "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER is required for bankr_real_rate"
        )
    pricing_source = values.get("SIGN402_BITREFILL_PRICING_SOURCE", "").strip().lower()
    if pricing_source == "cdp_wallet":
        swap_client = CdpWalletClient(
            service_dir=Path(
                values.get("SIGN402_CDP_X402_SERVICE_DIR", str(DEFAULT_CDP_X402_SERVICE_DIR))
            )
        )
    else:
        bankr_api_key = load_bankr_api_key(values)
        if bankr_api_key:
            swap_client = BankrWalletApiClient(
                api_key=bankr_api_key,
                base_url=values.get("SIGN402_BANKR_API_BASE_URL", "https://api.bankr.bot"),
            )
        else:
            bankr_cli = values.get("SIGN402_BANKR_CLI", DEFAULT_BANKR_CLI)
            swap_client = BankrSwapClient(bankr_cli=bankr_cli)
    return RealRateSingitPricer(
        quote_client=swap_client,
        from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
        to_token=values.get("SIGN402_BANKR_SWAP_TO_TOKEN", "USDC"),
        chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
        buffer_bps=int(values.get("SIGN402_BITREFILL_USDC_BUFFER_BPS", "1000")),
        max_singit=max_singit,
    )


def build_bitrefill_funding_runner_from_env(env: dict[str, str] | None = None):
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_FUNDING_MODE", "none").strip().lower()
    if mode in {"", "none", "disabled"}:
        return None
    if mode == "bankr_wallet_api_swap":
        bankr_api_key = load_bankr_api_key(values)
        if not bankr_api_key:
            raise ValueError("BANKR_API_KEY is required for bankr_wallet_api_swap funding")
        swap_client = BankrWalletApiClient(
            api_key=bankr_api_key,
            base_url=values.get("SIGN402_BANKR_API_BASE_URL", "https://api.bankr.bot"),
        )
    elif mode == "bankr_cli_swap":
        swap_client = BankrSwapClient(bankr_cli=values.get("SIGN402_BANKR_CLI", DEFAULT_BANKR_CLI))
    elif mode == "bankr_transfer_to_cdp_swap":
        cdp_wallet_address = values.get("SIGN402_CDP_WALLET_ADDRESS", "").strip()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", cdp_wallet_address):
            raise ValueError("SIGN402_CDP_WALLET_ADDRESS is required for bankr_transfer_to_cdp_swap")
        return BankrTransferToCdpSwapFundingRunner(
            bankr_transfer_client=BankrTreasuryClient(
                bankr_cli=values.get("SIGN402_BANKR_CLI", DEFAULT_BANKR_CLI)
            ),
            cdp_client=CdpWalletClient(
                service_dir=Path(
                    values.get("SIGN402_CDP_X402_SERVICE_DIR", str(DEFAULT_CDP_X402_SERVICE_DIR))
                )
            ),
            cdp_wallet_address=cdp_wallet_address,
            from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
            chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
        )
    elif mode == "cdp_wallet_swap":
        return CdpWalletSwapFundingRunner(
            cdp_client=CdpWalletClient(
                service_dir=Path(
                    values.get("SIGN402_CDP_X402_SERVICE_DIR", str(DEFAULT_CDP_X402_SERVICE_DIR))
                )
            ),
            from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
            chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
        )
    else:
        raise ValueError(f"unsupported SIGN402_BITREFILL_FUNDING_MODE: {mode}")
    return BankrSingitToUsdcFundingRunner(
        swap_client=swap_client,
        from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
        to_token=values.get("SIGN402_BANKR_SWAP_TO_TOKEN", "USDC"),
        chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
    )


def build_usdc_reserve_guard_from_env(env: dict[str, str] | None = None):
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_MODE", "test").strip().lower()
    payment_method = values.get("SIGN402_BITREFILL_PAYMENT_METHOD", "balance").strip().lower()
    if mode != "live" or payment_method != "usdc_base":
        return None
    if values.get("SIGN402_DISABLE_TREASURY_RESERVE_GUARD", "").strip() == "1":
        return None
    return BankrUsdcReserveGuard(
        treasury_client=BankrTreasuryClient(
            bankr_cli=values.get("SIGN402_BANKR_CLI", DEFAULT_BANKR_CLI)
        ),
        buffer_bps=int(values.get("SIGN402_TREASURY_USDC_BUFFER_BPS", "1000")),
        chain=values.get("SIGN402_TREASURY_CHAIN", "base"),
    )


def build_server(
    host: str,
    port: int,
    *,
    firefly_port: str,
    payment_executor_dir: Path = PAYMENT_EXECUTOR_DIR,
    event_store_path: Path = DEFAULT_EVENT_STORE_PATH,
    agent_state_path: Path = DEFAULT_AGENT_STATE_PATH,
    resource_base_url: str = "http://127.0.0.1:8090",
    cdp_x402_service_dir: Path = DEFAULT_CDP_X402_SERVICE_DIR,
    bitrefill_commerce_store_path: Path = DEFAULT_BITREFILL_COMMERCE_STORE_PATH,
) -> Sign402GatewayServer:
    from .bitrefill_runner import (
        BitrefillFulfillmentRunner,
        BitrefillProductDetailsService,
        BitrefillPurchaseRunner,
        BitrefillQuoteService,
        BitrefillSearchService,
        BitrefillSettlementPreparationRunner,
        WalletBitrefillPurchaseRunner,
        lookup_bitrefill_order,
    )
    from .commerce_store import BitrefillCommerceStore

    firefly = FireflyClient(port=firefly_port)
    payment_executor = build_payment_executor(payment_executor_dir)
    x402_payment_signature_builder = build_x402_payment_signature_builder(payment_executor_dir)
    base_payment_client = CdpBaseX402PaymentClient(cdp_x402_service_dir)
    bankr_x402_payment_client = BankrCliX402PaymentClient()
    event_store = LatestEventStore(event_store_path)
    agent_state_store = AgentStateStore(agent_state_path)
    bitrefill_commerce_store = BitrefillCommerceStore(bitrefill_commerce_store_path)
    bitrefill_client = build_bitrefill_client_from_env()
    real_rate_pricer = build_real_rate_pricer_from_env()
    bitrefill_search_service = BitrefillSearchService(bitrefill_client=bitrefill_client)
    bitrefill_product_details_service = BitrefillProductDetailsService(
        bitrefill_client=bitrefill_client
    )
    bitrefill_quote_service = BitrefillQuoteService(
        bitrefill_client=bitrefill_client,
        store=bitrefill_commerce_store,
        singit_usd_price_provider=lambda: os.getenv("SIGN402_SINGIT_USD_PRICE", "0.01"),
        real_rate_pricer=real_rate_pricer,
        ttl_seconds=int(os.getenv("SIGN402_BITREFILL_QUOTE_TTL_SECONDS", "120")),
    )
    bitrefill_fulfillment_runner = BitrefillFulfillmentRunner(
        store=bitrefill_commerce_store,
        bitrefill_client=bitrefill_client,
        funding_runner=build_bitrefill_funding_runner_from_env(),
    )
    bitrefill_settlement_preparation_runner = BitrefillSettlementPreparationRunner(
        store=bitrefill_commerce_store,
    )
    bitrefill_purchase_runner = BitrefillPurchaseRunner(
        store=bitrefill_commerce_store,
        firefly=firefly,
        bankr_payment_client=bankr_x402_payment_client,
        bankr_resource_url=DEFAULT_BANKR_BITREFILL_URL,
        pre_payment_guard=build_usdc_reserve_guard_from_env(),
        settlement_verifier=build_singit_settlement_verifier_from_env(),
        fulfillment_runner=bitrefill_fulfillment_runner,
    )
    bitrefill_wallet_purchase_runner = WalletBitrefillPurchaseRunner(
        store=bitrefill_commerce_store,
        approval_client=firefly.approve_payment_hash,
        fulfillment_runner=bitrefill_fulfillment_runner,
    )
    x402_inspector = ExternalX402Inspector()
    bankr_llm_topup_inspector = BankrLlmCreditsTopUpInspector()
    bankr_llm_topup = BankrLlmCreditsTopUpRunner(
        firefly=firefly,
        bankr_topup_executor=BankrLlmCreditsTopUpClient(),
        event_store=event_store,
        agent_state_store=agent_state_store,
    )
    x402_buyer = ExternalX402Buyer(
        firefly=firefly,
        payment_signature_builder=x402_payment_signature_builder,
        base_payment_client=base_payment_client,
        bankr_x402_payment_client=bankr_x402_payment_client,
        event_store=event_store,
        agent_state_store=agent_state_store,
    )
    agent_buy_probe = AgentBuyProbeRunner(
        firefly=firefly,
        payment_executor=payment_executor,
        event_store=event_store,
        agent_state_store=agent_state_store,
        resource_base_url=resource_base_url,
    )
    return Sign402GatewayServer(
        (host, port),
        Sign402GatewayHandler,
        firefly=firefly,
        payment_executor=payment_executor,
        event_store=event_store,
        agent_state_store=agent_state_store,
        agent_buy_probe=agent_buy_probe,
        x402_inspector=x402_inspector,
        x402_buyer=x402_buyer,
        bankr_llm_topup_inspector=bankr_llm_topup_inspector,
        bankr_llm_topup=bankr_llm_topup,
        bitrefill_search_service=bitrefill_search_service,
        bitrefill_product_details_service=bitrefill_product_details_service,
        bitrefill_quote_service=bitrefill_quote_service,
        bitrefill_purchase_runner=bitrefill_purchase_runner,
        bitrefill_wallet_purchase_runner=bitrefill_wallet_purchase_runner,
        bitrefill_order_lookup=lambda quote_id, **kwargs: lookup_bitrefill_order(
            bitrefill_commerce_store,
            quote_id,
            bitrefill_client=bitrefill_client,
            **kwargs,
        ),
        bitrefill_fulfillment_runner=bitrefill_fulfillment_runner,
        bitrefill_settlement_preparation_runner=bitrefill_settlement_preparation_runner,
    )


def build_payment_executor(payment_executor_dir: Path):
    from algosdk.v2client.algod import AlgodClient

    env = _read_env(payment_executor_dir / ".env")
    algod_client = AlgodClient("", env.get("ALGOD_URL", "https://testnet-api.algonode.cloud"))
    sender = env["ALGORAND_SENDER"]
    private_key = env["ALGORAND_PRIVATE_KEY"]

    def pay(requirement: dict[str, Any], policy_hash: str) -> dict[str, Any]:
        return execute_payment(
            algod_client=algod_client,
            sender=sender,
            private_key=private_key,
            payment_request=requirement,
            policy_hash=policy_hash,
        )

    return pay


def build_x402_payment_signature_builder(payment_executor_dir: Path):
    env = _read_env(payment_executor_dir / ".env")
    sender = env["ALGORAND_SENDER"]
    private_key = env["ALGORAND_PRIVATE_KEY"]
    algod_url = env.get("ALGOD_URL", "https://testnet-api.algonode.cloud")

    def build_signature(payment_required: dict[str, Any]) -> dict[str, Any]:
        return build_x402_avm_payment_signature_header(
            payment_required=payment_required,
            sender=sender,
            private_key=private_key,
            algod_url=algod_url,
        )

    return build_signature


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified local gateway for Hermes Sign402.")
    parser.add_argument("--host", default=os.getenv("SIGN402_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SIGN402_GATEWAY_PORT", "8099")))
    parser.add_argument("--firefly-port", default=os.getenv("FIREFLY_PORT"))
    parser.add_argument(
        "--payment-executor-dir",
        type=Path,
        default=Path(os.getenv("SIGN402_PAYMENT_EXECUTOR_DIR", PAYMENT_EXECUTOR_DIR)),
    )
    parser.add_argument(
        "--event-store-path",
        type=Path,
        default=Path(os.getenv("SIGN402_EVENT_STORE_PATH", DEFAULT_EVENT_STORE_PATH)),
    )
    parser.add_argument(
        "--agent-state-path",
        type=Path,
        default=Path(os.getenv("SIGN402_AGENT_STATE_PATH", DEFAULT_AGENT_STATE_PATH)),
    )
    parser.add_argument(
        "--resource-base-url",
        default=os.getenv("SIGN402_RESOURCE_BASE_URL", "http://127.0.0.1:8090"),
    )
    parser.add_argument(
        "--cdp-x402-service-dir",
        type=Path,
        default=Path(os.getenv("SIGN402_CDP_X402_SERVICE_DIR", DEFAULT_CDP_X402_SERVICE_DIR)),
    )
    args = parser.parse_args()

    firefly_port = args.firefly_port or find_firefly_port()
    server = build_server(
        args.host,
        args.port,
        firefly_port=firefly_port,
        payment_executor_dir=args.payment_executor_dir,
        event_store_path=args.event_store_path,
        agent_state_path=args.agent_state_path,
        resource_base_url=args.resource_base_url,
        cdp_x402_service_dir=args.cdp_x402_service_dir,
    )

    print(f"Sign402 gateway listening on http://{args.host}:{args.port}")
    print(f"Firefly port: {firefly_port}")
    print(f"Payment executor dir: {args.payment_executor_dir}")
    print(f"Event store path: {args.event_store_path}")
    print(f"Agent state path: {args.agent_state_path}")
    print(f"Resource base URL: {args.resource_base_url}")
    print(f"CDP x402 service dir: {args.cdp_x402_service_dir}")
    server.serve_forever()


class AgentBuyProbeRunner:
    def __init__(
        self,
        *,
        firefly: FireflyClient,
        payment_executor: Callable[[dict[str, Any], str], dict[str, Any]],
        event_store: "LatestEventStore",
        agent_state_store: "AgentStateStore",
        resource_base_url: str,
    ):
        self.firefly = firefly
        self.payment_executor = payment_executor
        self.event_store = event_store
        self.agent_state_store = agent_state_store
        self.resource_client = X402ResourceClient(resource_base_url)

    def __call__(self, target: str) -> dict[str, Any]:
        first_response = self.resource_client.get_probe_without_payment(target)
        if first_response.get("status") != 402:
            raise ValueError("Expected x402 resource server to return 402 Payment Required.")

        requirement = first_response["paymentRequirements"]
        policy_state = self.agent_state_store.read_policy_for_requirement(requirement)
        policy = policy_state["policy"]
        policy_hash = str(policy_state["policyHash"]).lower()
        approved_hash = str(policy_state["firefly"]["approvedHash"]).lower()
        if policy_hash != approved_hash:
            raise ValueError("Stored policy hash does not match Firefly approval.")

        self.agent_state_store.validate_policy_allows(policy, policy_hash, requirement)

        payment_commitment = build_payment_commitment(requirement, policy_hash)
        payment_hash = payment_commitment["paymentHash"]
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=_payment_context_lines(requirement),
        )
        if not approval.get("approved"):
            event = {
                "decision": "rejected_by_firefly",
                "target": target,
                "policyHash": policy_hash,
                "paymentApprovalHash": payment_hash,
                "paymentRequirements": requirement,
                "firefly": approval,
            }
            self.event_store.write(event)
            return event

        if str(approval.get("approvedHash", "")).lower() != payment_hash:
            raise ValueError("Firefly approved hash does not match payment commitment hash.")

        payment = self.payment_executor(requirement, policy_hash)
        self.agent_state_store.record_payment(
            policy_hash,
            requirement["paymentIntent"],
            int(str(requirement["amountAtomic"])),
        )

        payment_proof = {
            "verificationMode": "algorand",
            "txId": payment["txId"],
            "network": requirement["network"],
            "receiver": requirement["receiver"],
            "amountAtomic": requirement["amountAtomic"],
            "asset": requirement["asset"],
            "resource": requirement["resource"],
            "paymentIntent": requirement["paymentIntent"],
            "policyHash": policy_hash,
            "paymentApprovalHash": payment_hash,
        }
        encoded_payment = encode_payment_proof(payment_proof)
        resource_result = self._retry_paid_resource(target, encoded_payment)

        decision = "approved_and_executed"
        if resource_result.get("status") == 402 or resource_result.get("error"):
            decision = "payment_sent_access_denied"

        event = {
            "decision": decision,
            "target": target,
            "policyHash": policy_hash,
            "paymentApprovalHash": payment_hash,
            "txId": payment["txId"],
            "resource": requirement["resource"],
            "paymentIntent": requirement["paymentIntent"],
            "amountAtomic": requirement["amountAtomic"],
            "asset": requirement["asset"],
            "network": requirement["network"],
            "deviceModel": approval.get("deviceModel"),
            "deviceSerial": approval.get("deviceSerial"),
            "remainingBudgetAtomic": str(self.agent_state_store.remaining_budget(policy_hash)),
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment["commitment"],
            "payment": payment,
            "paymentProof": payment_proof,
            "resourceResult": resource_result,
            "result": resource_result.get("result"),
        }
        self.event_store.write(event)
        return event

    def _retry_paid_resource(self, target: str, encoded_payment: str) -> dict[str, Any]:
        last_response: dict[str, Any] = {}
        for attempt in range(8):
            last_response = self.resource_client.get_probe_with_payment(target, encoded_payment)
            if last_response.get("status") != 402 and not last_response.get("error"):
                return last_response
            if attempt < 7:
                time.sleep(2)
        return last_response


class ExternalX402Inspector:
    def __call__(
        self,
        resource_url: str,
        policy_hash: str,
        *,
        request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = fetch_x402_payment_required(resource_url, request_body=request_body)
        requirement = normalize_x402_payment_required(payload, resource_url=resource_url)
        payment_commitment = build_payment_commitment(requirement, policy_hash)
        return {
            "ok": True,
            "mode": "inspect_only",
            "resourceUrl": resource_url,
            "source": "goplausible-x402",
            "rawPaymentRequired": payload,
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment,
            "nextStep": "Use x402-avm to build official X-PAYMENT paymentGroup before executing.",
        }


class ExternalX402Buyer:
    def __init__(
        self,
        *,
        firefly: FireflyClient,
        payment_signature_builder: Callable[[dict[str, Any]], dict[str, Any]],
        base_payment_client: Callable[[str], dict[str, Any]] | None = None,
        bankr_x402_payment_client: Callable[..., dict[str, Any]] | None = None,
        event_store: "LatestEventStore",
        agent_state_store: "AgentStateStore",
    ):
        self.firefly = firefly
        self.payment_signature_builder = payment_signature_builder
        self.base_payment_client = base_payment_client
        self.bankr_x402_payment_client = bankr_x402_payment_client
        self.event_store = event_store
        self.agent_state_store = agent_state_store

    def __call__(
        self,
        resource_url: str,
        *,
        requirement_validator: Callable[[dict[str, Any]], None] | None = None,
        payment_context: dict[str, str] | None = None,
        request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_payment_required = fetch_x402_payment_required(resource_url, request_body=request_body)
        requirement = normalize_x402_payment_required(raw_payment_required, resource_url=resource_url)
        if requirement_validator is not None:
            requirement_validator(requirement)
        policy_state = self.agent_state_store.read_policy_for_requirement(requirement)
        policy = policy_state["policy"]
        policy_hash = str(policy_state["policyHash"]).lower()
        approved_hash = str(policy_state["firefly"]["approvedHash"]).lower()
        if policy_hash != approved_hash:
            raise ValueError("Stored policy hash does not match Firefly approval.")

        self.agent_state_store.validate_policy_allows(policy, policy_hash, requirement)

        payment_commitment = build_payment_commitment(requirement, policy_hash)
        payment_hash = payment_commitment["paymentHash"]
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=_payment_context_lines(requirement, payment_context=payment_context),
        )
        if not approval.get("approved"):
            event = {
                "decision": "rejected_by_firefly",
                "ok": False,
                "resourceUrl": resource_url,
                "policyHash": policy_hash,
                "paymentApprovalHash": payment_hash,
                "paymentRequirements": requirement,
                "firefly": approval,
            }
            self.event_store.write(event)
            return event

        if str(approval.get("approvedHash", "")).lower() != payment_hash:
            raise ValueError("Firefly approved hash does not match payment commitment hash.")

        if _is_singit_x402_requirement(requirement):
            if self.bankr_x402_payment_client is None:
                raise ValueError("SINGIT x402 payments require the Bankr CLI payment client.")
            resource_result = self.bankr_x402_payment_client(resource_url, request_body=request_body)
            mode = "official_x402_base_bankr_cli"
        elif requirement["network"] == "base-mainnet":
            if self.base_payment_client is None:
                raise ValueError("Base Mainnet x402 payments require the CDP payment client.")
            resource_result = self.base_payment_client(resource_url)
            mode = "official_x402_base_cdp"
        else:
            payment_signature = self.payment_signature_builder(raw_payment_required)
            resource_result = fetch_x402_paid_resource(
                resource_url,
                payment_signature_header=payment_signature["headerValue"],
            )
            mode = "official_x402_avm"

        if int(resource_result.get("status", 0)) != 200:
            raise ValueError(f"Official x402 resource denied payment: {resource_result}")

        payment_response = resource_result.get("paymentResponse", {})
        tx_id = (
            payment_response.get("transaction")
            or payment_response.get("transactionHash")
            or payment_response.get("txHash")
            or resource_result.get("transactionHash")
        )
        self.agent_state_store.record_payment(
            policy_hash,
            requirement["paymentIntent"],
            int(str(requirement["amountAtomic"])),
        )

        event = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": mode,
            "resourceUrl": resource_url,
            "policyHash": policy_hash,
            "paymentApprovalHash": payment_hash,
            "txId": tx_id,
            "paymentIntent": requirement["paymentIntent"],
            "amountAtomic": requirement["amountAtomic"],
            "asset": requirement["asset"],
            "network": requirement["network"],
            "x402Network": requirement.get("x402Network"),
            "receiver": requirement["receiver"],
            "deviceModel": approval.get("deviceModel"),
            "deviceSerial": approval.get("deviceSerial"),
            "remainingBudgetAtomic": str(self.agent_state_store.remaining_budget(policy_hash)),
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment["commitment"],
            "paymentResponse": payment_response,
            "resourceResult": resource_result,
            "result": "official_x402_resource_access_granted",
        }
        self.event_store.write(event)
        return event


class BankrLlmCreditsTopUpInspector:
    def __call__(self, payload: dict[str, Any], policy_hash: str) -> dict[str, Any]:
        top_up_intent = _build_bankr_llm_topup_intent(payload)
        requirement = _bankr_llm_topup_requirement(top_up_intent)
        payment_commitment = build_payment_commitment(requirement, policy_hash)
        return {
            "ok": True,
            "mode": "inspect_llm_credits_topup",
            "topUpIntent": top_up_intent,
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment,
            "quoteText": _bankr_llm_topup_quote_text(top_up_intent),
            "nextStep": "If acceptable, POST /agent/top-up-llm-credits with the same top-up payload. Firefly approval is required before Bankr is invoked.",
        }


class BankrLlmCreditsTopUpRunner:
    def __init__(
        self,
        *,
        firefly: FireflyClient,
        bankr_topup_executor: Callable[..., dict[str, Any]],
        event_store: "LatestEventStore",
        agent_state_store: "AgentStateStore",
    ):
        self.firefly = firefly
        self.bankr_topup_executor = bankr_topup_executor
        self.event_store = event_store
        self.agent_state_store = agent_state_store

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        top_up_intent = _build_bankr_llm_topup_intent(payload)
        requirement = _bankr_llm_topup_requirement(top_up_intent)
        policy_state = self.agent_state_store.read_policy_for_requirement(requirement)
        policy = policy_state["policy"]
        policy_hash = str(policy_state["policyHash"]).lower()
        approved_hash = str(policy_state["firefly"]["approvedHash"]).lower()
        if policy_hash != approved_hash:
            raise ValueError("Stored policy hash does not match Firefly approval.")

        self.agent_state_store.validate_policy_allows(policy, policy_hash, requirement)

        payment_commitment = build_payment_commitment(requirement, policy_hash)
        payment_hash = payment_commitment["paymentHash"]
        approval = self.firefly.approve_payment_hash(
            payment_hash,
            context_lines=_bankr_llm_topup_context_lines(top_up_intent),
        )
        if not approval.get("approved"):
            event = {
                "decision": "rejected_by_firefly",
                "ok": False,
                "mode": "bankr_llm_credits_topup",
                "topUpIntent": top_up_intent,
                "policyHash": policy_hash,
                "paymentApprovalHash": payment_hash,
                "paymentRequirements": requirement,
                "firefly": approval,
            }
            self.event_store.write(event)
            return event

        if str(approval.get("approvedHash", "")).lower() != payment_hash:
            raise ValueError("Firefly approved hash does not match LLM credits top-up hash.")

        bankr_result = self.bankr_topup_executor(
            credit_amount_usd=top_up_intent["creditAmountUsd"],
            funding_token_address=top_up_intent["fundingTokenAddress"],
        )
        self.agent_state_store.record_payment(
            policy_hash,
            requirement["paymentIntent"],
            int(str(requirement["amountAtomic"])),
        )

        event = {
            "decision": "approved_and_executed",
            "ok": bool(bankr_result.get("ok", False)),
            "mode": "bankr_llm_credits_topup",
            "creditAmountUsd": top_up_intent["creditAmountUsd"],
            "fundingTokenAddress": top_up_intent["fundingTokenAddress"],
            "fundingTokenSymbol": top_up_intent["fundingTokenSymbol"],
            "maxFundingTokenAmountAtomic": top_up_intent["maxFundingTokenAmountAtomic"],
            "topUpIntent": top_up_intent,
            "policyHash": policy_hash,
            "paymentApprovalHash": payment_hash,
            "paymentIntent": requirement["paymentIntent"],
            "amountAtomic": requirement["amountAtomic"],
            "asset": requirement["asset"],
            "network": requirement["network"],
            "receiver": requirement["receiver"],
            "deviceModel": approval.get("deviceModel"),
            "deviceSerial": approval.get("deviceSerial"),
            "remainingBudgetAtomic": str(self.agent_state_store.remaining_budget(policy_hash)),
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment["commitment"],
            "bankr": bankr_result,
            "telegramText": _bankr_llm_topup_telegram_text(top_up_intent, bankr_result),
        }
        self.event_store.write(event)
        return event


class BankrLlmCreditsTopUpClient:
    def __init__(
        self,
        bankr_cli: str | None = None,
        receipt_fetcher: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.bankr_cli = bankr_cli or os.getenv("SIGN402_BANKR_CLI", "bankr")
        self.receipt_fetcher = receipt_fetcher or fetch_base_transaction_receipt

    def __call__(self, *, credit_amount_usd: str, funding_token_address: str) -> dict[str, Any]:
        command = [
            self.bankr_cli,
            "llm",
            "credits",
            "add",
            credit_amount_usd,
            "--token",
            funding_token_address,
            "--yes",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = {
            "ok": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            message = payload["stderr"] or payload["stdout"] or "Bankr LLM credits top-up failed"
            raise ValueError(message)
        transaction_hash = _bankr_cli_transaction_hash(payload["stdout"])
        payload["transactionHash"] = transaction_hash
        return payload


class SingitSettlementVerifier:
    def __init__(
        self,
        *,
        receipt_fetcher: Callable[[str], dict[str, Any]] | None = None,
        transaction_resolver: Callable[..., str] | None = None,
        payer_address: str | None = None,
        singit_token_address: str = DEFAULT_SINGIT_TOKEN_ADDRESS,
    ):
        self.receipt_fetcher = receipt_fetcher or fetch_base_transaction_receipt
        self.transaction_resolver = transaction_resolver
        self.payer_address = payer_address
        self.singit_token_address = singit_token_address

    def __call__(self, *, bankr_result: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
        tx_hash = _bankr_result_transaction_hash(bankr_result)
        discovered = False
        expected_amount = int(str(quote["maxSingitAtomic"]))
        payment_made = bankr_result.get("paymentMade")
        pay_to = payment_made.get("payTo") if isinstance(payment_made, dict) else None
        if not tx_hash:
            if self.transaction_resolver is None:
                raise ValueError("SINGIT settlement transaction hash is missing")
            if self.payer_address is None:
                raise ValueError("SINGIT settlement payer address is missing")
            start_block = bankr_result.get("startBlock")
            if not isinstance(start_block, int):
                raise ValueError("SINGIT settlement startBlock is missing")
            if not isinstance(pay_to, str) or not pay_to.startswith("0x"):
                raise ValueError("SINGIT settlement paymentMade.payTo is missing")
            tx_hash = self.transaction_resolver(
                token_address=self.singit_token_address,
                sender=self.payer_address,
                recipient=pay_to,
                amount_atomic=str(expected_amount),
                from_block=start_block,
            )
            discovered = True
        receipt = self.receipt_fetcher(tx_hash)
        if str(receipt.get("status", "")).lower() not in {"0x1", "1", "true"}:
            raise ValueError(f"SINGIT settlement transaction failed: {tx_hash}")
        transfers = _erc20_transfer_logs(
            receipt,
            token_address=self.singit_token_address,
        )
        if self.payer_address is not None:
            if not isinstance(pay_to, str) or not pay_to.startswith("0x"):
                raise ValueError("SINGIT settlement paymentMade.payTo is missing")
            matching = [
                transfer
                for transfer in transfers
                if _same_evm_address(transfer["from"], self.payer_address)
                and _same_evm_address(transfer["to"], pay_to)
                and int(str(transfer["amountAtomic"])) == expected_amount
            ]
            if not matching:
                _raise_singit_transfer_mismatch(
                    transfers=transfers,
                    expected_sender=self.payer_address,
                    expected_recipient=pay_to,
                    expected_amount=expected_amount,
                )
            matched_transfer = matching[0]
            return {
                "network": "base-mainnet",
                "transactionHash": tx_hash,
                "tokenAddress": self.singit_token_address,
                "from": self.payer_address,
                "payTo": pay_to,
                "amountAtomic": str(matched_transfer["amountAtomic"]),
                "requiredAmountAtomic": str(expected_amount),
                "discovered": discovered,
            }
        matching = [
            transfer
            for transfer in transfers
            if int(str(transfer["amountAtomic"])) >= expected_amount
        ]
        if not matching:
            raise ValueError(
                "SINGIT settlement receipt did not include the required SINGIT transfer"
            )
        return {
            "network": "base-mainnet",
            "transactionHash": tx_hash,
            "tokenAddress": self.singit_token_address,
            "amountAtomic": str(max(int(str(transfer["amountAtomic"])) for transfer in matching)),
            "requiredAmountAtomic": str(expected_amount),
            "discovered": discovered,
        }


class BaseErc20TransactionResolver:
    def __init__(
        self,
        *,
        log_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
        sleeper: Callable[[float], None] | None = None,
        attempts: int = 6,
        interval_seconds: float = 2,
    ):
        self.log_fetcher = log_fetcher or fetch_base_erc20_transfer_logs
        self.sleeper = sleeper or time.sleep
        self.attempts = attempts
        self.interval_seconds = interval_seconds

    def __call__(
        self,
        *,
        token_address: str,
        sender: str,
        recipient: str,
        amount_atomic: str,
        from_block: int,
    ) -> str:
        for attempt in range(max(1, self.attempts)):
            logs = self.log_fetcher(
                token_address=token_address,
                sender=sender,
                recipient=recipient,
                from_block=from_block,
            )
            transaction_hashes = {
                str(log.get("transactionHash"))
                for log in logs
                if _erc20_log_amount_atomic(log) == str(amount_atomic)
                and isinstance(log.get("transactionHash"), str)
                and str(log.get("transactionHash")).startswith("0x")
            }
            if len(transaction_hashes) == 1:
                return next(iter(transaction_hashes))
            if len(transaction_hashes) > 1:
                raise ValueError("ambiguous SINGIT settlement")
            if attempt < self.attempts - 1:
                self.sleeper(self.interval_seconds)
        raise ValueError("SINGIT settlement transaction was not found")


class BankrTreasuryClient:
    def __init__(self, bankr_cli: str | None = None):
        configured = bankr_cli or os.getenv("SIGN402_BANKR_CLI")
        self.bankr_cli = configured or DEFAULT_BANKR_CLI

    def usdc_balance(self, *, chain: str = "base") -> Decimal:
        command = [
            self.bankr_cli,
            "wallet",
            "portfolio",
            "--chain",
            str(chain),
            "--json",
            "--low-value",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Bankr portfolio failed"
            raise ValueError(message)
        json_start = result.stdout.find("{")
        if json_start < 0:
            raise ValueError("Bankr portfolio did not return JSON")
        payload = json.loads(result.stdout[json_start:])
        return usdc_balance_from_portfolio(payload, chain=chain)

    def transfer_usdc(
        self,
        *,
        to_address: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        return self.transfer_token(
            to_address=to_address,
            amount=amount,
            token="USDC",
            chain=chain,
        )

    def transfer_singit(
        self,
        *,
        to_address: str,
        amount: str,
        token_address: str = DEFAULT_SINGIT_TOKEN_ADDRESS,
        chain: str = "base",
    ) -> dict[str, Any]:
        return self.transfer_token(
            to_address=to_address,
            amount=amount,
            token=token_address,
            chain=chain,
        )

    def transfer_token(
        self,
        *,
        to_address: str,
        amount: str,
        token: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        command = [
            self.bankr_cli,
            "--ni",
            "wallet",
            "transfer",
            "--to",
            str(to_address),
            "--amount",
            str(amount),
            "--token",
            str(token),
            "--chain",
            str(chain),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = {
            "ok": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "txId": _bankr_cli_transaction_hash(result.stdout),
        }
        if result.returncode != 0:
            message = payload["stderr"] or payload["stdout"] or "Bankr treasury transfer failed"
            raise ValueError(message)
        return payload


class CdpWalletClient:
    def __init__(self, service_dir: Path = DEFAULT_CDP_X402_SERVICE_DIR):
        self.service_dir = Path(service_dir)

    def quote(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        payload = self._run(
            [
                "swap-price",
                "--from-token",
                str(from_token),
                "--to-token",
                BASE_USDC_MAINNET if str(to_token).upper() == "USDC" else str(to_token),
                "--amount",
                str(amount),
                "--chain",
                str(chain),
            ]
        )
        return {
            "ok": True,
            "fromAmount": str(amount),
            "fromToken": "SINGIT",
            "toAmount": _format_usdc_atomic(payload.get("toAmount", "0")),
            "toToken": "USDC",
            "minToAmount": _format_usdc_atomic(payload.get("minToAmount", payload.get("toAmount", "0"))),
            "raw": payload,
        }

    def swap_singit_to_usdc(
        self,
        *,
        amount: str,
        from_token: str = DEFAULT_SINGIT_TOKEN_ADDRESS,
        min_usdc: str = "",
        chain: str = "base",
    ) -> dict[str, Any]:
        payload = self._run(
            [
                "swap",
                "--from-token",
                str(from_token),
                "--to-token",
                BASE_USDC_MAINNET,
                "--amount",
                str(amount),
                "--chain",
                str(chain),
                "--min-usdc",
                str(min_usdc),
            ]
        )
        return self._with_tx_id(payload)

    def transfer_usdc(
        self,
        *,
        to_address: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        payload = self._run(
            [
                "transfer-usdc",
                "--to",
                str(to_address),
                "--amount",
                str(amount),
                "--chain",
                str(chain),
            ]
        )
        return self._with_tx_id(payload)

    def _run(self, args: list[str]) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        command = ["node", str(script), *args]
        result = subprocess.run(
            command,
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "CDP wallet service failed"
            raise ValueError(message)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("CDP wallet service returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise ValueError("CDP wallet service returned non-object JSON")
        return payload

    def _with_tx_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        tx_id = payload.get("transactionHash") or payload.get("txId")
        return {**payload, "txId": str(tx_id) if tx_id else None}


class BankrTransferToCdpSwapFundingRunner:
    def __init__(
        self,
        *,
        bankr_transfer_client: Any,
        cdp_client: CdpWalletClient,
        cdp_wallet_address: str,
        from_token: str,
        chain: str = "base",
    ):
        self.bankr_transfer_client = bankr_transfer_client
        self.cdp_client = cdp_client
        self.cdp_wallet_address = cdp_wallet_address
        self.from_token = from_token
        self.chain = chain

    def __call__(self, quote: dict[str, Any]) -> dict[str, Any]:
        if quote.get("pricingMode") != "bankr_real_rate":
            raise ValueError("CDP swap funding requires pricingMode=bankr_real_rate")
        amount = str(quote.get("singitAmount", "")).strip()
        if not amount:
            raise ValueError("quote singitAmount is required for CDP swap funding")
        required_usdc = str(quote.get("requiredUsdc") or quote.get("priceUsd") or "")
        transfer_result = self.bankr_transfer_client.transfer_singit(
            to_address=self.cdp_wallet_address,
            amount=amount,
            token_address=self.from_token,
            chain=self.chain,
        )
        swap_result = self.cdp_client.swap_singit_to_usdc(
            amount=amount,
            from_token=self.from_token,
            min_usdc=required_usdc,
            chain=self.chain,
        )
        return {
            "ok": bool(transfer_result.get("ok", True)) and bool(swap_result.get("ok", True)),
            "pricingMode": "bankr_real_rate",
            "mode": "bankr_transfer_to_cdp_swap",
            "amount": amount,
            "fromToken": self.from_token,
            "toToken": "USDC",
            "chain": self.chain,
            "cdpWalletAddress": self.cdp_wallet_address,
            "expectedUsdc": str(quote.get("expectedUsdc", "")),
            "requiredUsdc": required_usdc,
            "transfer": transfer_result,
            "swap": swap_result,
        }


class CdpWalletSwapFundingRunner:
    def __init__(
        self,
        *,
        cdp_client: CdpWalletClient,
        from_token: str,
        chain: str = "base",
    ):
        self.cdp_client = cdp_client
        self.from_token = from_token
        self.chain = chain

    def __call__(self, quote: dict[str, Any]) -> dict[str, Any]:
        if quote.get("pricingMode") != "bankr_real_rate":
            raise ValueError("CDP wallet swap funding requires pricingMode=bankr_real_rate")
        amount = str(quote.get("singitAmount", "")).strip()
        if not amount:
            raise ValueError("quote singitAmount is required for CDP wallet swap funding")
        required_usdc = str(quote.get("requiredUsdc") or quote.get("priceUsd") or "")
        swap_result = self.cdp_client.swap_singit_to_usdc(
            amount=amount,
            from_token=self.from_token,
            min_usdc=required_usdc,
            chain=self.chain,
        )
        return {
            "ok": bool(swap_result.get("ok", True)),
            "pricingMode": "bankr_real_rate",
            "mode": "cdp_wallet_swap",
            "amount": amount,
            "fromToken": self.from_token,
            "toToken": "USDC",
            "chain": self.chain,
            "expectedUsdc": str(quote.get("expectedUsdc", "")),
            "requiredUsdc": required_usdc,
            "swap": swap_result,
            "txId": swap_result.get("txId"),
        }


class BankrUsdcReserveGuard:
    def __init__(
        self,
        *,
        treasury_client: BankrTreasuryClient,
        buffer_bps: int = 1000,
        chain: str = "base",
    ):
        self.treasury_client = treasury_client
        self.buffer_bps = int(buffer_bps)
        self.chain = chain

    def __call__(self, quote: dict[str, Any]) -> dict[str, Any]:
        price = Decimal(str(quote["priceUsd"]))
        required = (
            price
            * Decimal(10000 + self.buffer_bps)
            / Decimal(10000)
        ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        available = self.treasury_client.usdc_balance(chain=self.chain)
        if available < required:
            raise ValueError(
                f"insufficient USDC reserve: need {required} USDC, have {available} USDC"
            )
        return {
            "ok": True,
            "requiredUsdcReserve": str(required),
            "availableUsdcReserve": str(available),
        }


def _first_present_amount(source: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _assert_usdc_floor(value_raw: Any, *, required_usdc: str, label: str) -> None:
    """Raise when a known USDC amount is below the required floor.

    No-op when the floor or the amount is unavailable/unparseable, so callers
    that cannot observe a realized amount (e.g. the Bankr CLI swap) skip safely.
    """
    required_text = str(required_usdc).strip()
    if not required_text or value_raw is None:
        return
    try:
        value = Decimal(str(value_raw))
        required = Decimal(required_text)
    except (InvalidOperation, ValueError):
        return
    if value < required:
        raise ValueError(f"{label} {value} USDC, below required {required} USDC")


def _assert_quote_can_meet_required_usdc(
    quote_result: dict[str, Any],
    *,
    required_usdc: str,
) -> None:
    """Reject before swapping when the quoted USDC floor cannot meet the target.

    Both the Bankr CLI (``minToAmount`` parsed from "Min received") and the
    Wallet API quote expose a guaranteed minimum, so this floor check covers the
    CLI swap path, which returns no realized amount for the post-swap check.
    """
    _assert_usdc_floor(
        _first_present_amount(quote_result, ("minToAmount", "toAmount")),
        required_usdc=required_usdc,
        label="Bankr swap quote floor",
    )


def _assert_swap_received_enough_usdc(
    swap_result: dict[str, Any],
    *,
    required_usdc: str,
) -> None:
    """Reject a swap that demonstrably delivered less USDC than the quote needs.

    Only enforced when the swap result reports a realized output amount
    (``amountReceived``/``minToAmount``); the Bankr CLI swap path does not
    expose one and therefore cannot be verified here.
    """
    _assert_usdc_floor(
        _first_present_amount(swap_result, ("amountReceived", "minToAmount")),
        required_usdc=required_usdc,
        label="Bankr swap received",
    )


class BankrSingitToUsdcFundingRunner:
    def __init__(
        self,
        *,
        swap_client: Any,
        from_token: str,
        to_token: str = "USDC",
        chain: str = "base",
    ):
        self.swap_client = swap_client
        self.from_token = from_token
        self.to_token = to_token
        self.chain = chain

    def __call__(self, quote: dict[str, Any]) -> dict[str, Any]:
        if quote.get("pricingMode") != "bankr_real_rate":
            raise ValueError("Bankr real-rate funding requires pricingMode=bankr_real_rate")
        amount = str(quote.get("singitAmount", "")).strip()
        if not amount:
            raise ValueError("quote singitAmount is required for Bankr swap")
        required_usdc = str(quote.get("requiredUsdc") or quote.get("priceUsd") or "")
        if required_usdc:
            pre_swap_quote = self.swap_client.quote(
                from_token=self.from_token,
                to_token=self.to_token,
                amount=amount,
                chain=self.chain,
            )
            _assert_quote_can_meet_required_usdc(pre_swap_quote, required_usdc=required_usdc)
        result = self.swap_client.swap(
            from_token=self.from_token,
            to_token=self.to_token,
            amount=amount,
            chain=self.chain,
        )
        _assert_swap_received_enough_usdc(result, required_usdc=required_usdc)
        return {
            **result,
            "pricingMode": "bankr_real_rate",
            "fromToken": self.from_token,
            "toToken": self.to_token,
            "chain": self.chain,
            "amount": amount,
            "requiredUsdc": required_usdc,
            "expectedUsdc": str(quote.get("expectedUsdc", "")),
        }


class BankrCliX402PaymentClient:
    def __init__(
        self,
        bankr_cli: str | None = None,
        block_number_fetcher: Callable[[], int] | None = None,
    ):
        configured = bankr_cli or os.getenv("SIGN402_BANKR_CLI")
        self.bankr_cli = configured or DEFAULT_BANKR_CLI
        self.block_number_fetcher = block_number_fetcher or fetch_base_block_number

    def __call__(
        self,
        resource_url: str,
        *,
        request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = [self.bankr_cli, "x402", "call", resource_url, "--max-payment", "10", "-y", "--raw"]
        if request_body is not None:
            command.extend(["-X", "POST", "-d", json.dumps(request_body, separators=(",", ":"))])

        reported_command = list(command)
        if "-d" in reported_command:
            reported_command[reported_command.index("-d") + 1] = "<redacted>"

        start_block = self.block_number_fetcher()
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw_payload = _bankr_cli_raw_payload(result.stdout)
        status = _bankr_cli_raw_status(raw_payload) or _bankr_cli_status(result.stdout)
        body = _bankr_cli_raw_response_body(raw_payload) or _bankr_cli_response_body(result.stdout)
        transaction_hash = _bankr_cli_raw_transaction_hash(raw_payload) or _bankr_cli_transaction_hash(result.stdout)
        payload = {
            "ok": result.returncode == 0,
            "status": status,
            "resourceUrl": resource_url,
            "command": reported_command,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "body": body,
            "transactionHash": transaction_hash,
            "startBlock": start_block,
            "paymentMade": dict(raw_payload.get("paymentMade", {})) if raw_payload else {},
        }
        if payload["transactionHash"]:
            payload["paymentResponse"] = {"transaction": payload["transactionHash"]}
        if result.returncode != 0:
            message = payload["stderr"] or payload["stdout"] or "Bankr x402 call failed"
            raise ValueError(message)
        return payload


class CdpBaseX402PaymentClient:
    def __init__(self, service_dir: Path):
        self.service_dir = service_dir

    def __call__(self, resource_url: str) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")

        result = subprocess.run(
            ["node", str(script), "buy", "--url", resource_url],
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "CDP x402 service failed"
            raise ValueError(message)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("CDP x402 service returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise ValueError("CDP x402 service returned non-object JSON")
        return payload


class LatestEventStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> dict[str, Any] | None:
        with self.lock:
            if not self.path.exists():
                return None
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("event store must contain a JSON object")
            return payload

    def write(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.path)
            return event


class AgentStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def read_policy(self) -> dict[str, Any] | None:
        state = self._read_state()
        policy_state = state.get("policyApproval")
        if isinstance(policy_state, dict):
            return policy_state
        policies = state.get("policyApprovals")
        if isinstance(policies, dict):
            for policy_state in reversed(list(policies.values())):
                if isinstance(policy_state, dict):
                    return policy_state
        return None

    def read_policy_for_requirement(self, requirement: dict[str, Any]) -> dict[str, Any]:
        state = self._read_state()
        for policy_state in self._policy_approvals_from_state(state):
            policy = policy_state.get("policy")
            if not isinstance(policy, dict):
                continue
            if not _asset_matches(str(requirement.get("asset", "")), str(policy.get("asset", ""))):
                continue
            if str(requirement.get("purpose")) != str(policy.get("allowedPurpose")):
                continue
            return policy_state
        raise ValueError("No Firefly-approved policy matches payment requirement. Call /approve-policy for this asset.")

    def write_policy(self, policy_approval: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            state = self._read_state_unlocked()
            policy_hash = str(policy_approval.get("policyHash", "")).lower()
            if not HEX_32_RE.fullmatch(policy_hash):
                raise ValueError("policyApproval.policyHash must be 64 hex characters")

            policies = state.get("policyApprovals")
            if not isinstance(policies, dict):
                policies = {}
                existing = state.get("policyApproval")
                if isinstance(existing, dict):
                    existing_hash = str(existing.get("policyHash", "")).lower()
                    if HEX_32_RE.fullmatch(existing_hash):
                        policies[existing_hash] = existing

            policy_spend = state.get("policySpend")
            if not isinstance(policy_spend, dict):
                policy_spend = {}
                existing = state.get("policyApproval")
                existing_hash = str(existing.get("policyHash", "")).lower() if isinstance(existing, dict) else ""
                if HEX_32_RE.fullmatch(existing_hash):
                    policy_spend[existing_hash] = {
                        "spentAtomic": str(state.get("spentAtomic", "0")),
                        "usedPaymentIntents": list(state.get("usedPaymentIntents", [])),
                    }

            policies[policy_hash] = policy_approval
            policy_spend.setdefault(policy_hash, {"spentAtomic": "0", "usedPaymentIntents": []})
            state["policyApproval"] = policy_approval
            state["policyApprovals"] = policies
            state["policySpend"] = policy_spend
            self._write_state_unlocked(state)
            return policy_approval

    def validate_policy_allows(
        self,
        policy: dict[str, Any],
        policy_hash: str,
        requirement: dict[str, Any],
    ) -> None:
        state = self._read_state()
        if policy_hash not in self._policy_approvals_by_hash(state):
            raise ValueError("Policy hash does not match stored policy.")

        amount = int(str(requirement["amountAtomic"]))
        max_per_payment = int(str(policy["maxPerPaymentAtomic"]))
        max_budget = int(str(policy["maxBudgetAtomic"]))
        spend_state = self._policy_spend_state(state, policy_hash)
        spent = int(str(spend_state.get("spentAtomic", "0")))
        used_intents = set(spend_state.get("usedPaymentIntents", []))
        payment_intent = str(requirement["paymentIntent"])

        if payment_intent in used_intents:
            raise ValueError("paymentIntent already used")
        if amount > max_per_payment:
            raise ValueError("amountAtomic exceeds maxPerPaymentAtomic")
        if spent + amount > max_budget:
            raise ValueError("amountAtomic exceeds remaining budget")
        if not _asset_matches(str(requirement["asset"]), str(policy["asset"])):
            raise ValueError("asset does not match policy.asset")
        if str(requirement.get("purpose")) != str(policy["allowedPurpose"]):
            raise ValueError("purpose does not match policy.allowedPurpose")

    def record_payment(self, policy_hash: str, payment_intent: str, amount_atomic: int) -> None:
        with self.lock:
            state = self._read_state_unlocked()
            if policy_hash not in self._policy_approvals_by_hash(state):
                raise ValueError("Policy hash does not match stored policy.")

            policy_spend = state.get("policySpend")
            if not isinstance(policy_spend, dict):
                policy_spend = {}
            spend_state = dict(self._policy_spend_state(state, policy_hash))
            used_intents = list(spend_state.get("usedPaymentIntents", []))
            if payment_intent not in used_intents:
                used_intents.append(payment_intent)
            spent = int(str(spend_state.get("spentAtomic", "0"))) + amount_atomic
            spend_state["usedPaymentIntents"] = used_intents
            spend_state["spentAtomic"] = str(spent)
            policy_spend[policy_hash] = spend_state
            state["policySpend"] = policy_spend
            if policy_hash == str(state.get("policyApproval", {}).get("policyHash", "")).lower():
                state["usedPaymentIntents"] = used_intents
                state["spentAtomic"] = str(spent)
            self._write_state_unlocked(state)

    def remaining_budget(self, policy_hash: str) -> int:
        state = self._read_state()
        policy_approval = self._policy_approvals_by_hash(state).get(policy_hash)
        if not isinstance(policy_approval, dict):
            return 0
        policy = policy_approval.get("policy", {})
        max_budget = int(str(policy.get("maxBudgetAtomic", "0")))
        spent = int(str(self._policy_spend_state(state, policy_hash).get("spentAtomic", "0")))
        return max(0, max_budget - spent)

    def _policy_approvals_from_state(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        policies = state.get("policyApprovals")
        if isinstance(policies, dict):
            return [policy for policy in policies.values() if isinstance(policy, dict)]
        policy = state.get("policyApproval")
        return [policy] if isinstance(policy, dict) else []

    def _policy_approvals_by_hash(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        approvals: dict[str, dict[str, Any]] = {}
        for policy in self._policy_approvals_from_state(state):
            policy_hash = str(policy.get("policyHash", "")).lower()
            if HEX_32_RE.fullmatch(policy_hash):
                approvals[policy_hash] = policy
        return approvals

    def _policy_spend_state(self, state: dict[str, Any], policy_hash: str) -> dict[str, Any]:
        policy_spend = state.get("policySpend")
        if isinstance(policy_spend, dict) and isinstance(policy_spend.get(policy_hash), dict):
            return dict(policy_spend[policy_hash])

        current_hash = str(state.get("policyApproval", {}).get("policyHash", "")).lower()
        if policy_hash == current_hash:
            return {
                "spentAtomic": str(state.get("spentAtomic", "0")),
                "usedPaymentIntents": list(state.get("usedPaymentIntents", [])),
            }

        return {"spentAtomic": "0", "usedPaymentIntents": []}

    def _read_state(self) -> dict[str, Any]:
        with self.lock:
            return self._read_state_unlocked()

    def _read_state_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("agent state must contain a JSON object")
        return payload

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.path)


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"')
    return env


def _read_hash(payload: dict[str, Any], key: str) -> str:
    value = str(payload[key]).lower()
    if not HEX_32_RE.fullmatch(value):
        raise ValueError(f"{key} must be 64 hex characters")
    return value


def _payment_context_lines(
    requirement: dict[str, Any] | None,
    *,
    payment_context: dict[str, str] | None = None,
) -> list[str]:
    if not requirement or "amountAtomic" not in requirement:
        return ["x402 PAYMENT", "sign402 approval"]

    if payment_context:
        title = str(payment_context.get("title") or "x402 PAYMENT").strip()
        subject = str(payment_context.get("subject") or "").strip()
        if subject:
            return [
                title[:20],
                subject[:20],
                _format_display_amount(requirement),
            ]

    resource = str(requirement.get("resource", ""))
    network = str(requirement.get("network") or requirement.get("x402Network") or "")
    if network in {"base-mainnet", "eip155:8453"}:
        title = "BASE x402 PAYMENT"
        service = "Base Mainnet"
    elif "weather" in resource.lower():
        title = "x402 WEATHER"
        service = "GoPlausible API"
    else:
        title = "x402 PAYMENT"
        service = "x402 API"
    return [
        title,
        _format_display_amount(requirement),
        service,
    ]


def _format_display_amount(requirement: dict[str, Any]) -> str:
    amount_atomic = int(str(requirement.get("amountAtomic", "0")))
    asset = str(requirement.get("asset", ""))
    extra = requirement.get("extra")
    asset_name = ""
    decimals = 0
    if isinstance(extra, dict):
        asset_name = str(extra.get("name", ""))
        decimals = int(str(extra.get("decimals", "0")))

    if asset == "10458941":
        asset_name = "USDC"
        decimals = 6
    elif asset.lower() == BASE_USDC_MAINNET.lower():
        asset_name = "USDC"
        decimals = 6
    elif asset.lower() == DEFAULT_SINGIT_TOKEN_ADDRESS.lower():
        asset_name = asset_name or "SINGIT"
        decimals = decimals or 18
    elif asset == "ALGO_TEST":
        asset_name = "ALGO"
        decimals = 6
    elif not asset_name:
        asset_name = asset

    if decimals <= 0:
        return f"{amount_atomic} {asset_name}"

    divisor = 10**decimals
    whole = amount_atomic // divisor
    fraction = amount_atomic % divisor
    fraction_text = str(fraction).zfill(decimals).rstrip("0")
    if not fraction_text:
        return f"{whole} {asset_name}"
    return f"{whole}.{fraction_text} {asset_name}"


def _asset_matches(requirement_asset: str, policy_asset: str) -> bool:
    if requirement_asset.startswith("0x") or policy_asset.startswith("0x"):
        return requirement_asset.lower() == policy_asset.lower()
    return requirement_asset == policy_asset


def _validate_payment_requirements(requirement: Any) -> None:
    if not isinstance(requirement, dict):
        raise ValueError("paymentRequirements must be an object")
    if requirement.get("network") != "algorand-testnet":
        raise ValueError("Only algorand-testnet is supported")
    if requirement.get("asset") != "ALGO_TEST":
        raise ValueError("Only ALGO_TEST is supported")
    if not requirement.get("receiver"):
        raise ValueError("paymentRequirements.receiver is required")
    if not requirement.get("paymentIntent"):
        raise ValueError("paymentRequirements.paymentIntent is required")
    amount = int(str(requirement.get("amountAtomic", "0")))
    if amount <= 0:
        raise ValueError("paymentRequirements.amountAtomic must be positive")


def _build_bankr_llm_topup_intent(payload: dict[str, Any]) -> dict[str, Any]:
    credit_amount_usd = _read_positive_decimal_text(payload, "creditAmountUsd")
    funding_token_address = str(
        payload.get("fundingTokenAddress")
        or payload.get("tokenAddress")
        or payload.get("asset")
        or DEFAULT_SINGIT_TOKEN_ADDRESS
    ).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", funding_token_address):
        raise ValueError("fundingTokenAddress must be an EVM token contract address")

    max_amount_atomic = str(
        payload.get("maxFundingTokenAmountAtomic")
        or payload.get("fundingTokenAmountAtomic")
        or payload.get("amountAtomic")
        or ""
    ).strip()
    if not max_amount_atomic.isdigit() or int(max_amount_atomic) <= 0:
        raise ValueError("maxFundingTokenAmountAtomic must be a positive integer string")

    funding_token_symbol = str(payload.get("fundingTokenSymbol") or "SINGIT").strip() or "SINGIT"
    network = str(payload.get("network") or "base-mainnet").strip()
    if network not in {"base-mainnet", "eip155:8453"}:
        raise ValueError("Bankr LLM credits top-up currently supports Base token funding")

    top_up_intent = str(payload.get("topUpIntent") or payload.get("paymentIntent") or "").strip()
    if not top_up_intent:
        top_up_intent = _default_bankr_llm_topup_intent(
            credit_amount_usd=credit_amount_usd,
            funding_token_address=funding_token_address,
            max_amount_atomic=max_amount_atomic,
        )

    return {
        "creditAmountUsd": credit_amount_usd,
        "fundingTokenAddress": funding_token_address,
        "fundingTokenSymbol": funding_token_symbol,
        "maxFundingTokenAmountAtomic": max_amount_atomic,
        "network": "base-mainnet",
        "purpose": BANKR_LLM_CREDITS_PURPOSE,
        "topUpIntent": top_up_intent,
    }


def _bankr_llm_topup_requirement(top_up_intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "network": "base-mainnet",
        "asset": str(top_up_intent["fundingTokenAddress"]),
        "amountAtomic": str(top_up_intent["maxFundingTokenAmountAtomic"]),
        "receiver": BANKR_LLM_CREDITS_RECEIVER,
        "resource": BANKR_LLM_CREDITS_RESOURCE,
        "paymentIntent": str(top_up_intent["topUpIntent"]),
        "purpose": BANKR_LLM_CREDITS_PURPOSE,
        "extra": {
            "name": str(top_up_intent["fundingTokenSymbol"]),
            "creditAmountUsd": str(top_up_intent["creditAmountUsd"]),
        },
    }


def _read_positive_decimal_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be a positive decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{key} must be positive")
    normalized = format(parsed.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _default_bankr_llm_topup_intent(
    *,
    credit_amount_usd: str,
    funding_token_address: str,
    max_amount_atomic: str,
) -> str:
    canonical = json.dumps(
        {
            "creditAmountUsd": credit_amount_usd,
            "fundingTokenAddress": funding_token_address.lower(),
            "maxFundingTokenAmountAtomic": max_amount_atomic,
            "nonce": secrets.token_hex(8),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "bankr-llm-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _bankr_llm_topup_context_lines(top_up_intent: dict[str, Any]) -> list[str]:
    return [
        "LLM CREDITS",
        f"${top_up_intent['creditAmountUsd']}",
        str(top_up_intent["fundingTokenSymbol"])[:20],
    ]


def _bankr_llm_topup_quote_text(top_up_intent: dict[str, Any]) -> str:
    return (
        f"Bankr LLM credits top-up: ${top_up_intent['creditAmountUsd']} "
        f"funded with {top_up_intent['fundingTokenSymbol']}."
    )


def _bankr_llm_topup_telegram_text(
    top_up_intent: dict[str, Any],
    bankr_result: dict[str, Any],
) -> str:
    if not bankr_result.get("ok"):
        return ""
    return (
        f"✅ Bankr LLM credits topped up by ${top_up_intent['creditAmountUsd']} "
        f"using {top_up_intent['fundingTokenSymbol']}."
    )


def _resolve_paid_tool(payload: dict[str, Any]) -> dict[str, Any]:
    raw_tool = str(
        payload.get("tool")
        or payload.get("toolId")
        or payload.get("name")
        or payload.get("mcpTool")
        or ""
    ).strip()
    if not raw_tool:
        raise ValueError("tool is required")

    lookup = raw_tool.lower()
    tool_id = PAID_TOOL_ALIASES.get(lookup, lookup)
    tool = PAID_TOOLS.get(tool_id)
    if tool is None:
        available = ", ".join(sorted(PAID_TOOLS))
        raise ValueError(f"Unknown paid tool '{raw_tool}'. Available tools: {available}")
    return dict(tool)


def _paid_tool_resource_url(tool: dict[str, Any], payload: dict[str, Any]) -> str:
    template = str(tool.get("resourceUrlTemplate") or "").strip()
    if not template:
        return str(tool["resourceUrl"])

    fields = tool.get("templateFields")
    if not isinstance(fields, dict):
        raise ValueError(f"Tool '{tool.get('id')}' has an unsupported resourceUrlTemplate")

    values = {
        name: _paid_tool_template_value(name, spec, payload)
        for name, spec in fields.items()
    }
    return template.format(**values)


def _tool_payment_context(tool: dict[str, Any], payload: dict[str, Any]) -> dict[str, str] | None:
    config = tool.get("paymentContext")
    if not isinstance(config, dict):
        return None

    title = str(config.get("title") or tool.get("name") or "x402 PAYMENT").strip()
    subject = str(config.get("subject") or "").strip()
    subject_field = str(config.get("subjectField") or "").strip()
    if subject_field:
        fields = tool.get("templateFields")
        spec = fields.get(subject_field, {}) if isinstance(fields, dict) else {}
        subject = _paid_tool_template_value(subject_field, spec, payload, encoded=False)

    if not subject:
        return None

    return {
        "title": title,
        "subject": f"{config.get('subjectPrefix', '')}{subject}",
    }


def _paid_tool_template_value(
    name: str,
    spec: Any,
    payload: dict[str, Any],
    *,
    encoded: bool = True,
) -> str:
    field_spec = spec if isinstance(spec, dict) else {}
    aliases = [name, *field_spec.get("aliases", [])]
    value: Any = None
    for alias in aliases:
        if payload.get(alias) is not None:
            value = payload.get(alias)
            break

    if value is None:
        value = field_spec.get("default", "")

    text = str(value or "").strip()
    strip_prefix = str(field_spec.get("stripPrefix") or "")
    if strip_prefix:
        text = text.removeprefix(strip_prefix).strip()

    transform = str(field_spec.get("transform") or "")
    if transform == "upper":
        text = text.upper()
    elif transform == "lower":
        text = text.lower()

    if not text and field_spec.get("required"):
        raise ValueError(f"{name} is required")

    if not encoded:
        return text

    return quote(text, safe=str(field_spec.get("safe") or ""))


def _buy_tool_cache_key(tool: dict[str, Any], resource_url: str) -> str:
    return json.dumps(
        {
            "toolId": tool.get("id"),
            "resourceUrl": resource_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_result(
    tool: dict[str, Any],
    payload: dict[str, Any],
    resource_url: str | None = None,
) -> dict[str, Any]:
    resolved_resource_url = resource_url or str(tool["resourceUrl"])
    result = dict(payload)
    result["resourceUrl"] = str(result.get("resourceUrl") or resolved_resource_url)
    result["tool"] = {
        "id": tool["id"],
        "name": tool["name"],
        "kind": tool["kind"],
        "source": tool["source"],
        "description": tool["description"],
        "resourceUrl": resolved_resource_url,
        "mcpStyleName": tool["mcpStyleName"],
        "inputSchema": tool["inputSchema"],
    }
    result["toolId"] = tool["id"]
    result["toolName"] = tool["name"]
    result["command"] = tool["command"]
    result["mode"] = "paid_tool_" + str(payload.get("mode", "x402"))
    if not result.get("telegramText"):
        telegram_text = _tool_telegram_text(tool, result)
        if telegram_text:
            result["telegramText"] = telegram_text
    return result


def _tool_telegram_text(tool: dict[str, Any], result: dict[str, Any]) -> str:
    if not result.get("ok") or result.get("amountAtomic") is None:
        return ""

    tool_id = str(tool.get("id") or "")
    if tool_id == "bankr.singit.risk_check":
        return _singit_risk_check_telegram_text(tool, result)

    if not result.get("txId"):
        return ""

    network = str(result.get("network") or result.get("x402Network") or "")
    tx_url = _evm_transaction_url(str(result.get("txId") or ""), network)
    if not tx_url:
        return ""

    amount = _format_display_amount(
        {
            "amountAtomic": result.get("amountAtomic"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    remaining = _format_display_amount(
        {
            "amountAtomic": result.get("remainingBudgetAtomic", "0"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    value_summary, value_link = _tool_value_summary(tool, result)
    if value_summary:
        text = f"{value_summary} Paid {amount}. Tx {tx_url}. Budget left {remaining}."
        if value_link:
            text += f"\n{value_link}"
        return text

    return f"✅ {tool['name']} unlocked. Paid {amount}. Tx {tx_url}. Budget left {remaining}."


def _singit_risk_check_telegram_text(tool: dict[str, Any], result: dict[str, Any]) -> str:
    body = _resource_body(result)
    risk_level = str(body.get("riskLevel") or "unknown").strip()
    recommendation = str(body.get("recommendation") or "").strip()
    amount = _format_display_amount(
        {
            "amountAtomic": result.get("amountAtomic"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    remaining = _format_display_amount(
        {
            "amountAtomic": result.get("remainingBudgetAtomic", "0"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    text = f"✅ {tool['name']} unlocked. Risk: {risk_level}. Paid {amount}. Budget left {remaining}."
    if recommendation:
        text += f" {recommendation}"

    network = str(result.get("network") or result.get("x402Network") or "")
    tx_url = _evm_transaction_url(str(result.get("txId") or ""), network)
    if tx_url:
        text += f" Tx {tx_url}."
    return text


def _tool_value_summary(tool: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    body = _resource_body(result)
    tool_id = str(tool.get("id") or "")

    if tool_id == "otto.hyperliquid_market":
        market = body.get("market") if isinstance(body.get("market"), dict) else {}
        symbol = str(market.get("symbol") or "").upper()
        price = str(market.get("currentPrice") or market.get("markPrice") or "").strip()
        leverage = str(market.get("maxLeverage") or "").strip()
        trading_url = str(market.get("tradingUrl") or "").strip()
        if symbol and price:
            parts = [f"✅ Hyperliquid {symbol} unlocked.", f"Price ${price}."]
            if leverage:
                parts.append(f"Max leverage {leverage}x.")
            return " ".join(parts), trading_url

    if tool_id == "otto.crypto_news":
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        report = str(data.get("report") or body.get("report") or "").strip()
        if report:
            return f"✅ Crypto News unlocked.\n{_compact_multiline(report)}", ""

    if tool_id == "otto.funding_rates":
        report = str(body.get("report") or "").strip()
        if report:
            return f"✅ Funding Rates unlocked.\n{_compact_multiline(report)}", ""

    if tool_id == "onesource.ens":
        input_value = str(body.get("input") or "").strip()
        address = str(body.get("address") or body.get("primary") or body.get("name") or "").strip()
        if input_value and address:
            return f"✅ ENS resolved: {input_value} → {address}.", ""

    if tool_id == "anchor.token_price":
        symbol = str(body.get("symbol") or "").upper()
        usd = body.get("usd")
        change = body.get("usd_24h_change_pct")
        if symbol and usd is not None:
            summary = f"✅ {symbol} price: ${usd}."
            if change is not None:
                summary += f" 24h {change}%."
            return summary, ""

    return "", ""


def _resource_body(result: dict[str, Any]) -> dict[str, Any]:
    resource_result = result.get("resourceResult")
    if not isinstance(resource_result, dict):
        return {}
    body = resource_result.get("body")
    return body if isinstance(body, dict) else {}


def _compact_multiline(text: str, *, max_lines: int = 6, max_chars: int = 700) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = "\n".join(lines[:max_lines])
    if len(compact) > max_chars:
        compact = compact[: max_chars - 1].rstrip() + "…"
    return compact


def _x402_telegram_text(result: dict[str, Any]) -> str:
    if not result.get("ok") or not result.get("txId") or result.get("amountAtomic") is None:
        return ""

    network = str(result.get("network") or result.get("x402Network") or "")
    tx_url = _evm_transaction_url(str(result.get("txId") or ""), network)
    if not tx_url:
        return ""

    amount = _format_display_amount(
        {
            "amountAtomic": result.get("amountAtomic"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    remaining = _format_display_amount(
        {
            "amountAtomic": result.get("remainingBudgetAtomic", "0"),
            "asset": result.get("asset"),
            "extra": _asset_extra_for_result(result),
        }
    )
    return (
        f"✅ x402 resource unlocked. "
        f"Paid {amount}. "
        f"Tx {tx_url}. "
        f"Budget left {remaining}."
    )


def _base_x402_quote_text(requirement: dict[str, Any]) -> str:
    amount = _format_display_amount(requirement)
    return f"Base x402 quote: {amount} on Base Mainnet."


def _validate_base_usdc_x402_requirement(requirement: Any) -> None:
    if not isinstance(requirement, dict):
        raise ValueError("paymentRequirements must be an object")

    network = str(requirement.get("network") or requirement.get("x402Network") or "")
    if network not in {"base-mainnet", "eip155:8453"}:
        raise ValueError("Only Base Mainnet x402 endpoints are supported for raw URL purchases.")

    asset = str(requirement.get("asset") or "")
    if asset.lower() != BASE_USDC_MAINNET.lower():
        raise ValueError("Only Base USDC x402 endpoints are supported for raw URL purchases.")


def _is_singit_x402_requirement(requirement: dict[str, Any]) -> bool:
    network = str(requirement.get("network") or requirement.get("x402Network") or "")
    asset = str(requirement.get("asset") or "")
    return network in {"base-mainnet", "eip155:8453"} and asset.lower() == DEFAULT_SINGIT_TOKEN_ADDRESS.lower()


def _bankr_cli_status(stdout: str) -> int:
    match = re.search(r"^\s*Status\s+(\d+)\s*$", stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else 200


def _bankr_cli_raw_payload(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _bankr_cli_raw_status(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    status = payload.get("status")
    return int(status) if isinstance(status, int) else None


def _bankr_cli_raw_response_body(payload: dict[str, Any] | None) -> Any:
    if not payload:
        return None
    return payload.get("response")


def _bankr_cli_raw_transaction_hash(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    payment = payload.get("paymentMade")
    if not isinstance(payment, dict):
        return None
    for key in ("transactionHash", "txHash", "transaction"):
        value = payment.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            return value
    return None


def _bankr_cli_transaction_hash(stdout: str) -> str | None:
    patterns = (
        r"https://basescan\.org/tx/(0x[a-fA-F0-9]{64})",
        r"\bTx Hash:\s*(0x[a-fA-F0-9]{64})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, stdout)
        if match:
            return match.group(1)
    return None


def _bankr_result_transaction_hash(result: dict[str, Any]) -> str | None:
    for key in ("transactionHash", "txHash", "transaction", "txId"):
        value = result.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            return value
    payment = result.get("paymentMade")
    if isinstance(payment, dict):
        for key in ("transactionHash", "txHash", "transaction", "txId"):
            value = payment.get(key)
            if isinstance(value, str) and value.startswith("0x"):
                return value
    body = result.get("body")
    if isinstance(body, dict):
        return _bankr_result_transaction_hash(body)
    return None


def _base_rpc_call(method: str, params: list[Any]) -> Any:
    request_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        BASE_MAINNET_RPC_URL,
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Sign402/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Base RPC returned non-object JSON")
    if payload.get("error") is not None:
        raise ValueError(f"Base RPC error: {payload['error']}")
    return payload.get("result")


def fetch_base_block_number() -> int:
    result = _base_rpc_call("eth_blockNumber", [])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("Base RPC returned an invalid block number")
    return int(result, 16)


def fetch_base_transaction_receipt(tx_hash: str) -> dict[str, Any]:
    result = _base_rpc_call("eth_getTransactionReceipt", [tx_hash])
    if not isinstance(result, dict):
        raise ValueError(f"Base RPC did not return a receipt for {tx_hash}")
    return result


def fetch_base_erc20_transfer_logs(
    *,
    token_address: str,
    sender: str,
    recipient: str,
    from_block: int,
) -> list[dict[str, Any]]:
    result = _base_rpc_call(
        "eth_getLogs",
        [
            {
                "address": token_address,
                "fromBlock": hex(from_block),
                "toBlock": "latest",
                "topics": [
                    ERC20_TRANSFER_TOPIC,
                    _erc20_topic_address(sender),
                    _erc20_topic_address(recipient),
                ],
            }
        ],
    )
    if not isinstance(result, list):
        raise ValueError("Base RPC returned invalid logs")
    return [log for log in result if isinstance(log, dict)]


def _erc20_topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _normalize_evm_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x")


def _same_evm_address(left: str, right: str) -> bool:
    return _normalize_evm_address(left) == _normalize_evm_address(right)


def _erc20_log_amount_atomic(log: dict[str, Any]) -> str | None:
    data = log.get("data")
    if not isinstance(data, str):
        return None
    try:
        return str(int(data, 16))
    except ValueError:
        return None


def _format_usdc_atomic(value: Any) -> str:
    return format_decimal(Decimal(str(value)) / Decimal(1_000_000))


def _raise_singit_transfer_mismatch(
    *,
    transfers: list[dict[str, str]],
    expected_sender: str,
    expected_recipient: str,
    expected_amount: int,
) -> None:
    if not transfers:
        raise ValueError("SINGIT settlement receipt did not include the required token transfer")
    if not any(_same_evm_address(transfer["from"], expected_sender) for transfer in transfers):
        raise ValueError("SINGIT settlement receipt sender did not match the Bankr payer")
    if not any(_same_evm_address(transfer["to"], expected_recipient) for transfer in transfers):
        raise ValueError("SINGIT settlement receipt recipient did not match the Bankr payTo")
    if not any(int(str(transfer["amountAtomic"])) == expected_amount for transfer in transfers):
        raise ValueError("SINGIT settlement receipt amount did not match the quote")
    raise ValueError("SINGIT settlement receipt did not include the exact SINGIT transfer")


def _erc20_transfer_logs(
    receipt: dict[str, Any],
    *,
    token_address: str,
) -> list[dict[str, str]]:
    token = token_address.lower()
    transfers = []
    for log in receipt.get("logs", []):
        if not isinstance(log, dict):
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            continue
        if str(log.get("address", "")).lower() != token:
            continue
        if str(topics[0]).lower() != ERC20_TRANSFER_TOPIC:
            continue
        data = str(log.get("data", "0x0"))
        transfers.append(
            {
                "from": "0x" + str(topics[1])[-40:],
                "to": "0x" + str(topics[2])[-40:],
                "amountAtomic": str(int(data, 16)),
            }
        )
    return transfers


def _bankr_cli_response_body(stdout: str) -> Any:
    marker = re.search(r"^\s*Response\s*$", stdout, flags=re.MULTILINE)
    search_start = marker.end() if marker else 0
    brace_index = stdout.find("{", search_start)
    if brace_index < 0:
        return None

    decoder = json.JSONDecoder()
    try:
        body, _end = decoder.raw_decode(stdout[brace_index:])
    except json.JSONDecodeError:
        return None
    return body


def _asset_extra_for_result(result: dict[str, Any]) -> dict[str, Any]:
    requirement = result.get("paymentRequirements")
    if isinstance(requirement, dict) and isinstance(requirement.get("extra"), dict):
        return dict(requirement["extra"])
    return {}


def _evm_transaction_url(tx_id: str, network: str) -> str:
    if not tx_id:
        return ""
    if tx_id.startswith(("http://", "https://")):
        return tx_id
    if network in {"base-mainnet", "eip155:8453"}:
        return f"https://basescan.org/tx/{tx_id}"
    if network in {"base-sepolia", "eip155:84532"}:
        return f"https://sepolia.basescan.org/tx/{tx_id}"
    return ""


def _busy_payload() -> dict[str, Any]:
    return {
        "approved": False,
        "error": "firefly_busy",
        "message": "Firefly is already handling another approval request.",
    }


def _without_fulfillment_token(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the plaintext fulfillment token before persisting/broadcasting.

    The token is returned to the buying caller so they can reveal the
    redemption code, but it must never reach the dashboard event store, which
    is served back over an open-CORS GET endpoint.
    """
    return {key: value for key, value in result.items() if key != "fulfillmentToken"}


if __name__ == "__main__":
    main()
