from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
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
from typing import Any, Callable, Mapping
from urllib.parse import quote
import urllib.request


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
SIGN402_BRIDGE_DIR = ROOT_DIR / "sign402-bridge"
PAYMENT_EXECUTOR_DIR = ROOT_DIR / "payment-executor"
LIVE_DEMO_DIR = ROOT_DIR / "live-demo"
DEMO_RESOURCE_SERVER_DIR = ROOT_DIR / "demo-resource-server"
DEFAULT_EVENT_STORE_PATH = ROOT_DIR / "demo-dashboard" / "latest-run.json"
DEFAULT_AGENT_STATE_PATH = ROOT_DIR / "demo-dashboard" / "agent-state.json"
DEFAULT_BITREFILL_COMMERCE_STORE_PATH = ROOT_DIR / "demo-dashboard" / "bitrefill-orders.sqlite3"
DEFAULT_BITREFILL_LIVE_MAX_USD = "5.00"
DEFAULT_CDP_X402_SERVICE_DIR = ROOT_DIR / "cdp-x402-service"
DEFAULT_USER_SPEND_LIMIT_STORE_PATH = Path.home() / ".sign402" / "user-spend-limits.json"
DEFAULT_BUYER_EMAIL_STORE_PATH = Path.home() / ".sign402" / "buyer-emails.sqlite3"
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
DEFAULT_NATIVE_ETH_WITHDRAW_GAS_RESERVE = Decimal("0.000001")
MAX_REQUEST_BODY_BYTES = 1024 * 1024
# Must outlast the approval prompt (10 min) plus payment execution, so a hold
# never lapses while the purchase it guards is still in flight.
SPEND_RESERVATION_TTL_SECONDS = 15 * 60
# Only today's records bind the daily cap; the rest is a short audit tail.
SPEND_RECORD_RETENTION_DAYS = 30
MAX_BASE_RPC_RESPONSE_BYTES = 1024 * 1024
COINBASE_NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

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
from .bankr_llm_purchase import (
    BankrLlmError,
    build_bankr_llm_purchase_service_from_env,
)
from .bitrefill_quote import SERVICE_FEE_BPS
from .bitrefill_runner import CdpWalletServiceError
from .commerce_store import sanitize_bankr_reconciliation_snapshot
from .diagnostics import (
    configure_logging,
    log_hidden_detail,
    log_swallowed_failure,
)
from .numeric import format_decimal
from .goplausible import fetch_x402_paid_resource, fetch_x402_payment_required, normalize_x402_payment_required
from .real_rate_pricing import RealRateSingitPricer
from .secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateError,
    atomic_write_private_json,
)
from .user_emails import BuyerEmailStore, mask_email
from .user_wallets import (
    BASE_NATIVE_ETH_ASSET_ID,
    DEFAULT_USER_WALLET_STORE_PATH,
    WalletEncryptionError,
    build_wallet_service_from_env,
)
from .imessage_approvals import (
    DEFAULT_IMESSAGE_APPROVAL_STORE_PATH,
    build_imessage_approval_service_from_env,
)

HEX_32_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BUY_TOOL_DUPLICATE_SUPPRESSION_SECONDS = 120
FUND_MOVING_POST_PATHS = frozenset(
    {
        "/approve-payment",
        "/execute-payment",
        "/agent/buy-probe",
        "/agent/buy-tool",
        "/agent/buy-x402",
        "/agent/top-up-llm-credits",
        "/agent/buy-bitrefill",
        "/agent/buy-wallet-bitrefill",
        "/agent/withdraw",
        "/agent/llm-key/start",
        "/agent/llm-key/verify",
        "/agent/llm-key/reconcile",
        "/internal/fulfill-bitrefill",
    }
)
_PURCHASES_PAUSED_MESSAGE = (
    "⏸️ Purchases are temporarily paused for maintenance. "
    "Please try again later."
)


def _purchases_paused() -> bool:
    return os.getenv(
        "SIGN402_PURCHASES_PAUSED",
        "",
    ).strip().lower() in {"1", "true", "yes", "on"}


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
            endpoints = [
                "/agent/buy-tool",
                "/agent/search-bitrefill",
                "/agent/list-bitrefill-products",
                "/agent/get-bitrefill-product",
                "/agent/quote-bitrefill",
                "/agent/buy-wallet-bitrefill",
                "/agent/wallet",
                "/agent/create-wallet",
                "/agent/wallet-balance",
                "/agent/last-purchase",
                "/agent/spending-limits",
                "/agent/withdraw/tokens",
                "/agent/withdraw",
                "/agent/llm-key/start",
                "/agent/llm-key/accept-terms",
                "/agent/llm-key/verify",
                "/agent/llm-key/reconcile",
                "/agent/llm-credits",
                "/agent/approval-channel/select-existing",
                "/agent/imessage/pairing",
                "/agent/imessage/link",
                "/agent/imessage/unlink",
                "/agent/imessage/pending",
                "/agent/imessage/decision",
            ]
            if _test_endpoints_enabled():
                endpoints.append("/agent/test-imessage-approval")
            if _legacy_payment_executor_enabled():
                endpoints.extend(
                    [
                        "/approve-policy",
                        "/approve-payment",
                        "/execute-payment",
                        "/events/latest",
                        "/agent/buy-probe",
                        "/agent/tools",
                        "/agent/inspect-tool",
                        "/agent/inspect-x402",
                        "/agent/buy-x402",
                        "/agent/inspect-llm-credits-topup",
                        "/agent/top-up-llm-credits",
                        "/agent/buy-bitrefill",
                        "/agent/get-bitrefill-order",
                    ]
                )
            self._send_json(
                {
                    "ok": True,
                    "service": "sign402-gateway",
                    "endpoints": endpoints,
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
        try:
            declared_length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self._send_json({"error": "invalid_content_length"}, status=400)
            return
        if declared_length < 0:
            self._send_json({"error": "invalid_content_length"}, status=400)
            return
        if declared_length > MAX_REQUEST_BODY_BYTES:
            self._send_json({"error": "request_body_too_large"}, status=413)
            return
        if path in FUND_MOVING_POST_PATHS and self._reject_if_purchases_paused():
            return

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
        if path == "/agent/list-bitrefill-products":
            self._handle_agent_list_bitrefill_products()
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
        if path == "/agent/wallet":
            self._handle_agent_wallet()
            return
        if path == "/agent/create-wallet":
            self._handle_agent_create_wallet()
            return
        if path == "/agent/wallet-balance":
            self._handle_agent_wallet_balance()
            return
        if path == "/agent/last-purchase":
            self._handle_agent_last_purchase()
            return
        if path == "/agent/spending-limits":
            self._handle_agent_spending_limits()
            return
        if path == "/agent/buyer-email":
            self._handle_agent_buyer_email()
            return
        if path == "/agent/withdraw/tokens":
            self._handle_agent_withdraw_tokens()
            return
        if path == "/agent/withdraw":
            self._handle_agent_withdraw()
            return
        if path == "/agent/llm-key/start":
            self._handle_agent_llm_key_start()
            return
        if path == "/agent/llm-key/accept-terms":
            self._handle_agent_llm_key_accept_terms()
            return
        if path == "/agent/llm-key/verify":
            self._handle_agent_llm_key_verify()
            return
        if path == "/agent/llm-key/reconcile":
            self._handle_agent_llm_key_reconcile()
            return
        if path == "/agent/llm-credits":
            self._handle_agent_llm_credits()
            return
        if path == "/agent/approval-channel/select-existing":
            self._handle_agent_select_existing_approval_channel()
            return
        if path == "/agent/imessage/pairing":
            self._handle_agent_imessage_pairing()
            return
        if path == "/agent/imessage/link":
            self._handle_agent_imessage_link()
            return
        if path == "/agent/imessage/unlink":
            self._handle_agent_imessage_unlink()
            return
        if path == "/agent/imessage/pending":
            self._handle_agent_imessage_pending()
            return
        if path == "/agent/imessage/decision":
            self._handle_agent_imessage_decision()
            return
        if path == "/agent/test-imessage-approval":
            if not _test_endpoints_enabled():
                self._send_json({"error": "not_found"}, status=404)
                return
            self._handle_agent_test_imessage_approval()
            return
        if path == "/internal/fulfill-bitrefill":
            self._handle_internal_fulfill_bitrefill()
            return
        if path == "/internal/prepare-bitrefill-settlement":
            self._handle_internal_prepare_bitrefill_settlement()
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_OPTIONS(self) -> None:
        if not _cors_allowed_origin(self.headers.get("Origin", "")):
            self._send_json({"error": "not_found"}, status=404)
            return
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_approve_policy(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
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

    def _handle_agent_create_wallet(self) -> None:
        try:
            _require_wallet_api_token(self)
            payload = self._read_json()
            telegram_user_id = _read_telegram_user_id(payload)
            # This is the one endpoint that runs on the shared token alone: it
            # mints a per-user token for whatever id it is handed. Rate-limit it
            # so a leaked shared token cannot enumerate users or mint tokens in
            # bulk unnoticed.
            _enforce_wallet_creation_rate(telegram_user_id)
            result = self.server.user_wallet_service.create_wallet(
                telegram_user_id=telegram_user_id,
                telegram_username=str(payload.get("telegramUsername", "") or ""),
            )
            self._send_json(_without_private_key_material(result), status=200)
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except WalletEncryptionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_wallet(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.user_wallet_service.wallet_status(
                _require_authenticated_user(self, payload)
            )
            status = 200 if bool(result.get("ok")) else 404
            self._send_json(_without_private_key_material(result), status=status)
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except WalletEncryptionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_wallet_balance(self) -> None:
        try:
            payload = self._read_json()
            result = self.server.user_wallet_service.wallet_balance(
                _require_authenticated_user(self, payload)
            )
            status = 200 if bool(result.get("ok")) else 404
            self._send_json(_without_private_key_material(result), status=status)
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except WalletEncryptionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buyer_email(self) -> None:
        """Read, set, or forget the address a guest invoice delivers to.

        Responses carry only the masked form: the caller already knows the
        address it just sent, and nothing downstream needs the rest.
        """
        try:
            payload = self._read_json()
            telegram_user_id = _require_authenticated_user(self, payload)
            store = getattr(self.server, "buyer_email_store", None)
            if store is None:
                self._send_json(
                    {
                        "ok": False,
                        "telegramText": (
                            "Buyer email is not configured on this gateway."
                        ),
                    },
                    status=503,
                )
                return
            action = str(payload.get("action") or "get").strip().lower()
            if action == "get":
                stored = store.get_email(telegram_user_id)
            elif action == "set":
                stored = store.set_email(
                    telegram_user_id,
                    _read_required_text(payload, "email"),
                )
            elif action == "forget":
                store.forget_email(telegram_user_id)
                stored = None
            else:
                self._send_json(
                    {
                        "ok": False,
                        "telegramText": "Unsupported buyer email action.",
                    },
                    status=400,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "email": mask_email(stored or ""),
                    "required": bool(
                        getattr(self.server, "buyer_email_required", False)
                    ),
                }
            )
        except ValueError as exc:
            # `user_emails` never quotes the address into its errors, so this
            # message is safe to return.
            self._send_json({"ok": False, "telegramText": str(exc)}, status=400)

    def _handle_agent_last_purchase(self) -> None:
        try:
            payload = self._read_json()
            telegram_user_id = _require_authenticated_user(self, payload)
            self.server.user_event_store.preflight_write()
            event = self.server.user_event_store.read(telegram_user_id)
            if not isinstance(event, dict) or not event.get("ok"):
                self._send_json(
                    {
                        "ok": False,
                        "telegramText": "No completed Sign402 purchase found for this Telegram user.",
                    },
                    status=404,
                )
                return
            bitrefill_response = _last_bitrefill_purchase_response(
                self.server,
                event,
                telegram_user_id,
            )
            if bitrefill_response is not None:
                self._send_json(bitrefill_response)
                return
            telegram_text = event.get("telegramText")
            if not isinstance(telegram_text, str) or not telegram_text.strip():
                self._send_json(
                    {
                        "ok": False,
                        "telegramText": "The last Sign402 purchase has no Telegram result text.",
                    },
                    status=404,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "telegramText": telegram_text.strip(),
                    "toolId": event.get("toolId"),
                    "toolName": event.get("toolName"),
                    "txId": event.get("txId"),
                    "resourceUrl": event.get("resourceUrl"),
                }
            )
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except WalletEncryptionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_spending_limits(self) -> None:
        try:
            payload = self._read_json()
            telegram_user_id = _require_authenticated_user(self, payload)
            operator_limits = _operator_user_wallet_limits()
            operator_ceilings = _operator_user_wallet_ceilings()
            if _payload_has_spending_limit_update(payload):
                max_per_tx_atomic = _read_usdc_limit_atomic(payload, "maxPerTx")
                daily_cap_atomic = _read_usdc_limit_atomic(payload, "dailyCap")
                limits = self.server.user_spend_limit_store.set_limit_settings(
                    telegram_user_id,
                    max_per_tx_atomic=max_per_tx_atomic,
                    daily_cap_atomic=daily_cap_atomic,
                    operator_max_per_tx_atomic=operator_limits["maxPerTxAtomic"],
                    operator_daily_cap_atomic=operator_limits["dailyCapAtomic"],
                    operator_ceiling_per_tx_atomic=operator_ceilings["ceilingPerTxAtomic"],
                    operator_ceiling_daily_atomic=operator_ceilings["ceilingDailyAtomic"],
                )
                updated = True
            else:
                limits = self.server.user_spend_limit_store.limit_settings(
                    telegram_user_id,
                    operator_max_per_tx_atomic=operator_limits["maxPerTxAtomic"],
                    operator_daily_cap_atomic=operator_limits["dailyCapAtomic"],
                    operator_ceiling_per_tx_atomic=operator_ceilings["ceilingPerTxAtomic"],
                    operator_ceiling_daily_atomic=operator_ceilings["ceilingDailyAtomic"],
                )
                updated = False
            limits = {
                **limits,
                **_bitrefill_live_limit_settings(),
            }
            self._send_json(
                {
                    "ok": True,
                    "updated": updated,
                    "limits": limits,
                    "telegramText": _spending_limits_telegram_text(limits, updated=updated),
                }
            )
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_withdraw_tokens(self) -> None:
        try:
            payload = self._read_json()
            user_id = _require_authenticated_user(self, payload)
            result = self.server.user_wallet_service.withdrawable_tokens(user_id)
            status = 200 if bool(result.get("ok")) else 404
            self._send_json(_without_private_key_material(result), status=status)
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_withdraw(self) -> None:
        transfer_attempted = False
        try:
            payload = self._read_json()
            user_id = _require_authenticated_user(self, payload)
            _enforce_user_purchase_rate(user_id)
            amount = _read_positive_amount(payload, "amount")
            to_address = _read_evm_address(payload, "toAddress")
            token_address = _read_withdraw_asset(payload, "tokenAddress")
            inventory = self.server.user_wallet_service.withdrawable_tokens(user_id)
            token = _find_withdrawable_token(inventory, token_address)
            if token is None:
                raise ValueError("token is not available for withdrawal")
            if Decimal(amount) > Decimal(str(token.get("balance") or "0")):
                raise ValueError("withdraw amount exceeds token balance")
            is_native = bool(token.get("native"))
            if is_native:
                reserve = _native_eth_withdraw_gas_reserve()
                remaining = Decimal(str(token.get("balance") or "0")) - Decimal(amount)
                if remaining < reserve:
                    raise ValueError(
                        "ETH withdrawal must leave at least "
                        f"{format_decimal(reserve)} ETH for Base gas"
                    )

            wallet = inventory.get("wallet") if isinstance(inventory, dict) else {}
            from_address = str(wallet.get("address", "") if isinstance(wallet, dict) else "")
            symbol = str(token.get("symbol") or "ERC20")
            decimals = int(token.get("decimals"))
            commitment = _withdraw_commitment(
                telegram_user_id=user_id,
                from_address=from_address,
                to_address=to_address,
                token=token,
                amount=amount,
            )
            approval = self.server.imessage_approval_service.request_hash_approval(
                telegram_user_id=user_id,
                action_type="sign402_withdrawal",
                commitment_hash=commitment["hash"],
                context_lines=_withdraw_approval_context_lines(
                    symbol=symbol,
                    amount=amount,
                    from_address=from_address,
                    to_address=to_address,
                ),
            )
            if not approval.get("ok") or approval.get("status") != "approved":
                self._send_json(
                    {
                        "ok": False,
                        "approval": approval,
                        "telegramText": approval.get(
                            "telegramText",
                            "Withdrawal was not approved. No funds moved.",
                        ),
                    },
                    status=400,
                )
                return

            private_key = self.server.user_wallet_service.decrypt_private_key_for_future_signing(
                user_id
            )
            # Past this point a broadcast may already have happened, so a
            # failure can no longer promise that funds stayed put.
            transfer_attempted = True
            if is_native:
                transfer = self.server.user_token_transfer_client.transfer_native(
                    private_key=private_key,
                    to_address=to_address,
                    amount=amount,
                    chain="base",
                )
            else:
                transfer = self.server.user_token_transfer_client.transfer_token(
                    private_key=private_key,
                    to_address=to_address,
                    token_address=token_address,
                    amount=amount,
                    chain="base",
                    decimals=decimals,
                )
            tx_id = str(transfer.get("txId") or transfer.get("transactionHash") or "")
            telegram_text = _withdraw_telegram_text(
                symbol=symbol,
                amount=amount,
                to_address=to_address,
                tx_id=tx_id,
            )
            self._send_json(
                {
                    "ok": True,
                    "token": token,
                    "amount": amount,
                    "toAddress": to_address,
                    "txId": tx_id,
                    "commitmentHash": commitment["hash"],
                    "approval": approval,
                    "transfer": _without_private_key_material(transfer),
                    "telegramText": telegram_text,
                }
            )
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except WalletEncryptionError:
            self._send_json(
                {
                    "ok": False,
                    "error": "wallet_unavailable",
                    "telegramText": "Wallet service is temporarily unavailable.",
                },
                status=503,
            )
        except Exception as exc:
            # Only promise that nothing moved when the failure happened before
            # the transfer could be broadcast. Afterwards the transaction may
            # well be on-chain, and telling the user otherwise invites them to
            # withdraw a second time.
            self._send_json(
                {
                    "ok": False,
                    "error": "withdraw_request_failed",
                    "detail": _redact_error_detail(str(exc)),
                    "fundsMoved": "unknown" if transfer_attempted else "no",
                    "telegramText": (
                        "Withdrawal failed after the transfer was sent. It may still "
                        "have gone through — check your wallet balance before trying "
                        "again."
                        if transfer_attempted
                        else "Withdrawal failed. No funds moved."
                    ),
                },
                status=400,
            )

    def _handle_agent_llm_key_start(self) -> None:
        self._handle_agent_llm_purchase("start")

    def _handle_agent_llm_key_accept_terms(self) -> None:
        self._handle_agent_llm_purchase("accept-terms")

    def _handle_agent_llm_key_verify(self) -> None:
        self._handle_agent_llm_purchase("verify")

    def _handle_agent_llm_key_reconcile(self) -> None:
        self._handle_agent_llm_purchase("reconcile")

    def _handle_agent_llm_credits(self) -> None:
        self._handle_agent_llm_purchase("credits")

    def _handle_agent_llm_purchase(self, operation: str) -> None:
        try:
            payload = self._read_json()
            user_id = _require_authenticated_user(self, payload)
            service = self.server.bankr_llm_purchase_service
            if service is None:
                raise WalletApiTokenNotConfiguredError(
                    "Bankr LLM purchase service is not configured"
                )
            if operation == "start":
                _enforce_user_purchase_rate(user_id)
                result = service.start(
                    telegram_user_id=user_id,
                    amount_usd=_read_required_text(payload, "amountUsd"),
                    email=_read_required_text(payload, "email"),
                    payment_token=str(payload.get("paymentToken") or ""),
                )
            elif operation == "accept-terms":
                result = service.accept_terms(user_id)
            elif operation == "verify":
                result = service.verify_otp(
                    telegram_user_id=user_id,
                    code=_read_required_text(payload, "code"),
                )
                if result.get("state") == "AWAITING_TRANSFER":
                    result = service.resume(str(result.get("purchaseId") or ""))
            elif operation == "reconcile":
                result = service.reconcile(
                    _read_required_text(payload, "purchaseId"),
                    telegram_user_id=user_id,
                )
            elif operation == "credits":
                result = service.credits(user_id)
            else:
                raise ValueError("unsupported Bankr LLM operation")
            self._send_json(result, status=200)
        except WalletApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except BankrLlmError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": exc.code,
                    "telegramText": exc.user_message,
                },
                status=400,
            )
        except WalletEncryptionError:
            self._send_json(
                {
                    "ok": False,
                    "error": "wallet_unavailable",
                    "telegramText": "Wallet service is temporarily unavailable.",
                },
                status=503,
            )
        except Exception as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": "bankr_llm_request_failed",
                    "errorType": type(exc).__name__,
                    "detail": _redact_error_detail(str(exc)),
                    "telegramText": "Bankr LLM request failed. Please try again.",
                },
                status=500,
            )

    def _handle_agent_imessage_pairing(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            user_id = _read_telegram_user_id(payload)
            channel = _read_optional_text(payload, ("channel", "approvalChannel"))
            if channel:
                result = self.server.imessage_approval_service.create_pairing(
                    user_id,
                    channel=channel,
                )
            else:
                result = self.server.imessage_approval_service.create_pairing(user_id)
            self._send_json(_without_private_key_material(result), status=200 if result.get("ok") else 400)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_select_existing_approval_channel(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            result = self.server.imessage_approval_service.select_existing_channel(
                _read_telegram_user_id(payload),
                _read_required_text(payload, "channel"),
            )
            self._send_json(_without_private_key_material(result), status=200)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception:
            self._send_json(
                {"ok": False, "error": "invalid approval channel selection"},
                status=400,
            )

    def _handle_agent_imessage_link(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            channel = _read_optional_text(payload, ("channel", "approvalChannel"))
            if channel or _has_approval_user_id(payload):
                result = self.server.imessage_approval_service.link_sender(
                    _read_required_text(payload, "code"),
                    _read_approval_user_id(payload),
                    channel=channel or "imessage",
                )
            else:
                result = self.server.imessage_approval_service.link_photon_sender(
                    _read_required_text(payload, "code"),
                    _read_photon_user_id(payload),
                )
            self._send_json(_without_private_key_material(result), status=200 if result.get("ok") else 400)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_imessage_unlink(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            result = self.server.imessage_approval_service.unlink_photon_sender(
                telegram_user_id=_read_optional_text(
                    payload,
                    ("telegramUserId", "telegram_user_id", "userId"),
                ),
                photon_user_id=_read_optional_text(
                    payload,
                    ("photonUserId", "photon_user_id"),
                ),
            )
            self._send_json(
                _without_private_key_material(result),
                status=200 if result.get("ok") else 400,
            )
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_imessage_pending(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            channel = _read_optional_text(payload, ("channel", "approvalChannel"))
            if channel or _has_approval_user_id(payload):
                result = self.server.imessage_approval_service.pending_for_photon_sender(
                    _read_approval_user_id(payload),
                    channel=channel or "imessage",
                )
            else:
                result = self.server.imessage_approval_service.pending_for_photon_sender(
                    _read_photon_user_id(payload)
                )
            self._send_json(_without_private_key_material(result), status=200)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_imessage_decision(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            channel = _read_optional_text(payload, ("channel", "approvalChannel"))
            if channel or _has_approval_user_id(payload):
                result = self.server.imessage_approval_service.record_decision(
                    _read_approval_user_id(payload),
                    _read_required_text(payload, "decision"),
                    approval_id=str(payload.get("approvalId", "")).strip() or None,
                    channel=channel or "imessage",
                )
            else:
                result = self.server.imessage_approval_service.record_decision(
                    _read_photon_user_id(payload),
                    _read_required_text(payload, "decision"),
                    approval_id=str(payload.get("approvalId", "")).strip() or None,
                )
            self._send_json(_without_private_key_material(result), status=200 if result.get("ok") else 404)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_test_imessage_approval(self) -> None:
        try:
            _require_imessage_approval_api_token(self)
            payload = self._read_json()
            result = self.server.imessage_approval_service.create_test_approval(
                _read_telegram_user_id(payload)
            )
            self._send_json(_without_private_key_material(result), status=200 if result.get("ok") else 400)
        except ImessageApprovalApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except ImessageApprovalApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_get_latest_event(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
        event = self.server.event_store.read()
        self._send_json({"ok": event is not None, "event": event})

    def _handle_post_latest_event(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
        self._send_json(
            {
                "ok": True,
                "mode": "paid_tool_catalog",
                "tools": list(PAID_TOOLS.values()),
                "nextStep": "POST /agent/inspect-tool with {\"tool\":\"goplausible.weather\"}, then POST /agent/buy-tool.",
            }
        )

    def _handle_agent_inspect_tool(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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
            telegram_user_id = str(payload.get("telegramUserId", "") or "").strip()
            if telegram_user_id:
                self._handle_agent_buy_tool_for_user(
                    payload=payload,
                    tool=tool,
                    resource_url=resource_url,
                    telegram_user_id=telegram_user_id,
                )
                return
            if not self._legacy_operator_request_allowed():
                return
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

    def _handle_agent_buy_tool_for_user(
        self,
        *,
        payload: dict[str, Any],
        tool: dict[str, Any],
        resource_url: str,
        telegram_user_id: str,
    ) -> None:
        # Any exit that does not settle must give the held budget back, so a
        # failed purchase never eats into the user's daily cap.
        reservation_id: str | None = None
        settled = False
        try:
            user_id = _require_authenticated_user(
                self, {"telegramUserId": telegram_user_id}
            )
            _enforce_user_purchase_rate(user_id)
            self.server.user_event_store.preflight_write()
            payment_context = _tool_payment_context(tool, payload)
            request_body = tool.get("requestBody") if isinstance(tool.get("requestBody"), dict) else None
            raw_payment_required = fetch_x402_payment_required(
                resource_url,
                request_body=request_body,
            )
            payment_requirements = normalize_x402_payment_required(
                raw_payment_required,
                resource_url=resource_url,
            )
            _validate_base_usdc_x402_requirement(payment_requirements)
            reservation_id = _reserve_user_wallet_spend(
                self.server, user_id, payment_requirements
            )
            approval = self.server.imessage_approval_service.request_purchase_approval(
                telegram_user_id=user_id,
                tool_name=str(tool.get("name") or "x402 resource"),
                resource_url=resource_url,
                payment_requirements=payment_requirements,
                payment_context=payment_context,
            )
            if not approval.get("ok") or approval.get("status") != "approved":
                self._send_json(
                    {
                        "decision": "rejected_by_imessage",
                        "ok": False,
                        "approval": approval,
                        "telegramText": approval.get(
                            "telegramText",
                            "Purchase was not approved in iMessage.",
                        ),
                    },
                    status=400,
                )
                return

            private_key = self.server.user_wallet_service.decrypt_private_key_for_future_signing(
                user_id
            )
            kwargs: dict[str, Any] = {
                "private_key": private_key,
                "approval": approval,
                "payment_requirements": payment_requirements,
            }
            if payment_context:
                kwargs["payment_context"] = payment_context
            if request_body is not None:
                kwargs["request_body"] = request_body
            result = self.server.user_x402_buyer(resource_url, **kwargs)
            enriched = _tool_result(tool, result, resource_url)
            enriched["decision"] = result.get("decision", "approved_and_executed")
            enriched["ok"] = bool(result.get("ok", False))
            enriched["telegramUserId"] = user_id
            if enriched.get("ok"):
                _settle_user_wallet_spend(
                    self.server,
                    reservation_id,
                    tool,
                    resource_url,
                    payment_requirements,
                    enriched,
                )
                settled = True
                self.server.user_event_store.write(user_id, enriched)
            self._send_json(enriched, status=200 if enriched.get("ok") else 400)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except (WalletApiTokenNotConfiguredError, WalletEncryptionError) as exc:
            # Server-side misconfiguration (missing token / bad master key):
            # signal 503 so callers retry rather than treating it as a bad request.
            self._send_json({"ok": False, "error": str(exc)}, status=503)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            if not settled:
                _release_user_wallet_spend(self.server, reservation_id)

    def _handle_agent_inspect_x402(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
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
        if not self._legacy_operator_request_allowed():
            return
        try:
            payload = self._read_json()
            policy_hash = self._policy_hash_from_payload_or_state(payload)
            result = self.server.bankr_llm_topup_inspector(payload, policy_hash)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_top_up_llm_credits(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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
            user_id = ""
            if str(payload.get("telegramUserId", "") or "").strip():
                user_id = _require_authenticated_user(self, payload)
                payload = {**payload, "telegramUserId": user_id}
            elif not self._legacy_operator_request_allowed():
                return
            result = self.server.bitrefill_quote_service(payload)
            if user_id and isinstance(result, dict) and result.get("priceUsd"):
                _enforce_user_wallet_spend_limits(
                    self.server,
                    user_id,
                    _bitrefill_spend_requirement(
                        result.get("totalUsd") or result["priceUsd"]
                    ),
                )
            self._send_json(result)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_search_bitrefill(self) -> None:
        if not self._bitrefill_catalog_request_allowed():
            return
        try:
            payload = self._read_json()
            result = self.server.bitrefill_search_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_list_bitrefill_products(self) -> None:
        if not self._bitrefill_catalog_request_allowed():
            return
        try:
            payload = self._read_json()
            result = self.server.bitrefill_catalog_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_get_bitrefill_product(self) -> None:
        if not self._bitrefill_catalog_request_allowed():
            return
        try:
            payload = self._read_json()
            result = self.server.bitrefill_product_details_service(payload)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _handle_agent_buy_bitrefill(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            payload = self._read_json()
            result = _sanitize_bitrefill_purchase_result(
                self.server.bitrefill_purchase_runner(payload)
            )
            if result.get("ok"):
                self.server.event_store.write(_without_fulfillment_token(result))
            self._send_json(result)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_buy_wallet_bitrefill(self) -> None:
        try:
            payload = self._read_json()
            user_id = ""
            if str(payload.get("telegramUserId", "") or "").strip():
                user_id = _require_authenticated_user(self, payload)
                _enforce_user_purchase_rate(user_id)
                self.server.user_event_store.preflight_write()
                payload = {**payload, "telegramUserId": user_id}
            elif not self._legacy_operator_request_allowed():
                return
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
            return
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
            return

        if not self._acquire_firefly():
            self._send_json(_busy_payload(), status=409)
            return

        try:
            result = _sanitize_bitrefill_purchase_result(
                self.server.bitrefill_wallet_purchase_runner(payload)
            )
            if result.get("ok"):
                redacted = _without_fulfillment_token(result)
                if user_id:
                    # Per-user purchase: keep it out of the public /events/latest
                    # feed; only the token-gated /agent/last-purchase reads it.
                    self.server.user_event_store.write(user_id, result)
                    if result.get("priceUsd"):
                        _settle_user_wallet_spend(
                            self.server,
                            result.get("spendReservationId"),
                            {"id": "bitrefill"},
                            "bitrefill",
                            _bitrefill_spend_requirement(
                                result.get("totalUsd") or result["priceUsd"]
                            ),
                            result,
                        )
                else:
                    self.server.event_store.write(redacted)
            self._send_json(result)
        except WalletApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
        except Exception as exc:
            self._send_json({"decision": "rejected", "ok": False, "error": str(exc)}, status=400)
        finally:
            self._release_firefly()

    def _handle_agent_get_bitrefill_order(self) -> None:
        if not self._legacy_operator_request_allowed():
            return
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

    def _reject_if_purchases_paused(self) -> bool:
        if not _purchases_paused():
            return False
        self._send_json(
            {
                "ok": False,
                "paused": True,
                "telegramText": _PURCHASES_PAUSED_MESSAGE,
            },
            status=503,
        )
        return True

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
        origin = _cors_allowed_origin(self.headers.get("Origin", ""))
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Sign402-Wallet-Token, "
            "X-Sign402-User-Token, X-Sign402-Photon-Token",
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _service_secret_authorized(self) -> bool:
        expected_secret = os.getenv("SIGN402_BANKR_FULFILLMENT_SECRET", "")
        if not expected_secret:
            return False
        authorization = str(self.headers.get("Authorization", ""))
        return hmac.compare_digest(authorization, f"Bearer {expected_secret}")

    def _legacy_operator_request_allowed(self) -> bool:
        if not _legacy_payment_executor_enabled():
            self._send_json({"error": "not_found"}, status=404)
            return False
        try:
            _require_legacy_operator_token(self)
        except LegacyOperatorApiTokenNotConfiguredError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=503)
            return False
        except LegacyOperatorApiAuthError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=401)
            return False
        return True

    def _bitrefill_catalog_request_allowed(self) -> bool:
        """Catalog endpoints proxy the operator's Bitrefill API key, so they
        require either the wallet API token or a legacy operator token."""
        try:
            _require_wallet_api_token(self)
            return True
        except (WalletApiTokenNotConfiguredError, WalletApiAuthError):
            return self._legacy_operator_request_allowed()

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
        user_event_store: "UserPurchaseStore",
        agent_state_store: "AgentStateStore",
        agent_buy_probe: Callable[[str], dict[str, Any]],
        x402_inspector: Callable[[str, str], dict[str, Any]],
        x402_buyer: Callable[[str], dict[str, Any]],
        user_x402_buyer: Callable[..., dict[str, Any]],
        bankr_llm_topup_inspector: Callable[[dict[str, Any], str], dict[str, Any]],
        bankr_llm_topup: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_search_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_catalog_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_product_details_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_quote_service: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_purchase_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_wallet_purchase_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_order_lookup: Callable[..., dict[str, Any]],
        bitrefill_fulfillment_runner: Callable[[dict[str, Any]], dict[str, Any]],
        bitrefill_settlement_preparation_runner: Callable[[dict[str, Any]], dict[str, Any]],
        user_wallet_service,
        user_wallet_api_token: str,
        user_spend_limit_store: "UserSpendLimitStore",
        buyer_email_store: "BuyerEmailStore | None" = None,
        buyer_email_required: bool = False,
        bankr_llm_purchase_service,
        user_token_transfer_client,
        imessage_approval_service,
        imessage_approval_api_token: str,
    ):
        super().__init__(server_address, handler_class)
        self.firefly = firefly
        self.payment_executor = payment_executor
        self.event_store = event_store
        self.user_event_store = user_event_store
        self.agent_state_store = agent_state_store
        self.agent_buy_probe = agent_buy_probe
        self.x402_inspector = x402_inspector
        self.x402_buyer = x402_buyer
        self.user_x402_buyer = user_x402_buyer
        self.bankr_llm_topup_inspector = bankr_llm_topup_inspector
        self.bankr_llm_topup = bankr_llm_topup
        self.bitrefill_search_service = bitrefill_search_service
        self.bitrefill_catalog_service = bitrefill_catalog_service
        self.bitrefill_product_details_service = bitrefill_product_details_service
        self.bitrefill_quote_service = bitrefill_quote_service
        self.bitrefill_purchase_runner = bitrefill_purchase_runner
        self.bitrefill_wallet_purchase_runner = bitrefill_wallet_purchase_runner
        self.bitrefill_order_lookup = bitrefill_order_lookup
        self.bitrefill_fulfillment_runner = bitrefill_fulfillment_runner
        self.bitrefill_settlement_preparation_runner = bitrefill_settlement_preparation_runner
        self.user_wallet_service = user_wallet_service
        self.user_wallet_api_token = user_wallet_api_token
        self.user_spend_limit_store = user_spend_limit_store
        self.buyer_email_store = buyer_email_store
        # Guest checkout cannot create an invoice without one, so the chat
        # client has to ask for it before it starts a purchase.
        self.buyer_email_required = buyer_email_required
        self.bankr_llm_purchase_service = bankr_llm_purchase_service
        self.user_token_transfer_client = user_token_transfer_client
        self.imessage_approval_service = imessage_approval_service
        self.imessage_approval_api_token = imessage_approval_api_token
        self.firefly_lock = threading.Lock()
        self.buy_tool_response_cache: dict[str, dict[str, Any]] = {}


class DisabledApprovalClient:
    approval_method = "disabled"

    def approve_payment_hash(
        self,
        payment_hash: str,
        *,
        context_lines: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "approved": False,
            "approvedHash": str(payment_hash).lower(),
            "approvalMethod": self.approval_method,
            "error": "approval_provider_disabled",
            "message": (
                "No approval provider is configured. Set SIGN402_APPROVAL_PROVIDER=firefly "
                "for hardware approvals or configure an iMessage approval provider before spending."
            ),
        }


def build_approval_client_from_env(
    *,
    firefly_port: str | None,
    env: dict[str, str] | None = None,
):
    values = os.environ if env is None else env
    provider = values.get("SIGN402_APPROVAL_PROVIDER", "firefly").strip().lower()
    if provider in {"disabled", "none", "server"}:
        return DisabledApprovalClient()
    if provider == "firefly":
        port = firefly_port or find_firefly_port()
        return FireflyClient(port=port)
    raise ValueError(f"unsupported SIGN402_APPROVAL_PROVIDER: {provider}")


def _bounded_int_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
    env: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if env is None else env
    raw = str(values.get(name, default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be an integer from {minimum} to {maximum}"
        ) from None
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be an integer from {minimum} to {maximum}"
        )
    return value


def _bitrefill_live_limit_settings(
    env: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    values = os.environ if env is None else env
    mode = str(values.get("SIGN402_BITREFILL_MODE", "test")).strip().lower()
    if mode != "live":
        return {
            "bitrefillMode": mode,
            "bitrefillLiveMaxUsd": None,
        }
    value = Decimal(
        str(
            values.get(
                "SIGN402_BITREFILL_LIVE_MAX_USD",
                DEFAULT_BITREFILL_LIVE_MAX_USD,
            )
        )
    )
    if not value.is_finite() or value <= 0:
        raise ValueError("SIGN402_BITREFILL_LIVE_MAX_USD must be positive")
    return {
        "bitrefillMode": mode,
        "bitrefillLiveMaxUsd": format_decimal(value),
    }


def bitrefill_checkout_mode(env: dict[str, str] | None = None) -> str:
    """Read which Bitrefill checkout the gateway buys through.

    `account` is the default and the historical behaviour. `guest` opens an
    unauthenticated MCP session so the purchase is attributable to our
    affiliate code, which a purchase from our own account never is.
    """
    values = os.environ if env is None else env
    mode = (
        values.get("SIGN402_BITREFILL_CHECKOUT_MODE", "account").strip().lower()
        or "account"
    )
    if mode not in {"account", "guest"}:
        raise ValueError(
            f"unsupported SIGN402_BITREFILL_CHECKOUT_MODE: {mode}"
        )
    return mode


def build_bitrefill_client_from_env(env: dict[str, str] | None = None):
    from .bitrefill import TestBitrefillClient
    from .bitrefill_guest_auth import GuestMcpAuthorizer, build_http_request
    from .bitrefill_mcp import McpBitrefillClient

    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_MODE", "test").strip().lower()
    if mode == "test":
        return TestBitrefillClient()
    if mode == "live":
        if not values.get("BITREFILL_API_KEY", "").strip():
            raise ValueError("BITREFILL_API_KEY is required in live Bitrefill mode")
        payment_method = values.get("SIGN402_BITREFILL_PAYMENT_METHOD", "balance").strip().lower()
        treasury_client = None
        if payment_method == "usdc_base":
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
        mcp_url = values.get(
            "SIGN402_BITREFILL_MCP_URL",
            "https://api.bitrefill.com/mcp",
        )
        checkout_mode = bitrefill_checkout_mode(values)
        guest_authorizer = (
            GuestMcpAuthorizer(
                mcp_url=mcp_url,
                request=build_http_request(),
            )
            if checkout_mode == "guest"
            else None
        )
        return McpBitrefillClient(
            api_key=values["BITREFILL_API_KEY"],
            mcp_url=mcp_url,
            affiliate_ref=values.get("SIGN402_BITREFILL_AFFILIATE_REF", ""),
            max_purchase_usd=values.get(
                "SIGN402_BITREFILL_LIVE_MAX_USD",
                DEFAULT_BITREFILL_LIVE_MAX_USD,
            ),
            max_invoice_overage_bps=int(
                values.get("SIGN402_BITREFILL_LIVE_MAX_INVOICE_OVERAGE_BPS", "500")
            ),
            payment_method=payment_method,
            checkout_mode=checkout_mode,
            guest_authorizer=guest_authorizer,
            treasury_client=treasury_client,
            invoice_poll_attempts=int(
                values.get("SIGN402_BITREFILL_INVOICE_POLL_ATTEMPTS", "12")
            ),
            invoice_poll_interval_seconds=float(
                values.get(
                    "SIGN402_BITREFILL_INVOICE_POLL_INTERVAL_SECONDS",
                    "5",
                )
            ),
            invoice_min_seconds_left=float(
                values.get("SIGN402_BITREFILL_INVOICE_MIN_SECONDS_LEFT", "180")
            ),
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
    # The priced amount has to clear the same USDC floor again at swap time,
    # seconds later. With no buffer the minimiser leaves the order one tick away
    # from a refund, so a real margin is the default.
    try:
        buffer_bps = int(
            values.get("SIGN402_BITREFILL_PRICING_BUFFER_BPS", "200")
        )
    except (TypeError, ValueError):
        raise ValueError(
            "SIGN402_BITREFILL_PRICING_BUFFER_BPS must be an integer"
        ) from None
    if buffer_bps < 0:
        raise ValueError(
            "SIGN402_BITREFILL_PRICING_BUFFER_BPS must be non-negative"
        )
    return RealRateSingitPricer(
        quote_client=swap_client,
        from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
        to_token=values.get("SIGN402_BANKR_SWAP_TO_TOKEN", "USDC"),
        chain=values.get("SIGN402_BANKR_SWAP_CHAIN", "base"),
        buffer_bps=buffer_bps,
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


def build_bitrefill_user_funding_runner_from_env(
    *,
    user_wallet_service: Any,
    env: dict[str, str] | None = None,
):
    values = os.environ if env is None else env
    mode = values.get("SIGN402_BITREFILL_USER_FUNDING_MODE", "").strip().lower()
    funding_mode = values.get("SIGN402_BITREFILL_FUNDING_MODE", "none").strip().lower()
    if not mode and funding_mode == "cdp_wallet_swap":
        mode = "user_wallet_transfer_to_cdp"
    if mode in {"", "none", "disabled"}:
        return None
    if mode != "user_wallet_transfer_to_cdp":
        raise ValueError(f"unsupported SIGN402_BITREFILL_USER_FUNDING_MODE: {mode}")

    cdp_wallet_address = (
        values.get("SIGN402_CDP_WALLET_ADDRESS", "").strip()
        or values.get("CDP_EVM_ACCOUNT_ADDRESS", "").strip()
    )
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", cdp_wallet_address):
        raise ValueError(
            "SIGN402_CDP_WALLET_ADDRESS or CDP_EVM_ACCOUNT_ADDRESS is required "
            "for user wallet Bitrefill funding"
        )
    return UserWalletTransferToCdpFundingRunner(
        wallet_service=user_wallet_service,
        transfer_client=UserWalletTokenTransferClient(
            Path(values.get("SIGN402_CDP_X402_SERVICE_DIR", str(DEFAULT_CDP_X402_SERVICE_DIR)))
        ),
        cdp_wallet_address=cdp_wallet_address,
        from_token=values.get("SIGN402_BANKR_SWAP_FROM_TOKEN", DEFAULT_SINGIT_TOKEN_ADDRESS),
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
    firefly_port: str | None,
    approval_provider: str | None = None,
    payment_executor_dir: Path = PAYMENT_EXECUTOR_DIR,
    event_store_path: Path = DEFAULT_EVENT_STORE_PATH,
    agent_state_path: Path = DEFAULT_AGENT_STATE_PATH,
    resource_base_url: str = "http://127.0.0.1:8090",
    cdp_x402_service_dir: Path = DEFAULT_CDP_X402_SERVICE_DIR,
    bitrefill_commerce_store_path: Path = DEFAULT_BITREFILL_COMMERCE_STORE_PATH,
    user_wallet_store_path: Path = DEFAULT_USER_WALLET_STORE_PATH,
    user_spend_limit_store_path: Path = DEFAULT_USER_SPEND_LIMIT_STORE_PATH,
    buyer_email_store_path: Path = DEFAULT_BUYER_EMAIL_STORE_PATH,
    imessage_approval_store_path: Path = DEFAULT_IMESSAGE_APPROVAL_STORE_PATH,
) -> Sign402GatewayServer:
    from .bitrefill_runner import (
        BitrefillCatalogService,
        BitrefillFulfillmentRunner,
        BitrefillProductDetailsService,
        BitrefillPurchaseRunner,
        BitrefillQuoteService,
        BitrefillSearchService,
        BitrefillSettlementPreparationRunner,
        WalletBitrefillExecutionPricer,
        WalletPaymentTokenResolver,
        WalletBitrefillPurchaseRunner,
        lookup_bitrefill_order,
    )
    from .commerce_store import BitrefillCommerceStore

    approval_env = None
    if approval_provider is not None:
        approval_env = dict(os.environ)
        approval_env["SIGN402_APPROVAL_PROVIDER"] = approval_provider
    firefly = build_approval_client_from_env(firefly_port=firefly_port, env=approval_env)
    payment_executor = build_payment_executor(payment_executor_dir)
    x402_payment_signature_builder = build_x402_payment_signature_builder(payment_executor_dir)
    base_payment_client = CdpBaseX402PaymentClient(cdp_x402_service_dir)
    bankr_x402_payment_client = BankrCliX402PaymentClient()
    sensitive_state_key = os.environ.get(
        "SIGN402_WALLET_MASTER_KEY",
        "",
    ).strip()
    sensitive_state_cipher = (
        SensitiveStateCipher(sensitive_state_key)
        if sensitive_state_key
        else None
    )
    checkout_mode = bitrefill_checkout_mode()
    if checkout_mode == "guest" and sensitive_state_cipher is None:
        # A guest order is only reachable again through its invoice access
        # token, and the buyer's address is personal data. Neither may be
        # written in the clear, so refuse here rather than at the first sale.
        raise ValueError(
            "SIGN402_WALLET_MASTER_KEY is required for guest Bitrefill checkout"
        )
    event_store = LatestEventStore(event_store_path)
    user_event_store = UserPurchaseStore(
        event_store_path.parent / "user-purchases.json",
        cipher=sensitive_state_cipher,
    )
    user_spend_limit_store = UserSpendLimitStore(user_spend_limit_store_path)
    buyer_email_store = BuyerEmailStore(
        buyer_email_store_path,
        cipher=sensitive_state_cipher,
    )
    agent_state_store = AgentStateStore(agent_state_path)
    bitrefill_commerce_store = BitrefillCommerceStore(
        bitrefill_commerce_store_path,
        cipher=sensitive_state_cipher,
    )
    user_wallet_service = build_wallet_service_from_env(
        env=dict(os.environ),
        store_path=user_wallet_store_path,
    )
    imessage_approval_service = build_imessage_approval_service_from_env(
        env=dict(os.environ),
        wallet_service=user_wallet_service,
        store_path=imessage_approval_store_path,
    )
    bitrefill_client = build_bitrefill_client_from_env()
    real_rate_pricer = build_real_rate_pricer_from_env()
    bitrefill_search_service = BitrefillSearchService(bitrefill_client=bitrefill_client)
    bitrefill_catalog_service = BitrefillCatalogService(bitrefill_client=bitrefill_client)
    bitrefill_product_details_service = BitrefillProductDetailsService(
        bitrefill_client=bitrefill_client
    )
    bitrefill_payment_token_resolver = WalletPaymentTokenResolver(
        user_wallet_service.withdrawable_tokens
    )
    bitrefill_max_reprice_bps = _bounded_int_env(
        "SIGN402_BITREFILL_MAX_REPRICE_BPS",
        default=500,
        minimum=0,
        maximum=500,
    )
    bitrefill_quote_service = BitrefillQuoteService(
        bitrefill_client=bitrefill_client,
        store=bitrefill_commerce_store,
        singit_usd_price_provider=lambda: os.getenv("SIGN402_SINGIT_USD_PRICE", "0.01"),
        real_rate_pricer=real_rate_pricer,
        payment_token_resolver=bitrefill_payment_token_resolver,
        ttl_seconds=int(os.getenv("SIGN402_BITREFILL_QUOTE_TTL_SECONDS", "120")),
        max_reprice_bps=bitrefill_max_reprice_bps,
    )
    bitrefill_funding_runner = build_bitrefill_funding_runner_from_env()
    bitrefill_fulfillment_runner = BitrefillFulfillmentRunner(
        store=bitrefill_commerce_store,
        bitrefill_client=bitrefill_client,
        funding_runner=bitrefill_funding_runner,
        buyer_email_provider=(
            buyer_email_store.get_email if checkout_mode == "guest" else None
        ),
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
    def bitrefill_wallet_approval_client(
        payment_hash: str,
        *,
        context_lines: list[str],
        telegram_user_id: str | None = None,
    ) -> dict[str, Any]:
        if telegram_user_id:
            return imessage_approval_service.request_hash_approval(
                telegram_user_id=telegram_user_id,
                action_type="sign402_bitrefill",
                commitment_hash=payment_hash,
                context_lines=context_lines,
            )
        return firefly.approve_payment_hash(payment_hash, context_lines=context_lines)

    bitrefill_wallet_purchase_runner = WalletBitrefillPurchaseRunner(
        store=bitrefill_commerce_store,
        approval_client=bitrefill_wallet_approval_client,
        fulfillment_runner=bitrefill_fulfillment_runner,
        user_funding_runner=build_bitrefill_user_funding_runner_from_env(
            user_wallet_service=user_wallet_service,
        ),
        execution_pricer=(
            WalletBitrefillExecutionPricer(
                real_rate_pricer=real_rate_pricer,
                payment_token_resolver=bitrefill_payment_token_resolver,
            )
            if real_rate_pricer is not None
            else None
        ),
        return_runner=(
            bitrefill_funding_runner.cdp_client.return_token
            if isinstance(
                bitrefill_funding_runner,
                CdpWalletSwapFundingRunner,
            )
            else None
        ),
        source_wallet_provider=lambda user_id: _managed_wallet_address(
            user_wallet_service,
            user_id,
        ),
        # `server` is bound later in this scope; the closure resolves it at
        # call time, so spend limits are re-checked at buy time (not only at
        # quote time, which a direct /agent/buy-wallet-bitrefill call skips).
        enforce_spend=lambda user_id, quote: _reserve_user_wallet_spend(
            server,
            user_id,
            _bitrefill_spend_requirement(quote.get("totalUsd") or quote["priceUsd"]),
        ),
        release_spend=lambda reservation_id: _release_user_wallet_spend(
            server,
            reservation_id,
        ),
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
    user_x402_buyer = UserWalletX402Buyer(
        base_payment_client=UserWalletBaseX402PaymentClient(cdp_x402_service_dir),
    )
    user_token_transfer_client = UserWalletTokenTransferClient(cdp_x402_service_dir)
    agent_buy_probe = AgentBuyProbeRunner(
        firefly=firefly,
        payment_executor=payment_executor,
        event_store=event_store,
        agent_state_store=agent_state_store,
        resource_base_url=resource_base_url,
    )
    server = Sign402GatewayServer(
        (host, port),
        Sign402GatewayHandler,
        firefly=firefly,
        payment_executor=payment_executor,
        event_store=event_store,
        user_event_store=user_event_store,
        agent_state_store=agent_state_store,
        agent_buy_probe=agent_buy_probe,
        x402_inspector=x402_inspector,
        x402_buyer=x402_buyer,
        user_x402_buyer=user_x402_buyer,
        bankr_llm_topup_inspector=bankr_llm_topup_inspector,
        bankr_llm_topup=bankr_llm_topup,
        bitrefill_search_service=bitrefill_search_service,
        bitrefill_catalog_service=bitrefill_catalog_service,
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
        user_wallet_service=user_wallet_service,
        user_wallet_api_token=os.getenv("SIGN402_WALLET_API_TOKEN", ""),
        user_spend_limit_store=user_spend_limit_store,
        buyer_email_store=buyer_email_store,
        buyer_email_required=checkout_mode == "guest",
        bankr_llm_purchase_service=None,
        user_token_transfer_client=user_token_transfer_client,
        imessage_approval_service=imessage_approval_service,
        imessage_approval_api_token=os.getenv("SIGN402_PHOTON_API_TOKEN", ""),
    )
    server.bankr_llm_purchase_service = build_bankr_llm_purchase_service_from_env(
        env=dict(os.environ),
        wallet_service=user_wallet_service,
        pricer=real_rate_pricer,
        approval_service=imessage_approval_service,
        transfer_client=user_token_transfer_client,
        enforce_spend=lambda user_id, requirement: _enforce_user_wallet_spend_limits(
            server,
            user_id,
            requirement,
        ),
        record_spend=lambda user_id, purchase, metadata: _record_bankr_llm_spend(
            server,
            user_id,
            purchase,
            metadata,
        ),
    )
    return server


def build_payment_executor(payment_executor_dir: Path):
    mode = os.getenv("SIGN402_PAYMENT_EXECUTOR_MODE", "local").strip().lower()
    if mode in {"disabled", "none", "server"}:
        def disabled_pay(requirement: dict[str, Any], policy_hash: str) -> dict[str, Any]:
            raise RuntimeError(
                "Algorand payment executor is disabled. Configure SIGN402_PAYMENT_EXECUTOR_MODE=local "
                "and payment-executor/.env before sending Algorand payments."
            )

        return disabled_pay

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
    mode = os.getenv("SIGN402_PAYMENT_EXECUTOR_MODE", "local").strip().lower()
    if mode in {"disabled", "none", "server"}:
        def disabled_build_signature(payment_required: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(
                "x402-avm payment signature builder is disabled. Configure "
                "SIGN402_PAYMENT_EXECUTOR_MODE=local and payment-executor/.env before "
                "sending Algorand x402 payments."
            )

        return disabled_build_signature

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
    configure_logging(
        level=os.getenv("SIGN402_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
    )
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
    parser.add_argument(
        "--user-wallet-store-path",
        type=Path,
        default=Path(os.getenv("SIGN402_USER_WALLET_STORE_PATH", DEFAULT_USER_WALLET_STORE_PATH)),
    )
    parser.add_argument(
        "--user-spend-limit-store-path",
        type=Path,
        default=Path(
            os.getenv("SIGN402_USER_SPEND_LIMIT_STORE_PATH", DEFAULT_USER_SPEND_LIMIT_STORE_PATH)
        ),
    )
    parser.add_argument(
        "--approval-provider",
        default=os.getenv("SIGN402_APPROVAL_PROVIDER", "firefly"),
        choices=("firefly", "disabled", "none", "server"),
    )
    args = parser.parse_args()

    firefly_port = args.firefly_port
    if args.approval_provider == "firefly":
        firefly_port = firefly_port or find_firefly_port()
    server = build_server(
        args.host,
        args.port,
        firefly_port=firefly_port,
        approval_provider=args.approval_provider,
        payment_executor_dir=args.payment_executor_dir,
        event_store_path=args.event_store_path,
        agent_state_path=args.agent_state_path,
        resource_base_url=args.resource_base_url,
        cdp_x402_service_dir=args.cdp_x402_service_dir,
        user_wallet_store_path=args.user_wallet_store_path,
        user_spend_limit_store_path=args.user_spend_limit_store_path,
    )

    print(f"Sign402 gateway listening on http://{args.host}:{args.port}")
    print(f"Approval provider: {args.approval_provider}")
    if firefly_port:
        print(f"Firefly port: {firefly_port}")
    print(f"Payment executor dir: {args.payment_executor_dir}")
    print(f"Event store path: {args.event_store_path}")
    print(f"Agent state path: {args.agent_state_path}")
    print(f"Resource base URL: {args.resource_base_url}")
    print(f"CDP x402 service dir: {args.cdp_x402_service_dir}")
    print(f"User wallet store path: {args.user_wallet_store_path}")
    print(f"User spend limit store path: {args.user_spend_limit_store_path}")
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
        decimals: int = 18,
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
                "--decimals",
                str(int(decimals)),
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
        decimals: int = 18,
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
                "--decimals",
                str(int(decimals)),
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

    def return_token(
        self,
        *,
        quote_id: str,
        token_address: str,
        to_address: str,
        amount_atomic: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        return self.transfer_token_exact(
            token_address=token_address,
            to_address=to_address,
            amount_atomic=amount_atomic,
            chain=chain,
            idempotency_key=f"bitrefill-return:{quote_id}",
        )

    def transfer_token_exact(
        self,
        *,
        token_address: str,
        to_address: str,
        amount_atomic: str,
        idempotency_key: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        payload = self._run(
            [
                "transfer-token",
                "--token",
                str(token_address),
                "--to",
                str(to_address),
                "--amount-atomic",
                str(amount_atomic),
                "--chain",
                str(chain),
                "--idempotency-key",
                str(idempotency_key),
            ]
        )
        return self._with_tx_id(payload)

    def _run(self, args: list[str]) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        command = ["node", str(script), *args]
        subcommand = args[0] if args else ""
        try:
            result = subprocess.run(
                command,
                cwd=str(self.service_dir),
                check=False,
                capture_output=True,
                text=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:
            log_swallowed_failure(
                logger,
                "CDP wallet service timed out",
                exc,
                command=subcommand,
            )
            raise CdpWalletServiceError("CDP wallet service failed") from None
        if result.returncode != 0:
            # The service's own output never reaches responses or stored state;
            # without this line a CDP failure leaves no trace anywhere.
            log_hidden_detail(
                logger,
                "CDP wallet service exited non-zero",
                f"exit={result.returncode} stderr={result.stderr} stdout={result.stdout}",
                command=subcommand,
            )
            stage = ""
            reason = ""
            try:
                error_payload = json.loads(result.stderr.strip())
            except (json.JSONDecodeError, TypeError):
                error_payload = None
            if (
                isinstance(error_payload, dict)
                and error_payload.get("ok") is False
                and error_payload.get("error") == "CDP wallet service failed"
                and error_payload.get("stage") == "pre_swap"
            ):
                stage = "pre_swap"
                # CdpWalletServiceError drops anything outside the closed set,
                # so provider text can never reach a buyer-facing message.
                reason = str(error_payload.get("reason") or "")
            raise CdpWalletServiceError(
                "CDP wallet service failed",
                stage=stage,
                reason=reason,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CdpWalletServiceError(
                "CDP wallet service returned non-JSON output"
            ) from exc
        if not isinstance(payload, dict):
            raise CdpWalletServiceError(
                "CDP wallet service returned non-object JSON"
            )
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
        token_quote = quote.get("paymentTokenAddress") is not None
        amount = str(
            quote.get("actualPaymentTokenAmount")
            if token_quote
            else quote.get("singitAmount", "")
        ).strip()
        if not amount:
            raise ValueError(
                "fresh payment token amount is required for CDP wallet swap funding"
            )
        from_token = str(
            quote.get("paymentTokenAddress") if token_quote else self.from_token
        ).strip()
        decimals = int(quote.get("paymentTokenDecimals", 18))
        native = bool(quote.get("paymentTokenNative", False)) if token_quote else False
        swap_token = COINBASE_NATIVE_TOKEN_ADDRESS if native else from_token
        required_usdc = str(quote.get("requiredUsdc") or quote.get("priceUsd") or "")
        if token_quote and from_token.casefold() == BASE_USDC_MAINNET.casefold():
            return {
                "ok": True,
                "pricingMode": "bankr_real_rate",
                "mode": "cdp_wallet_usdc_ready",
                "amount": amount,
                "fromToken": from_token,
                "toToken": "USDC",
                "chain": self.chain,
                "expectedUsdc": str(quote.get("expectedUsdc", "")),
                "requiredUsdc": required_usdc,
                "swap": None,
                "txId": None,
            }
        swap_kwargs = {
            "amount": amount,
            "from_token": swap_token,
            "min_usdc": required_usdc,
            "chain": self.chain,
        }
        if token_quote:
            swap_kwargs["decimals"] = decimals
        swap_result = self.cdp_client.swap_singit_to_usdc(**swap_kwargs)
        return {
            "ok": bool(swap_result.get("ok", True)),
            "pricingMode": "bankr_real_rate",
            "mode": "cdp_wallet_swap",
            "amount": amount,
            "fromToken": from_token,
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


class UserWalletBaseX402PaymentClient:
    def __init__(
        self,
        service_dir: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: float = 120.0,
    ):
        self.service_dir = service_dir
        self.runner = runner
        self.timeout = timeout

    def __call__(
        self,
        resource_url: str,
        *,
        private_key: str,
        max_atomic: str | None = None,
        expected_receiver: str | None = None,
        expected_asset: str | None = None,
    ) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")
        if not str(private_key or "").strip():
            raise ValueError("user wallet private key is required")

        # Pass the exact terms the user approved so the node signer refuses to
        # pay a different amount/receiver/asset than was shown in the approval.
        # All three are mandatory: omitting one would leave the signer free to
        # accept whatever the resource server asks for.
        approved_terms = {
            "--max-atomic": str(max_atomic or "").strip(),
            "--expected-receiver": str(expected_receiver or "").strip(),
            "--expected-asset": str(expected_asset or "").strip(),
        }
        missing = sorted(flag for flag, value in approved_terms.items() if not value)
        if missing:
            raise ValueError(
                "refusing to pay without approved terms: "
                + ", ".join(flag.removeprefix("--") for flag in missing)
            )

        command = ["node", str(script), "buy-user", "--url", resource_url]
        for flag, value in approved_terms.items():
            command += [flag, value]

        env = dict(os.environ)
        env["SIGN402_EVM_PRIVATE_KEY"] = str(private_key).strip()
        result = self.runner(
            command,
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        if result.returncode != 0:
            key = str(private_key).strip()
            raw = (result.stderr.strip() or result.stdout.strip() or "").replace(
                key, "***"
            ) if key else (result.stderr.strip() or result.stdout.strip())
            # The node signer surfaces the approval-guard rejection on stderr
            # (e.g. "does not match approved terms"); keep that useful line but
            # never leak the wallet private key into the error text.
            message = raw or "user wallet x402 payment failed"
            raise ValueError(message)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("user wallet x402 service returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise ValueError("user wallet x402 service returned non-object JSON")
        return payload


class UserWalletTokenTransferClient:
    def __init__(
        self,
        service_dir: Path = DEFAULT_CDP_X402_SERVICE_DIR,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: int = 240,
    ):
        self.service_dir = service_dir
        self.runner = runner
        self.timeout = timeout

    def transfer_token(
        self,
        *,
        private_key: str,
        to_address: str,
        token_address: str,
        amount: str,
        chain: str = "base",
        decimals: int = 18,
    ) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")
        if not str(private_key or "").strip():
            raise ValueError("user wallet private key is required")

        command = [
            "node",
            str(script),
            "transfer-token-user",
            "--to",
            str(to_address),
            "--token",
            str(token_address),
            "--amount",
            str(amount),
            "--chain",
            str(chain),
            "--decimals",
            str(int(decimals)),
        ]
        env = dict(os.environ)
        env["SIGN402_EVM_PRIVATE_KEY"] = str(private_key).strip()
        result = self.runner(
            command,
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        if result.returncode != 0:
            key = str(private_key).strip()
            raw = result.stderr.strip() or result.stdout.strip() or ""
            if key:
                raw = raw.replace(key, "***")
            raise ValueError(raw or "user wallet token transfer failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("user wallet token transfer returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise ValueError("user wallet token transfer returned non-object JSON")
        tx_id = payload.get("transactionHash") or payload.get("txId")
        return {**payload, "txId": str(tx_id) if tx_id else None}

    def transfer_native(
        self,
        *,
        private_key: str,
        to_address: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")
        if not str(private_key or "").strip():
            raise ValueError("user wallet private key is required")

        command = [
            "node",
            str(script),
            "transfer-native-user",
            "--to",
            str(to_address),
            "--amount",
            str(amount),
            "--chain",
            str(chain),
        ]
        env = dict(os.environ)
        env["SIGN402_EVM_PRIVATE_KEY"] = str(private_key).strip()
        result = self.runner(
            command,
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        if result.returncode != 0:
            key = str(private_key).strip()
            raw = result.stderr.strip() or result.stdout.strip() or ""
            if key:
                raw = raw.replace(key, "***")
            raise ValueError(raw or "user wallet native transfer failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("user wallet native transfer returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise ValueError("user wallet native transfer returned non-object JSON")
        tx_id = payload.get("transactionHash") or payload.get("txId")
        return {**payload, "txId": str(tx_id) if tx_id else None}

    def _run_read_only(self, args: list[str]) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")
        result = self.runner(
            ["node", str(script), *args],
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip() or result.stdout.strip() or "token read failed"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("token read returned non-JSON output") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("token read returned an invalid payload")
        return payload

    def token_info(self, token_address: str, *, chain: str = "base") -> dict[str, Any]:
        return self._run_read_only(
            ["token-info", "--token", str(token_address), "--chain", str(chain)]
        )

    def token_balance(
        self, token_address: str, owner: str, *, chain: str = "base"
    ) -> str:
        payload = self._run_read_only(
            [
                "token-balance",
                "--token",
                str(token_address),
                "--owner",
                str(owner),
                "--chain",
                str(chain),
            ]
        )
        return str(payload.get("balanceAtomic") or "0")


class UserWalletTransferToCdpFundingRunner:
    def __init__(
        self,
        *,
        wallet_service: Any,
        transfer_client: UserWalletTokenTransferClient,
        cdp_wallet_address: str,
        from_token: str,
        chain: str = "base",
    ):
        self.wallet_service = wallet_service
        self.transfer_client = transfer_client
        self.cdp_wallet_address = cdp_wallet_address
        self.from_token = from_token
        self.chain = chain

    def __call__(
        self,
        *,
        telegram_user_id: str,
        quote: dict[str, Any],
        recipient: dict[str, Any],
    ) -> dict[str, Any]:
        del recipient
        if quote.get("pricingMode") != "bankr_real_rate":
            raise ValueError("user wallet Bitrefill funding requires pricingMode=bankr_real_rate")
        token_quote = quote.get("paymentTokenAddress") is not None
        amount = str(
            quote.get("actualPaymentTokenAmount")
            if token_quote
            else quote.get("singitAmount", "")
        ).strip()
        if not amount:
            raise ValueError(
                "fresh payment token amount is required for user wallet Bitrefill funding"
            )
        user_id = str(telegram_user_id or "").strip()
        if not user_id:
            raise ValueError("telegramUserId is required for user wallet Bitrefill funding")

        token_address = self.from_token
        decimals = 18
        native = False
        wallet_status = None
        if token_quote:
            token_address = str(quote.get("paymentTokenAddress", "")).strip()
            decimals = int(quote.get("paymentTokenDecimals", 18))
            native = bool(quote.get("paymentTokenNative", False))
            wallet_status = self.wallet_service.withdrawable_tokens(user_id)
            tokens = wallet_status.get("tokens", []) if isinstance(wallet_status, dict) else []
            selected = next(
                (
                    token
                    for token in tokens
                    if isinstance(token, dict)
                    and str(token.get("contractAddress", "")).strip().casefold()
                    == token_address.casefold()
                ),
                None,
            )
            if selected is None:
                raise ValueError("selected payment token is no longer available in this wallet")
            if int(selected.get("decimals", -1)) != decimals or bool(
                selected.get("native", False)
            ) != native:
                raise ValueError("selected payment token metadata changed")
            current_balance = Decimal(str(selected.get("balance") or "0"))
            transfer_amount = Decimal(amount)
            if transfer_amount > current_balance:
                raise ValueError("selected payment token balance is insufficient")
            approved_atomic = str(quote.get("maxPaymentTokenAtomic", "")).strip()
            actual_atomic = str(
                quote.get("actualPaymentTokenAtomic", "")
            ).strip()
            amount_atomic = transfer_amount * (Decimal(10) ** decimals)
            if (
                not actual_atomic
                or amount_atomic != Decimal(actual_atomic)
                or amount_atomic != amount_atomic.to_integral_value()
            ):
                raise ValueError(
                    "fresh payment token amount does not match execution pricing"
                )
            if (
                not approved_atomic
                or Decimal(actual_atomic) > Decimal(approved_atomic)
            ):
                raise ValueError(
                    "fresh payment token amount exceeds approved maximum"
                )
            if native and current_balance - transfer_amount < DEFAULT_NATIVE_ETH_WITHDRAW_GAS_RESERVE:
                raise ValueError("ETH payment must leave enough balance for network gas")

        private_key = self.wallet_service.decrypt_private_key_for_future_signing(user_id)
        if wallet_status is None:
            wallet_status = self.wallet_service.wallet_status(user_id)
        wallet = wallet_status.get("wallet") if isinstance(wallet_status, dict) else {}
        from_wallet = str(wallet.get("address", "") if isinstance(wallet, dict) else "")
        if native:
            transfer_result = self.transfer_client.transfer_native(
                private_key=private_key,
                to_address=self.cdp_wallet_address,
                amount=amount,
                chain=self.chain,
            )
        else:
            transfer_kwargs = {
                "private_key": private_key,
                "to_address": self.cdp_wallet_address,
                "token_address": token_address,
                "amount": amount,
                "chain": self.chain,
            }
            if token_quote:
                transfer_kwargs["decimals"] = decimals
            transfer_result = self.transfer_client.transfer_token(**transfer_kwargs)
        return {
            "ok": bool(transfer_result.get("ok", True)),
            "mode": "user_wallet_transfer_to_cdp_swap",
            "fromWallet": from_wallet or str(transfer_result.get("from", "")),
            "toWallet": self.cdp_wallet_address,
            "amount": amount,
            "fromToken": token_address,
            "chain": self.chain,
            "transfer": transfer_result,
            "txId": transfer_result.get("txId"),
        }


class UserWalletX402Buyer:
    def __init__(
        self,
        *,
        base_payment_client: Callable[..., dict[str, Any]],
    ):
        self.base_payment_client = base_payment_client

    def __call__(
        self,
        resource_url: str,
        *,
        private_key: str,
        approval: dict[str, Any],
        payment_requirements: dict[str, Any],
        payment_context: dict[str, str] | None = None,
        request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not approval.get("ok") or approval.get("status") != "approved":
            return {
                "decision": "rejected_by_imessage",
                "ok": False,
                "resourceUrl": resource_url,
                "approval": approval,
            }
        if request_body is not None:
            raise ValueError("user wallet x402 purchases currently support GET resources only")
        _validate_base_usdc_x402_requirement(payment_requirements)

        resource_result = self.base_payment_client(
            resource_url,
            private_key=private_key,
            max_atomic=str(payment_requirements["amountAtomic"]),
            expected_receiver=str(payment_requirements["receiver"]),
            expected_asset=str(payment_requirements["asset"]),
        )
        if int(resource_result.get("status", 0)) != 200:
            raise ValueError(f"Official x402 resource denied payment: {resource_result}")

        payment_response = resource_result.get("paymentResponse", {})
        tx_id = (
            payment_response.get("transaction")
            or payment_response.get("transactionHash")
            or payment_response.get("txHash")
            or resource_result.get("transactionHash")
        )
        event = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_user_wallet",
            "resourceUrl": resource_url,
            "approvalId": approval.get("approvalId"),
            "paymentApprovalHash": approval.get("commitmentHash"),
            "txId": tx_id,
            "paymentIntent": payment_requirements.get("paymentIntent"),
            "amountAtomic": payment_requirements["amountAtomic"],
            "asset": payment_requirements["asset"],
            "network": payment_requirements["network"],
            "x402Network": payment_requirements.get("x402Network"),
            "receiver": payment_requirements["receiver"],
            "paymentRequirements": payment_requirements,
            "paymentResponse": payment_response,
            "resourceResult": resource_result,
            "result": "official_x402_resource_access_granted",
            "telegramText": _user_wallet_x402_telegram_text(
                payment_requirements=payment_requirements,
                tx_id=str(tx_id or ""),
                resource_result=resource_result,
                payment_context=payment_context,
            ),
        }
        return event


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


class UserPurchaseStore:
    """Per-user latest purchase, kept OUT of the public global event store.

    Purchases made from a user's managed wallet carry their telegram id, wallet
    address and payment details. The global LatestEventStore is served
    unauthenticated by /events/latest (the demo dashboard), so per-user
    purchases are persisted here instead, keyed by telegram user id, and read
    back only through the token-gated /agent/last-purchase.
    """

    def __init__(
        self,
        path: Path,
        *,
        cipher: SensitiveStateCipher | None = None,
    ) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.cipher = cipher

    def _read_all_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _assert_no_legacy_tokens(data: dict[str, Any]) -> None:
        if any(
            isinstance(item, dict) and "fulfillmentToken" in item
            for item in data.values()
        ):
            raise SensitiveStateError(
                "legacy plaintext fulfillment tokens must be migrated "
                "before updating user purchase state"
            )

    def _persisted_event(self, event: dict[str, Any]) -> dict[str, Any]:
        persisted = dict(event)
        persisted.pop("encryptedFulfillmentToken", None)
        if "fulfillmentToken" in persisted:
            if self.cipher is None:
                raise SensitiveStateConfigurationError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist fulfillment tokens"
                )
            token = str(persisted.pop("fulfillmentToken"))
            persisted["encryptedFulfillmentToken"] = self.cipher.encrypt_text(token)
        return persisted

    def preflight_write(self) -> None:
        with self.lock:
            if self.cipher is None:
                raise SensitiveStateConfigurationError(
                    "SIGN402_WALLET_MASTER_KEY is required "
                    "to persist user purchase state"
                )
            self._assert_no_legacy_tokens(self._read_all_unlocked())

    def write(
        self,
        telegram_user_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            data[key] = self._persisted_event(event)
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)
            return event

    def read(self, telegram_user_id: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._read_all_unlocked().get(str(telegram_user_id))
            if not isinstance(value, dict):
                return None
            event = dict(value)
            if "encryptedFulfillmentToken" in event:
                encrypted = event.pop("encryptedFulfillmentToken")
                if self.cipher is None:
                    raise SensitiveStateConfigurationError(
                        "SIGN402_WALLET_MASTER_KEY is required "
                        "to read encrypted fulfillment tokens"
                    )
                event.pop("fulfillmentToken", None)
                event["fulfillmentToken"] = self.cipher.decrypt_text(str(encrypted))
            return event

    def clear_fulfillment_token(self, telegram_user_id: str) -> None:
        key = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            event = data.get(key)
            if not isinstance(event, dict):
                return
            if (
                "fulfillmentToken" not in event
                and "encryptedFulfillmentToken" not in event
            ):
                return
            cleaned = dict(event)
            cleaned.pop("fulfillmentToken", None)
            cleaned.pop("encryptedFulfillmentToken", None)
            data[key] = cleaned
            self._assert_no_legacy_tokens(data)
            atomic_write_private_json(self.path, data)


class UserSpendLimitStore:
    """Successful per-user wallet spends for enforcing daily caps."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _read_all_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"records": [], "limits": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"records": [], "limits": {}}

    def _write_all_unlocked(self, data: dict[str, Any]) -> None:
        atomic_write_private_json(self.path, data)

    def limit_settings(
        self,
        telegram_user_id: str,
        *,
        operator_max_per_tx_atomic: int | None,
        operator_daily_cap_atomic: int | None,
        operator_ceiling_per_tx_atomic: int | None = None,
        operator_ceiling_daily_atomic: int | None = None,
    ) -> dict[str, Any]:
        user_id = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            limits = data.get("limits")
            user_limits = limits.get(user_id) if isinstance(limits, dict) else None
            if not isinstance(user_limits, dict):
                user_limits = {}

        user_max_per_tx = _optional_int(user_limits.get("maxPerTxAtomic"))
        user_daily_cap = _optional_int(user_limits.get("dailyCapAtomic"))
        max_per_tx = user_max_per_tx if user_max_per_tx is not None else operator_max_per_tx_atomic
        daily_cap = user_daily_cap if user_daily_cap is not None else operator_daily_cap_atomic
        # Operator ceilings are a hard maximum a user may never exceed. Clamp on
        # read too, so a stored user limit can never exceed a ceiling the
        # operator lowered after the user configured their own limit.
        if operator_ceiling_per_tx_atomic is not None and max_per_tx is not None:
            max_per_tx = min(max_per_tx, operator_ceiling_per_tx_atomic)
        if operator_ceiling_daily_atomic is not None and daily_cap is not None:
            daily_cap = min(daily_cap, operator_ceiling_daily_atomic)
        return {
            "telegramUserId": user_id,
            "maxPerTxAtomic": max_per_tx,
            "dailyCapAtomic": daily_cap,
            "maxPerTxUsdc": _format_usdc_atomic(max_per_tx),
            "dailyCapUsdc": _format_usdc_atomic(daily_cap),
            "operatorMaxPerTxAtomic": operator_max_per_tx_atomic,
            "operatorDailyCapAtomic": operator_daily_cap_atomic,
            "operatorCeilingPerTxAtomic": operator_ceiling_per_tx_atomic,
            "operatorCeilingDailyAtomic": operator_ceiling_daily_atomic,
            "userMaxPerTxAtomic": user_max_per_tx,
            "userDailyCapAtomic": user_daily_cap,
            "userConfigured": user_max_per_tx is not None or user_daily_cap is not None,
        }

    def set_limit_settings(
        self,
        telegram_user_id: str,
        *,
        max_per_tx_atomic: int,
        daily_cap_atomic: int,
        operator_max_per_tx_atomic: int | None,
        operator_daily_cap_atomic: int | None,
        operator_ceiling_per_tx_atomic: int | None = None,
        operator_ceiling_daily_atomic: int | None = None,
    ) -> dict[str, Any]:
        if max_per_tx_atomic <= 0:
            raise ValueError("max per transaction limit must be greater than zero")
        if daily_cap_atomic <= 0:
            raise ValueError("daily spending limit must be greater than zero")
        if daily_cap_atomic < max_per_tx_atomic:
            raise ValueError("daily spending limit must be at least the per-transaction limit")
        # Operator defaults (operator_max/daily) are starting values a user may
        # raise; operator ceilings are a hard maximum they may not. Enforced at
        # write time; limit_settings additionally re-clamps on read so a later
        # lowered ceiling still wins over a previously stored user limit.
        if (
            operator_ceiling_per_tx_atomic is not None
            and max_per_tx_atomic > operator_ceiling_per_tx_atomic
        ):
            raise ValueError(
                "max per transaction limit exceeds the operator ceiling "
                f"{_format_usdc_atomic(operator_ceiling_per_tx_atomic)} USDC"
            )
        if (
            operator_ceiling_daily_atomic is not None
            and daily_cap_atomic > operator_ceiling_daily_atomic
        ):
            raise ValueError(
                "daily spending limit exceeds the operator ceiling "
                f"{_format_usdc_atomic(operator_ceiling_daily_atomic)} USDC"
            )

        user_id = str(telegram_user_id)
        with self.lock:
            data = self._read_all_unlocked()
            limits = data.get("limits")
            if not isinstance(limits, dict):
                limits = {}
            limits[user_id] = {
                "maxPerTxAtomic": int(max_per_tx_atomic),
                "dailyCapAtomic": int(daily_cap_atomic),
                "updatedAt": time.time(),
            }
            data["limits"] = limits
            self._write_all_unlocked(data)
        return self.limit_settings(
            user_id,
            operator_max_per_tx_atomic=operator_max_per_tx_atomic,
            operator_daily_cap_atomic=operator_daily_cap_atomic,
            operator_ceiling_per_tx_atomic=operator_ceiling_per_tx_atomic,
            operator_ceiling_daily_atomic=operator_ceiling_daily_atomic,
        )

    def spent_today_atomic(self, telegram_user_id: str, *, asset: str, network: str) -> int:
        with self.lock:
            data = self._read_all_unlocked()
            return self._spent_today_unlocked(
                data,
                str(telegram_user_id),
                asset=asset,
                network=network,
            )

    def _matches_scope(
        self,
        entry: Any,
        user_id: str,
        *,
        asset_key: str,
        network_key: str,
        today: str,
    ) -> bool:
        return (
            isinstance(entry, dict)
            and str(entry.get("telegramUserId")) == user_id
            and str(entry.get("day")) == today
            and str(entry.get("asset", "")).lower() == asset_key
            and str(entry.get("network")) == network_key
        )

    def _spent_today_unlocked(
        self,
        data: dict[str, Any],
        user_id: str,
        *,
        asset: str,
        network: str,
        now: float | None = None,
    ) -> int:
        """Settled spend plus every hold that has not expired yet.

        A reservation counts against the cap exactly like a settled record, so
        a purchase waiting on human approval cannot be measured against a total
        that ignores it.
        """
        asset_key = str(asset).lower()
        network_key = str(network)
        today = self._today_key()
        current_time = time.time() if now is None else now
        total = 0
        for entry in self._entries(data, "records"):
            if not self._matches_scope(
                entry, user_id, asset_key=asset_key, network_key=network_key, today=today
            ):
                continue
            try:
                total += int(str(entry.get("amountAtomic", "0")))
            except ValueError:
                continue
        for entry in self._entries(data, "reservations"):
            if not self._matches_scope(
                entry, user_id, asset_key=asset_key, network_key=network_key, today=today
            ):
                continue
            try:
                if float(entry.get("expiresAt", 0)) <= current_time:
                    continue
                total += int(str(entry.get("amountAtomic", "0")))
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _entries(data: dict[str, Any], key: str) -> list[Any]:
        value = data.get(key)
        return value if isinstance(value, list) else []

    @staticmethod
    def _prune_old_records(data: dict[str, Any], now: float) -> None:
        """Drop settled records too old to affect any daily cap.

        The whole file is rewritten on every spend, so an unbounded history
        makes each purchase progressively slower. Only today's records matter
        for the cap; the rest is kept for a short audit window and then cut.
        """
        cutoff_day = time.strftime(
            "%Y-%m-%d",
            time.gmtime(now - SPEND_RECORD_RETENTION_DAYS * 86400),
        )
        data["records"] = [
            entry
            for entry in UserSpendLimitStore._entries(data, "records")
            if isinstance(entry, dict) and str(entry.get("day", "")) >= cutoff_day
        ]

    @staticmethod
    def _drop_expired_reservations(data: dict[str, Any], now: float) -> None:
        reservations = UserSpendLimitStore._entries(data, "reservations")
        data["reservations"] = [
            entry
            for entry in reservations
            if isinstance(entry, dict) and _safe_float(entry.get("expiresAt")) > now
        ]

    def reserve_within_limits(
        self,
        telegram_user_id: str,
        *,
        amount_atomic: int,
        asset: str,
        network: str,
        max_per_tx_atomic: int | None,
        daily_cap_atomic: int | None,
        ttl_seconds: float = SPEND_RESERVATION_TTL_SECONDS,
    ) -> str | None:
        """Check the caps and hold the amount in one atomic step.

        Returns a reservation id, or None when the amount does not fit. The
        caller must settle the reservation once the payment succeeds, or
        release it on rejection, timeout, or error.
        """
        amount = int(amount_atomic)
        if max_per_tx_atomic is not None and amount > int(max_per_tx_atomic):
            return None

        user_id = str(telegram_user_id)
        now = time.time()
        reservation_id = f"hold_{secrets.token_urlsafe(12)}"
        with self.lock:
            data = self._read_all_unlocked()
            self._drop_expired_reservations(data, now)
            if daily_cap_atomic is not None:
                spent = self._spent_today_unlocked(
                    data, user_id, asset=asset, network=network, now=now
                )
                if spent + amount > int(daily_cap_atomic):
                    self._write_all_unlocked(data)
                    return None
            reservations = self._entries(data, "reservations")
            reservations.append(
                {
                    "reservationId": reservation_id,
                    "telegramUserId": user_id,
                    "day": self._today_key(),
                    "amountAtomic": amount,
                    "asset": str(asset),
                    "network": str(network),
                    "createdAt": now,
                    "expiresAt": now + float(ttl_seconds),
                }
            )
            data["reservations"] = reservations
            self._write_all_unlocked(data)
        return reservation_id

    def release_reservation(self, reservation_id: str | None) -> None:
        if not reservation_id:
            return
        with self.lock:
            data = self._read_all_unlocked()
            self._drop_expired_reservations(data, time.time())
            data["reservations"] = [
                entry
                for entry in self._entries(data, "reservations")
                if str(entry.get("reservationId")) != str(reservation_id)
            ]
            self._write_all_unlocked(data)

    def settle_reservation(
        self,
        reservation_id: str | None,
        *,
        tx_id: str = "",
        payment_intent: str | None = None,
        approval_id: str | None = None,
        tool_id: str | None = None,
        resource_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Turn a hold into a settled record without double-counting it."""
        if not reservation_id:
            return None
        with self.lock:
            data = self._read_all_unlocked()
            now = time.time()
            self._drop_expired_reservations(data, now)
            reservations = self._entries(data, "reservations")
            held = next(
                (
                    entry
                    for entry in reservations
                    if str(entry.get("reservationId")) == str(reservation_id)
                ),
                None,
            )
            if held is None:
                # The hold expired mid-flight. Still record the spend, so a slow
                # payment cannot settle without counting against the cap.
                self._write_all_unlocked(data)
                return None
            data["reservations"] = [
                entry
                for entry in reservations
                if str(entry.get("reservationId")) != str(reservation_id)
            ]
            record = {
                "telegramUserId": str(held.get("telegramUserId")),
                "day": str(held.get("day")),
                "amountAtomic": int(str(held.get("amountAtomic", "0"))),
                "asset": str(held.get("asset", "")),
                "network": str(held.get("network", "")),
                "txId": str(tx_id or ""),
                "paymentIntent": str(payment_intent or ""),
                "approvalId": str(approval_id or ""),
                "toolId": str(tool_id or ""),
                "resourceUrl": str(resource_url or ""),
                "recordedAt": now,
            }
            records = self._entries(data, "records")
            records.append(record)
            data["records"] = records
            self._prune_old_records(data, now)
            self._write_all_unlocked(data)
        return record

    def record_successful_spend(
        self,
        telegram_user_id: str,
        *,
        amount_atomic: int,
        asset: str,
        network: str,
        tx_id: str,
        payment_intent: str | None = None,
        approval_id: str | None = None,
        tool_id: str | None = None,
        resource_url: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "telegramUserId": str(telegram_user_id),
            "day": self._today_key(),
            "amountAtomic": int(amount_atomic),
            "asset": str(asset),
            "network": str(network),
            "txId": str(tx_id or ""),
            "paymentIntent": str(payment_intent or ""),
            "approvalId": str(approval_id or ""),
            "toolId": str(tool_id or ""),
            "resourceUrl": str(resource_url or ""),
            "recordedAt": time.time(),
        }
        with self.lock:
            data = self._read_all_unlocked()
            records = data.get("records")
            if not isinstance(records, list):
                records = []
            records.append(record)
            data["records"] = records
            self._prune_old_records(data, time.time())
            self._write_all_unlocked(data)
        return record


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


class RateLimitExceededError(ValueError):
    """Raised when a per-user request or purchase budget is exhausted."""


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window limiter keyed by (scope, key)."""

    def __init__(self):
        self._events: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, scope: str, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        bucket_key = (scope, str(key))
        cutoff = now - window_seconds
        with self._lock:
            events = [t for t in self._events.get(bucket_key, []) if t > cutoff]
            if len(events) >= limit:
                self._events[bucket_key] = events
                return False
            events.append(now)
            self._events[bucket_key] = events
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_USER_RATE_LIMITER = SlidingWindowRateLimiter()

DEFAULT_USER_REQUESTS_PER_MINUTE = "60"
DEFAULT_USER_PURCHASES_PER_HOUR = "20"
DEFAULT_WALLET_CREATIONS_PER_MINUTE = "30"


def _rate_limit_setting(env_name: str, default_value: str) -> int | None:
    raw = os.getenv(env_name, default_value).strip()
    if not raw:
        return None
    value = int(raw)
    return value if value > 0 else None


def _enforce_user_request_rate(user_id: str) -> None:
    limit = _rate_limit_setting(
        "SIGN402_USER_REQUESTS_PER_MINUTE",
        DEFAULT_USER_REQUESTS_PER_MINUTE,
    )
    if limit is None:
        return
    if not _USER_RATE_LIMITER.allow("requests", user_id, limit=limit, window_seconds=60.0):
        raise RateLimitExceededError(
            "Too many wallet requests. Please wait a minute and try again."
        )


def _enforce_wallet_creation_rate(telegram_user_id: str) -> None:
    """Bound wallet creation per user and across all users.

    The per-user bucket alone would not help: the caller chooses the id, so it
    could walk a fresh id every request. The global bucket is what actually
    caps a leaked shared token.
    """
    _enforce_user_request_rate(telegram_user_id)
    limit = _rate_limit_setting(
        "SIGN402_WALLET_CREATIONS_PER_MINUTE",
        DEFAULT_WALLET_CREATIONS_PER_MINUTE,
    )
    if limit is None:
        return
    if not _USER_RATE_LIMITER.allow(
        "wallet_creations", "global", limit=limit, window_seconds=60.0
    ):
        raise RateLimitExceededError(
            "Too many wallet creations right now. Please try again shortly."
        )


def _enforce_user_purchase_rate(user_id: str) -> None:
    limit = _rate_limit_setting(
        "SIGN402_USER_PURCHASES_PER_HOUR",
        DEFAULT_USER_PURCHASES_PER_HOUR,
    )
    if limit is None:
        return
    if not _USER_RATE_LIMITER.allow("purchases", user_id, limit=limit, window_seconds=3600.0):
        raise RateLimitExceededError(
            "Too many purchase attempts this hour. Please try again later."
        )


def _read_telegram_user_id(payload: dict[str, Any]) -> str:
    for key in ("telegramUserId", "telegram_user_id", "userId"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    raise ValueError("telegramUserId is required")


def _authenticated_user_id(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> str:
    """Resolve which user the caller may act as.

    When a per-user access token is supplied (X-Sign402-User-Token) it is
    authoritative: the user id is taken from the token, and a body-supplied
    telegramUserId that disagrees is rejected — so a token holder cannot act as
    a different user. When absent, falls back to the body id (shared-token
    migration path).
    """
    token = str(handler.headers.get("X-Sign402-User-Token", "") or "").strip()
    if not token:
        return _read_telegram_user_id(payload)
    resolved = handler.server.user_wallet_service.resolve_telegram_user_id(token)
    if not resolved:
        raise WalletApiAuthError("invalid per-user access token")
    body_user_id = ""
    for key in ("telegramUserId", "telegram_user_id", "userId"):
        body_user_id = str(payload.get(key, "") or "").strip()
        if body_user_id:
            break
    if body_user_id and body_user_id != str(resolved):
        raise WalletApiAuthError("per-user access token does not match requested user")
    return str(resolved)


def _require_authenticated_user(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
) -> str:
    _require_wallet_api_token(handler)
    token = str(handler.headers.get("X-Sign402-User-Token", "") or "").strip()
    if not token:
        raise WalletApiAuthError("per-user access token is required")
    user_id = _authenticated_user_id(handler, payload)
    _enforce_user_request_rate(user_id)
    return user_id


def _read_photon_user_id(payload: dict[str, Any]) -> str:
    for key in ("photonUserId", "photon_user_id", "userId"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    raise ValueError("photonUserId is required")


def _has_approval_user_id(payload: dict[str, Any]) -> bool:
    return any(
        str(payload.get(key, "") or "").strip()
        for key in ("approvalUserId", "approval_user_id")
    )


def _read_approval_user_id(payload: dict[str, Any]) -> str:
    for key in (
        "approvalUserId",
        "approval_user_id",
        "photonUserId",
        "photon_user_id",
        "userId",
    ):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    raise ValueError("approvalUserId is required")


def _read_required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "") or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _read_optional_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _read_evm_address(payload: dict[str, Any], key: str) -> str:
    value = _read_required_text(payload, key)
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", value):
        raise ValueError(f"{key} must be a Base EVM address")
    return value


def _read_withdraw_asset(payload: dict[str, Any], key: str) -> str:
    value = _read_required_text(payload, key)
    if value.lower() == BASE_NATIVE_ETH_ASSET_ID:
        return BASE_NATIVE_ETH_ASSET_ID
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", value):
        raise ValueError(f"{key} must be a Base token address or native")
    return value


def _read_positive_amount(payload: dict[str, Any], key: str) -> str:
    value = _read_required_text(payload, key)
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be a positive number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{key} must be a positive number")
    if amount.as_tuple().exponent < -36:
        raise ValueError(f"{key} has too many decimal places")
    return format_decimal(amount)


def _native_eth_withdraw_gas_reserve() -> Decimal:
    raw = str(
        os.environ.get(
            "SIGN402_NATIVE_ETH_WITHDRAW_GAS_RESERVE_ETH",
            str(DEFAULT_NATIVE_ETH_WITHDRAW_GAS_RESERVE),
        )
    ).strip()
    try:
        reserve = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("SIGN402_NATIVE_ETH_WITHDRAW_GAS_RESERVE_ETH must be positive") from exc
    if not reserve.is_finite() or reserve <= 0:
        raise ValueError("SIGN402_NATIVE_ETH_WITHDRAW_GAS_RESERVE_ETH must be positive")
    return reserve


def _find_withdrawable_token(
    inventory: dict[str, Any],
    token_address: str,
) -> dict[str, Any] | None:
    if not isinstance(inventory, dict) or inventory.get("ok") is not True:
        return None
    for token in inventory.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        contract = str(token.get("contractAddress") or "")
        if contract.lower() != token_address.lower():
            continue
        decimals = token.get("decimals")
        if isinstance(decimals, bool) or not isinstance(decimals, int):
            return None
        if decimals < 0 or decimals > 36:
            return None
        return token
    return None


def _withdraw_commitment(
    *,
    telegram_user_id: str,
    from_address: str,
    to_address: str,
    token: dict[str, Any],
    amount: str,
) -> dict[str, str]:
    canonical = {
        "schemaVersion": 1,
        "actionType": "sign402_withdrawal",
        "telegramUserId": str(telegram_user_id),
        "fromAddress": str(from_address),
        "toAddress": str(to_address),
        "tokenAddress": str(token.get("contractAddress") or ""),
        "tokenSymbol": str(token.get("symbol") or "ERC20"),
        "tokenDecimals": int(token.get("decimals")),
        "amount": str(amount),
        "network": "base-mainnet",
        "nonce": secrets.token_hex(16),
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return {
        "canonicalJson": canonical_json,
        "hash": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    }


def _withdraw_approval_context_lines(
    *,
    symbol: str,
    amount: str,
    from_address: str,
    to_address: str,
) -> list[str]:
    # The destination is shown in full, never abbreviated. A withdrawal's only
    # safeguard is this approval, and it is worth nothing if the approver cannot
    # tell the real address from a look-alike: an abbreviation to first-6 and
    # last-4 hex leaves ~40 bits to collide, which a vanity generator does in
    # minutes. The amount and destination are what the human actually verifies.
    return [
        f"Action: WITHDRAW {symbol}",
        f"Amount: {amount} {symbol}",
        f"To: {to_address}",
        f"From: {_short_address(from_address)}",
        "Network: Base",
    ]


def _withdraw_telegram_text(
    *,
    symbol: str,
    amount: str,
    to_address: str,
    tx_id: str,
) -> str:
    tx_url = _evm_transaction_url(tx_id, network="base-mainnet") if tx_id else ""
    suffix = f"\nTx: {tx_url}" if tx_url else ""
    return (
        f"Withdrawal sent: {amount} {symbol} to {_short_address(to_address)}."
        f"{suffix}"
    )


def _short_address(address: str) -> str:
    text = str(address or "")
    if len(text) < 12:
        return text
    return f"{text[:8]}...{text[-4:]}"


def _redact_error_detail(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"bk_[A-Za-z0-9_-]+", "bk_[redacted]", text)
    text = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b",
        "[jwt-redacted]",
        text,
    )
    text = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[token-redacted]", text)
    return text[:500]


class WalletApiAuthError(ValueError):
    pass


class WalletApiTokenNotConfiguredError(RuntimeError):
    pass


class ImessageApprovalApiAuthError(ValueError):
    pass


class ImessageApprovalApiTokenNotConfiguredError(RuntimeError):
    pass


class LegacyOperatorApiAuthError(ValueError):
    pass


class LegacyOperatorApiTokenNotConfiguredError(RuntimeError):
    pass


def _legacy_payment_executor_enabled() -> bool:
    return str(
        os.environ.get("SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR", "")
    ).strip().lower() in {"1", "true", "yes", "on"}


def _test_endpoints_enabled() -> bool:
    """Keep non-product approval probes out of normal production runtime."""
    return str(os.environ.get("SIGN402_ENABLE_TEST_ENDPOINTS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cors_enabled() -> bool:
    return str(os.environ.get("SIGN402_ENABLE_CORS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cors_allowed_origin(origin: str) -> str:
    """Return an explicitly allowlisted browser origin, or an empty string."""
    if not _cors_enabled():
        return ""
    requested = str(origin or "").strip().rstrip("/")
    if not requested:
        return ""
    configured = {
        value.strip().rstrip("/")
        for value in str(os.environ.get("SIGN402_CORS_ALLOWED_ORIGINS", "")).split(",")
        if value.strip().rstrip("/")
    }
    return requested if requested in configured else ""


def _require_legacy_operator_token(handler: BaseHTTPRequestHandler) -> None:
    expected = str(
        os.environ.get("SIGN402_LEGACY_OPERATOR_API_TOKEN", "")
    ).strip()
    if not expected:
        raise LegacyOperatorApiTokenNotConfiguredError(
            "SIGN402_LEGACY_OPERATOR_API_TOKEN is required when legacy payment execution is enabled"
        )
    authorization = str(handler.headers.get("Authorization", "") or "").strip()
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise LegacyOperatorApiAuthError("invalid legacy operator API token")


def _require_wallet_api_token(handler: BaseHTTPRequestHandler) -> None:
    expected = str(getattr(handler.server, "user_wallet_api_token", "") or "").strip()
    if not expected:
        raise WalletApiTokenNotConfiguredError("SIGN402_WALLET_API_TOKEN is required")

    authorization = str(handler.headers.get("Authorization", "") or "").strip()
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = str(handler.headers.get("X-Sign402-Wallet-Token", "") or "").strip()

    if not supplied or not hmac.compare_digest(supplied, expected):
        raise WalletApiAuthError("invalid wallet API token")


def _require_imessage_approval_api_token(handler: BaseHTTPRequestHandler) -> None:
    expected = str(
        getattr(handler.server, "imessage_approval_api_token", "") or ""
    ).strip()
    if not expected:
        raise ImessageApprovalApiTokenNotConfiguredError(
            "SIGN402_PHOTON_API_TOKEN is required"
        )

    authorization = str(handler.headers.get("Authorization", "") or "").strip()
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = str(handler.headers.get("X-Sign402-Photon-Token", "") or "").strip()

    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ImessageApprovalApiAuthError("invalid iMessage approval API token")


def _without_private_key_material(value):
    if isinstance(value, dict):
        return {
            key: _without_private_key_material(item)
            for key, item in value.items()
            if "private" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_without_private_key_material(item) for item in value]
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


def _user_wallet_x402_telegram_text(
    *,
    payment_requirements: dict[str, Any],
    tx_id: str,
    resource_result: dict[str, Any],
    payment_context: dict[str, str] | None = None,
) -> str:
    tx_url = _evm_transaction_url(tx_id, str(payment_requirements.get("network") or ""))
    amount = _format_display_amount(
        {
            "amountAtomic": payment_requirements.get("amountAtomic"),
            "asset": payment_requirements.get("asset"),
            "extra": payment_requirements.get("extra", {}),
        }
    )
    title = str((payment_context or {}).get("title") or "x402 resource").strip()
    if title.isupper():
        title = title.title()
    body = resource_result.get("body") if isinstance(resource_result.get("body"), dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    report = str(data.get("report") or body.get("report") or "").strip()

    text = f"✅ {title} unlocked. Paid {amount}."
    if tx_url:
        text += f" Tx {tx_url}."
    if report:
        text += f"\n{_compact_multiline(report)}"
    return text


def _base_x402_quote_text(requirement: dict[str, Any]) -> str:
    amount = _format_display_amount(requirement)
    return f"Base x402 quote: {amount} on Base Mainnet."


DEFAULT_USER_WALLET_MAX_ATOMIC_PER_TX = "10000"  # 0.01 USDC (6 decimals)
DEFAULT_USER_WALLET_DAILY_ATOMIC_CAP = "100000"  # 0.10 USDC (6 decimals)


def _payment_amount_atomic(payment_requirements: dict[str, Any]) -> int:
    amount = int(str(payment_requirements["amountAtomic"]))
    if amount < 0:
        raise ValueError("amountAtomic must be non-negative")
    return amount


def _user_wallet_atomic_limit(env_name: str, default_value: str) -> int | None:
    limit_text = os.getenv(env_name, default_value).strip()
    if not limit_text:
        return None
    limit = int(limit_text)
    if limit <= 0:
        return None
    return limit


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(str(value))


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _operator_user_wallet_limits() -> dict[str, int | None]:
    return {
        "maxPerTxAtomic": _user_wallet_atomic_limit(
            "SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX",
            DEFAULT_USER_WALLET_MAX_ATOMIC_PER_TX,
        ),
        "dailyCapAtomic": _user_wallet_atomic_limit(
            "SIGN402_USER_WALLET_DAILY_ATOMIC_CAP",
            DEFAULT_USER_WALLET_DAILY_ATOMIC_CAP,
        ),
    }


def _operator_user_wallet_ceilings() -> dict[str, int | None]:
    """Hard maximums users may never exceed, unlike the raiseable defaults.

    Unset (the default) means no ceiling, preserving pre-ceiling behavior.
    """
    return {
        "ceilingPerTxAtomic": _user_wallet_atomic_limit(
            "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX",
            "",
        ),
        "ceilingDailyAtomic": _user_wallet_atomic_limit(
            "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC",
            "",
        ),
    }


def _payload_has_spending_limit_update(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "maxPerTxUsdc",
            "dailyCapUsdc",
            "maxPerTxAtomic",
            "dailyCapAtomic",
        )
    )


def _read_usdc_limit_atomic(payload: dict[str, Any], prefix: str) -> int:
    atomic_key = f"{prefix}Atomic"
    usdc_key = f"{prefix}Usdc"
    if atomic_key in payload:
        return int(str(payload[atomic_key]))
    if usdc_key not in payload:
        raise ValueError(f"{usdc_key} is required")
    value = Decimal(str(payload[usdc_key]))
    if value <= 0:
        raise ValueError(f"{usdc_key} must be greater than zero")
    atomic = value * Decimal("1000000")
    if atomic != atomic.to_integral_value():
        raise ValueError(f"{usdc_key} supports up to 6 decimal places")
    return int(atomic)


def _spending_limits_telegram_text(limits: dict[str, Any], *, updated: bool) -> str:
    heading = "Spending limits updated." if updated else "Current spending limits."
    max_per_tx = str(limits.get("maxPerTxUsdc") or "unlimited")
    daily_cap = str(limits.get("dailyCapUsdc") or "unlimited")
    platform_max = _format_usdc_atomic(limits.get("operatorCeilingPerTxAtomic"))
    platform_daily = _format_usdc_atomic(limits.get("operatorCeilingDailyAtomic"))
    bitrefill_max = limits.get("bitrefillLiveMaxUsd")
    service_fee_percent = format_decimal(
        Decimal(SERVICE_FEE_BPS) / Decimal(100)
    )
    bitrefill_line = (
        f"Bitrefill product maximum: {bitrefill_max} USD before the "
        f"{service_fee_percent}% service fee."
        if bitrefill_max is not None
        else "Bitrefill product maximum: inactive."
    )
    return (
        f"{heading}\n\n"
        "Your spending limits:\n"
        f"- Max per transaction: {max_per_tx} USDC\n"
        f"- Daily cap: {daily_cap} USDC\n\n"
        "Platform maximums:\n"
        f"- Max per transaction: {platform_max} USDC\n"
        f"- Daily cap: {platform_daily} USDC\n\n"
        f"{bitrefill_line}\n"
        "Service fees count toward your spending limits.\n"
        "The lowest applicable limit wins.\n\n"
        "To change: /limits <per-transaction> <daily>"
    )


def _user_wallet_spend_scope(
    server: Sign402GatewayServer,
    telegram_user_id: str,
    payment_requirements: dict[str, Any],
) -> dict[str, Any]:
    operator_limits = _operator_user_wallet_limits()
    operator_ceilings = _operator_user_wallet_ceilings()
    limits = server.user_spend_limit_store.limit_settings(
        telegram_user_id,
        operator_max_per_tx_atomic=operator_limits["maxPerTxAtomic"],
        operator_daily_cap_atomic=operator_limits["dailyCapAtomic"],
        operator_ceiling_per_tx_atomic=operator_ceilings["ceilingPerTxAtomic"],
        operator_ceiling_daily_atomic=operator_ceilings["ceilingDailyAtomic"],
    )
    return {
        "maxPerTx": limits.get("maxPerTxAtomic"),
        "dailyCap": limits.get("dailyCapAtomic"),
        "amount": _payment_amount_atomic(payment_requirements),
        "asset": str(payment_requirements.get("asset", "")),
        "network": str(
            payment_requirements.get("network")
            or payment_requirements.get("x402Network")
            or ""
        ),
    }


SPEND_LIMIT_MESSAGE_PREFIX = "Raise your spending limit to continue."


def _spend_limit_message(
    amount_atomic: int,
    *,
    per_tx_cap: int | None = None,
    daily_cap: int | None = None,
    spent_today: int = 0,
) -> str:
    """Say which limit stopped the purchase, in the units the buyer thinks in.

    This text reaches the buyer's chat, so it leads with what to do about it and
    quotes USDC rather than the atomic amounts the caps are stored in — a cap
    read as `50000000` tells nobody they are $50 short.
    """
    needed = _format_usdc_atomic(amount_atomic)
    if per_tx_cap is not None:
        return (
            f"{SPEND_LIMIT_MESSAGE_PREFIX} This purchase needs {needed} USDC, "
            f"but your limit is {_format_usdc_atomic(per_tx_cap)} USDC per "
            "transaction."
        )
    remaining = max(int(daily_cap or 0) - int(spent_today), 0)
    return (
        f"{SPEND_LIMIT_MESSAGE_PREFIX} This purchase needs {needed} USDC, but "
        f"only {_format_usdc_atomic(remaining)} USDC is left of your "
        f"{_format_usdc_atomic(daily_cap)} USDC daily limit."
    )


def _enforce_user_wallet_spend_limits(
    server: Sign402GatewayServer,
    telegram_user_id: str,
    payment_requirements: dict[str, Any],
) -> None:
    """Check the caps without holding budget.

    Used where nothing is about to be spent yet (a quote). Purchases must use
    `_reserve_user_wallet_spend` instead, so the amount is held for the whole
    approve-then-pay window.
    """
    scope = _user_wallet_spend_scope(server, telegram_user_id, payment_requirements)
    amount = scope["amount"]
    if scope["maxPerTx"] is not None and amount > int(scope["maxPerTx"]):
        raise ValueError(
            _spend_limit_message(amount, per_tx_cap=int(scope["maxPerTx"]))
        )
    if scope["dailyCap"] is None:
        return
    spent_today = int(
        server.user_spend_limit_store.spent_today_atomic(
            telegram_user_id,
            asset=scope["asset"],
            network=scope["network"],
        )
    )
    if spent_today + amount > int(scope["dailyCap"]):
        raise ValueError(
            _spend_limit_message(
                amount,
                daily_cap=int(scope["dailyCap"]),
                spent_today=spent_today,
            )
        )


def _reserve_user_wallet_spend(
    server: Sign402GatewayServer,
    telegram_user_id: str,
    payment_requirements: dict[str, Any],
) -> str:
    """Check the caps and hold the amount for this purchase.

    The hold is what closes the window between the cap check and the recorded
    spend: approval can take minutes, and a second purchase started in that
    window would otherwise measure itself against a total that ignores the
    first. Callers must settle the returned id on success and release it on
    rejection, timeout, or error.
    """
    scope = _user_wallet_spend_scope(server, telegram_user_id, payment_requirements)
    amount = scope["amount"]
    reservation_id = server.user_spend_limit_store.reserve_within_limits(
        telegram_user_id,
        amount_atomic=amount,
        asset=scope["asset"],
        network=scope["network"],
        max_per_tx_atomic=scope["maxPerTx"],
        daily_cap_atomic=scope["dailyCap"],
    )
    if reservation_id is not None:
        return reservation_id

    if scope["maxPerTx"] is not None and amount > int(scope["maxPerTx"]):
        raise ValueError(
            _spend_limit_message(amount, per_tx_cap=int(scope["maxPerTx"]))
        )
    spent_today = int(
        server.user_spend_limit_store.spent_today_atomic(
            telegram_user_id,
            asset=scope["asset"],
            network=scope["network"],
        )
    )
    raise ValueError(
        _spend_limit_message(
            amount,
            daily_cap=int(scope["dailyCap"]),
            spent_today=spent_today,
        )
    )


def _release_user_wallet_spend(
    server: Sign402GatewayServer,
    reservation_id: str | None,
) -> None:
    if not reservation_id:
        return
    try:
        server.user_spend_limit_store.release_reservation(reservation_id)
    except Exception:
        # A stuck hold expires on its own; never let cleanup mask the real
        # outcome of the purchase.
        pass


def _record_user_wallet_spend(
    server: Sign402GatewayServer,
    telegram_user_id: str,
    tool: dict[str, Any],
    resource_url: str,
    payment_requirements: dict[str, Any],
    event: dict[str, Any],
) -> None:
    amount = _payment_amount_atomic(payment_requirements)
    server.user_spend_limit_store.record_successful_spend(
        telegram_user_id,
        amount_atomic=amount,
        asset=str(payment_requirements.get("asset", "")),
        network=str(payment_requirements.get("network") or payment_requirements.get("x402Network") or ""),
        tx_id=str(event.get("txId") or ""),
        payment_intent=str(payment_requirements.get("paymentIntent") or event.get("paymentIntent") or ""),
        approval_id=str(event.get("approvalId") or ""),
        tool_id=str(tool.get("id") or event.get("toolId") or ""),
        resource_url=resource_url,
    )


def _settle_user_wallet_spend(
    server: Sign402GatewayServer,
    reservation_id: str | None,
    tool: dict[str, Any],
    resource_url: str,
    payment_requirements: dict[str, Any],
    event: dict[str, Any],
) -> None:
    """Convert this purchase's hold into a settled spend record."""
    server.user_spend_limit_store.settle_reservation(
        reservation_id,
        tx_id=str(event.get("txId") or ""),
        payment_intent=str(
            payment_requirements.get("paymentIntent") or event.get("paymentIntent") or ""
        ),
        approval_id=str(event.get("approvalId") or ""),
        tool_id=str(tool.get("id") or event.get("toolId") or ""),
        resource_url=resource_url,
    )


def _record_bankr_llm_spend(
    server: Sign402GatewayServer,
    telegram_user_id: str,
    purchase: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    server.user_spend_limit_store.record_successful_spend(
        telegram_user_id,
        amount_atomic=int(str(metadata["amountAtomic"])),
        asset=str(metadata["asset"]),
        network=str(metadata["network"]),
        tx_id=str(metadata.get("transferHash") or ""),
        payment_intent=str(purchase.get("purchaseId") or ""),
        approval_id=str(purchase.get("approvalRequestId") or ""),
        tool_id="bankr.llm.credits",
        resource_url="bankr://llm-credits/top-up",
    )


def _bitrefill_spend_requirement(price_usd: Any) -> dict[str, Any]:
    """Represent a Bitrefill purchase's USD value as a USDC spend requirement.

    Spending limits are denominated in USD (6-decimal atomic), so a Bitrefill
    order is capped by its USD price regardless of the token actually debited.
    """
    amount_atomic = int((Decimal(str(price_usd)) * Decimal(1_000_000)).to_integral_value())
    return {
        "amountAtomic": str(amount_atomic),
        "asset": BASE_USDC_MAINNET,
        "network": "base-mainnet",
    }


def _validate_base_usdc_x402_requirement(requirement: Any) -> None:
    if not isinstance(requirement, dict):
        raise ValueError("paymentRequirements must be an object")

    network = str(requirement.get("network") or requirement.get("x402Network") or "")
    if network not in {"base-mainnet", "eip155:8453"}:
        raise ValueError("Only Base Mainnet x402 endpoints are supported for raw URL purchases.")

    asset = str(requirement.get("asset") or "")
    if asset.lower() != BASE_USDC_MAINNET.lower():
        raise ValueError("Only Base USDC x402 endpoints are supported for raw URL purchases.")

    # The signer binds the payment to these three values, so a blank one would
    # leave the purchase unbounded. Reject it here, with a readable reason,
    # rather than letting the node signer fail on a missing argument.
    receiver = str(requirement.get("receiver") or requirement.get("payTo") or "")
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", receiver):
        raise ValueError("x402 paymentRequirements.payTo must be a Base EVM address")

    try:
        amount_atomic = int(str(requirement.get("amountAtomic") or requirement.get("amount") or ""))
    except ValueError as exc:
        raise ValueError("x402 paymentRequirements amount must be a positive integer") from exc
    if amount_atomic <= 0:
        raise ValueError("x402 paymentRequirements amount must be a positive integer")


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
        raw = response.read(MAX_BASE_RPC_RESPONSE_BYTES + 1)
    if len(raw) > MAX_BASE_RPC_RESPONSE_BYTES:
        raise ValueError("Base RPC response is too large")
    payload = json.loads(raw.decode("utf-8"))
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
    """Render an atomic USDC amount as the figure a person reads.

    Accepts the int limits carry and the strings swap payloads carry; `None` is
    an absent limit, which reads as unlimited rather than as zero.
    """
    if value is None:
        return "unlimited"
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


def _sanitize_bitrefill_purchase_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    sanitized = dict(result)
    if "bankr" not in sanitized:
        return sanitized
    bankr = sanitized["bankr"]
    if not isinstance(bankr, dict):
        raise ValueError("bankr reconciliation result must be an object")
    sanitized["bankr"] = sanitize_bankr_reconciliation_snapshot(bankr)
    return sanitized


def _without_fulfillment_token(result: dict[str, Any]) -> dict[str, Any]:
    """Drop the plaintext fulfillment token before persisting/broadcasting.

    The token is returned to the buying caller so they can reveal the
    redemption code, but it must never reach the dashboard event store, which
    is served back over an open-CORS GET endpoint.
    """
    sanitized = _sanitize_bitrefill_purchase_result(result)
    return {
        key: value
        for key, value in sanitized.items()
        if key != "fulfillmentToken"
    }


def _last_bitrefill_purchase_response(
    server: Any,
    event: dict[str, Any],
    telegram_user_id: str,
) -> dict[str, Any] | None:
    quote_id = str(event.get("quoteId", "") or "").strip()
    if not quote_id or "bitrefill" not in event:
        return None
    fulfillment_token = str(event.get("fulfillmentToken", "") or "").strip()
    if not fulfillment_token:
        product_name = str(
            event.get("productName") or "Your Bitrefill order"
        )
        return {
            "ok": True,
            "telegramText": (
                f"{product_name} was already delivered. For security its "
                "redemption code can only be revealed once — check where "
                "you saved it."
            ),
            "quoteId": quote_id,
        }

    order = server.bitrefill_order_lookup(
        quote_id,
        include_redemption=True,
        fulfillment_token=fulfillment_token,
    )
    redemption = order.get("redemption")
    has_redemption = _has_nonempty_redemption_value(redemption)
    telegram_text = order.get("telegramText")
    has_reveal_text = isinstance(telegram_text, str) and bool(
        telegram_text.strip()
    )
    if not has_redemption or not has_reveal_text:
        product_name = str(
            order.get("productName")
            or event.get("productName")
            or "Your Bitrefill order"
        )
        telegram_text = (
            f"{product_name} is still processing. Try /last_purchase again in a minute."
        )
    response = {
        "ok": True,
        "telegramText": telegram_text.strip(),
        "quoteId": quote_id,
        "orderId": order.get("orderId"),
        "state": order.get("state"),
        "status": order.get("status"),
    }
    if has_redemption and has_reveal_text:
        server.user_event_store.clear_fulfillment_token(telegram_user_id)
    return response


def _has_nonempty_redemption_value(redemption: Any) -> bool:
    if not isinstance(redemption, dict) or "value" not in redemption:
        return False

    def has_value(value: Any) -> bool:
        if isinstance(value, dict):
            return any(has_value(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_value(item) for item in value)
        return bool(str(value or "").strip())

    return has_value(redemption["value"])


def _managed_wallet_address(wallet_service: Any, telegram_user_id: str) -> str:
    status = wallet_service.wallet_status(str(telegram_user_id or "").strip())
    wallet = status.get("wallet") if isinstance(status, dict) else {}
    if not isinstance(wallet, dict):
        return ""
    return str(wallet.get("address", "") or "").strip()


if __name__ == "__main__":
    main()
