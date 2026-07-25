import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from cryptography.fernet import Fernet

from sign402_gateway.bankr_llm_purchase import BankrLlmError
from sign402_gateway.bitrefill import TestBitrefillClient
from sign402_gateway.bitrefill_mcp import McpBitrefillClient
from sign402_gateway.secure_state import (
    SensitiveStateCipher,
    SensitiveStateConfigurationError,
    SensitiveStateDecryptionError,
    SensitiveStateError,
)
from sign402_gateway.user_wallets import BASE_NATIVE_ETH_ASSET_ID
from sign402_gateway.server import (
    FUND_MOVING_POST_PATHS,
    MAX_REQUEST_BODY_BYTES,
    _USER_RATE_LIMITER,
    _purchases_paused,
    AgentStateStore,
    BankrCliX402PaymentClient,
    BankrLlmCreditsTopUpClient,
    BankrLlmCreditsTopUpRunner,
    BankrSingitToUsdcFundingRunner,
    BankrTreasuryClient,
    BankrTransferToCdpSwapFundingRunner,
    BankrUsdcReserveGuard,
    CdpWalletClient,
    CdpWalletSwapFundingRunner,
    DisabledApprovalClient,
    BankrWalletApiClient,
    DEFAULT_SINGIT_RISK_CHECK_URL,
    DEFAULT_SINGIT_TOKEN_ADDRESS,
    ExternalX402Buyer,
    SPEND_RESERVATION_TTL_SECONDS,
    Sign402GatewayHandler,
    SingitSettlementVerifier,
    UserPurchaseStore,
    UserSpendLimitStore,
    UserWalletBaseX402PaymentClient,
    UserWalletTokenTransferClient,
    UserWalletTransferToCdpFundingRunner,
    UserWalletX402Buyer,
    build_bitrefill_funding_runner_from_env,
    build_bitrefill_user_funding_runner_from_env,
    build_bitrefill_client_from_env,
    build_approval_client_from_env,
    build_server,
    build_payment_executor,
    build_x402_payment_signature_builder,
    build_real_rate_pricer_from_env,
    build_singit_settlement_verifier_from_env,
    build_usdc_reserve_guard_from_env,
    _bankr_cli_transaction_hash,
    _base_rpc_call,
    _build_bankr_llm_topup_intent,
    _resolve_paid_tool,
    _tool_result,
)


def subprocess_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["bankr"], returncode=returncode, stdout=stdout, stderr=stderr)


ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def erc20_receipt(
    *,
    token: str = DEFAULT_SINGIT_TOKEN_ADDRESS,
    sender: str,
    recipient: str,
    amount: int,
    status: str = "0x1",
) -> dict[str, object]:
    return {
        "status": status,
        "logs": [
            {
                "address": token,
                "topics": [
                    ERC20_TRANSFER_TOPIC,
                    _topic_address(sender),
                    _topic_address(recipient),
                ],
                "data": hex(amount),
            }
        ],
    }


class DummyServer:
    firefly = Mock()
    payment_executor = Mock()
    firefly_busy = False
    event_store = Mock()
    user_event_store = Mock()
    agent_state_store = Mock()
    agent_buy_probe = Mock()
    x402_inspector = Mock()
    x402_buyer = Mock()
    bankr_llm_topup_inspector = Mock()
    bankr_llm_topup = Mock()

    def __init__(self):
        self.bitrefill_catalog_service = Mock()
        self.user_wallet_service = Mock()
        self.user_wallet_api_token = "test-wallet-token"
        self.bankr_llm_purchase_service = Mock()
        self.imessage_approval_service = Mock()
        self.imessage_approval_api_token = "test-photon-token"
        self.user_x402_buyer = Mock()
        self.user_token_transfer_client = Mock()
        self.user_spend_limit_store = Mock()
        self.user_spend_limit_store.limit_settings.return_value = {
            "maxPerTxAtomic": 10000,
            "dailyCapAtomic": 100000,
            "maxPerTxUsdc": "0.01",
            "dailyCapUsdc": "0.1",
            "operatorMaxPerTxAtomic": 10000,
            "operatorDailyCapAtomic": 100000,
            "userConfigured": False,
        }
        self.user_spend_limit_store.spent_today_atomic.return_value = 0
        # Mirror the real store's contract: a reservation is refused (None)
        # exactly when the amount does not fit the caps. Without this the Mock
        # would hand back a truthy handle and every cap test would pass
        # vacuously.
        self.user_spend_limit_store.reserve_within_limits.side_effect = (
            self._reserve_within_limits
        )

    def _reserve_within_limits(
        self,
        telegram_user_id,
        *,
        amount_atomic,
        asset,
        network,
        max_per_tx_atomic,
        daily_cap_atomic,
        **_kwargs,
    ):
        amount = int(amount_atomic)
        if max_per_tx_atomic is not None and amount > int(max_per_tx_atomic):
            return None
        if daily_cap_atomic is not None:
            spent = int(
                self.user_spend_limit_store.spent_today_atomic(
                    telegram_user_id, asset=asset, network=network
                )
            )
            if spent + amount > int(daily_cap_atomic):
                return None
        return "hold_test"


class FakeSocket:
    def __init__(self, request: bytes):
        self.rfile = io.BytesIO(request)
        self.wfile = io.BytesIO()

    def makefile(self, mode, buffering=None):
        if "r" in mode:
            return self.rfile
        return self.wfile

    def sendall(self, data):
        self.wfile.write(data)


class GatewayServerTests(unittest.TestCase):
    def state_cipher(self):
        return SensitiveStateCipher(Fernet.generate_key().decode("ascii"))

    def test_user_purchase_store_encrypts_token_at_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "user-purchases.json"
            store = UserPurchaseStore(path, cipher=self.state_cipher())
            event = {
                "ok": True,
                "quoteId": "q1",
                "fulfillmentToken": "reveal_secret_1",
            }

            observed_temp_documents: list[str] = []
            real_replace = os.replace

            def inspect_then_replace(source, target):
                observed_temp_documents.append(
                    Path(source).read_text(encoding="utf-8")
                )
                real_replace(source, target)

            with patch(
                "sign402_gateway.secure_state.os.replace",
                side_effect=inspect_then_replace,
            ):
                returned = store.write("1045618308", event)
            raw = path.read_text(encoding="utf-8")
            persisted = json.loads(raw)["1045618308"]

            self.assertIs(returned, event)
            self.assertNotIn("reveal_secret_1", raw)
            self.assertNotIn(
                "reveal_secret_1",
                "".join(observed_temp_documents),
            )
            self.assertNotIn("fulfillmentToken", persisted)
            self.assertIn("encryptedFulfillmentToken", persisted)
            self.assertEqual(store.read("1045618308"), event)
            self.assertNotIn(
                "encryptedFulfillmentToken",
                store.read("1045618308"),
            )
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_user_purchase_store_reads_legacy_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy = (
                '{"1045618308":{"ok":true,"quoteId":"q1",'
                '"fulfillmentToken":"legacy_secret"}}\n'
            )
            path.write_text(legacy, encoding="utf-8")
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            loaded = store.read("1045618308")

            self.assertEqual(loaded["fulfillmentToken"], "legacy_secret")
            self.assertEqual(path.read_text(encoding="utf-8"), legacy)

    def test_user_purchase_store_refuses_to_copy_other_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy = (
                '{"legacy-user":{"ok":true,'
                '"fulfillmentToken":"legacy_secret"}}\n'
            )
            path.write_text(legacy, encoding="utf-8")
            before = path.read_bytes()
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaisesRegex(
                SensitiveStateError,
                "legacy plaintext fulfillment tokens must be migrated",
            ):
                store.write(
                    "new-user",
                    {"ok": True, "fulfillmentToken": "new_secret"},
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_user_purchase_store_write_preflight_requires_cipher_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path)
            preflight = getattr(store, "preflight_write", None)

            self.assertIsNotNone(preflight)
            with self.assertRaises(SensitiveStateConfigurationError):
                preflight()

            self.assertFalse(path.exists())

    def test_user_purchase_store_invalid_envelope_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            path.write_text(
                '{"u":{"encryptedFulfillmentToken":"not-ciphertext"}}\n',
                encoding="utf-8",
            )
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaises(SensitiveStateDecryptionError):
                store.read("u")

    def test_user_purchase_store_null_envelope_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy_marker = "LEGACY-FULFILLMENT-TOKEN-MARKER"
            path.write_text(
                json.dumps(
                    {
                        "u": {
                            "encryptedFulfillmentToken": None,
                            "fulfillmentToken": legacy_marker,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaises(SensitiveStateDecryptionError) as captured:
                store.read("u")

            self.assertNotIn(legacy_marker, str(captured.exception))
            self.assertIsNone(captured.exception.__cause__)

    def test_user_purchase_store_token_write_without_cipher_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path)

            with self.assertRaises(
                SensitiveStateConfigurationError
            ):
                store.write(
                    "u",
                    {
                        "ok": True,
                        "fulfillmentToken": "reveal_secret",
                    },
                )

            self.assertFalse(path.exists())

    def test_user_purchase_store_encrypted_read_without_cipher_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            UserPurchaseStore(
                path,
                cipher=self.state_cipher(),
            ).write(
                "u",
                {
                    "ok": True,
                    "fulfillmentToken": "reveal_secret",
                },
            )

            with self.assertRaises(
                SensitiveStateConfigurationError
            ):
                UserPurchaseStore(path).read("u")

    def test_user_purchase_store_clear_removes_both_token_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path, cipher=self.state_cipher())
            store.write(
                "u",
                {"ok": True, "fulfillmentToken": "reveal_secret"},
            )
            seeded = json.loads(path.read_text(encoding="utf-8"))
            seeded["u"]["fulfillmentToken"] = "legacy_secret"
            path.write_text(
                json.dumps(seeded) + "\n",
                encoding="utf-8",
            )

            store.clear_fulfillment_token("u")

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("reveal_secret", raw)
            self.assertNotIn("legacy_secret", raw)
            self.assertNotIn("encryptedFulfillmentToken", raw)
            self.assertNotIn("fulfillmentToken", store.read("u"))

    def test_user_purchase_store_clears_last_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            path.write_text(
                '{"u":{"ok":true,"fulfillmentToken":"legacy_secret"}}\n',
                encoding="utf-8",
            )
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            store.clear_fulfillment_token("u")

            self.assertNotIn(
                "legacy_secret",
                path.read_text(encoding="utf-8"),
            )
            self.assertNotIn("fulfillmentToken", store.read("u"))

    def test_user_purchase_store_clear_refuses_to_copy_other_legacy_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            original = (
                '{"u":{"encryptedFulfillmentToken":"not-read"},'
                '"other":{"fulfillmentToken":"other_legacy_secret"}}\n'
            )
            path.write_text(original, encoding="utf-8")
            store = UserPurchaseStore(path, cipher=self.state_cipher())

            with self.assertRaises(SensitiveStateError):
                store.clear_fulfillment_token("u")

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_user_spend_limit_store_writes_private_state(self):
        previous_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "state" / "limits.json"
                store = UserSpendLimitStore(path)
                store.set_limit_settings(
                    "u",
                    max_per_tx_atomic=10,
                    daily_cap_atomic=100,
                    operator_max_per_tx_atomic=None,
                    operator_daily_cap_atomic=None,
                )
                self.assertEqual(
                    stat.S_IMODE(path.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            os.umask(previous_umask)

    def test_base_rpc_rejects_oversized_response(self):
        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return b"x" * 1_048_577

        with patch("urllib.request.urlopen", return_value=OversizedResponse()):
            with self.assertRaisesRegex(ValueError, "response is too large"):
                _base_rpc_call("eth_blockNumber", [])

    _LEGACY_TEST_PATHS = frozenset(
        {
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
            "/agent/quote-bitrefill",
            "/agent/search-bitrefill",
            "/agent/list-bitrefill-products",
            "/agent/get-bitrefill-product",
            "/agent/buy-bitrefill",
            "/agent/buy-wallet-bitrefill",
            "/agent/get-bitrefill-order",
        }
    )

    def setUp(self):
        # Per-user rate limiting is process-global; isolate tests from each other.
        _USER_RATE_LIMITER.reset()
        # Older tests exercise the local Firefly/demo endpoints intentionally.
        # Production coverage below explicitly disables this opt-in mode.
        self._legacy_env = patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "true",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "legacy-operator-token",
            },
        )
        self._legacy_env.start()
        self.addCleanup(self._legacy_env.stop)

    def test_disabled_approval_provider_rejects_without_firefly(self):
        client = build_approval_client_from_env(
            firefly_port=None,
            env={"SIGN402_APPROVAL_PROVIDER": "disabled"},
        )

        approval = client.approve_payment_hash("a" * 64, context_lines=["TEST"])

        self.assertIsInstance(client, DisabledApprovalClient)
        self.assertFalse(approval["approved"])
        self.assertEqual(approval["approvedHash"], "a" * 64)
        self.assertEqual(approval["error"], "approval_provider_disabled")
        self.assertEqual(approval["approvalMethod"], "disabled")

    def test_disabled_payment_executor_rejects_before_local_signing(self):
        with patch.dict(os.environ, {"SIGN402_PAYMENT_EXECUTOR_MODE": "disabled"}):
            executor = build_payment_executor(Path("/tmp/missing-payment-executor"))
            signature_builder = build_x402_payment_signature_builder(Path("/tmp/missing-payment-executor"))

        with self.assertRaisesRegex(RuntimeError, "disabled"):
            executor({}, "a" * 64)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            signature_builder({})

    def test_build_server_allows_missing_master_key_but_rejects_invalid_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_server = Mock()
            fake_wallet_service = Mock()
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {}, clear=True))
                for target in (
                    "sign402_gateway.server.build_approval_client_from_env",
                    "sign402_gateway.server.build_payment_executor",
                    (
                        "sign402_gateway.server."
                        "build_x402_payment_signature_builder"
                    ),
                    "sign402_gateway.server.CdpBaseX402PaymentClient",
                    "sign402_gateway.server.BankrCliX402PaymentClient",
                    (
                        "sign402_gateway.commerce_store."
                        "BitrefillCommerceStore"
                    ),
                    (
                        "sign402_gateway.server."
                        "build_imessage_approval_service_from_env"
                    ),
                    "sign402_gateway.server.build_bitrefill_client_from_env",
                    "sign402_gateway.server.build_real_rate_pricer_from_env",
                    (
                        "sign402_gateway.server."
                        "build_bitrefill_funding_runner_from_env"
                    ),
                    "sign402_gateway.server.build_usdc_reserve_guard_from_env",
                    (
                        "sign402_gateway.server."
                        "build_singit_settlement_verifier_from_env"
                    ),
                    (
                        "sign402_gateway.server."
                        "build_bitrefill_user_funding_runner_from_env"
                    ),
                    "sign402_gateway.server.UserWalletBaseX402PaymentClient",
                    "sign402_gateway.server.UserWalletTokenTransferClient",
                    "sign402_gateway.server.BankrLlmCreditsTopUpClient",
                    (
                        "sign402_gateway.server."
                        "build_bankr_llm_purchase_service_from_env"
                    ),
                ):
                    stack.enter_context(patch(target, return_value=Mock()))
                stack.enter_context(
                    patch(
                        "sign402_gateway.server.build_wallet_service_from_env",
                        return_value=fake_wallet_service,
                    )
                )
                server_constructor = stack.enter_context(
                    patch(
                        "sign402_gateway.server.Sign402GatewayServer",
                        return_value=fake_server,
                    )
                )
                missing_key_server = build_server(
                    "127.0.0.1",
                    0,
                    firefly_port=None,
                    approval_provider="disabled",
                    payment_executor_dir=root / "payment-executor",
                    event_store_path=root / "latest-run.json",
                    agent_state_path=root / "agent-state.json",
                    cdp_x402_service_dir=root / "cdp-x402-service",
                    bitrefill_commerce_store_path=root / "orders.sqlite3",
                    user_wallet_store_path=root / "user-wallets.json",
                    user_spend_limit_store_path=root / "spend-limits.json",
                    imessage_approval_store_path=root / "approvals.json",
                )

                self.assertIs(missing_key_server, fake_server)
                self.assertEqual(server_constructor.call_count, 1)

                os.environ["SIGN402_WALLET_MASTER_KEY"] = (
                    "nonempty-invalid-master-key-marker"
                )
                with self.assertRaises(
                    SensitiveStateConfigurationError
                ) as captured:
                    build_server(
                        "127.0.0.1",
                        0,
                        firefly_port=None,
                        approval_provider="disabled",
                        payment_executor_dir=root / "payment-executor",
                        event_store_path=root / "latest-run.json",
                        agent_state_path=root / "agent-state.json",
                        cdp_x402_service_dir=root / "cdp-x402-service",
                        bitrefill_commerce_store_path=root / "orders.sqlite3",
                        user_wallet_store_path=root / "user-wallets.json",
                        user_spend_limit_store_path=root / "spend-limits.json",
                        imessage_approval_store_path=root / "approvals.json",
                    )

                self.assertNotIn(
                    "nonempty-invalid-master-key-marker",
                    str(captured.exception),
                )
                self.assertEqual(server_constructor.call_count, 1)
                self.assertEqual(list(root.iterdir()), [])

    def test_bitrefill_client_factory_defaults_to_safe_test_mode(self):
        client = build_bitrefill_client_from_env({})

        self.assertIsInstance(client, TestBitrefillClient)

    def test_bitrefill_client_factory_rejects_live_mode_without_api_key(self):
        with self.assertRaisesRegex(ValueError, "BITREFILL_API_KEY is required"):
            build_bitrefill_client_from_env({"SIGN402_BITREFILL_MODE": "live"})

    def test_bitrefill_client_factory_builds_live_client_with_api_key(self):
        client = build_bitrefill_client_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "BITREFILL_API_KEY": "test_key",
                "SIGN402_BITREFILL_MCP_URL": "https://bitrefill.example/mcp",
                "SIGN402_BITREFILL_LIVE_MAX_USD": "3.50",
            }
        )

        self.assertIsInstance(client, McpBitrefillClient)
        self.assertEqual(str(client.max_purchase_usd), "3.50")
        self.assertNotIn("test_key", repr(client))
        self.assertNotIn("test_key", repr(client._call_tool))

    def test_bitrefill_client_factory_builds_usdc_base_live_client_with_treasury(self):
        client = build_bitrefill_client_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "BITREFILL_API_KEY": "test_key",
                "SIGN402_BITREFILL_PAYMENT_METHOD": "usdc_base",
            }
        )

        self.assertIsInstance(client, McpBitrefillClient)
        self.assertEqual(client.payment_method, "usdc_base")
        self.assertIsInstance(client.treasury_client, BankrTreasuryClient)

    def test_bitrefill_client_factory_ignores_removed_rest_base_url(self):
        client = build_bitrefill_client_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "BITREFILL_API_KEY": "test_key",
                "SIGN402_BITREFILL_BASE_URL": "http://localhost:9999/v2",
            }
        )

        self.assertIsInstance(client, McpBitrefillClient)

    def test_bitrefill_client_factory_rejects_unknown_mode(self):
        with self.assertRaisesRegex(ValueError, "unsupported SIGN402_BITREFILL_MODE"):
            build_bitrefill_client_from_env({"SIGN402_BITREFILL_MODE": "automatic"})

    def test_live_settlement_requires_bankr_wallet(self):
        with self.assertRaisesRegex(ValueError, "SIGN402_BANKR_WALLET_ADDRESS"):
            build_singit_settlement_verifier_from_env(
                {
                    "SIGN402_BITREFILL_MODE": "live",
                }
            )

    def test_live_settlement_can_be_disabled_for_wallet_native_bitrefill(self):
        verifier = build_singit_settlement_verifier_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "SIGN402_DISABLE_BANKR_BITREFILL_SETTLEMENT": "1",
            }
        )

        self.assertIsNone(verifier)

    def test_live_settlement_accepts_base_bankr_wallet(self):
        verifier = build_singit_settlement_verifier_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "SIGN402_BANKR_WALLET_ADDRESS": "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98",
            }
        )

        self.assertIsInstance(verifier, SingitSettlementVerifier)
        self.assertEqual(
            verifier.payer_address,
            "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98",
        )

    def test_bankr_singit_to_usdc_funding_runner_swaps_quote_singit(self):
        swap_client = Mock(
            **{
                "swap.return_value": {
                    "ok": True,
                    "txId": "0xSWAP",
                    "stdout": "Swap successful",
                }
            }
        )
        runner = BankrSingitToUsdcFundingRunner(
            swap_client=swap_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            chain="base",
        )

        result = runner(
            {
                "quoteId": "quote_1",
                "pricingMode": "bankr_real_rate",
                "singitAmount": "25000",
                "expectedUsdc": "0.11",
            }
        )

        self.assertEqual(result["txId"], "0xSWAP")
        swap_client.swap.assert_called_once_with(
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            amount="25000",
            chain="base",
        )

    def test_bankr_singit_to_usdc_funding_runner_rejects_underfilled_swap(self):
        swap_client = Mock(
            **{
                "quote.return_value": {"ok": True, "minToAmount": "0.12", "toAmount": "0.13"},
                "swap.return_value": {
                    "ok": True,
                    "txId": "0xSWAP",
                    "amountReceived": "0.05",
                },
            }
        )
        runner = BankrSingitToUsdcFundingRunner(
            swap_client=swap_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            chain="base",
        )

        with self.assertRaisesRegex(ValueError, "USDC"):
            runner(
                {
                    "quoteId": "quote_1",
                    "pricingMode": "bankr_real_rate",
                    "singitAmount": "25000",
                    "requiredUsdc": "0.10",
                    "expectedUsdc": "0.11",
                }
            )

    def test_bankr_singit_to_usdc_funding_runner_rejects_when_quote_floor_below_required(self):
        swap_client = Mock(
            **{
                "quote.return_value": {"ok": True, "minToAmount": "0.05", "toAmount": "0.06"},
                "swap.return_value": {"ok": True, "txId": "0xSWAP", "amountReceived": "0.12"},
            }
        )
        runner = BankrSingitToUsdcFundingRunner(
            swap_client=swap_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            chain="base",
        )

        with self.assertRaisesRegex(ValueError, "USDC"):
            runner(
                {
                    "quoteId": "quote_1",
                    "pricingMode": "bankr_real_rate",
                    "singitAmount": "25000",
                    "requiredUsdc": "0.10",
                    "expectedUsdc": "0.11",
                }
            )

        swap_client.swap.assert_not_called()

    def test_bankr_singit_to_usdc_funding_runner_accepts_sufficient_swap(self):
        swap_client = Mock(
            **{
                "quote.return_value": {"ok": True, "minToAmount": "0.12", "toAmount": "0.13"},
                "swap.return_value": {
                    "ok": True,
                    "txId": "0xSWAP",
                    "amountReceived": "0.12",
                },
            }
        )
        runner = BankrSingitToUsdcFundingRunner(
            swap_client=swap_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            chain="base",
        )

        result = runner(
            {
                "quoteId": "quote_1",
                "pricingMode": "bankr_real_rate",
                "singitAmount": "25000",
                "requiredUsdc": "0.10",
                "expectedUsdc": "0.11",
            }
        )

        self.assertEqual(result["txId"], "0xSWAP")

    def test_bankr_singit_to_usdc_funding_runner_rejects_fixed_price_quote(self):
        runner = BankrSingitToUsdcFundingRunner(
            swap_client=Mock(),
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            to_token="USDC",
            chain="base",
        )

        with self.assertRaisesRegex(ValueError, "bankr_real_rate"):
            runner({"quoteId": "quote_1", "singitAmount": "11"})

    def test_cdp_wallet_swap_funding_runner_swaps_quote_singit_without_bankr_transfer(self):
        cdp_client = Mock(
            **{"swap_singit_to_usdc.return_value": {"ok": True, "txId": "0xCDPSWAP"}}
        )
        runner = CdpWalletSwapFundingRunner(
            cdp_client=cdp_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            chain="base",
        )

        result = runner(
            {
                "quoteId": "quote_1",
                "pricingMode": "bankr_real_rate",
                "singitAmount": "130000",
                "expectedUsdc": "0.111",
                "priceUsd": "0.10",
            }
        )

        self.assertEqual(result["mode"], "cdp_wallet_swap")
        self.assertEqual(result["txId"], "0xCDPSWAP")
        cdp_client.swap_singit_to_usdc.assert_called_once_with(
            amount="130000",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            min_usdc="0.10",
            chain="base",
        )

    def test_cdp_wallet_swap_funding_runner_uses_quote_payment_token(self):
        cdp_client = Mock(
            **{"swap_singit_to_usdc.return_value": {"ok": True, "txId": "0xTOKEN"}}
        )
        runner = CdpWalletSwapFundingRunner(
            cdp_client=cdp_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            chain="base",
        )

        result = runner(
            {
                "pricingMode": "bankr_real_rate",
                "paymentTokenAddress": "0x1111111111111111111111111111111111111111",
                "paymentTokenSymbol": "TOKEN",
                "paymentTokenDecimals": 6,
                "paymentTokenNative": False,
                "paymentTokenAmount": "2.5",
                "requiredUsdc": "1.00",
            }
        )

        self.assertEqual(result["fromToken"], "0x1111111111111111111111111111111111111111")
        cdp_client.swap_singit_to_usdc.assert_called_once_with(
            amount="2.5",
            from_token="0x1111111111111111111111111111111111111111",
            min_usdc="1.00",
            chain="base",
            decimals=6,
        )

    def test_cdp_wallet_swap_funding_runner_skips_usdc_to_usdc_swap(self):
        cdp_client = Mock()
        runner = CdpWalletSwapFundingRunner(
            cdp_client=cdp_client,
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            chain="base",
        )

        result = runner(
            {
                "pricingMode": "bankr_real_rate",
                "paymentTokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "paymentTokenSymbol": "USDC",
                "paymentTokenDecimals": 6,
                "paymentTokenNative": False,
                "paymentTokenAmount": "1.10",
                "requiredUsdc": "1.00",
            }
        )

        self.assertEqual(result["mode"], "cdp_wallet_usdc_ready")
        self.assertEqual(result["amount"], "1.10")
        cdp_client.swap_singit_to_usdc.assert_not_called()

    def test_bankr_transfer_to_cdp_swap_runner_transfers_then_swaps(self):
        transfer_client = Mock(
            **{"transfer_singit.return_value": {"ok": True, "txId": "0xTRANSFER"}}
        )
        cdp_client = Mock(
            **{"swap_singit_to_usdc.return_value": {"ok": True, "txId": "0xSWAP", "amountReceived": "0.12"}}
        )
        runner = BankrTransferToCdpSwapFundingRunner(
            bankr_transfer_client=transfer_client,
            cdp_client=cdp_client,
            cdp_wallet_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        )

        result = runner(
            {
                "quoteId": "quote_1",
                "pricingMode": "bankr_real_rate",
                "singitAmount": "150000",
                "requiredUsdc": "0.10",
            }
        )

        self.assertEqual(result["transfer"]["txId"], "0xTRANSFER")
        self.assertEqual(result["swap"]["txId"], "0xSWAP")
        transfer_client.transfer_singit.assert_called_once_with(
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            amount="150000",
            token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
            chain="base",
        )
        cdp_client.swap_singit_to_usdc.assert_called_once_with(
            amount="150000",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            min_usdc="0.10",
            chain="base",
        )

    def test_cdp_wallet_client_runs_swap_and_transfer_commands(self):
        price_completed = subprocess_completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "fromAmount": "150000000000000000000000",
                    "toAmount": "134576",
                    "minToAmount": "133237",
                    "liquidityAvailable": True,
                }
            )
        )
        swap_completed = subprocess_completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "transactionHash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "amountReceived": "0.134",
                }
            )
        )
        transfer_completed = subprocess_completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "transactionHash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                }
            )
        )
        with patch("subprocess.run", side_effect=[price_completed, swap_completed, transfer_completed]) as run:
            client = CdpWalletClient(service_dir=Path("/tmp/cdp"))
            quote = client.quote(
                from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
                to_token="USDC",
                amount="150000",
                chain="base",
            )
            swap = client.swap_singit_to_usdc(
                amount="150000",
                from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
                min_usdc="0.10",
            )
            transfer = client.transfer_usdc(to_address="0xInvoice", amount="0.10")

        self.assertEqual(quote["toAmount"], "0.134576")
        self.assertEqual(quote["minToAmount"], "0.133237")
        self.assertEqual(swap["txId"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(transfer["txId"], "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertEqual(run.call_args_list[0].args[0][2], "swap-price")
        self.assertEqual(run.call_args_list[1].args[0][2], "swap")
        self.assertIn("--amount", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0][2], "transfer-usdc")
        self.assertIn("--to", run.call_args_list[2].args[0])

    def test_cdp_wallet_client_quote_passes_custom_decimals(self):
        price_completed = subprocess_completed(
            stdout=json.dumps(
                {
                    "ok": True,
                    "fromAmount": "150000000000",
                    "toAmount": "134576",
                    "minToAmount": "133237",
                    "liquidityAvailable": True,
                }
            )
        )
        with patch("subprocess.run", side_effect=[price_completed]) as run:
            client = CdpWalletClient(service_dir=Path("/tmp/cdp"))
            client.quote(
                from_token="0xCUSTOM",
                to_token="USDC",
                amount="150000",
                chain="base",
                decimals=8,
            )

        command = run.call_args_list[0].args[0]
        self.assertIn("--decimals", command)
        self.assertEqual(command[command.index("--decimals") + 1], "8")

    def test_cdp_wallet_client_swap_passes_custom_decimals(self):
        completed = subprocess_completed(
            stdout=json.dumps({"ok": True, "transactionHash": "0xSWAP"})
        )
        with patch("subprocess.run", side_effect=[completed]) as run:
            client = CdpWalletClient(service_dir=Path("/tmp/cdp"))
            client.swap_singit_to_usdc(
                amount="1.25",
                from_token="0x1111111111111111111111111111111111111111",
                min_usdc="1.00",
                decimals=6,
            )

        command = run.call_args_list[0].args[0]
        self.assertIn("--decimals", command)
        self.assertEqual(command[command.index("--decimals") + 1], "6")

    def test_real_rate_pricer_env_builder_requires_max_singit(self):
        with self.assertRaisesRegex(ValueError, "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER"):
            build_real_rate_pricer_from_env(
                {
                    "SIGN402_BITREFILL_PRICING_MODE": "bankr_real_rate",
                }
            )

    def test_real_rate_pricer_env_builder_uses_bankr_wallet_api_when_key_is_set(self):
        pricer = build_real_rate_pricer_from_env(
            {
                "SIGN402_BITREFILL_PRICING_MODE": "bankr_real_rate",
                "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER": "1000000",
                "BANKR_API_KEY": "test_key",
            }
        )

        self.assertIsInstance(pricer.quote_client, BankrWalletApiClient)
        self.assertEqual(pricer.buffer_bps, 0)

    def test_real_rate_pricer_env_builder_ignores_legacy_pricing_buffer(self):
        pricer = build_real_rate_pricer_from_env(
            {
                "SIGN402_BITREFILL_PRICING_MODE": "bankr_real_rate",
                "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER": "1000000",
                "SIGN402_BITREFILL_USDC_BUFFER_BPS": "1000",
                "BANKR_API_KEY": "test_key",
            }
        )

        self.assertEqual(pricer.buffer_bps, 0)

    def test_bitrefill_funding_runner_env_builder_uses_bankr_wallet_api_swap(self):
        runner = build_bitrefill_funding_runner_from_env(
            {
                "SIGN402_BITREFILL_FUNDING_MODE": "bankr_wallet_api_swap",
                "BANKR_API_KEY": "test_key",
            }
        )

        self.assertIsInstance(runner, BankrSingitToUsdcFundingRunner)
        self.assertIsInstance(runner.swap_client, BankrWalletApiClient)

    def test_bitrefill_funding_runner_env_builder_requires_bankr_api_key(self):
        with patch("sign402_gateway.server.load_bankr_api_key", return_value=None):
            with self.assertRaisesRegex(ValueError, "BANKR_API_KEY"):
                build_bitrefill_funding_runner_from_env(
                    {"SIGN402_BITREFILL_FUNDING_MODE": "bankr_wallet_api_swap"}
                )

    def test_bitrefill_funding_runner_env_builder_uses_bankr_transfer_to_cdp_swap(self):
        runner = build_bitrefill_funding_runner_from_env(
            {
                "SIGN402_BITREFILL_FUNDING_MODE": "bankr_transfer_to_cdp_swap",
                "SIGN402_CDP_WALLET_ADDRESS": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                "SIGN402_CDP_X402_SERVICE_DIR": "/tmp/cdp",
            }
        )

        self.assertIsInstance(runner, BankrTransferToCdpSwapFundingRunner)
        self.assertIsInstance(runner.cdp_client, CdpWalletClient)

    def test_bitrefill_funding_runner_env_builder_uses_cdp_wallet_swap(self):
        runner = build_bitrefill_funding_runner_from_env(
            {
                "SIGN402_BITREFILL_FUNDING_MODE": "cdp_wallet_swap",
                "SIGN402_CDP_X402_SERVICE_DIR": "/tmp/cdp",
            }
        )

        self.assertIsInstance(runner, CdpWalletSwapFundingRunner)
        self.assertIsInstance(runner.cdp_client, CdpWalletClient)

    def test_real_rate_pricer_env_builder_uses_cdp_wallet_when_configured(self):
        pricer = build_real_rate_pricer_from_env(
            {
                "SIGN402_BITREFILL_PRICING_MODE": "bankr_real_rate",
                "SIGN402_BITREFILL_PRICING_SOURCE": "cdp_wallet",
                "SIGN402_MAX_SINGIT_PER_BITREFILL_ORDER": "1000000",
                "SIGN402_CDP_X402_SERVICE_DIR": "/tmp/cdp",
            }
        )

        self.assertIsInstance(pricer.quote_client, CdpWalletClient)

    def test_bitrefill_client_factory_uses_cdp_treasury_for_usdc_base(self):
        client = build_bitrefill_client_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "BITREFILL_API_KEY": "test_key",
                "SIGN402_BITREFILL_PAYMENT_METHOD": "usdc_base",
                "SIGN402_BITREFILL_USDC_TREASURY_MODE": "cdp_wallet",
                "SIGN402_CDP_X402_SERVICE_DIR": "/tmp/cdp",
            }
        )

        self.assertIsInstance(client.treasury_client, CdpWalletClient)

    def make_handler(
        self,
        path: str,
        body: dict | None = None,
        method: str = "POST",
        server=None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if headers is None and path in self._LEGACY_TEST_PATHS:
            request_headers["Authorization"] = "Bearer legacy-operator-token"

        encoded = b""
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")

        request = (
            f"{method} {path} HTTP/1.1\r\n".encode("ascii")
            + f"Content-Length: {len(encoded)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"".join(
                f"{key}: {value}\r\n".encode("ascii")
                for key, value in request_headers.items()
            )
            + b"\r\n"
            + encoded
        )
        socket = FakeSocket(request)
        handler = Sign402GatewayHandler(socket, ("127.0.0.1", 12345), server or DummyServer())
        handler.response = socket.wfile
        return handler

    def response_text(self, handler) -> str:
        return handler.response.getvalue().decode("utf-8", "replace")

    def response_json(self, handler) -> dict:
        response = self.response_text(handler)
        _, body = response.split("\r\n\r\n", 1)
        return json.loads(body)

    def test_post_rejects_oversized_request_before_dispatch(self):
        request = (
            b"POST /agent/wallet HTTP/1.1\r\n"
            b"Content-Length: 1048577\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"{}"
        )
        socket = FakeSocket(request)
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = Sign402GatewayHandler(
                socket,
                ("127.0.0.1", 12345),
                server,
            )
        handler.response = socket.wfile

        self.assertIn(
            "HTTP/1.0 413 ",
            self.response_text(handler),
        )
        self.assertEqual(self.response_json(handler)["error"], "request_body_too_large")
        server.user_wallet_service.wallet_status.assert_not_called()

    def test_post_rejects_negative_content_length_before_dispatch(self):
        request = (
            b"POST /agent/wallet HTTP/1.1\r\n"
            b"Content-Length: -1\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"{}"
        )
        socket = FakeSocket(request)
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = Sign402GatewayHandler(
                socket,
                ("127.0.0.1", 12345),
                server,
            )
        handler.response = socket.wfile

        self.assertIn("HTTP/1.0 400 ", self.response_text(handler))
        self.assertEqual(self.response_json(handler)["error"], "invalid_content_length")
        server.user_wallet_service.wallet_status.assert_not_called()

    def test_post_rejects_malformed_content_length_before_dispatch(self):
        request = (
            b"POST /agent/wallet HTTP/1.1\r\n"
            b"Content-Length: nope\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b"{}"
        )
        socket = FakeSocket(request)
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = Sign402GatewayHandler(
                socket,
                ("127.0.0.1", 12345),
                server,
            )
        handler.response = socket.wfile

        self.assertIn("HTTP/1.0 400 ", self.response_text(handler))
        self.assertEqual(self.response_json(handler)["error"], "invalid_content_length")
        server.user_wallet_service.wallet_status.assert_not_called()

    def test_paused_transaction_still_validates_content_length_before_pause(self):
        cases = (
            ("nope", "HTTP/1.0 400 Bad Request", "invalid_content_length"),
            ("-1", "HTTP/1.0 400 Bad Request", "invalid_content_length"),
            (
                str(MAX_REQUEST_BODY_BYTES + 1),
                "HTTP/1.0 413 ",
                "request_body_too_large",
            ),
        )
        with patch.dict(
            os.environ,
            {"SIGN402_PURCHASES_PAUSED": "1"},
        ):
            for declared_length, status, error in cases:
                with self.subTest(declared_length=declared_length):
                    request = (
                        b"POST /agent/buy-tool HTTP/1.1\r\n"
                        + f"Content-Length: {declared_length}\r\n".encode("ascii")
                        + b"Content-Type: application/json\r\n"
                        + b"\r\n"
                        + b"{}"
                    )
                    socket = FakeSocket(request)
                    server = DummyServer()
                    with (
                        patch.object(
                            Sign402GatewayHandler,
                            "_read_json",
                            side_effect=AssertionError("body was read"),
                        ) as read_json,
                        patch.object(
                            Sign402GatewayHandler,
                            "_handle_agent_buy_tool",
                            side_effect=AssertionError("handler was dispatched"),
                        ) as dispatched,
                        patch("sys.stderr", io.StringIO()),
                    ):
                        handler = Sign402GatewayHandler(
                            socket,
                            ("127.0.0.1", 12345),
                            server,
                        )
                    handler.response = socket.wfile

                    self.assertIn(status, self.response_text(handler))
                    self.assertEqual(self.response_json(handler)["error"], error)
                    read_json.assert_not_called()
                    dispatched.assert_not_called()

    def wallet_auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-wallet-token"}

    def photon_auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-photon-token"}

    def llm_auth_headers(self, user_token: str = "user-token-1") -> dict[str, str]:
        return {
            **self.wallet_auth_headers(),
            "X-Sign402-User-Token": user_token,
        }

    def test_kill_switch_blocks_every_transaction_route_before_dispatch(self):
        routes = {
            "/approve-payment": "_handle_approve_payment",
            "/execute-payment": "_handle_execute_payment",
            "/agent/buy-probe": "_handle_agent_buy_probe",
            "/agent/buy-tool": "_handle_agent_buy_tool",
            "/agent/buy-x402": "_handle_agent_buy_x402",
            "/agent/top-up-llm-credits": "_handle_agent_top_up_llm_credits",
            "/agent/buy-bitrefill": "_handle_agent_buy_bitrefill",
            "/agent/buy-wallet-bitrefill": "_handle_agent_buy_wallet_bitrefill",
            "/agent/withdraw": "_handle_agent_withdraw",
            "/agent/llm-key/start": "_handle_agent_llm_key_start",
            "/agent/llm-key/verify": "_handle_agent_llm_key_verify",
            "/agent/llm-key/reconcile": "_handle_agent_llm_key_reconcile",
            "/internal/fulfill-bitrefill": (
                "_handle_internal_fulfill_bitrefill"
            ),
        }
        self.assertEqual(FUND_MOVING_POST_PATHS, frozenset(routes))

        for enabled in ("1", "true", "yes", "on"):
            with patch.dict(
                os.environ,
                {"SIGN402_PURCHASES_PAUSED": enabled},
            ):
                for path, handler_name in routes.items():
                    with self.subTest(enabled=enabled, path=path):
                        server = DummyServer()
                        server.firefly = Mock()
                        server.payment_executor = Mock()
                        server.agent_buy_probe = Mock()
                        server.x402_buyer = Mock()
                        server.user_x402_buyer = Mock()
                        server.bankr_llm_topup = Mock()
                        server.bitrefill_purchase_runner = Mock()
                        server.bitrefill_wallet_purchase_runner = Mock()
                        server.bitrefill_fulfillment_runner = Mock()
                        tripwires = (
                            server.firefly.approve_payment_hash,
                            server.payment_executor,
                            server.agent_buy_probe,
                            server.x402_buyer,
                            server.user_x402_buyer,
                            server.bankr_llm_topup,
                            server.bitrefill_purchase_runner,
                            server.bitrefill_wallet_purchase_runner,
                            server.bitrefill_fulfillment_runner,
                            server.user_wallet_service.decrypt_private_key_for_future_signing,
                            server.user_token_transfer_client.transfer_token,
                            server.user_token_transfer_client.transfer_native,
                            server.imessage_approval_service.request_purchase_approval,
                            server.bankr_llm_purchase_service.start,
                            server.bankr_llm_purchase_service.verify_otp,
                            server.bankr_llm_purchase_service.resume,
                            server.bankr_llm_purchase_service.reconcile,
                        )
                        with patch.object(
                            Sign402GatewayHandler,
                            "_read_json",
                            side_effect=AssertionError("body was read"),
                        ) as read_json:
                            with patch.object(
                                Sign402GatewayHandler,
                                handler_name,
                                side_effect=AssertionError(
                                    "handler was dispatched"
                                ),
                            ):
                                with patch("sys.stderr", io.StringIO()):
                                    handler = self.make_handler(
                                        path,
                                        {},
                                        server=server,
                                        headers=self.llm_auth_headers(),
                                    )
                        response = self.response_text(handler)
                        body = json.loads(
                            response.split("\r\n\r\n", 1)[1]
                        )
                        self.assertIn("HTTP/1.0 503", response)
                        self.assertEqual(
                            body,
                            {
                                "ok": False,
                                "paused": True,
                                "telegramText": (
                                    "⏸️ Purchases are temporarily paused for "
                                    "maintenance. Please try again later."
                                ),
                            },
                        )
                        read_json.assert_not_called()
                        for tripwire in tripwires:
                            tripwire.assert_not_called()

    def test_kill_switch_allows_representative_non_transaction_routes(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "u"
        server.user_event_store = Mock()
        server.user_event_store.read.return_value = {
            "ok": True,
            "telegramText": "Last purchase",
        }
        server.bitrefill_search_service = Mock(
            return_value={"ok": True, "products": []}
        )
        server.bitrefill_quote_service = Mock(
            return_value={"ok": True, "quoteId": "q1"}
        )
        server.bitrefill_settlement_preparation_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "q1",
                "status": "ready",
            }
        )
        server.user_spend_limit_store.limit_settings.return_value = {
            "maxPerTxAtomic": 10000,
            "dailyCapAtomic": 100000,
            "maxPerTxUsdc": "0.01",
            "dailyCapUsdc": "0.1",
            "operatorMaxPerTxAtomic": 10000,
            "operatorDailyCapAtomic": 100000,
            "userConfigured": False,
        }
        server.user_spend_limit_store.set_limit_settings.return_value = dict(
            server.user_spend_limit_store.limit_settings.return_value
        )
        cases = (
            (
                "/agent/spending-limits",
                {"telegramUserId": "u"},
                self.llm_auth_headers(),
            ),
            (
                "/agent/spending-limits",
                {
                    "telegramUserId": "u",
                    "maxPerTxAtomic": 10000,
                    "dailyCapAtomic": 100000,
                },
                self.llm_auth_headers(),
            ),
            (
                "/agent/last-purchase",
                {"telegramUserId": "u"},
                self.llm_auth_headers(),
            ),
            (
                "/agent/search-bitrefill",
                {"query": "gift"},
                None,
            ),
            (
                "/agent/quote-bitrefill",
                {"productId": "p1", "packageId": "pkg1"},
                None,
            ),
            (
                "/internal/prepare-bitrefill-settlement",
                {"quoteId": "q1", "fulfillmentToken": "test"},
                {"Authorization": "Bearer internal-test-secret"},
            ),
        )
        with patch.dict(
            os.environ,
            {
                "SIGN402_PURCHASES_PAUSED": "1",
                "SIGN402_BANKR_FULFILLMENT_SECRET": (
                    "internal-test-secret"
                ),
            },
        ):
            for path, payload, headers in cases:
                with self.subTest(path=path):
                    with patch("sys.stderr", io.StringIO()):
                        handler = self.make_handler(
                            path,
                            payload,
                            server=server,
                            headers=headers,
                        )
                    response = self.response_text(handler)
                    self.assertIn("HTTP/1.0 200 OK", response)
                    self.assertNotIn("HTTP/1.0 503", response)

    def test_purchase_pause_parser_rejects_all_other_values(self):
        for value in ("", "0", "false", "no", "off", "enabled"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"SIGN402_PURCHASES_PAUSED": value},
                ):
                    self.assertFalse(_purchases_paused())

    def test_withdraw_tokens_requires_per_user_token(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/withdraw/tokens",
                {"telegramUserId": "123"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        self.assertIn("HTTP/1.0 401 Unauthorized", self.response_text(handler))
        server.user_wallet_service.withdrawable_tokens.assert_not_called()

    def test_withdraw_tokens_returns_authenticated_inventory(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.user_wallet_service.withdrawable_tokens.return_value = {
            "ok": True,
            "wallet": {"address": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C"},
            "tokens": [
                {
                    "symbol": "SINGIT",
                    "contractAddress": DEFAULT_SINGIT_TOKEN_ADDRESS,
                    "balance": "250",
                    "decimals": 18,
                    "verified": True,
                }
            ],
            "telegramText": "Choose an asset to withdraw:\n1. SINGIT: 250",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/withdraw/tokens",
                {"telegramUserId": "123"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertEqual(body["tokens"][0]["symbol"], "SINGIT")
        server.user_wallet_service.withdrawable_tokens.assert_called_once_with("123")

    def test_withdraw_uses_imessage_approval_before_token_transfer(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.user_wallet_service.withdrawable_tokens.return_value = {
            "ok": True,
            "wallet": {"address": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C"},
            "tokens": [
                {
                    "symbol": "SINGIT",
                    "contractAddress": DEFAULT_SINGIT_TOKEN_ADDRESS,
                    "balance": "250",
                    "decimals": 18,
                    "verified": True,
                }
            ],
        }
        server.user_wallet_service.decrypt_private_key_for_future_signing.return_value = (
            "0xSECRET"
        )
        server.imessage_approval_service.request_hash_approval.return_value = {
            "ok": True,
            "status": "approved",
            "commitmentHash": "a" * 64,
        }
        server.user_token_transfer_client.transfer_token.return_value = {
            "ok": True,
            "txId": "0x" + "b" * 64,
            "transactionHash": "0x" + "b" * 64,
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/withdraw",
                {
                    "telegramUserId": "123",
                    "tokenAddress": DEFAULT_SINGIT_TOKEN_ADDRESS,
                    "amount": "100",
                    "toAddress": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertTrue(body["ok"])
        self.assertEqual(body["txId"], "0x" + "b" * 64)
        server.imessage_approval_service.request_hash_approval.assert_called_once()
        # The approval is the only safeguard on a withdrawal, so the approver
        # must see the destination in full. An abbreviated address cannot be
        # distinguished from a cheaply generated look-alike.
        approval_kwargs = (
            server.imessage_approval_service.request_hash_approval.call_args.kwargs
        )
        self.assertIn(
            "To: 0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            approval_kwargs["context_lines"],
        )
        destination_lines = [
            line for line in approval_kwargs["context_lines"] if line.startswith("To:")
        ]
        self.assertEqual(len(destination_lines), 1)
        self.assertNotIn("...", destination_lines[0])
        server.user_token_transfer_client.transfer_token.assert_called_once_with(
            private_key="0xSECRET",
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
            amount="100",
            chain="base",
            decimals=18,
        )

    def test_withdraw_native_eth_uses_approval_and_native_transfer(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.user_wallet_service.withdrawable_tokens.return_value = {
            "ok": True,
            "wallet": {"address": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C"},
            "tokens": [
                {
                    "symbol": "ETH",
                    "contractAddress": BASE_NATIVE_ETH_ASSET_ID,
                    "balance": "0.01",
                    "decimals": 18,
                    "verified": True,
                    "native": True,
                }
            ],
        }
        server.user_wallet_service.decrypt_private_key_for_future_signing.return_value = (
            "0xSECRET"
        )
        server.imessage_approval_service.request_hash_approval.return_value = {
            "ok": True,
            "status": "approved",
            "commitmentHash": "a" * 64,
        }
        server.user_token_transfer_client.transfer_native.return_value = {
            "ok": True,
            "txId": "0x" + "c" * 64,
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/withdraw",
                {
                    "telegramUserId": "123",
                    "tokenAddress": BASE_NATIVE_ETH_ASSET_ID,
                    "amount": "0.005",
                    "toAddress": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertTrue(body["ok"])
        self.assertEqual(body["txId"], "0x" + "c" * 64)
        server.imessage_approval_service.request_hash_approval.assert_called_once()
        server.user_token_transfer_client.transfer_native.assert_called_once_with(
            private_key="0xSECRET",
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            amount="0.005",
            chain="base",
        )

    def test_withdraw_native_eth_keeps_gas_reserve_before_approval(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.user_wallet_service.withdrawable_tokens.return_value = {
            "ok": True,
            "wallet": {"address": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C"},
            "tokens": [
                {
                    "symbol": "ETH",
                    "contractAddress": BASE_NATIVE_ETH_ASSET_ID,
                    "balance": "0.01",
                    "decimals": 18,
                    "verified": True,
                    "native": True,
                }
            ],
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/withdraw",
                {
                    "telegramUserId": "123",
                    "tokenAddress": BASE_NATIVE_ETH_ASSET_ID,
                    "amount": "0.01",
                    "toAddress": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 400 Bad Request", self.response_text(handler))
        self.assertEqual(body["error"], "withdraw_request_failed")
        self.assertIn("leave at least", body["detail"])
        server.imessage_approval_service.request_hash_approval.assert_not_called()
        server.user_token_transfer_client.transfer_native.assert_not_called()

    def test_llm_key_start_requires_per_user_token(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/start",
                {
                    "telegramUserId": "123",
                    "amountUsd": "10",
                    "email": "user@example.com",
                },
                server=server,
                headers=self.wallet_auth_headers(),
            )

        self.assertIn("HTTP/1.0 401 Unauthorized", self.response_text(handler))
        server.bankr_llm_purchase_service.start.assert_not_called()

    def test_llm_key_start_rejects_token_for_another_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "456"

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/start",
                {
                    "telegramUserId": "123",
                    "amountUsd": "10",
                    "email": "user@example.com",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        self.assertIn("HTTP/1.0 401 Unauthorized", self.response_text(handler))
        server.bankr_llm_purchase_service.start.assert_not_called()

    def test_llm_key_start_dispatches_authenticated_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.start.return_value = {
            "ok": True,
            "state": "AWAITING_TERMS",
            "telegramText": "Review terms.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/start",
                {
                    "telegramUserId": "123",
                    "amountUsd": "10",
                    "email": "user@example.com",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        server.bankr_llm_purchase_service.start.assert_called_once_with(
            telegram_user_id="123",
            amount_usd="10",
            email="user@example.com",
            payment_token="",
        )

    def test_llm_key_start_dispatches_payment_token(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.start.return_value = {
            "ok": True,
            "state": "AWAITING_TERMS",
            "telegramText": "Review terms.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/start",
                {
                    "telegramUserId": "123",
                    "amountUsd": "10",
                    "email": "user@example.com",
                    "paymentToken": "USDC",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        server.bankr_llm_purchase_service.start.assert_called_once_with(
            telegram_user_id="123",
            amount_usd="10",
            email="user@example.com",
            payment_token="USDC",
        )

    def test_llm_key_accept_terms_dispatches_authenticated_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.accept_terms.return_value = {
            "ok": True,
            "state": "AWAITING_OTP",
            "telegramText": "Verification code sent.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/accept-terms",
                {"telegramUserId": "123"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        server.bankr_llm_purchase_service.accept_terms.assert_called_once_with("123")

    def test_llm_key_verify_resumes_approved_purchase(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.verify_otp.return_value = {
            "ok": True,
            "purchaseId": "purchase-1",
            "state": "AWAITING_TRANSFER",
        }
        server.bankr_llm_purchase_service.resume.return_value = {
            "ok": True,
            "purchaseId": "purchase-1",
            "state": "COMPLETE",
            "apiKey": "bk_return_once",
            "telegramText": "Bankr LLM purchase complete.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/verify",
                {"telegramUserId": "123", "code": "123456"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertEqual(body["apiKey"], "bk_return_once")
        server.bankr_llm_purchase_service.verify_otp.assert_called_once_with(
            telegram_user_id="123",
            code="123456",
        )
        server.bankr_llm_purchase_service.resume.assert_called_once_with("purchase-1")

    def test_llm_key_reconcile_requires_per_user_token(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/reconcile",
                {"telegramUserId": "123", "purchaseId": "purchase-1"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        self.assertIn("HTTP/1.0 401 Unauthorized", self.response_text(handler))
        server.bankr_llm_purchase_service.reconcile.assert_not_called()

    def test_llm_key_reconcile_dispatches_authenticated_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.reconcile.return_value = {
            "ok": True,
            "purchaseId": "purchase-1",
            "state": "COMPLETE",
            "apiKey": "bk_return_once",
            "telegramText": "Bankr LLM purchase complete.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/reconcile",
                {"telegramUserId": "123", "purchaseId": "purchase-1"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertEqual(body["apiKey"], "bk_return_once")
        server.bankr_llm_purchase_service.reconcile.assert_called_once_with(
            "purchase-1",
            telegram_user_id="123",
        )

    def test_llm_credits_dispatches_authenticated_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.credits.return_value = {
            "ok": True,
            "state": "COMPLETE",
            "credits": {"credits": "10.00"},
            "apiKeyFingerprint": "abc123",
            "telegramText": "Credits: $10.00",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-credits",
                {"telegramUserId": "123"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        server.bankr_llm_purchase_service.credits.assert_called_once_with("123")

    def test_llm_route_returns_only_safe_bankr_error(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        error = BankrLlmError(
            "bankr_auth_unavailable",
            "Bankr authentication is unavailable. Please try again.",
        )
        error.__cause__ = RuntimeError("raw provider token and response")
        server.bankr_llm_purchase_service.start.side_effect = error

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/start",
                {
                    "telegramUserId": "123",
                    "amountUsd": "10",
                    "email": "user@example.com",
                },
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 400 Bad Request", response)
        self.assertEqual(body["error"], "bankr_auth_unavailable")
        self.assertEqual(
            body["telegramText"],
            "Bankr authentication is unavailable. Please try again.",
        )
        self.assertNotIn("raw provider", response)

    def test_llm_route_returns_redacted_unexpected_error_detail(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "123"
        server.bankr_llm_purchase_service.verify_otp.side_effect = ValueError(
            "required SINGIT exceeds configured maximum for bk_private_key"
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/llm-key/verify",
                {"telegramUserId": "123", "code": "123456"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)
        self.assertIn("HTTP/1.0 500 Internal Server Error", response)
        self.assertEqual(body["error"], "bankr_llm_request_failed")
        self.assertEqual(body["errorType"], "ValueError")
        self.assertIn("required SINGIT exceeds configured maximum", body["detail"])
        self.assertNotIn("bk_private_key", response)
        self.assertIn("bk_[redacted]", response)

    def test_health_lists_bankr_llm_purchase_routes(self):
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/health",
                method="GET",
                server=DummyServer(),
            )

        endpoints = self.response_json(handler)["endpoints"]
        self.assertIn("/agent/llm-key/start", endpoints)
        self.assertIn("/agent/llm-key/accept-terms", endpoints)
        self.assertIn("/agent/llm-key/verify", endpoints)
        self.assertIn("/agent/llm-key/reconcile", endpoints)
        self.assertIn("/agent/llm-credits", endpoints)
        self.assertNotIn("/agent/test-imessage-approval", endpoints)

    def test_health_lists_test_approval_only_when_explicitly_enabled(self):
        with patch.dict(
            os.environ,
            {"SIGN402_ENABLE_TEST_ENDPOINTS": "true"},
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/health",
                method="GET",
                server=DummyServer(),
            )

        endpoints = self.response_json(handler)["endpoints"]
        self.assertIn("/agent/test-imessage-approval", endpoints)

    def test_health_lists_bitrefill_catalog_route(self):
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/health",
                method="GET",
                server=DummyServer(),
            )

        endpoints = self.response_json(handler)["endpoints"]
        self.assertIn("/agent/list-bitrefill-products", endpoints)

    def test_agent_create_wallet_requires_telegram_user_id(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/create-wallet",
                {"telegramUsername": "mp"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400 Bad Request", response)
        self.assertEqual(self.response_json(handler)["error"], "telegramUserId is required")
        server.user_wallet_service.create_wallet.assert_not_called()

    def test_agent_create_wallet_returns_safe_metadata(self):
        server = DummyServer()
        server.user_wallet_service.create_wallet.return_value = {
            "ok": True,
            "created": True,
            "wallet": {
                "chain": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "status": "created",
                "spendingEnabled": False,
            },
            "telegramText": "Wallet ready",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/create-wallet",
                {"telegramUserId": "1045618308", "telegramUsername": "AlpskyKnedlik"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertNotIn("private", response.lower())
        self.assertEqual(body["wallet"]["address"], "0x1111111111111111111111111111111111111111")
        server.user_wallet_service.create_wallet.assert_called_once_with(
            telegram_user_id="1045618308",
            telegram_username="AlpskyKnedlik",
        )

    def test_agent_create_wallet_is_rate_limited_across_all_users(self):
        """A leaked shared token must not be able to mint tokens in bulk.

        Limiting per telegramUserId alone would not bite, because the caller
        picks the id and can walk a fresh one on every request.
        """
        server = DummyServer()
        server.user_wallet_service.create_wallet.return_value = {"ok": True}
        statuses = []

        with patch("sys.stderr", io.StringIO()):
            with patch.dict(
                os.environ, {"SIGN402_WALLET_CREATIONS_PER_MINUTE": "3"}
            ):
                for index in range(5):
                    handler = self.make_handler(
                        "/agent/create-wallet",
                        {"telegramUserId": f"10456183{index:02d}"},
                        server=server,
                        headers=self.wallet_auth_headers(),
                    )
                    statuses.append("HTTP/1.0 200 OK" in self.response_text(handler))

        self.assertEqual(statuses, [True, True, True, False, False])
        self.assertEqual(server.user_wallet_service.create_wallet.call_count, 3)

    def test_agent_create_wallet_requires_wallet_api_token(self):
        server = DummyServer()
        server.user_wallet_service.create_wallet.return_value = {"ok": True}

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/create-wallet",
                {"telegramUserId": "1045618308"},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401 Unauthorized", response)
        self.assertEqual(self.response_json(handler)["error"], "invalid wallet API token")
        server.user_wallet_service.create_wallet.assert_not_called()

    def test_agent_create_wallet_fails_when_wallet_api_token_not_configured(self):
        server = DummyServer()
        server.user_wallet_api_token = ""

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/create-wallet",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 503 Service Unavailable", response)
        self.assertEqual(self.response_json(handler)["error"], "SIGN402_WALLET_API_TOKEN is required")
        server.user_wallet_service.create_wallet.assert_not_called()

    def test_agent_create_wallet_strips_accidental_private_key_material(self):
        server = DummyServer()
        server.user_wallet_service.create_wallet.return_value = {
            "ok": True,
            "created": True,
            "wallet": {
                "chain": "base",
                "address": "0x1111111111111111111111111111111111111111",
                "status": "created",
                "spendingEnabled": False,
                "privateKey": "0x" + "a" * 64,
                "nested": {"encrypted_private_key": "secret-ciphertext"},
            },
            "debug": [{"privateMaterial": "do-not-return"}],
            "telegramText": "Wallet ready",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/create-wallet",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertNotIn("private", response.lower())
        self.assertNotIn("secret-ciphertext", response)
        self.assertNotIn("0x" + "a" * 64, response)
        self.assertEqual(body["wallet"]["address"], "0x1111111111111111111111111111111111111111")

    def test_agent_wallet_status_uses_user_wallet_service(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_wallet_service.wallet_status.return_value = {
            "ok": False,
            "wallet": None,
            "telegramText": "No Base agent wallet yet.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/wallet",
                {"userId": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 404 Not Found", response)
        server.user_wallet_service.wallet_status.assert_called_once_with("1045618308")

    def test_agent_wallet_balance_degrades_safely(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_wallet_service.wallet_balance.return_value = {
            "ok": True,
            "wallet": {
                "chain": "base",
                "address": "0x2222222222222222222222222222222222222222",
                "status": "created",
                "spendingEnabled": False,
            },
            "balanceUnavailable": True,
            "telegramText": "Balance lookup is not configured yet.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/wallet-balance",
                {"telegram_user_id": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertNotIn("private", response.lower())
        self.assertTrue(self.response_json(handler)["balanceUnavailable"])
        server.user_wallet_service.wallet_balance.assert_called_once_with("1045618308")

    def test_user_scoped_endpoints_require_per_user_token(self):
        cases = [
            ("/agent/wallet", {"telegramUserId": "1045618308"}),
            ("/agent/wallet-balance", {"telegramUserId": "1045618308"}),
            ("/agent/last-purchase", {"telegramUserId": "1045618308"}),
            (
                "/agent/quote-bitrefill",
                {
                    "telegramUserId": "1045618308",
                    "productId": "x",
                    "packageId": "y",
                },
            ),
            (
                "/agent/buy-wallet-bitrefill",
                {"telegramUserId": "1045618308", "quoteId": "q"},
            ),
        ]
        for path, payload in cases:
            with self.subTest(path=path):
                with patch("sys.stderr", io.StringIO()):
                    handler = self.make_handler(
                        path,
                        payload,
                        server=DummyServer(),
                        headers=self.wallet_auth_headers(),
                    )
                self.assertIn(
                    "HTTP/1.0 401 Unauthorized", self.response_text(handler)
                )

    def test_imessage_pairing_requires_photon_api_token(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/imessage/pairing",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401 Unauthorized", response)
        self.assertEqual(
            self.response_json(handler)["error"],
            "invalid iMessage approval API token",
        )
        server.imessage_approval_service.create_pairing.assert_not_called()

    def test_imessage_pairing_fails_when_photon_api_token_not_configured(self):
        server = DummyServer()
        server.imessage_approval_api_token = ""

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/imessage/pairing",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 503 Service Unavailable", response)
        self.assertEqual(
            self.response_json(handler)["error"],
            "SIGN402_PHOTON_API_TOKEN is required",
        )

    def test_imessage_pairing_endpoint_returns_safe_text(self):
        server = DummyServer()
        server.imessage_approval_service.create_pairing.return_value = {
            "ok": True,
            "code": "ABCDEFGH",
            "telegramText": "Send this code",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/imessage/pairing",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertEqual(body["telegramText"], "Send this code")
        server.imessage_approval_service.create_pairing.assert_called_once_with(
            "1045618308"
        )

    def test_select_existing_approval_channel_uses_photon_auth(self):
        server = DummyServer()
        server.imessage_approval_service.select_existing_channel.return_value = {
            "ok": True,
            "selected": True,
            "requiresPairing": False,
            "channel": "imessage",
            "telegramText": "iMessage selected for Sign402 approvals.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/approval-channel/select-existing",
                {"telegramUserId": "1045618308", "channel": "imessage"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["selected"])
        self.assertNotIn("phone", json.dumps(body).lower())
        server.imessage_approval_service.select_existing_channel.assert_called_once_with(
            "1045618308",
            "imessage",
        )

    def test_select_existing_approval_channel_rejects_wallet_token(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/approval-channel/select-existing",
                {"telegramUserId": "1045618308", "channel": "whatsapp"},
                server=server,
                headers=self.wallet_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401 Unauthorized", response)
        self.assertEqual(
            self.response_json(handler)["error"],
            "invalid iMessage approval API token",
        )
        server.imessage_approval_service.select_existing_channel.assert_not_called()

    def test_imessage_link_endpoint_uses_trusted_photon_source(self):
        server = DummyServer()
        server.imessage_approval_service.link_photon_sender.return_value = {
            "ok": True,
            "imessageText": "iMessage linked.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/imessage/link",
                {"code": "ABCDEFGH", "photonUserId": "+15551234567"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        server.imessage_approval_service.link_photon_sender.assert_called_once_with(
            "ABCDEFGH",
            "+15551234567",
        )

    def test_approval_endpoints_accept_whatsapp_identity_and_channel(self):
        server = DummyServer()
        server.imessage_approval_service.link_sender.return_value = {
            "ok": True,
            "channel": "whatsapp",
            "imessageText": "WhatsApp linked.",
        }
        server.imessage_approval_service.pending_for_photon_sender.return_value = {
            "ok": True,
            "pending": True,
            "approvalId": "approval-123",
        }
        server.imessage_approval_service.record_decision.return_value = {
            "ok": True,
            "status": "approved",
            "imessageText": "Approved.",
        }

        with patch("sys.stderr", io.StringIO()):
            link_handler = self.make_handler(
                "/agent/imessage/link",
                {
                    "code": "ABCDEFGH",
                    "approvalUserId": "420777111222",
                    "channel": "whatsapp",
                },
                server=server,
                headers=self.photon_auth_headers(),
            )
            pending_handler = self.make_handler(
                "/agent/imessage/pending",
                {"approvalUserId": "420777111222", "channel": "whatsapp"},
                server=server,
                headers=self.photon_auth_headers(),
            )
            decision_handler = self.make_handler(
                "/agent/imessage/decision",
                {
                    "approvalUserId": "420777111222",
                    "channel": "whatsapp",
                    "decision": "YES",
                    "approvalId": "approval-123",
                },
                server=server,
                headers=self.photon_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(link_handler))
        self.assertIn("HTTP/1.0 200 OK", self.response_text(pending_handler))
        self.assertIn("HTTP/1.0 200 OK", self.response_text(decision_handler))
        server.imessage_approval_service.link_sender.assert_called_once_with(
            "ABCDEFGH", "420777111222", channel="whatsapp"
        )
        server.imessage_approval_service.pending_for_photon_sender.assert_called_once_with(
            "420777111222", channel="whatsapp"
        )
        server.imessage_approval_service.record_decision.assert_called_once_with(
            "420777111222",
            "YES",
            approval_id="approval-123",
            channel="whatsapp",
        )

    def test_imessage_pending_and_decision_endpoints_use_photon_source(self):
        server = DummyServer()
        server.imessage_approval_service.pending_for_photon_sender.return_value = {
            "ok": True,
            "pending": True,
            "approvalId": "appr_1",
        }
        server.imessage_approval_service.record_decision.return_value = {
            "ok": True,
            "status": "approved",
            "imessageText": "Approved.",
        }

        with patch("sys.stderr", io.StringIO()):
            pending_handler = self.make_handler(
                "/agent/imessage/pending",
                {"photonUserId": "+15551234567"},
                server=server,
                headers=self.photon_auth_headers(),
            )
            decision_handler = self.make_handler(
                "/agent/imessage/decision",
                {"photonUserId": "+15551234567", "decision": "YES"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(pending_handler))
        self.assertIn("HTTP/1.0 200 OK", self.response_text(decision_handler))
        server.imessage_approval_service.pending_for_photon_sender.assert_called_once_with(
            "+15551234567"
        )
        server.imessage_approval_service.record_decision.assert_called_once_with(
            "+15551234567",
            "YES",
            approval_id=None,
        )

    def test_imessage_unlink_endpoint_removes_link_by_telegram_user(self):
        server = DummyServer()
        server.imessage_approval_service.unlink_photon_sender.return_value = {
            "ok": True,
            "removed": True,
            "telegramText": "iMessage approval link removed.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/imessage/unlink",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)
        body = self.response_json(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["removed"])
        server.imessage_approval_service.unlink_photon_sender.assert_called_once_with(
            telegram_user_id="1045618308",
            photon_user_id="",
        )

    def test_test_imessage_approval_endpoint_uses_telegram_identity(self):
        server = DummyServer()
        server.imessage_approval_service.create_test_approval.return_value = {
            "ok": True,
            "telegramText": "Test approval sent",
        }

        with patch.dict(
            os.environ,
            {"SIGN402_ENABLE_TEST_ENDPOINTS": "true"},
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/test-imessage-approval",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        server.imessage_approval_service.create_test_approval.assert_called_once_with(
            "1045618308"
        )

    def test_test_imessage_approval_endpoint_is_disabled_by_default(self):
        server = DummyServer()

        with patch.dict(
            os.environ,
            {"SIGN402_ENABLE_TEST_ENDPOINTS": ""},
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/test-imessage-approval",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.photon_auth_headers(),
            )

        self.assertIn("HTTP/1.0 404 Not Found", self.response_text(handler))
        server.imessage_approval_service.create_test_approval.assert_not_called()

    def test_approve_payment_uses_firefly(self):
        payment_hash = "b" * 64
        DummyServer.firefly.reset_mock()
        DummyServer.payment_executor.reset_mock()
        DummyServer.firefly_busy = False
        DummyServer.firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": payment_hash,
            "deviceModel": 262,
            "deviceSerial": 1056,
            "raw": "<OK",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/approve-payment",
                {"paymentHash": payment_hash, "paymentCommitment": {"type": "sign402-payment"}},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"approved": true', response)
        DummyServer.firefly.approve_payment_hash.assert_called_once_with(
            payment_hash,
            context_lines=["x402 PAYMENT", "sign402 approval"],
        )
        DummyServer.payment_executor.assert_not_called()

    def test_approve_policy_stores_policy_for_agent_endpoint(self):
        policy = {
            "version": "1",
            "agentId": "hermes-demo",
            "policyId": "policy-test",
            "allowedPurpose": "x402_api_access",
            "asset": "ALGO_TEST",
            "maxBudgetAtomic": "1000000",
            "maxPerPaymentAtomic": "50000",
            "nonce": "test",
        }
        DummyServer.firefly.reset_mock()
        DummyServer.agent_state_store.reset_mock()
        DummyServer.firefly_busy = False

        from sign402_bridge.policy import hash_policy

        policy_hash = hash_policy(policy)
        DummyServer.firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": policy_hash,
            "deviceModel": 262,
            "deviceSerial": 1056,
            "raw": "<OK",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/approve-policy", {"policy": policy})

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"approved": true', response)
        DummyServer.firefly.approve_payment_hash.assert_called_once_with(policy_hash)
        DummyServer.firefly.approve_policy_hash.assert_not_called()
        DummyServer.agent_state_store.write_policy.assert_called_once()
        stored = DummyServer.agent_state_store.write_policy.call_args.args[0]
        self.assertEqual(stored["policy"], policy)
        self.assertEqual(stored["policyHash"], policy_hash)
        self.assertEqual(stored["firefly"]["approvedHash"], policy_hash)

    def test_execute_payment_uses_local_executor_without_exposing_secrets(self):
        policy_hash = "a" * 64
        approval_hash = "b" * 64
        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }
        DummyServer.firefly.reset_mock()
        DummyServer.payment_executor.reset_mock()
        DummyServer.payment_executor.return_value = {
            "txId": "TXID",
            "network": "algorand-testnet",
            "receiver": "MERCHANT",
            "amountAtomic": "50000",
            "asset": "ALGO_TEST",
            "paymentIntent": "intent-001",
            "policyHash": policy_hash,
            "note": f"sign402:{policy_hash}:intent-001",
        }

        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "true",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "legacy-operator-token",
            },
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/execute-payment",
                {
                    "policyHash": policy_hash,
                    "paymentApprovalHash": approval_hash,
                    "paymentRequirements": requirement,
                },
                headers={"Authorization": "Bearer legacy-operator-token"},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        self.assertIn('"txId": "TXID"', response)
        self.assertNotIn("private", response.lower())
        self.assertNotIn("mnemonic", response.lower())
        DummyServer.payment_executor.assert_called_once_with(requirement, policy_hash)

    def test_execute_payment_is_not_available_without_explicit_operator_opt_in(self):
        DummyServer.payment_executor.reset_mock()
        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "paymentIntent": "intent-001",
        }

        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "",
            },
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/execute-payment",
                {
                    "policyHash": "a" * 64,
                    "paymentApprovalHash": "b" * 64,
                    "paymentRequirements": requirement,
                },
            )

        self.assertIn("HTTP/1.0 404 Not Found", self.response_text(handler))
        self.assertEqual(self.response_json(handler)["error"], "not_found")
        DummyServer.payment_executor.assert_not_called()

    def test_legacy_payment_routes_are_hidden_without_explicit_operator_opt_in(self):
        DummyServer.x402_buyer.reset_mock()

        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "",
            },
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-x402",
                {"url": "https://merchant.example/paid"},
                headers={},
            )

        self.assertIn("HTTP/1.0 404 Not Found", self.response_text(handler))
        self.assertEqual(self.response_json(handler)["error"], "not_found")
        DummyServer.x402_buyer.assert_not_called()

    def test_execute_payment_rejects_invalid_hash_before_executor(self):
        DummyServer.payment_executor.reset_mock()
        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "paymentIntent": "intent-001",
        }

        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "true",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "legacy-operator-token",
            },
        ), patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/execute-payment",
                {
                    "policyHash": "not-a-hash",
                    "paymentApprovalHash": "b" * 64,
                    "paymentRequirements": requirement,
                },
                headers={"Authorization": "Bearer legacy-operator-token"},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400 Bad Request", response)
        DummyServer.payment_executor.assert_not_called()

    def test_events_latest_can_be_written_and_read(self):
        event = {
            "decision": "APPROVED & EXECUTED",
            "policyHash": "a" * 64,
            "paymentApprovalHash": "b" * 64,
            "txId": "TXID",
        }
        DummyServer.event_store.reset_mock()
        DummyServer.event_store.write.return_value = event
        DummyServer.event_store.read.return_value = event

        with patch("sys.stderr", io.StringIO()):
            post_handler = self.make_handler("/events/latest", {"event": event})
            get_handler = self.make_handler("/events/latest", method="GET")

        post_response = self.response_text(post_handler)
        get_response = self.response_text(get_handler)

        self.assertIn("HTTP/1.0 200 OK", post_response)
        self.assertIn('"ok": true', post_response)
        self.assertNotIn("Access-Control-Allow-Origin", post_response)
        self.assertIn("HTTP/1.0 200 OK", get_response)
        self.assertIn('"decision": "APPROVED & EXECUTED"', get_response)
        DummyServer.event_store.write.assert_called_once_with(event)
        DummyServer.event_store.read.assert_called_once()

    def test_cors_requires_an_explicit_allowlisted_origin(self):
        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_CORS": "true",
                "SIGN402_CORS_ALLOWED_ORIGINS": "https://app.sign402.example",
            },
        ), patch("sys.stderr", io.StringIO()):
            allowed = self.make_handler(
                "/health",
                method="OPTIONS",
                headers={"Origin": "https://app.sign402.example"},
            )
            denied = self.make_handler(
                "/health",
                method="OPTIONS",
                headers={"Origin": "https://evil.example"},
            )

        allowed_response = self.response_text(allowed)
        denied_response = self.response_text(denied)
        self.assertIn("HTTP/1.0 204 No Content", allowed_response)
        self.assertIn(
            "Access-Control-Allow-Origin: https://app.sign402.example",
            allowed_response,
        )
        self.assertIn("Vary: Origin", allowed_response)
        self.assertIn("HTTP/1.0 404 Not Found", denied_response)
        self.assertNotIn("Access-Control-Allow-Origin", denied_response)

    def test_agent_buy_probe_runs_single_orchestrated_flow(self):
        DummyServer.agent_buy_probe.reset_mock()
        DummyServer.agent_buy_probe.return_value = {
            "decision": "approved_and_executed",
            "target": "algorand.co",
            "txId": "TXID",
            "result": "reachable",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/buy-probe", {"target": "algorand.co"})

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"decision": "approved_and_executed"', response)
        DummyServer.agent_buy_probe.assert_called_once_with("algorand.co")

    def test_agent_inspect_llm_credits_topup_returns_singit_commitment(self):
        DummyServer.bankr_llm_topup_inspector.reset_mock()
        DummyServer.bankr_llm_topup_inspector.return_value = {
            "ok": True,
            "mode": "inspect_llm_credits_topup",
            "topUpIntent": {
                "creditAmountUsd": "5",
                "fundingTokenAddress": "0x2222222222222222222222222222222222222222",
                "fundingTokenSymbol": "SINGIT",
                "maxFundingTokenAmountAtomic": "5000000000000000000000",
                "purpose": "bankr_llm_credits_topup",
            },
            "paymentCommitment": {
                "paymentHash": "d" * 64,
                "commitment": {"type": "sign402-payment"},
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-llm-credits-topup",
                {
                    "creditAmountUsd": "5",
                    "fundingTokenAddress": "0x2222222222222222222222222222222222222222",
                    "fundingTokenSymbol": "SINGIT",
                    "maxFundingTokenAmountAtomic": "5000000000000000000000",
                    "policyHash": "c" * 64,
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"mode": "inspect_llm_credits_topup"', response)
        self.assertIn('"fundingTokenSymbol": "SINGIT"', response)
        DummyServer.bankr_llm_topup_inspector.assert_called_once()

    def test_agent_topup_llm_credits_delegates_to_topup_runner(self):
        DummyServer.bankr_llm_topup.reset_mock()
        DummyServer.bankr_llm_topup.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "bankr_llm_credits_topup",
            "creditAmountUsd": "5",
            "fundingTokenSymbol": "SINGIT",
        }

        payload = {
            "creditAmountUsd": "5",
            "fundingTokenAddress": "0x2222222222222222222222222222222222222222",
            "fundingTokenSymbol": "SINGIT",
            "maxFundingTokenAmountAtomic": "5000000000000000000000",
        }
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/top-up-llm-credits", payload)

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"mode": "bankr_llm_credits_topup"', response)
        DummyServer.bankr_llm_topup.assert_called_once_with(payload)

    def test_llm_credits_topup_defaults_to_singit_token_contract(self):
        intent = _build_bankr_llm_topup_intent(
            {
                "creditAmountUsd": "5",
                "maxFundingTokenAmountAtomic": "5000000000000000000000",
            }
        )

        self.assertEqual(intent["fundingTokenAddress"], DEFAULT_SINGIT_TOKEN_ADDRESS)
        self.assertEqual(intent["fundingTokenSymbol"], "SINGIT")

    def test_agent_inspect_x402_accepts_base_usdc_requirements(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "resourceUrl": "https://merchant.example/paid",
            "paymentRequirements": {
                "network": "base-mainnet",
                "x402Network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amountAtomic": "10000",
                "receiver": "0x1111111111111111111111111111111111111111",
                "resource": "https://merchant.example/paid",
                "paymentIntent": "intent-from-resource",
                "purpose": "x402_api_access",
                "extra": {"name": "USD Coin", "version": "2"},
            },
            "paymentCommitment": {
                "paymentHash": "d" * 64,
                "commitment": {"type": "sign402-payment"},
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-x402",
                {
                    "url": "https://merchant.example/paid",
                    "policyHash": policy_hash,
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        self.assertIn('"amountAtomic": "10000"', response)
        self.assertIn('"quoteText": "Base x402 quote: 0.01 USDC on Base Mainnet."', response)
        DummyServer.x402_inspector.assert_called_once_with(
            "https://merchant.example/paid",
            policy_hash,
        )

    def test_agent_inspect_x402_rejects_non_base_usdc_requirements(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "resourceUrl": "https://x402.goplausible.xyz/examples/weather",
            "paymentRequirements": {
                "network": "algorand-testnet",
                "asset": "10458941",
                "amountAtomic": "10000",
                "receiver": "PAYEE",
                "paymentIntent": "intent-from-resource",
                "purpose": "x402_api_access",
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-x402",
                {
                    "url": "https://x402.goplausible.xyz/examples/weather",
                    "policyHash": policy_hash,
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400 Bad Request", response)
        self.assertIn("Only Base Mainnet x402 endpoints are supported", response)

    def test_agent_buy_x402_runs_base_usdc_buyer_and_returns_telegram_text(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "resourceUrl": "https://merchant.example/paid",
            "txId": "0xTX",
            "amountAtomic": "10000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "90000",
            "result": "official_x402_resource_access_granted",
            "paymentRequirements": {
                "extra": {"name": "USD Coin", "version": "2"},
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-x402",
                {"url": "https://merchant.example/paid"},
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"decision": "approved_and_executed"', response)
        self.assertEqual(
            body["telegramText"],
            "✅ x402 resource unlocked. Paid 0.01 USDC. Tx https://basescan.org/tx/0xTX. Budget left 0.09 USDC.",
        )
        DummyServer.x402_buyer.assert_called_once_with(
            "https://merchant.example/paid",
            requirement_validator=ANY,
        )

    def test_external_x402_buyer_uses_cdp_for_base_mainnet_after_firefly_approval(self):
        policy_hash = "a" * 64
        payment_hash = "b" * 64
        policy = {
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
            "allowedPurpose": "x402_api_access",
            "maxPerPaymentAtomic": "10000",
            "maxBudgetAtomic": "30000",
        }
        policy_state = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        raw_payment_required = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "amount": "10000",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
                    "payTo": "0x1111111111111111111111111111111111111111",
                    "extra": {
                        "name": "USD Coin",
                        "version": "2",
                        "paymentIntent": "base-intent-1",
                    },
                }
            ],
        }

        firefly = Mock()
        firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": payment_hash,
            "deviceModel": 262,
            "deviceSerial": 1056,
        }
        agent_state_store = Mock()
        agent_state_store.read_policy.return_value = policy_state
        agent_state_store.read_policy_for_requirement.return_value = policy_state
        agent_state_store.remaining_budget.return_value = 20000
        event_store = Mock()
        cdp_buyer = Mock(
            return_value={
                "status": 200,
                "body": {"ok": True},
                "paymentResponse": {"transaction": "0xTX"},
            }
        )
        algorand_builder = Mock()

        buyer = ExternalX402Buyer(
            firefly=firefly,
            payment_signature_builder=algorand_builder,
            base_payment_client=cdp_buyer,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with (
            patch(
                "sign402_gateway.server.fetch_x402_payment_required",
                return_value=raw_payment_required,
            ) as fetch_required,
            patch(
                "sign402_gateway.server.build_payment_commitment",
                return_value={"paymentHash": payment_hash, "commitment": {"type": "sign402-payment"}},
            ),
        ):
            result = buyer("https://api.example.com/paid-report")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "official_x402_base_cdp")
        self.assertEqual(result["txId"], "0xTX")
        firefly.approve_payment_hash.assert_called_once()
        self.assertEqual(
            firefly.approve_payment_hash.call_args.kwargs["context_lines"],
            ["BASE x402 PAYMENT", "0.01 USDC", "Base Mainnet"],
        )
        cdp_buyer.assert_called_once_with("https://api.example.com/paid-report")
        algorand_builder.assert_not_called()
        agent_state_store.record_payment.assert_called_once_with(
            policy_hash,
            "base-intent-1",
            10000,
        )

    def test_agent_buy_tool_with_user_identity_uses_imessage_and_user_wallet(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.event_store = Mock()
        server.user_event_store = Mock()
        server.imessage_approval_service.request_purchase_approval.return_value = {
            "ok": True,
            "status": "approved",
            "approvalId": "approval-1",
            "commitmentHash": "c" * 64,
        }
        server.user_wallet_service.decrypt_private_key_for_future_signing.return_value = (
            "0xUSER_PRIVATE_KEY"
        )
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "paymentIntent": "crypto-news-1",
            "purpose": "x402_api_access",
            "extra": {"name": "USD Coin", "version": "2"},
        }
        server.user_x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_user_wallet",
            "resourceUrl": "https://x402.ottoai.services/crypto-news",
            "txId": "0xTX",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "paymentRequirements": {"extra": {"name": "USD Coin", "version": "2"}},
            "telegramText": "✅ Crypto News unlocked. Paid 0.001 USDC. Tx https://basescan.org/tx/0xTX.",
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "paid_tool_official_x402_base_user_wallet")
        self.assertEqual(
            body["telegramText"],
            "✅ Crypto News unlocked. Paid 0.001 USDC. Tx https://basescan.org/tx/0xTX.",
        )
        server.imessage_approval_service.request_purchase_approval.assert_called_once()
        approval_call = server.imessage_approval_service.request_purchase_approval.call_args.kwargs
        self.assertEqual(approval_call["telegram_user_id"], "1045618308")
        self.assertEqual(approval_call["tool_name"], "Crypto News")
        self.assertEqual(approval_call["payment_requirements"], payment_requirements)
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_called_once_with(
            "1045618308"
        )
        server.user_x402_buyer.assert_called_once_with(
            "https://x402.ottoai.services/crypto-news",
            private_key="0xUSER_PRIVATE_KEY",
            approval=server.imessage_approval_service.request_purchase_approval.return_value,
            payment_requirements=payment_requirements,
            payment_context={"title": "CRYPTO NEWS", "subject": "Otto AI"},
        )
        DummyServer.firefly.approve_payment_hash.assert_not_called()
        # Per-user purchase must be persisted only to the per-user store, never
        # to the public event_store that /events/latest serves unauthenticated.
        server.event_store.write.assert_not_called()
        server.user_event_store.write.assert_called_once()
        saved = server.user_event_store.write.call_args
        self.assertEqual(saved.args[0], "1045618308")
        self.assertEqual(saved.args[1]["telegramUserId"], "1045618308")
        reserve_args = server.user_spend_limit_store.reserve_within_limits.call_args
        self.assertEqual(reserve_args.args[0], "1045618308")
        self.assertEqual(reserve_args.kwargs["amount_atomic"], 1000)
        self.assertEqual(reserve_args.kwargs["network"], "base-mainnet")
        # The hold is settled, never released, once the payment succeeds.
        server.user_spend_limit_store.settle_reservation.assert_called_once()
        settle_args = server.user_spend_limit_store.settle_reservation.call_args
        self.assertEqual(settle_args.args[0], "hold_test")
        self.assertEqual(settle_args.kwargs["tx_id"], "0xTX")
        server.user_spend_limit_store.release_reservation.assert_not_called()

    def test_agent_buy_tool_preflights_shared_user_state_before_external_side_effects(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy_marker = "OTHER-USER-LEGACY-TOKEN"
            path.write_text(
                json.dumps(
                    {
                        "other-user": {
                            "ok": True,
                            "fulfillmentToken": legacy_marker,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = path.read_bytes()
            server = DummyServer()
            server.user_event_store = UserPurchaseStore(
                path,
                cipher=self.state_cipher(),
            )
            server.user_wallet_service = Mock()
            server.user_wallet_service.resolve_telegram_user_id.return_value = (
                "1045618308"
            )
            server.imessage_approval_service = Mock()
            server.user_x402_buyer = Mock()
            server.event_store = Mock()

            with (
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    side_effect=AssertionError("requirement fetch was called"),
                ) as requirement_fetch,
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    side_effect=AssertionError("requirement normalization was called"),
                ) as normalize_requirement,
                patch("sys.stderr", io.StringIO()),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

            response = self.response_text(handler)
            self.assertIn("HTTP/1.0 400 Bad Request", response)
            self.assertIn(
                "legacy plaintext fulfillment tokens must be migrated",
                response,
            )
            self.assertNotIn(legacy_marker, response)
            self.assertEqual(path.read_bytes(), before)
            requirement_fetch.assert_not_called()
            normalize_requirement.assert_not_called()
            server.imessage_approval_service.request_purchase_approval.assert_not_called()
            server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()
            server.user_x402_buyer.assert_not_called()
            server.user_spend_limit_store.record_successful_spend.assert_not_called()
            server.event_store.write.assert_not_called()

    def test_agent_last_purchase_returns_users_own_event(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_event_store = Mock()
        server.user_event_store.read.return_value = {
            "ok": True,
            "telegramUserId": "1045618308",
            "toolId": "otto.crypto_news",
            "toolName": "Crypto News",
            "txId": "0xTX",
            "resourceUrl": "https://x402.ottoai.services/crypto-news",
            "telegramText": "✅ Crypto News unlocked. Paid 0.001 USDC.",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/last-purchase",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["telegramText"], "✅ Crypto News unlocked. Paid 0.001 USDC.")
        self.assertEqual(body["txId"], "0xTX")
        # Ownership is enforced by the per-user key, not a payer heuristic.
        server.user_event_store.read.assert_called_once_with("1045618308")

    def test_agent_last_purchase_preflights_shared_state_before_bitrefill_refresh(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            store = UserPurchaseStore(path, cipher=self.state_cipher())
            store.write(
                "1045618308",
                {
                    "ok": True,
                    "quoteId": "quote_wallet_1",
                    "fulfillmentToken": "reveal_secret_1",
                    "bitrefill": {"orderId": "order_1"},
                },
            )
            legacy_marker = "OTHER-USER-LAST-PURCHASE-LEGACY-TOKEN"
            seeded = json.loads(path.read_text(encoding="utf-8"))
            seeded["other-user"] = {
                "ok": True,
                "fulfillmentToken": legacy_marker,
            }
            path.write_text(json.dumps(seeded) + "\n", encoding="utf-8")
            before = path.read_bytes()
            server = DummyServer()
            server.user_event_store = store
            server.user_wallet_service.resolve_telegram_user_id.return_value = (
                "1045618308"
            )
            server.bitrefill_order_lookup = Mock(
                side_effect=AssertionError("Bitrefill refresh was called")
            )

            with (
                patch.object(store, "read", wraps=store.read) as read,
                patch("sys.stderr", io.StringIO()),
            ):
                handler = self.make_handler(
                    "/agent/last-purchase",
                    {"telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

            response = self.response_text(handler)
            self.assertIn("HTTP/1.0 400 Bad Request", response)
            self.assertIn(
                "legacy plaintext fulfillment tokens must be migrated",
                response,
            )
            self.assertNotIn(legacy_marker, response)
            self.assertEqual(path.read_bytes(), before)
            read.assert_not_called()
            server.bitrefill_order_lookup.assert_not_called()

    def test_agent_last_purchase_reveals_users_bitrefill_code(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_event_store = Mock()
        server.user_event_store.read.return_value = {
            "ok": True,
            "decision": "approved_and_fulfilled",
            "quoteId": "quote_wallet_1",
            "fulfillmentToken": "reveal_secret_1",
            "bitrefill": {"orderId": "order_1"},
            "telegramText": "✅ Bitrefill Gift Card (USD) $0.1 is ready. Use /last_purchase to reveal your code.",
        }
        server.bitrefill_order_lookup = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_wallet_1",
                "state": "DELIVERED",
                "redemption": {"value": {"code": "SECRET-CODE"}},
                "telegramText": "✅ Bitrefill Gift Card (USD) $0.1 is ready.\nCode: SECRET-CODE",
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/last-purchase",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertEqual(
            body["telegramText"],
            "✅ Bitrefill Gift Card (USD) $0.1 is ready.\nCode: SECRET-CODE",
        )
        server.bitrefill_order_lookup.assert_called_once_with(
            "quote_wallet_1",
            include_redemption=True,
            fulfillment_token="reveal_secret_1",
        )
        server.user_event_store.clear_fulfillment_token.assert_called_once_with(
            "1045618308"
        )

    def test_agent_last_purchase_does_not_clear_token_for_empty_redemption(self):
        server = self._bitrefill_last_purchase_server(
            {"redemption": {"value": ""}, "state": "BITREFILL_PURCHASED"}
        )
        body = self._agent_last_purchase_body(server)
        self.assertIn("still processing", body["telegramText"])
        server.user_event_store.clear_fulfillment_token.assert_not_called()

    def test_agent_last_purchase_does_not_clear_token_when_refresh_is_unavailable(self):
        server = self._bitrefill_last_purchase_server(
            {"redemptionUnavailable": True, "state": "BITREFILL_PURCHASED"}
        )
        body = self._agent_last_purchase_body(server)
        self.assertIn("still processing", body["telegramText"])
        server.user_event_store.clear_fulfillment_token.assert_not_called()

    def test_agent_last_purchase_does_not_clear_token_for_ready_text_without_redemption(self):
        server = self._bitrefill_last_purchase_server(
            {
                "state": "DELIVERED",
                "telegramText": "✅ Gift Card is ready.\nCode: READY-LOOKING",
            }
        )
        body = self._agent_last_purchase_body(server)
        self.assertIn("still processing", body["telegramText"])
        server.user_event_store.clear_fulfillment_token.assert_not_called()

    def test_agent_last_purchase_after_reveal_hides_code(self):
        server = self._bitrefill_last_purchase_server({})
        event = server.user_event_store.read.return_value
        event.pop("fulfillmentToken")

        body = self._agent_last_purchase_body(server)

        self.assertIn("already delivered", body["telegramText"])
        self.assertNotIn("SECRET-CODE", body["telegramText"])
        server.bitrefill_order_lookup.assert_not_called()
        server.user_event_store.clear_fulfillment_token.assert_not_called()

    def _bitrefill_last_purchase_server(self, order):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_event_store = Mock()
        server.user_event_store.read.return_value = {
            "ok": True,
            "decision": "approved_and_fulfilled",
            "quoteId": "quote_wallet_1",
            "productName": "Gift Card",
            "fulfillmentToken": "reveal_secret_1",
            "bitrefill": {"orderId": "order_1"},
        }
        server.bitrefill_order_lookup = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_wallet_1",
                "orderId": "order_1",
                **order,
            }
        )
        return server

    def _agent_last_purchase_body(self, server):
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/last-purchase",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )
        response = self.response_text(handler)
        self.assertIn("HTTP/1.0 200 OK", response)
        return json.loads(response.split("\r\n\r\n", 1)[1])

    def test_agent_last_purchase_isolated_per_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_event_store = Mock()
        # No purchase stored for this user id.
        server.user_event_store.read.return_value = None

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/last-purchase",
                {"telegramUserId": "1045618308"},
                server=server,
                headers=self.llm_auth_headers(),
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 404 Not Found", response)
        self.assertFalse(body["ok"])
        self.assertIn("No completed Sign402 purchase found", body["telegramText"])
        server.user_event_store.read.assert_called_once_with("1045618308")

    def test_agent_spending_limits_returns_default_operator_caps(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {"telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["limits"]["maxPerTxAtomic"], 10000)
        self.assertEqual(body["limits"]["dailyCapAtomic"], 100000)
        self.assertIn("0.01 USDC", body["telegramText"])
        self.assertIn("0.1 USDC", body["telegramText"])

    def test_agent_spending_limits_updates_user_limits_below_operator_caps(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {
                        "telegramUserId": "1045618308",
                        "maxPerTxUsdc": "0.005",
                        "dailyCapUsdc": "0.05",
                    },
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["limits"]["maxPerTxAtomic"], 5000)
        self.assertEqual(body["limits"]["dailyCapAtomic"], 50000)
        self.assertTrue(body["limits"]["userConfigured"])
        self.assertIn("updated", body["telegramText"].lower())

    def test_agent_spending_limits_allows_user_limits_above_default_caps(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {
                        "telegramUserId": "1045618308",
                        "maxPerTxUsdc": "100",
                        "dailyCapUsdc": "500",
                    },
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["limits"]["maxPerTxAtomic"], 100000000)
        self.assertEqual(body["limits"]["dailyCapAtomic"], 500000000)
        self.assertTrue(body["limits"]["userConfigured"])

    def test_authenticated_requests_are_rate_limited_per_user(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch.dict(os.environ, {"SIGN402_USER_REQUESTS_PER_MINUTE": "3"}):
                responses = []
                for _ in range(4):
                    with patch("sys.stderr", io.StringIO()):
                        handler = self.make_handler(
                            "/agent/spending-limits",
                            {"telegramUserId": "1045618308"},
                            server=server,
                            headers=self.llm_auth_headers(),
                        )
                    responses.append(self.response_text(handler))

        for ok_response in responses[:3]:
            self.assertIn("HTTP/1.0 200 OK", ok_response)
        self.assertIn("HTTP/1.0 400", responses[3])
        self.assertIn("Too many wallet requests", responses[3])

    def test_purchase_rate_limit_blocks_wallet_bitrefill_buy(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.firefly_busy = False
        server.bitrefill_wallet_purchase_runner = Mock(
            return_value={"ok": True, "quoteId": "quote_1"}
        )

        with patch.dict(
            os.environ,
            {
                "SIGN402_USER_PURCHASES_PER_HOUR": "1",
                "SIGN402_USER_REQUESTS_PER_MINUTE": "100",
            },
        ):
            with patch("sys.stderr", io.StringIO()):
                first = self.make_handler(
                    "/agent/buy-wallet-bitrefill",
                    {"quoteId": "quote_1", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )
                second = self.make_handler(
                    "/agent/buy-wallet-bitrefill",
                    {"quoteId": "quote_2", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(first))
        self.assertIn("Too many purchase attempts", self.response_text(second))
        server.bitrefill_wallet_purchase_runner.assert_called_once()

    def test_agent_spending_limits_rejects_user_limits_above_operator_ceiling(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch.dict(
                os.environ,
                {
                    "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "50000000",
                    "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC": "100000000",
                },
            ):
                with patch("sys.stderr", io.StringIO()):
                    handler = self.make_handler(
                        "/agent/spending-limits",
                        {
                            "telegramUserId": "1045618308",
                            "maxPerTxUsdc": "100",
                            "dailyCapUsdc": "500",
                        },
                        server=server,
                        headers=self.llm_auth_headers(),
                    )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 400", response)
        self.assertFalse(body["ok"])
        self.assertIn("operator ceiling", body["error"])

    def test_agent_spending_limits_clamps_stored_limits_to_lowered_ceiling(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = UserSpendLimitStore(Path(tmpdir) / "limits.json")
            server.user_spend_limit_store = store
            store.set_limit_settings(
                "1045618308",
                max_per_tx_atomic=100_000_000,
                daily_cap_atomic=500_000_000,
                operator_max_per_tx_atomic=10000,
                operator_daily_cap_atomic=100000,
            )

            with patch.dict(
                os.environ,
                {
                    "SIGN402_USER_WALLET_CEILING_ATOMIC_PER_TX": "50000000",
                    "SIGN402_USER_WALLET_CEILING_DAILY_ATOMIC": "100000000",
                },
            ):
                with patch("sys.stderr", io.StringIO()):
                    handler = self.make_handler(
                        "/agent/spending-limits",
                        {"telegramUserId": "1045618308"},
                        server=server,
                        headers=self.llm_auth_headers(),
                    )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertEqual(body["limits"]["maxPerTxAtomic"], 50000000)
        self.assertEqual(body["limits"]["dailyCapAtomic"], 100000000)

    def test_agent_spending_limits_requires_the_callers_per_user_token(self):
        server = DummyServer()
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")

            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/spending-limits",
                    {"telegramUserId": "1045618308"},
                    server=server,
                    headers=self.wallet_auth_headers(),
                )

        self.assertIn("HTTP/1.0 401 Unauthorized", self.response_text(handler))
        self.assertEqual(
            self.response_json(handler)["error"],
            "per-user access token is required",
        )

    def test_agent_buy_tool_for_user_rejects_amount_over_default_tx_cap(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "11000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "extra": {"name": "USD Coin", "version": "2"},
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch.dict(os.environ, {}, clear=False),
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 400", response)
        self.assertIn("per-transaction cap", body["error"])
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()
        server.user_x402_buyer.assert_not_called()

    def test_agent_buy_tool_for_user_rejects_amount_over_user_tx_limit(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")
            server.user_spend_limit_store.set_limit_settings(
                "1045618308",
                max_per_tx_atomic=500,
                daily_cap_atomic=5000,
                operator_max_per_tx_atomic=10000,
                operator_daily_cap_atomic=100000,
            )
            payment_requirements = {
                "scheme": "exact",
                "network": "base-mainnet",
                "x402Network": "eip155:8453",
                "amountAtomic": "1000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
                "paymentIntent": "crypto-news-1",
                "purpose": "x402_api_access",
                "extra": {"name": "USD Coin", "version": "2"},
            }

            with patch("sys.stderr", io.StringIO()):
                with (
                    patch(
                        "sign402_gateway.server.fetch_x402_payment_required",
                        return_value={"x402Version": 2, "accepts": [{}]},
                    ),
                    patch(
                        "sign402_gateway.server.normalize_x402_payment_required",
                        return_value=payment_requirements,
                    ),
                ):
                    handler = self.make_handler(
                        "/agent/buy-tool",
                        {"tool": "news", "telegramUserId": "1045618308"},
                        server=server,
                        headers=self.llm_auth_headers(),
                    )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 400", response)
        self.assertIn("per-transaction cap", body["error"])
        self.assertIn("500", body["error"])
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()

    def test_agent_buy_tool_for_user_allows_amount_over_default_under_user_limit(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        with tempfile.TemporaryDirectory() as tmpdir:
            server.user_spend_limit_store = UserSpendLimitStore(Path(tmpdir) / "limits.json")
            server.user_spend_limit_store.set_limit_settings(
                "1045618308",
                max_per_tx_atomic=100000000,
                daily_cap_atomic=500000000,
                operator_max_per_tx_atomic=10000,
                operator_daily_cap_atomic=100000,
            )
            server.imessage_approval_service.request_purchase_approval.return_value = {
                "ok": True,
                "status": "approved",
                "approvalId": "approval-1",
                "commitmentHash": "c" * 64,
            }
            server.user_wallet_service.decrypt_private_key_for_future_signing.return_value = (
                "0xUSER_PRIVATE_KEY"
            )
            server.user_x402_buyer.return_value = {
                "decision": "approved_and_executed",
                "ok": True,
                "mode": "official_x402_base_user_wallet",
                "resourceUrl": "https://x402.ottoai.services/crypto-news",
                "txId": "0xTX",
                "paymentRequirements": {},
                "telegramText": "paid",
            }
            payment_requirements = {
                "scheme": "exact",
                "network": "base-mainnet",
                "x402Network": "eip155:8453",
                "amountAtomic": "11000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
                "paymentIntent": "crypto-news-1",
                "purpose": "x402_api_access",
                "extra": {"name": "USD Coin", "version": "2"},
            }

            with patch("sys.stderr", io.StringIO()):
                with (
                    patch(
                        "sign402_gateway.server.fetch_x402_payment_required",
                        return_value={"x402Version": 2, "accepts": [{}]},
                    ),
                    patch(
                        "sign402_gateway.server.normalize_x402_payment_required",
                        return_value=payment_requirements,
                    ),
                ):
                    handler = self.make_handler(
                        "/agent/buy-tool",
                        {"tool": "news", "telegramUserId": "1045618308"},
                        server=server,
                        headers=self.llm_auth_headers(),
                    )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        server.imessage_approval_service.request_purchase_approval.assert_called_once()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_called_once()

    def test_agent_buy_tool_for_user_rejects_amount_over_configured_tx_cap(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "60000000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "extra": {"name": "USD Coin", "version": "2"},
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch.dict(
                    os.environ,
                    {"SIGN402_USER_WALLET_MAX_ATOMIC_PER_TX": "50000000"},
                ),
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400", response)
        # No approval prompt or wallet access when the amount is over the cap.
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()

    def test_agent_buy_tool_for_user_rejects_daily_cap_before_approval(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.user_spend_limit_store.spent_today_atomic.return_value = 99500
        server.imessage_approval_service.request_purchase_approval.return_value = {
            "ok": True,
            "status": "approved",
        }
        server.user_x402_buyer.return_value = {"ok": True}
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "paymentIntent": "crypto-news-1",
            "purpose": "x402_api_access",
            "extra": {"name": "USD Coin", "version": "2"},
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 400", response)
        self.assertIn("daily spending cap", body["error"])
        server.user_spend_limit_store.spent_today_atomic.assert_any_call(
            "1045618308",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            network="base-mainnet",
        )
        server.user_spend_limit_store.settle_reservation.assert_not_called()
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()
        server.user_x402_buyer.assert_not_called()

    def test_agent_buy_tool_for_user_rejects_without_imessage_link_before_signing(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.imessage_approval_service.request_purchase_approval.return_value = {
            "ok": False,
            "status": "imessage_not_linked",
            "telegramText": "iMessage is not linked yet. Send /connect_imessage first.",
        }
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "paymentIntent": "crypto-news-1",
            "purpose": "x402_api_access",
            "extra": {"name": "USD Coin", "version": "2"},
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 400", response)
        self.assertEqual(body["decision"], "rejected_by_imessage")
        self.assertIn("iMessage is not linked", body["telegramText"])
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()
        server.user_x402_buyer.assert_not_called()
        # The budget was held before the prompt; a refused prompt must give it
        # back, or one declined purchase would burn the user's daily cap.
        server.user_spend_limit_store.release_reservation.assert_called_once_with(
            "hold_test"
        )
        server.user_spend_limit_store.settle_reservation.assert_not_called()

    def test_agent_buy_tool_for_user_releases_the_hold_when_payment_fails(self):
        server = DummyServer()
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"
        server.imessage_approval_service.request_purchase_approval.return_value = {
            "ok": True,
            "status": "approved",
        }
        server.user_x402_buyer.side_effect = ValueError("x402 resource denied payment")
        payment_requirements = {
            "scheme": "exact",
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            "paymentIntent": "crypto-news-1",
            "purpose": "x402_api_access",
            "extra": {"name": "USD Coin", "version": "2"},
        }

        with patch("sys.stderr", io.StringIO()):
            with (
                patch(
                    "sign402_gateway.server.fetch_x402_payment_required",
                    return_value={"x402Version": 2, "accepts": [{}]},
                ),
                patch(
                    "sign402_gateway.server.normalize_x402_payment_required",
                    return_value=payment_requirements,
                ),
            ):
                handler = self.make_handler(
                    "/agent/buy-tool",
                    {"tool": "news", "telegramUserId": "1045618308"},
                    server=server,
                    headers=self.llm_auth_headers(),
                )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400", response)
        server.user_spend_limit_store.release_reservation.assert_called_once_with(
            "hold_test"
        )
        server.user_spend_limit_store.settle_reservation.assert_not_called()
        server.user_spend_limit_store.record_successful_spend.assert_not_called()

    def test_agent_buy_tool_rejects_per_user_token_acting_as_another_user(self):
        server = DummyServer()
        # Per-user token belongs to user A ...
        server.user_wallet_service.resolve_telegram_user_id.return_value = "1045618308"

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-tool",
                # ... but the request body claims to act as user B.
                {"tool": "news", "telegramUserId": "999"},
                server=server,
                headers={
                    "Authorization": "Bearer test-wallet-token",
                    "X-Sign402-User-Token": "user-a-token",
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401", response)
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()

    def test_agent_buy_tool_with_user_identity_requires_wallet_auth(self):
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-tool",
                {"tool": "news", "telegramUserId": "1045618308"},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401 Unauthorized", response)
        server.imessage_approval_service.request_purchase_approval.assert_not_called()
        server.user_x402_buyer.assert_not_called()

    def test_user_wallet_base_x402_payment_client_passes_private_key_in_env_only(self):
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps({"ok": True, "status": 200}),
            stderr="",
        )
        runner = Mock(return_value=completed)
        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletBaseX402PaymentClient(service_dir, runner=runner)

            result = client("https://x402.example/paid", private_key="0xSECRET")

        self.assertTrue(result["ok"])
        args = runner.call_args.args[0]
        kwargs = runner.call_args.kwargs
        self.assertEqual(args[-3:], ["buy-user", "--url", "https://x402.example/paid"])
        self.assertNotIn("0xSECRET", args)
        self.assertEqual(kwargs["env"]["SIGN402_EVM_PRIVATE_KEY"], "0xSECRET")

    def test_user_wallet_base_x402_payment_client_forwards_approved_caps(self):
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps({"ok": True, "status": 200}),
            stderr="",
        )
        runner = Mock(return_value=completed)
        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletBaseX402PaymentClient(service_dir, runner=runner)

            client(
                "https://x402.example/paid",
                private_key="0xSECRET",
                max_atomic="1000",
                expected_receiver="0xRECV",
                expected_asset="0xASSET",
            )

        args = runner.call_args.args[0]
        self.assertIn("--max-atomic", args)
        self.assertIn("1000", args)
        self.assertIn("--expected-receiver", args)
        self.assertIn("0xRECV", args)
        self.assertIn("--expected-asset", args)
        self.assertIn("0xASSET", args)

    def test_user_wallet_token_transfer_client_passes_private_key_in_env_only(self):
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "transactionHash": "0xUSERTRANSFER",
                    "from": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
                }
            ),
            stderr="",
        )
        runner = Mock(return_value=completed)
        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletTokenTransferClient(service_dir, runner=runner)

            result = client.transfer_token(
                private_key="0xSECRET",
                to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
                amount="130000",
                chain="base",
            )

        self.assertTrue(result["ok"])
        args = runner.call_args.args[0]
        kwargs = runner.call_args.kwargs
        self.assertEqual(args[2], "transfer-token-user")
        self.assertIn("--to", args)
        self.assertIn("0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd", args)
        self.assertIn("--token", args)
        self.assertIn(DEFAULT_SINGIT_TOKEN_ADDRESS, args)
        self.assertIn("--amount", args)
        self.assertIn("130000", args)
        self.assertNotIn("0xSECRET", args)
        self.assertEqual(kwargs["env"]["SIGN402_EVM_PRIVATE_KEY"], "0xSECRET")

    def test_user_wallet_token_transfer_client_passes_decimals(self):
        recorded = {}

        def runner(command, **kwargs):
            recorded["command"] = command
            return subprocess.CompletedProcess(
                command, 0, stdout='{"ok": true, "transactionHash": "0xabc"}', stderr=""
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletTokenTransferClient(service_dir, runner=runner)
            client.transfer_token(
                private_key="0xkey",
                to_address="0x" + "1" * 40,
                token_address="0x" + "2" * 40,
                amount="1.5",
                decimals=6,
            )
        self.assertIn("--decimals", recorded["command"])
        self.assertEqual(
            recorded["command"][recorded["command"].index("--decimals") + 1], "6"
        )

    def test_user_wallet_native_transfer_client_passes_private_key_in_env_only(self):
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "transactionHash": "0xNATIVETRANSFER",
                    "from": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
                }
            ),
            stderr="",
        )
        runner = Mock(return_value=completed)
        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletTokenTransferClient(service_dir, runner=runner)

            result = client.transfer_native(
                private_key="0xSECRET",
                to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                amount="0.005",
                chain="base",
            )

        self.assertTrue(result["ok"])
        args = runner.call_args.args[0]
        kwargs = runner.call_args.kwargs
        self.assertEqual(args[2], "transfer-native-user")
        self.assertIn("--to", args)
        self.assertIn("--amount", args)
        self.assertIn("0.005", args)
        self.assertNotIn("0xSECRET", args)
        self.assertEqual(kwargs["env"]["SIGN402_EVM_PRIVATE_KEY"], "0xSECRET")

    def test_user_wallet_token_transfer_client_reads_token_info_and_balance(self):
        recorded = []

        def runner(command, **kwargs):
            recorded.append(command)
            if "token-info" in command:
                body = '{"ok": true, "symbol": "USDC", "decimals": 6}'
            else:
                body = '{"ok": true, "balanceAtomic": "123"}'
            return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            service_dir = Path(tmpdir)
            script = service_dir / "src" / "index.mjs"
            script.parent.mkdir(parents=True)
            script.write_text("// test", encoding="utf-8")
            client = UserWalletTokenTransferClient(service_dir, runner=runner)
            info = client.token_info("0x" + "2" * 40)
            balance = client.token_balance("0x" + "2" * 40, "0x" + "3" * 40)
        self.assertEqual(info["symbol"], "USDC")
        self.assertEqual(info["decimals"], 6)
        self.assertEqual(balance, "123")
        self.assertIn("token-info", recorded[0])
        self.assertIn("token-balance", recorded[1])

    def test_user_wallet_transfer_to_cdp_funding_runner_debits_user_wallet(self):
        wallet_service = Mock(
            **{
                "decrypt_private_key_for_future_signing.return_value": "0xSECRET",
                "wallet_status.return_value": {
                    "wallet": {
                        "address": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
                    }
                },
            }
        )
        transfer_client = Mock(
            **{
                "transfer_token.return_value": {
                    "ok": True,
                    "txId": "0xUSERTRANSFER",
                    "from": "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C",
                }
            }
        )
        runner = UserWalletTransferToCdpFundingRunner(
            wallet_service=wallet_service,
            transfer_client=transfer_client,
            cdp_wallet_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
            chain="base",
        )

        result = runner(
            telegram_user_id="1045618308",
            quote={
                "pricingMode": "bankr_real_rate",
                "singitAmount": "130000",
            },
            recipient={},
        )

        self.assertEqual(result["mode"], "user_wallet_transfer_to_cdp_swap")
        self.assertEqual(result["fromWallet"], "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C")
        self.assertEqual(result["toWallet"], "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd")
        self.assertEqual(result["transfer"]["txId"], "0xUSERTRANSFER")
        wallet_service.decrypt_private_key_for_future_signing.assert_called_once_with(
            "1045618308"
        )
        transfer_client.transfer_token.assert_called_once_with(
            private_key="0xSECRET",
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
            amount="130000",
            chain="base",
        )

    def test_user_wallet_transfer_to_cdp_uses_selected_wallet_token(self):
        wallet_service = Mock(
            **{
                "withdrawable_tokens.return_value": {
                    "ok": True,
                    "wallet": {"address": "0xUser"},
                    "tokens": [
                        {
                            "symbol": "USDC",
                            "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            "balance": "4.82",
                            "decimals": 6,
                            "verified": True,
                            "native": False,
                        }
                    ],
                },
                "decrypt_private_key_for_future_signing.return_value": "0xSECRET",
            }
        )
        transfer_client = Mock(
            **{"transfer_token.return_value": {"ok": True, "txId": "0xTRANSFER"}}
        )
        runner = UserWalletTransferToCdpFundingRunner(
            wallet_service=wallet_service,
            transfer_client=transfer_client,
            cdp_wallet_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        )

        result = runner(
            telegram_user_id="1045618308",
            quote={
                "pricingMode": "bankr_real_rate",
                "paymentTokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "paymentTokenSymbol": "USDC",
                "paymentTokenDecimals": 6,
                "paymentTokenNative": False,
                "paymentTokenAmount": "1.10",
                "maxPaymentTokenAtomic": "1100000",
            },
            recipient={},
        )

        self.assertEqual(result["fromToken"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        wallet_service.withdrawable_tokens.assert_called_once_with("1045618308")
        transfer_client.transfer_token.assert_called_once_with(
            private_key="0xSECRET",
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            amount="1.10",
            chain="base",
            decimals=6,
        )

    def test_user_wallet_transfer_to_cdp_uses_native_eth_transfer(self):
        wallet_service = Mock(
            **{
                "withdrawable_tokens.return_value": {
                    "ok": True,
                    "wallet": {"address": "0xUser"},
                    "tokens": [
                        {
                            "symbol": "ETH",
                            "contractAddress": BASE_NATIVE_ETH_ASSET_ID,
                            "balance": "0.01",
                            "decimals": 18,
                            "verified": True,
                            "native": True,
                        }
                    ],
                },
                "decrypt_private_key_for_future_signing.return_value": "0xSECRET",
            }
        )
        transfer_client = Mock(
            **{"transfer_native.return_value": {"ok": True, "txId": "0xNATIVE"}}
        )
        runner = UserWalletTransferToCdpFundingRunner(
            wallet_service=wallet_service,
            transfer_client=transfer_client,
            cdp_wallet_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        )

        runner(
            telegram_user_id="1045618308",
            quote={
                "pricingMode": "bankr_real_rate",
                "paymentTokenAddress": BASE_NATIVE_ETH_ASSET_ID,
                "paymentTokenSymbol": "ETH",
                "paymentTokenDecimals": 18,
                "paymentTokenNative": True,
                "paymentTokenAmount": "0.001",
                "maxPaymentTokenAtomic": "1000000000000000",
            },
            recipient={},
        )

        transfer_client.transfer_native.assert_called_once_with(
            private_key="0xSECRET",
            to_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            amount="0.001",
            chain="base",
        )
        transfer_client.transfer_token.assert_not_called()

    def test_user_wallet_transfer_to_cdp_rechecks_selected_token_balance(self):
        wallet_service = Mock(
            **{
                "withdrawable_tokens.return_value": {
                    "ok": True,
                    "wallet": {"address": "0xUser"},
                    "tokens": [
                        {
                            "symbol": "USDC",
                            "contractAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            "balance": "0.50",
                            "decimals": 6,
                            "verified": True,
                            "native": False,
                        }
                    ],
                }
            }
        )
        transfer_client = Mock()
        runner = UserWalletTransferToCdpFundingRunner(
            wallet_service=wallet_service,
            transfer_client=transfer_client,
            cdp_wallet_address="0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
            from_token=DEFAULT_SINGIT_TOKEN_ADDRESS,
        )

        with self.assertRaisesRegex(ValueError, "balance"):
            runner(
                telegram_user_id="1045618308",
                quote={
                    "pricingMode": "bankr_real_rate",
                    "paymentTokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "paymentTokenSymbol": "USDC",
                    "paymentTokenDecimals": 6,
                    "paymentTokenNative": False,
                    "paymentTokenAmount": "1.10",
                    "maxPaymentTokenAtomic": "1100000",
                },
                recipient={},
            )

        transfer_client.transfer_token.assert_not_called()

    def test_user_wallet_bitrefill_funding_env_builder_uses_user_transfer_to_cdp(self):
        wallet_service = Mock()

        runner = build_bitrefill_user_funding_runner_from_env(
            user_wallet_service=wallet_service,
            env={
                "SIGN402_BITREFILL_FUNDING_MODE": "cdp_wallet_swap",
                "SIGN402_CDP_WALLET_ADDRESS": "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
                "SIGN402_CDP_X402_SERVICE_DIR": "/tmp/cdp",
            },
        )

        self.assertIsInstance(runner, UserWalletTransferToCdpFundingRunner)
        self.assertIs(runner.wallet_service, wallet_service)

    def test_user_wallet_x402_buyer_enforces_approved_caps_on_payment(self):
        user_payment_client = Mock(
            return_value={
                "ok": True,
                "status": 200,
                "paymentResponse": {"transaction": "0xTX"},
            }
        )
        buyer = UserWalletX402Buyer(
            base_payment_client=user_payment_client,
        )

        buyer(
            "https://x402.example/paid",
            private_key="0xSECRET",
            approval={"ok": True, "status": "approved", "commitmentHash": "h"},
            payment_requirements={
                "network": "base-mainnet",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amountAtomic": "1000",
                "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            },
        )

        kwargs = user_payment_client.call_args.kwargs
        self.assertEqual(kwargs["max_atomic"], "1000")
        self.assertEqual(kwargs["expected_receiver"], "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808")
        self.assertEqual(kwargs["expected_asset"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

    def test_user_wallet_x402_buyer_rejects_without_imessage_approval(self):
        user_payment_client = Mock()
        buyer = UserWalletX402Buyer(
            base_payment_client=user_payment_client,
        )

        result = buyer(
            "https://x402.ottoai.services/crypto-news",
            private_key="0xSECRET",
            approval={"ok": False, "status": "denied"},
            payment_requirements={
                "network": "base-mainnet",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amountAtomic": "1000",
                "receiver": "0x0E84dDEdAaE6A779c462C22a59F301EC31B6b808",
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "rejected_by_imessage")
        user_payment_client.assert_not_called()

    def test_external_x402_buyer_uses_bankr_cli_for_singit_bankr_endpoint(self):
        policy_hash = "a" * 64
        payment_hash = "b" * 64
        token = DEFAULT_SINGIT_TOKEN_ADDRESS
        policy = {
            "asset": token,
            "allowedPurpose": "x402_api_access",
            "maxPerPaymentAtomic": "10000000000000000000",
            "maxBudgetAtomic": "100000000000000000000",
        }
        policy_state = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        raw_payment_required = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "upto",
                    "network": "eip155:8453",
                    "amount": "10000000000000000000",
                    "asset": token,
                    "payTo": "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0",
                    "resource": "https://x402.bankr.bot/0xabc/paid-risk-check",
                    "extra": {
                        "name": "SINGIT",
                        "version": "1",
                        "paymentIntent": "singit-risk-check-1",
                    },
                }
            ],
        }

        firefly = Mock()
        firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": payment_hash,
            "deviceModel": 262,
            "deviceSerial": 1056,
        }
        agent_state_store = Mock()
        agent_state_store.read_policy_for_requirement.return_value = policy_state
        agent_state_store.remaining_budget.return_value = 90000000000000000000
        event_store = Mock()
        bankr_buyer = Mock(
            return_value={
                "status": 200,
                "body": {"ok": True, "riskLevel": "low"},
                "transactionHash": "0xSINGITTX",
                "paymentResponse": {"transaction": "0xSINGITTX"},
            }
        )
        cdp_buyer = Mock()
        algorand_builder = Mock()

        buyer = ExternalX402Buyer(
            firefly=firefly,
            payment_signature_builder=algorand_builder,
            base_payment_client=cdp_buyer,
            bankr_x402_payment_client=bankr_buyer,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with (
            patch(
                "sign402_gateway.server.fetch_x402_payment_required",
                return_value=raw_payment_required,
            ) as fetch_required,
            patch(
                "sign402_gateway.server.build_payment_commitment",
                return_value={"paymentHash": payment_hash, "commitment": {"type": "sign402-payment"}},
            ),
        ):
            result = buyer(
                "https://x402.bankr.bot/0xabc/paid-risk-check",
                payment_context={"title": "SINGIT RISK", "subject": "Risk Check"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "official_x402_base_bankr_cli")
        self.assertEqual(result["txId"], "0xSINGITTX")
        self.assertEqual(result["asset"], token)
        bankr_buyer.assert_called_once_with(
            "https://x402.bankr.bot/0xabc/paid-risk-check",
            request_body=None,
        )
        fetch_required.assert_called_once_with(
            "https://x402.bankr.bot/0xabc/paid-risk-check",
            request_body=None,
        )
        cdp_buyer.assert_not_called()
        algorand_builder.assert_not_called()
        self.assertEqual(
            firefly.approve_payment_hash.call_args.kwargs["context_lines"],
            ["SINGIT RISK", "Risk Check", "10 SINGIT"],
        )
        agent_state_store.validate_policy_allows.assert_called_once()
        validated_requirement = agent_state_store.validate_policy_allows.call_args.args[2]
        self.assertEqual(validated_requirement["network"], "base-mainnet")
        self.assertEqual(validated_requirement["x402Network"], "eip155:8453")
        self.assertEqual(validated_requirement["asset"], token)
        self.assertEqual(validated_requirement["amountAtomic"], "10000000000000000000")
        self.assertEqual(validated_requirement["paymentIntent"], "singit-risk-check-1")
        self.assertEqual(validated_requirement["purpose"], "x402_api_access")
        agent_state_store.record_payment.assert_called_once_with(
            policy_hash,
            "singit-risk-check-1",
            10000000000000000000,
        )

    def test_bankr_cli_x402_payment_client_parses_response_and_tx(self):
        completed = subprocess_completed(
            stdout="""
{
  "success": true,
  "status": 200,
  "paymentMade": {
    "network": "eip155:8453",
    "transactionHash": "0xABC123"
  },
  "response": {
    "ok": true,
    "riskLevel": "low"
  }
}
""",
        )
        with patch("subprocess.run", return_value=completed) as run:
            client = BankrCliX402PaymentClient(
                bankr_cli="/tmp/bankr",
                block_number_fetcher=Mock(return_value=47_751_000),
            )
            result = client("https://x402.bankr.bot/0xabc/paid-risk-check")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["transactionHash"], "0xABC123")
        self.assertEqual(result["body"], {"ok": True, "riskLevel": "low"})
        self.assertEqual(run.call_args.args[0][:3], ["/tmp/bankr", "x402", "call"])
        self.assertIn("--raw", run.call_args.args[0])

    def test_bankr_transaction_hash_parser_accepts_tx_hash_line(self):
        self.assertEqual(
            _bankr_cli_transaction_hash(
                "Tx Hash:  "
                "0x453cff05c73f8fc70a9418520bec12ec538cb2cee7a7fbcac8751d177f94483d"
            ),
            "0x453cff05c73f8fc70a9418520bec12ec538cb2cee7a7fbcac8751d177f94483d",
        )

    def test_bankr_x402_client_preserves_payment_made_and_start_block(self):
        completed = subprocess_completed(
            stdout="""
{
  "success": true,
  "status": 200,
  "paymentMade": {
    "amountUsd": 0.0057,
    "network": "eip155:8453",
    "payTo": "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0"
  },
  "response": {"ok": true}
}
""",
        )
        with patch("subprocess.run", return_value=completed):
            client = BankrCliX402PaymentClient(
                bankr_cli="/tmp/bankr",
                block_number_fetcher=Mock(return_value=47_751_000),
            )
            result = client("https://x402.bankr.bot/wallet/buy-bitrefill")

        self.assertEqual(result["startBlock"], 47_751_000)
        self.assertEqual(
            result["paymentMade"]["payTo"],
            "0x8AEE621035D93Deb3C0C1177fac252dC2dd501a0",
        )

    def test_singit_verifier_discovers_exact_transfer_when_hash_is_missing(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        tx_hash = "0x" + "ab" * 32
        resolver = Mock(return_value=tx_hash)
        receipt = erc20_receipt(
            sender=payer,
            recipient=pay_to,
            amount=11_000_000_000_000_000_000,
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            transaction_resolver=resolver,
            payer_address=payer,
        )

        result = verifier(
            bankr_result={
                "transactionHash": None,
                "startBlock": 47_751_000,
                "paymentMade": {"payTo": pay_to},
            },
            quote={"maxSingitAtomic": "11000000000000000000"},
        )

        self.assertEqual(result["transactionHash"], tx_hash)
        self.assertEqual(result["from"], payer)
        self.assertEqual(result["payTo"], pay_to)
        self.assertEqual(result["amountAtomic"], "11000000000000000000")
        self.assertTrue(result["discovered"])
        resolver.assert_called_once_with(
            token_address=DEFAULT_SINGIT_TOKEN_ADDRESS,
            sender=payer,
            recipient=pay_to,
            amount_atomic="11000000000000000000",
            from_block=47_751_000,
        )

    def test_singit_verifier_rejects_wrong_sender(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        receipt = erc20_receipt(
            sender="0x1111111111111111111111111111111111111111",
            recipient=pay_to,
            amount=11_000_000_000_000_000_000,
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            payer_address=payer,
        )

        with self.assertRaisesRegex(ValueError, "sender"):
            verifier(
                bankr_result={"transactionHash": "0x" + "ab" * 32, "paymentMade": {"payTo": pay_to}},
                quote={"maxSingitAtomic": "11000000000000000000"},
            )

    def test_singit_verifier_rejects_wrong_recipient(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        receipt = erc20_receipt(
            sender=payer,
            recipient="0x1111111111111111111111111111111111111111",
            amount=11_000_000_000_000_000_000,
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            payer_address=payer,
        )

        with self.assertRaisesRegex(ValueError, "recipient"):
            verifier(
                bankr_result={"transactionHash": "0x" + "ab" * 32, "paymentMade": {"payTo": pay_to}},
                quote={"maxSingitAtomic": "11000000000000000000"},
            )

    def test_singit_verifier_rejects_wrong_amount(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        receipt = erc20_receipt(
            sender=payer,
            recipient=pay_to,
            amount=10_999_999_999_999_999_999,
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            payer_address=payer,
        )

        with self.assertRaisesRegex(ValueError, "amount"):
            verifier(
                bankr_result={"transactionHash": "0x" + "ab" * 32, "paymentMade": {"payTo": pay_to}},
                quote={"maxSingitAtomic": "11000000000000000000"},
            )

    def test_singit_verifier_rejects_wrong_token(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        receipt = erc20_receipt(
            token="0x1111111111111111111111111111111111111111",
            sender=payer,
            recipient=pay_to,
            amount=11_000_000_000_000_000_000,
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            payer_address=payer,
        )

        with self.assertRaisesRegex(ValueError, "token"):
            verifier(
                bankr_result={"transactionHash": "0x" + "ab" * 32, "paymentMade": {"payTo": pay_to}},
                quote={"maxSingitAtomic": "11000000000000000000"},
            )

    def test_singit_verifier_rejects_failed_receipt(self):
        payer = "0x3b3e349e6cfee692b69d2c63ce86f7d444667d98"
        pay_to = "0x8aee621035d93deb3c0c1177fac252dc2dd501a0"
        receipt = erc20_receipt(
            sender=payer,
            recipient=pay_to,
            amount=11_000_000_000_000_000_000,
            status="0x0",
        )
        verifier = SingitSettlementVerifier(
            receipt_fetcher=Mock(return_value=receipt),
            payer_address=payer,
        )

        with self.assertRaisesRegex(ValueError, "failed"):
            verifier(
                bankr_result={"transactionHash": "0x" + "ab" * 32, "paymentMade": {"payTo": pay_to}},
                quote={"maxSingitAtomic": "11000000000000000000"},
            )

    def test_bankr_cli_x402_payment_client_redacts_request_body_from_result(self):
        completed = subprocess_completed(
            stdout='{"success":true,"status":200,"response":{"ok":true}}',
        )
        with patch("subprocess.run", return_value=completed) as run:
            client = BankrCliX402PaymentClient(
                bankr_cli="/tmp/bankr",
                block_number_fetcher=Mock(return_value=47_751_000),
            )
            result = client(
                "https://x402.bankr.bot/0xabc/buy-bitrefill",
                request_body={"quoteId": "quote_1", "fulfillmentToken": "secret_token"},
            )

        self.assertIn("secret_token", str(run.call_args.args[0]))
        self.assertNotIn("secret_token", str(result))
        self.assertIn("<redacted>", str(result["command"]))

    def test_bankr_treasury_client_transfers_usdc_with_bankr_wallet(self):
        completed = subprocess_completed(
            stdout=(
                "Transfer submitted\n"
                "Transaction: https://basescan.org/tx/"
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
        )
        with patch("subprocess.run", return_value=completed) as run:
            client = BankrTreasuryClient(bankr_cli="/tmp/bankr")
            result = client.transfer_usdc(
                to_address="0xBitrefillInvoice",
                amount="5.01",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["txId"],
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(
            run.call_args.args[0],
            [
                "/tmp/bankr",
                "--ni",
                "wallet",
                "transfer",
                "--to",
                "0xBitrefillInvoice",
                "--amount",
                "5.01",
                "--token",
                "USDC",
                "--chain",
                "base",
            ],
        )

    def test_bankr_treasury_client_reads_usdc_balance_from_portfolio_json(self):
        stdout = """
- Fetching portfolio...
✔ Portfolio loaded
{
  "success": true,
  "balances": {
    "base": {
      "tokenBalances": [
        {
          "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
          "token": {
            "balance": "15.868754",
            "baseToken": {"symbol": "USDC"}
          }
        }
      ]
    }
  }
}
"""
        completed = subprocess_completed(stdout=stdout)
        with patch("subprocess.run", return_value=completed) as run:
            client = BankrTreasuryClient(bankr_cli="/tmp/bankr")
            balance = client.usdc_balance(chain="base")

        self.assertEqual(balance, Decimal("15.868754"))
        self.assertEqual(
            run.call_args.args[0],
            [
                "/tmp/bankr",
                "wallet",
                "portfolio",
                "--chain",
                "base",
                "--json",
                "--low-value",
            ],
        )

    def test_usdc_reserve_guard_rejects_quote_before_singit_payment_when_reserve_is_low(self):
        treasury = Mock(**{"usdc_balance.return_value": Decimal("0.10")})
        guard = BankrUsdcReserveGuard(treasury_client=treasury, buffer_bps=1000)

        with self.assertRaisesRegex(ValueError, "insufficient USDC reserve"):
            guard({"quoteId": "quote_1", "priceUsd": "0.10"})

        treasury.usdc_balance.assert_called_once_with(chain="base")

    def test_usdc_reserve_guard_accepts_quote_when_reserve_covers_buffer(self):
        treasury = Mock(**{"usdc_balance.return_value": Decimal("0.12")})
        guard = BankrUsdcReserveGuard(treasury_client=treasury, buffer_bps=1000)

        guard({"quoteId": "quote_1", "priceUsd": "0.10"})

    def test_usdc_reserve_guard_env_builder_only_enables_live_usdc_base(self):
        self.assertIsNone(build_usdc_reserve_guard_from_env({}))
        self.assertIsNone(
            build_usdc_reserve_guard_from_env(
                {
                    "SIGN402_BITREFILL_MODE": "live",
                    "SIGN402_BITREFILL_PAYMENT_METHOD": "balance",
                }
            )
        )

        guard = build_usdc_reserve_guard_from_env(
            {
                "SIGN402_BITREFILL_MODE": "live",
                "SIGN402_BITREFILL_PAYMENT_METHOD": "usdc_base",
                "SIGN402_TREASURY_USDC_BUFFER_BPS": "500",
                "SIGN402_BANKR_CLI": "/tmp/bankr",
            }
        )

        self.assertIsInstance(guard, BankrUsdcReserveGuard)
        self.assertEqual(guard.buffer_bps, 500)
        self.assertEqual(guard.treasury_client.bankr_cli, "/tmp/bankr")

    def test_bankr_treasury_client_raises_on_failed_transfer(self):
        completed = subprocess_completed(
            stdout="",
            stderr="insufficient USDC",
            returncode=1,
        )
        with patch("subprocess.run", return_value=completed):
            client = BankrTreasuryClient(bankr_cli="/tmp/bankr")
            with self.assertRaisesRegex(ValueError, "insufficient USDC"):
                client.transfer_usdc(
                    to_address="0xBitrefillInvoice",
                    amount="5.01",
                )

    def test_singit_risk_check_tool_passes_request_body_for_402_probe_and_payment(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "ok": True,
            "mode": "official_x402_base_bankr_cli",
            "txId": "0xTX",
            "amountAtomic": "10000000000000000000",
            "asset": DEFAULT_SINGIT_TOKEN_ADDRESS,
            "network": "base-mainnet",
            "remainingBudgetAtomic": "90000000000000000000",
            "paymentRequirements": {
                "asset": DEFAULT_SINGIT_TOKEN_ADDRESS,
                "extra": {"name": "SINGIT"},
            },
            "resourceResult": {"body": {"ok": True, "riskLevel": "low"}},
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/buy-tool", {"tool": "singit-risk-check"})

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"toolId": "bankr.singit.risk_check"', response)
        DummyServer.x402_buyer.assert_called_once()
        self.assertEqual(DummyServer.x402_buyer.call_args.args[0], DEFAULT_SINGIT_RISK_CHECK_URL)
        self.assertEqual(
            DummyServer.x402_buyer.call_args.kwargs["payment_context"],
            {"title": "SINGIT RISK", "subject": "Risk Check"},
        )
        self.assertIn("request_body", DummyServer.x402_buyer.call_args.kwargs)

    def test_singit_risk_check_tool_passes_request_body_for_inspection(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "mode": "inspect_only",
            "resourceUrl": DEFAULT_SINGIT_RISK_CHECK_URL,
            "paymentRequirements": {
                "network": "eip155:8453",
                "amountAtomic": "10000000000000000000",
                "asset": DEFAULT_SINGIT_TOKEN_ADDRESS,
                "extra": {"name": "SINGIT"},
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-tool",
                {"tool": "singit-risk-check", "policyHash": policy_hash},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"toolId": "bankr.singit.risk_check"', response)
        DummyServer.x402_inspector.assert_called_once()
        self.assertEqual(DummyServer.x402_inspector.call_args.args, (DEFAULT_SINGIT_RISK_CHECK_URL, policy_hash))
        self.assertIn("request_body", DummyServer.x402_inspector.call_args.kwargs)

    def test_bankr_llm_credits_topup_runner_approves_and_executes_singit_topup(self):
        policy_hash = "a" * 64
        payment_hash = "b" * 64
        token = "0x2222222222222222222222222222222222222222"
        policy = {
            "asset": token,
            "allowedPurpose": "bankr_llm_credits_topup",
            "maxPerPaymentAtomic": "5000000000000000000000",
            "maxBudgetAtomic": "10000000000000000000000",
        }
        requirement = {
            "network": "base-mainnet",
            "asset": token,
            "amountAtomic": "5000000000000000000000",
            "receiver": "bankr.llm",
            "resource": "bankr://llm-credits/top-up",
            "paymentIntent": "llm-topup-1",
            "purpose": "bankr_llm_credits_topup",
            "extra": {"name": "SINGIT", "creditAmountUsd": "5"},
        }

        firefly = Mock()
        firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": payment_hash,
            "deviceModel": 262,
            "deviceSerial": 1056,
        }
        agent_state_store = Mock()
        agent_state_store.read_policy_for_requirement.return_value = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        agent_state_store.remaining_budget.return_value = 5000000000000000000000
        event_store = Mock()
        bankr_topup_executor = Mock(
            return_value={
                "ok": True,
                "command": ["bankr", "llm", "credits", "add", "5", "--token", token, "--yes"],
                "stdout": "Added $5 LLM credits",
            }
        )

        runner = BankrLlmCreditsTopUpRunner(
            firefly=firefly,
            bankr_topup_executor=bankr_topup_executor,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with patch(
            "sign402_gateway.server.build_payment_commitment",
            return_value={"paymentHash": payment_hash, "commitment": {"type": "sign402-payment"}},
        ):
            result = runner(
                {
                    "creditAmountUsd": "5",
                    "fundingTokenAddress": token,
                    "fundingTokenSymbol": "SINGIT",
                    "maxFundingTokenAmountAtomic": "5000000000000000000000",
                    "topUpIntent": "llm-topup-1",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "approved_and_executed")
        self.assertEqual(result["mode"], "bankr_llm_credits_topup")
        bankr_topup_executor.assert_called_once_with(
            credit_amount_usd="5",
            funding_token_address=token,
        )
        firefly.approve_payment_hash.assert_called_once()
        self.assertEqual(
            firefly.approve_payment_hash.call_args.kwargs["context_lines"],
            ["LLM CREDITS", "$5", "SINGIT"],
        )
        agent_state_store.validate_policy_allows.assert_called_once_with(
            policy,
            policy_hash,
            requirement,
        )
        agent_state_store.record_payment.assert_called_once_with(
            policy_hash,
            "llm-topup-1",
            5000000000000000000000,
        )
        event_store.write.assert_called_once()

    def test_bankr_llm_topup_client_accepts_stablecoin_settlement_when_singit_was_requested(self):
        singit = DEFAULT_SINGIT_TOKEN_ADDRESS
        usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        receipt_fetcher = Mock(
            return_value={
                "logs": [
                    {
                        "address": usdc,
                        "topics": [transfer_topic],
                    }
                ]
            }
        )
        client = BankrLlmCreditsTopUpClient(
            bankr_cli="bankr",
            receipt_fetcher=receipt_fetcher,
        )

        stdout = (
            "Add Credits:  $1.00\n"
            "  Pay with:           SINGIT on Base\n"
            "\n"
            "✓ Added $1.00 credits\n"
            "New Balance:  $1.00\n"
            "  Transaction:        https://basescan.org/tx/0x251c9fbdd6b1ca6affa2e353442bf4e74687a952a43f0c67dfd93b8ee0ab683f"
        )
        with patch(
            "sign402_gateway.server.subprocess.run",
            return_value=subprocess_completed(stdout=stdout),
        ):
            result = client(credit_amount_usd="1", funding_token_address=singit)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["transactionHash"],
            "0x251c9fbdd6b1ca6affa2e353442bf4e74687a952a43f0c67dfd93b8ee0ab683f",
        )
        self.assertNotIn("receiptVerified", result)
        receipt_fetcher.assert_not_called()

    def test_bankr_llm_topup_client_preserves_transaction_hash_without_receipt_verification(self):
        singit = DEFAULT_SINGIT_TOKEN_ADDRESS
        receipt_fetcher = Mock(return_value={"logs": []})
        client = BankrLlmCreditsTopUpClient(
            bankr_cli="bankr",
            receipt_fetcher=receipt_fetcher,
        )

        stdout = (
            "✓ Added $1.00 credits\n"
            "New Balance:  $1.00\n"
            "  Transaction:        https://basescan.org/tx/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        with patch(
            "sign402_gateway.server.subprocess.run",
            return_value=subprocess_completed(stdout=stdout),
        ):
            result = client(credit_amount_usd="1", funding_token_address=singit)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["transactionHash"],
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertNotIn("receiptVerified", result)
        receipt_fetcher.assert_not_called()

    def test_agent_tools_lists_paid_tool_catalog(self):
        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/tools", method="GET")

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"mode": "paid_tool_catalog"', response)
        self.assertIn('"id": "goplausible.weather"', response)
        self.assertIn('"mcpStyleName": "get_weather"', response)
        self.assertIn('"id": "base.sign402.report"', response)
        self.assertIn('"mcpStyleName": "get_sign402_report"', response)
        self.assertIn('"id": "x402.twitter.profile"', response)
        self.assertIn('"mcpStyleName": "get_x_profile"', response)
        self.assertIn('"id": "otto.crypto_news"', response)
        self.assertIn('"id": "otto.hyperliquid_market"', response)
        self.assertIn('"id": "otto.funding_rates"', response)
        self.assertIn('"id": "onesource.ens"', response)
        self.assertIn('"id": "anchor.token_price"', response)

    def test_agent_quote_bitrefill_uses_quote_service(self):
        server = DummyServer()
        server.bitrefill_quote_service = Mock(return_value={"ok": True, "quoteId": "quote_1"})

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/quote-bitrefill",
                {"productId": "test-gift-card-code", "packageId": "1", "country": "US"},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"quoteId": "quote_1"', response)
        server.bitrefill_quote_service.assert_called_once_with(
            {"productId": "test-gift-card-code", "packageId": "1", "country": "US"}
        )

    def test_agent_search_bitrefill_uses_catalog_service(self):
        server = DummyServer()
        server.bitrefill_search_service = Mock(
            return_value={"ok": True, "products": [{"productId": "test-phone-refill"}]}
        )
        payload = {"query": "phone", "country": "US", "includeTestProducts": True}

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/search-bitrefill", payload, server=server)

        response = self.response_text(handler)
        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"productId": "test-phone-refill"', response)
        server.bitrefill_search_service.assert_called_once_with(payload)

    def test_agent_list_bitrefill_products_uses_catalog_service(self):
        server = DummyServer()
        server.bitrefill_catalog_service.return_value = {
            "ok": True,
            "products": [{"productId": "product-1"}],
            "start": 8,
            "limit": 8,
            "hasPrevious": True,
            "hasNext": False,
        }
        payload = {
            "country": "CZ",
            "category": "Food",
            "start": 8,
            "limit": 8,
            "includeInternational": True,
            "includeTestProducts": False,
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/list-bitrefill-products",
                payload,
                server=server,
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        self.assertEqual(
            self.response_json(handler),
            server.bitrefill_catalog_service.return_value,
        )
        server.bitrefill_catalog_service.assert_called_once_with(payload)

    def test_agent_list_bitrefill_products_returns_400_for_service_error(self):
        server = DummyServer()
        server.bitrefill_catalog_service.side_effect = ValueError("country is invalid")

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/list-bitrefill-products",
                {"country": "C"},
                server=server,
            )

        self.assertIn("HTTP/1.0 400 Bad Request", self.response_text(handler))
        self.assertEqual(
            self.response_json(handler),
            {"ok": False, "error": "country is invalid"},
        )

    def test_agent_get_bitrefill_product_uses_details_service(self):
        server = DummyServer()
        server.bitrefill_product_details_service = Mock(
            return_value={"ok": True, "productId": "test-phone-refill"}
        )
        payload = {"productId": "test-phone-refill", "country": "US"}

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/get-bitrefill-product", payload, server=server)

        response = self.response_text(handler)
        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"productId": "test-phone-refill"', response)
        server.bitrefill_product_details_service.assert_called_once_with(payload)

    def test_agent_search_bitrefill_accepts_wallet_api_token(self):
        server = DummyServer()
        server.bitrefill_search_service = Mock(return_value={"ok": True, "products": []})

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/search-bitrefill",
                {"query": "phone", "country": "US"},
                server=server,
                headers={"Authorization": "Bearer test-wallet-token"},
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        server.bitrefill_search_service.assert_called_once()

    def test_agent_catalog_endpoints_reject_unauthenticated_requests(self):
        for path in (
            "/agent/search-bitrefill",
            "/agent/list-bitrefill-products",
            "/agent/get-bitrefill-product",
        ):
            with self.subTest(path=path):
                server = DummyServer()
                server.bitrefill_search_service = Mock()
                server.bitrefill_product_details_service = Mock()

                with patch("sys.stderr", io.StringIO()):
                    handler = self.make_handler(
                        path,
                        {"query": "phone", "country": "US"},
                        server=server,
                        headers={},
                    )

                self.assertIn("HTTP/1.0 401", self.response_text(handler))
                server.bitrefill_search_service.assert_not_called()
                server.bitrefill_catalog_service.assert_not_called()
                server.bitrefill_product_details_service.assert_not_called()

    def test_agent_catalog_endpoints_hidden_without_legacy_mode_or_token(self):
        server = DummyServer()
        server.bitrefill_search_service = Mock()

        with patch.dict(
            os.environ,
            {
                "SIGN402_ENABLE_LEGACY_PAYMENT_EXECUTOR": "",
                "SIGN402_LEGACY_OPERATOR_API_TOKEN": "",
            },
        ):
            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/agent/search-bitrefill",
                    {"query": "phone", "country": "US"},
                    server=server,
                    headers={},
                )

        self.assertIn("HTTP/1.0 404", self.response_text(handler))
        server.bitrefill_search_service.assert_not_called()

    def test_agent_buy_bitrefill_acquires_firefly_and_uses_runner(self):
        server = DummyServer()
        server.firefly_busy = False
        server.bitrefill_purchase_runner = Mock(return_value={"ok": True, "quoteId": "quote_1"})

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-bitrefill",
                {"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        server.bitrefill_purchase_runner.assert_called_once_with(
            {"quoteId": "quote_1", "recipient": {"email": "buyer@example.com"}}
        )
        self.assertFalse(server.firefly_busy)

    def test_agent_buy_bitrefill_redacts_fulfillment_token_from_event_store(self):
        server = DummyServer()
        server.firefly_busy = False
        DummyServer.event_store.reset_mock()
        server.bitrefill_purchase_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_1",
                "fulfillmentToken": "reveal_secret_1",
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-bitrefill",
                {"quoteId": "quote_1"},
                server=server,
            )

        response = self.response_text(handler)

        # Caller still receives the token (needed to reveal the redemption code).
        self.assertIn("reveal_secret_1", response)
        # But the dashboard event store must not persist the plaintext token.
        DummyServer.event_store.write.assert_called_once()
        saved_event = DummyServer.event_store.write.call_args.args[0]
        self.assertNotIn("fulfillmentToken", saved_event)
        self.assertNotIn("reveal_secret_1", json.dumps(saved_event))

    def test_agent_buy_bitrefill_never_returns_or_persists_raw_bankr_diagnostics(
        self,
    ):
        server = DummyServer()
        server.firefly_busy = False
        server.event_store = Mock()
        server.bitrefill_purchase_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_1",
                "fulfillmentToken": "reveal_secret_1",
                "bankr": {
                    "ok": True,
                    "status": 200,
                    "transactionHash": "0xSINGITTX",
                    "startBlock": 47_751_000,
                    "paymentMade": {
                        "network": "eip155:8453",
                        "payTo": "0x1111111111111111111111111111111111111111",
                        "amountUsd": "0.0057",
                        "credential": "GATEWAY-BANKR-CREDENTIAL-MARKER",
                    },
                    "command": ["bankr", "GATEWAY-BANKR-COMMAND-MARKER"],
                    "stdout": "GATEWAY-BANKR-STDOUT-TOKEN-MARKER",
                    "stderr": "GATEWAY-BANKR-STDERR-PAYMENT-LINK-MARKER",
                    "body": {
                        "redemption": "GATEWAY-BANKR-REDEMPTION-MARKER",
                    },
                },
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-bitrefill",
                {"quoteId": "quote_1"},
                server=server,
            )

        expected_bankr = {
            "ok": True,
            "status": "200",
            "transactionHash": "0xSINGITTX",
            "startBlock": "47751000",
            "paymentMade": {
                "network": "eip155:8453",
                "payTo": "0x1111111111111111111111111111111111111111",
                "amountUsd": "0.0057",
            },
        }
        response_body = self.response_json(handler)
        saved_event = server.event_store.write.call_args.args[0]
        self.assertEqual(response_body["bankr"], expected_bankr)
        self.assertEqual(saved_event["bankr"], expected_bankr)
        self.assertNotIn("fulfillmentToken", saved_event)
        for marker in (
            "GATEWAY-BANKR-CREDENTIAL-MARKER",
            "GATEWAY-BANKR-COMMAND-MARKER",
            "GATEWAY-BANKR-STDOUT-TOKEN-MARKER",
            "GATEWAY-BANKR-STDERR-PAYMENT-LINK-MARKER",
            "GATEWAY-BANKR-REDEMPTION-MARKER",
        ):
            self.assertNotIn(marker, self.response_text(handler))
            self.assertNotIn(marker, json.dumps(saved_event))

    def test_agent_buy_wallet_bitrefill_uses_wallet_runner(self):
        server = DummyServer()
        server.firefly_busy = False
        server.bitrefill_wallet_purchase_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_wallet_1",
                "fulfillmentToken": "reveal_secret_1",
            }
        )
        server.bitrefill_purchase_runner = Mock()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-wallet-bitrefill",
                {"quoteId": "quote_wallet_1", "recipient": {"email": "buyer@example.com"}},
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        server.bitrefill_wallet_purchase_runner.assert_called_once_with(
            {"quoteId": "quote_wallet_1", "recipient": {"email": "buyer@example.com"}}
        )
        server.bitrefill_purchase_runner.assert_not_called()
        self.assertFalse(server.firefly_busy)

    def test_agent_buy_wallet_bitrefill_authenticates_telegram_user_token(self):
        server = DummyServer()
        server.firefly_busy = False
        server.user_wallet_api_token = "wallet-token-secret-value"
        server.event_store = Mock()
        server.user_event_store = Mock()
        server.user_wallet_service.resolve_telegram_user_id = Mock(
            return_value="1045618308"
        )
        server.bitrefill_wallet_purchase_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_wallet_1",
                "fulfillmentToken": "reveal_secret_1",
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-wallet-bitrefill",
                {"quoteId": "quote_wallet_1", "telegramUserId": "1045618308"},
                headers={
                    "Authorization": "Bearer wallet-token-secret-value",
                    "X-Sign402-User-Token": "user-token-1",
                },
                server=server,
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        server.user_wallet_service.resolve_telegram_user_id.assert_called_once_with(
            "user-token-1"
        )
        server.bitrefill_wallet_purchase_runner.assert_called_once_with(
            {"quoteId": "quote_wallet_1", "telegramUserId": "1045618308"}
        )
        server.user_event_store.write.assert_called_once()
        saved_user_event = server.user_event_store.write.call_args.args[1]
        self.assertEqual(saved_user_event.get("fulfillmentToken"), "reveal_secret_1")
        # A per-user purchase must not leak into the public /events/latest store.
        server.event_store.write.assert_not_called()

    def test_agent_buy_wallet_bitrefill_preflights_shared_user_state_before_purchase(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user-purchases.json"
            legacy_marker = "OTHER-USER-BITREFILL-LEGACY-TOKEN"
            path.write_text(
                json.dumps(
                    {
                        "other-user": {
                            "ok": True,
                            "fulfillmentToken": legacy_marker,
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = path.read_bytes()
            server = DummyServer()
            server.firefly_busy = False
            server.user_event_store = UserPurchaseStore(
                path,
                cipher=self.state_cipher(),
            )
            server.user_wallet_service = Mock()
            server.user_wallet_service.resolve_telegram_user_id.return_value = (
                "1045618308"
            )
            server.imessage_approval_service = Mock()
            server.user_token_transfer_client = Mock()
            server.bitrefill_wallet_purchase_runner = Mock(
                return_value={
                    "ok": True,
                    "quoteId": "quote-wallet-1",
                    "fulfillmentToken": "new-token",
                }
            )
            server.bitrefill_fulfillment_runner = Mock()
            server.event_store = Mock()

            with (
                patch.object(
                    Sign402GatewayHandler,
                    "_acquire_firefly",
                    return_value=True,
                ) as acquire_firefly,
                patch("sys.stderr", io.StringIO()),
            ):
                handler = self.make_handler(
                    "/agent/buy-wallet-bitrefill",
                    {
                        "quoteId": "quote-wallet-1",
                        "telegramUserId": "1045618308",
                    },
                    server=server,
                    headers=self.llm_auth_headers(),
                )

            response = self.response_text(handler)
            self.assertIn("HTTP/1.0 400 Bad Request", response)
            self.assertIn(
                "legacy plaintext fulfillment tokens must be migrated",
                response,
            )
            self.assertNotIn(legacy_marker, response)
            self.assertEqual(path.read_bytes(), before)
            acquire_firefly.assert_not_called()
            server.bitrefill_wallet_purchase_runner.assert_not_called()
            server.bitrefill_fulfillment_runner.assert_not_called()
            server.imessage_approval_service.request_hash_approval.assert_not_called()
            server.user_wallet_service.decrypt_private_key_for_future_signing.assert_not_called()
            server.user_token_transfer_client.transfer_token.assert_not_called()
            server.user_token_transfer_client.transfer_native.assert_not_called()
            server.event_store.write.assert_not_called()

    def test_quote_bitrefill_rejects_amount_over_spend_cap(self):
        server = DummyServer()
        server.user_wallet_api_token = "wallet-token-secret-value"
        server.user_wallet_service.resolve_telegram_user_id = Mock(return_value="1045618308")
        # Product cost is below the 0.01 USDC cap, but the complete charge is above it.
        server.bitrefill_quote_service = Mock(
            return_value={
                "ok": True,
                "priceUsd": "0.009",
                "totalUsd": "0.011",
                "quoteId": "q1",
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/quote-bitrefill",
                {"productId": "p", "packageId": "1", "telegramUserId": "1045618308"},
                headers={
                    "Authorization": "Bearer wallet-token-secret-value",
                    "X-Sign402-User-Token": "user-token-1",
                },
                server=server,
            )

        response = self.response_text(handler)
        self.assertIn("HTTP/1.0 400", response)
        self.assertIn("cap", response.lower())

    def test_buy_wallet_bitrefill_records_user_spend(self):
        server = DummyServer()
        server.firefly_busy = False
        server.user_wallet_api_token = "wallet-token-secret-value"
        server.user_event_store = Mock()
        server.user_wallet_service.resolve_telegram_user_id = Mock(return_value="1045618308")
        server.bitrefill_wallet_purchase_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "q1",
                "priceUsd": "0.005",
                "totalUsd": "0.0051",
                "txId": "0xTX",
                "spendReservationId": "hold_bitrefill",
            }
        )

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-wallet-bitrefill",
                {"quoteId": "q1", "telegramUserId": "1045618308"},
                headers={
                    "Authorization": "Bearer wallet-token-secret-value",
                    "X-Sign402-User-Token": "user-token-1",
                },
                server=server,
            )

        self.assertIn("HTTP/1.0 200 OK", self.response_text(handler))
        # The runner already holds the budget; the handler settles that hold
        # rather than recording a second, unreserved spend.
        server.user_spend_limit_store.settle_reservation.assert_called_once()
        settle_args = server.user_spend_limit_store.settle_reservation.call_args
        self.assertEqual(settle_args.args[0], "hold_bitrefill")
        self.assertEqual(settle_args.kwargs["tx_id"], "0xTX")
        server.user_spend_limit_store.record_successful_spend.assert_not_called()

    def test_internal_fulfill_bitrefill_requires_service_secret(self):
        server = DummyServer()
        server.bitrefill_fulfillment_runner = Mock()

        with patch.dict(os.environ, {"SIGN402_BANKR_FULFILLMENT_SECRET": "secret_123"}):
            with patch("sys.stderr", io.StringIO()):
                handler = self.make_handler(
                    "/internal/fulfill-bitrefill",
                    {"quoteId": "quote_1", "fulfillmentToken": "fulfill_1"},
                    server=server,
                )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 401 Unauthorized", response)
        server.bitrefill_fulfillment_runner.assert_not_called()

    def test_internal_fulfill_bitrefill_disables_legacy_bankr_flow_by_default(self):
        server = DummyServer()
        server.bitrefill_fulfillment_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_1",
                "orderId": "order_1",
                "status": "delivered",
                "settleAmountAtomic": "2625000000000000000000",
            }
        )

        encoded = json.dumps({"quoteId": "quote_1", "fulfillmentToken": "fulfill_1"}).encode(
            "utf-8"
        )
        request = (
            b"POST /internal/fulfill-bitrefill HTTP/1.1\r\n"
            + f"Content-Length: {len(encoded)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"Authorization: Bearer secret_123\r\n"
            + b"\r\n"
            + encoded
        )
        socket = FakeSocket(request)

        with patch.dict(os.environ, {"SIGN402_BANKR_FULFILLMENT_SECRET": "secret_123"}):
            with patch("sys.stderr", io.StringIO()):
                handler = Sign402GatewayHandler(socket, ("127.0.0.1", 12345), server)
                handler.response = socket.wfile

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 410 Gone", response)
        self.assertIn("legacy fulfillment disabled", response)
        server.bitrefill_fulfillment_runner.assert_not_called()

    def test_internal_prepare_bitrefill_settlement_calls_runner_with_service_secret(self):
        server = DummyServer()
        server.bitrefill_settlement_preparation_runner = Mock(
            return_value={
                "ok": True,
                "quoteId": "quote_1",
                "status": "ready_for_singit_settlement",
                "pricingMode": "bankr_real_rate",
                "settleAmountAtomic": "2625000000000000000000",
                "maxSingitAtomic": "2625000000000000000000",
            }
        )

        encoded = json.dumps({"quoteId": "quote_1", "fulfillmentToken": "fulfill_1"}).encode(
            "utf-8"
        )
        request = (
            b"POST /internal/prepare-bitrefill-settlement HTTP/1.1\r\n"
            + f"Content-Length: {len(encoded)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"Authorization: Bearer secret_123\r\n"
            + b"\r\n"
            + encoded
        )
        socket = FakeSocket(request)

        with patch.dict(os.environ, {"SIGN402_BANKR_FULFILLMENT_SECRET": "secret_123"}):
            with patch("sys.stderr", io.StringIO()):
                handler = Sign402GatewayHandler(socket, ("127.0.0.1", 12345), server)
                handler.response = socket.wfile

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"status": "ready_for_singit_settlement"', response)
        self.assertIn('"pricingMode": "bankr_real_rate"', response)
        self.assertIn('"settleAmountAtomic": "2625000000000000000000"', response)
        self.assertNotIn("redemption", response)
        server.bitrefill_settlement_preparation_runner.assert_called_once_with(
            {"quoteId": "quote_1", "fulfillmentToken": "fulfill_1"}
        )

    def test_agent_inspect_base_tool_resolves_alias_and_returns_offer(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "mode": "inspect_only",
            "resourceUrl": "http://127.0.0.1:4021/paid/sign402-report",
            "paymentRequirements": {
                "network": "base-mainnet",
                "amountAtomic": "10000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-tool",
                {"tool": "sign402-report", "policyHash": policy_hash},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"toolId": "base.sign402.report"', response)
        self.assertIn('"command": "buy base sign402 report"', response)
        DummyServer.x402_inspector.assert_called_once_with(
            "http://127.0.0.1:4021/paid/sign402-report",
            policy_hash,
        )

    def test_agent_buy_base_tool_uses_x402_buyer_and_writes_tool_event(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.event_store.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_cdp",
            "resourceUrl": "http://127.0.0.1:4021/paid/sign402-report",
            "txId": "0xTX",
            "amountAtomic": "10000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "80000",
            "paymentRequirements": {
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amountAtomic": "10000",
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                },
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/buy-tool", {"tool": "base-report"})

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"decision": "approved_and_executed"', response)
        self.assertIn('"toolName": "Base Sign402 Report"', response)
        self.assertEqual(
            body["telegramText"],
            "✅ Base Sign402 Report unlocked. Paid 0.01 USDC. Tx https://basescan.org/tx/0xTX. Budget left 0.08 USDC.",
        )
        DummyServer.x402_buyer.assert_called_once_with(
            "http://127.0.0.1:4021/paid/sign402-report"
        )
        DummyServer.event_store.write.assert_called_once()
        saved_event = DummyServer.event_store.write.call_args.args[0]
        self.assertEqual(saved_event["toolId"], "base.sign402.report")
        self.assertEqual(saved_event["command"], "buy base sign402 report")
        self.assertEqual(saved_event["telegramText"], body["telegramText"])

    def test_agent_inspect_twitter_profile_tool_builds_username_url(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "mode": "inspect_only",
            "resourceUrl": "https://x402.twit.sh/users/by/username?username=elonmusk",
            "paymentRequirements": {
                "network": "base-mainnet",
                "amountAtomic": "5000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-tool",
                {
                    "tool": "x-profile",
                    "username": "elonmusk",
                    "policyHash": policy_hash,
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"toolId": "x402.twitter.profile"', response)
        self.assertIn('"command": "buy x profile <username>"', response)
        DummyServer.x402_inspector.assert_called_once_with(
            "https://x402.twit.sh/users/by/username?username=elonmusk",
            policy_hash,
        )

    def test_agent_buy_twitter_profile_tool_requires_username(self):
        DummyServer.x402_buyer.reset_mock()

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/buy-tool", {"tool": "x-profile"})

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 400 Bad Request", response)
        self.assertIn("username is required", response)
        DummyServer.x402_buyer.assert_not_called()

    def test_agent_buy_twitter_profile_tool_uses_base_x402_url(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.event_store.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_cdp",
            "resourceUrl": "https://x402.twit.sh/users/by/username?username=elonmusk",
            "txId": "0xTX",
            "amountAtomic": "5000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "25000",
            "paymentRequirements": {
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                },
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-tool",
                {"tool": "x-profile", "username": "elonmusk"},
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertEqual(
            body["telegramText"],
            "✅ X/Twitter Profile unlocked. Paid 0.005 USDC. Tx https://basescan.org/tx/0xTX. Budget left 0.025 USDC.",
        )
        DummyServer.x402_buyer.assert_called_once_with(
            "https://x402.twit.sh/users/by/username?username=elonmusk",
            payment_context={"title": "X PROFILE", "subject": "@elonmusk"},
        )
        saved_event = DummyServer.event_store.write.call_args.args[0]
        self.assertEqual(saved_event["toolId"], "x402.twitter.profile")
        self.assertEqual(saved_event["resourceUrl"], "https://x402.twit.sh/users/by/username?username=elonmusk")

    def test_agent_buy_tool_suppresses_duplicate_purchase_retry(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.event_store.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_cdp",
            "resourceUrl": "https://x402.twit.sh/users/by/username?username=elonmusk",
            "txId": "0xTX",
            "amountAtomic": "5000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "25000",
            "paymentRequirements": {
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                },
            },
        }
        server = DummyServer()

        with patch("sys.stderr", io.StringIO()):
            first = self.make_handler(
                "/agent/buy-tool",
                {"tool": "x-profile", "username": "elonmusk"},
                server=server,
            )
            second = self.make_handler(
                "/agent/buy-tool",
                {"tool": "x-profile", "username": "elonmusk"},
                server=server,
            )

        first_body = json.loads(self.response_text(first).split("\r\n\r\n", 1)[1])
        second_body = json.loads(self.response_text(second).split("\r\n\r\n", 1)[1])

        self.assertTrue(first_body["ok"])
        self.assertTrue(second_body["ok"])
        self.assertTrue(second_body["duplicateSuppressed"])
        self.assertEqual(second_body["txId"], "0xTX")
        DummyServer.x402_buyer.assert_called_once_with(
            "https://x402.twit.sh/users/by/username?username=elonmusk",
            payment_context={"title": "X PROFILE", "subject": "@elonmusk"},
        )
        DummyServer.event_store.write.assert_called_once()

    def test_agent_inspect_recommended_x402_tools_builds_parameterized_urls(self):
        policy_hash = "c" * 64
        cases = [
            (
                {"tool": "crypto-news", "policyHash": policy_hash},
                "otto.crypto_news",
                "https://x402.ottoai.services/crypto-news",
            ),
            (
                {"tool": "hyperliquid", "asset": "BTC", "policyHash": policy_hash},
                "otto.hyperliquid_market",
                "https://x402.ottoai.services/hyperliquid-market?asset=BTC",
            ),
            (
                {"tool": "funding", "symbol": "BTC", "policyHash": policy_hash},
                "otto.funding_rates",
                "https://x402.ottoai.services/funding-rates?symbol=BTC",
            ),
            (
                {"tool": "ens", "input": "vitalik.eth", "policyHash": policy_hash},
                "onesource.ens",
                "https://skills.onesource.io/api/chain/ens/vitalik.eth?network=ethereum",
            ),
            (
                {"tool": "token-price", "symbol": "ETH", "policyHash": policy_hash},
                "anchor.token_price",
                "https://api.anchor-x402.com/v1/price/token?symbol=ETH",
            ),
        ]
        for payload, tool_id, expected_url in cases:
            with self.subTest(tool=tool_id):
                DummyServer.x402_inspector.reset_mock()
                DummyServer.x402_inspector.return_value = {
                    "ok": True,
                    "mode": "inspect_only",
                    "resourceUrl": expected_url,
                    "paymentRequirements": {
                        "network": "base-mainnet",
                        "amountAtomic": "1000",
                        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    },
                }

                with patch("sys.stderr", io.StringIO()):
                    handler = self.make_handler("/agent/inspect-tool", payload)

                response = self.response_text(handler)

                self.assertIn("HTTP/1.0 200 OK", response)
                self.assertIn(f'"toolId": "{tool_id}"', response)
                DummyServer.x402_inspector.assert_called_once_with(expected_url, policy_hash)

    def test_agent_buy_hyperliquid_tool_passes_firefly_context(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.event_store.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "mode": "official_x402_base_cdp",
            "resourceUrl": "https://x402.ottoai.services/hyperliquid-market?asset=BTC",
            "txId": "0xTX",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "99000",
            "resourceResult": {
                "body": {
                    "status": "success",
                    "market": {
                        "symbol": "BTC",
                        "currentPrice": "64138.50",
                        "markPrice": "64138.50",
                        "maxLeverage": 40,
                        "tradingUrl": "https://app.hyperliquid.xyz/trade/BTC",
                    },
                },
            },
            "paymentRequirements": {
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                },
            },
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/buy-tool",
                {"tool": "hyperliquid", "asset": "BTC"},
            )

        response = self.response_text(handler)
        body = json.loads(response.split("\r\n\r\n", 1)[1])

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertEqual(
            body["telegramText"],
            "✅ Hyperliquid BTC unlocked. Price $64138.50. Max leverage 40x. Paid 0.001 USDC. Tx https://basescan.org/tx/0xTX. Budget left 0.099 USDC.\nhttps://app.hyperliquid.xyz/trade/BTC",
        )
        DummyServer.x402_buyer.assert_called_once_with(
            "https://x402.ottoai.services/hyperliquid-market?asset=BTC",
            payment_context={"title": "HYPERLIQUID", "subject": "BTC"},
        )

    def test_paid_tool_telegram_text_includes_resource_value_for_recommended_tools(self):
        cases = [
            (
                "otto.crypto_news",
                {"resourceResult": {"body": {"data": {"report": "Latest crypto news..."}}}},
                "Latest crypto news...",
            ),
            (
                "otto.funding_rates",
                {"resourceResult": {"body": {"report": "Symbol: BTC\nFunding is positive"}}},
                "Symbol: BTC",
            ),
            (
                "onesource.ens",
                {"resourceResult": {"body": {"input": "vitalik.eth", "address": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"}}},
                "vitalik.eth → 0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
            ),
            (
                "anchor.token_price",
                {"resourceResult": {"body": {"symbol": "ETH", "usd": 3120.55, "usd_24h_change_pct": 1.23}}},
                "ETH price: $3120.55",
            ),
        ]
        base_payload = {
            "decision": "approved_and_executed",
            "ok": True,
            "txId": "0xTX",
            "amountAtomic": "1000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "network": "base-mainnet",
            "remainingBudgetAtomic": "99000",
            "paymentRequirements": {
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                },
            },
        }

        for tool_id, extra_payload, expected_text in cases:
            with self.subTest(tool=tool_id):
                result = _tool_result(
                    _resolve_paid_tool({"tool": tool_id}),
                    {**base_payload, **extra_payload},
                )

                self.assertIn(expected_text, result["telegramText"])
                self.assertIn("Paid 0.001 USDC", result["telegramText"])
                self.assertIn("https://basescan.org/tx/0xTX", result["telegramText"])

    def test_singit_risk_check_telegram_text_does_not_require_tx_hash(self):
        result = _tool_result(
            _resolve_paid_tool({"tool": "singit-risk-check"}),
            {
                "decision": "approved_and_executed",
                "ok": True,
                "txId": None,
                "amountAtomic": "10000000000000000000",
                "asset": DEFAULT_SINGIT_TOKEN_ADDRESS,
                "network": "base-mainnet",
                "remainingBudgetAtomic": "90000000000000000000",
                "resourceResult": {
                    "body": {
                        "ok": True,
                        "riskLevel": "low",
                        "recommendation": "Payment looks acceptable for a bounded Sign402 policy.",
                    }
                },
                "paymentRequirements": {
                    "extra": {
                        "name": "SINGIT",
                    },
                },
            },
        )

        self.assertEqual(
            result["telegramText"],
            "✅ SINGIT Risk Check unlocked. Risk: low. Paid 10 SINGIT. Budget left 90 SINGIT. Payment looks acceptable for a bounded Sign402 policy.",
        )

    def test_agent_inspect_tool_resolves_alias_and_returns_offer(self):
        policy_hash = "c" * 64
        DummyServer.x402_inspector.reset_mock()
        DummyServer.x402_inspector.return_value = {
            "ok": True,
            "mode": "inspect_only",
            "resourceUrl": "https://x402.goplausible.xyz/examples/weather",
            "paymentRequirements": {"amountAtomic": "10000", "asset": "10458941"},
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/agent/inspect-tool",
                {"tool": "weather", "policyHash": policy_hash},
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"toolId": "goplausible.weather"', response)
        self.assertIn('"command": "buy goplausible weather"', response)
        DummyServer.x402_inspector.assert_called_once_with(
            "https://x402.goplausible.xyz/examples/weather",
            policy_hash,
        )

    def test_agent_buy_tool_uses_x402_buyer_and_writes_tool_event(self):
        DummyServer.x402_buyer.reset_mock()
        DummyServer.event_store.reset_mock()
        DummyServer.x402_buyer.return_value = {
            "decision": "approved_and_executed",
            "ok": True,
            "resourceUrl": "https://x402.goplausible.xyz/examples/weather",
            "txId": "TXID",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler("/agent/buy-tool", {"tool": "get_weather"})

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"decision": "approved_and_executed"', response)
        self.assertIn('"toolName": "GoPlausible Weather"', response)
        DummyServer.x402_buyer.assert_called_once_with(
            "https://x402.goplausible.xyz/examples/weather"
        )
        DummyServer.event_store.write.assert_called_once()
        saved_event = DummyServer.event_store.write.call_args.args[0]
        self.assertEqual(saved_event["toolId"], "goplausible.weather")
        self.assertEqual(saved_event["command"], "buy goplausible weather")

    def test_external_x402_buyer_sends_human_payment_context_to_firefly(self):
        from sign402_gateway.server import ExternalX402Buyer

        policy_hash = "a" * 64
        policy = {
            "asset": "10458941",
            "allowedPurpose": "x402_api_access",
            "maxBudgetAtomic": "100000",
            "maxPerPaymentAtomic": "10000",
        }
        requirement = {
            "network": "algorand-testnet",
            "x402Network": "algorand:testnet",
            "asset": "10458941",
            "amountAtomic": "10000",
            "receiver": "PAYEE",
            "resource": "https://x402.goplausible.xyz/examples/weather",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
            "extra": {"name": "USDC", "decimals": 6},
        }

        firefly = Mock()
        event_store = Mock()
        agent_state_store = Mock()
        agent_state_store.read_policy.return_value = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        agent_state_store.read_policy_for_requirement.return_value = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        agent_state_store.remaining_budget.return_value = 90000
        payment_signature_builder = Mock(return_value={"headerValue": "PAYMENT-SIGNATURE token"})

        def approve_payment_hash(payment_hash, context_lines=None):
            return {
                "approved": True,
                "approvedHash": payment_hash,
                "deviceModel": 262,
                "deviceSerial": 1056,
            }

        firefly.approve_payment_hash.side_effect = approve_payment_hash

        buyer = ExternalX402Buyer(
            firefly=firefly,
            payment_signature_builder=payment_signature_builder,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with (
            patch("sign402_gateway.server.fetch_x402_payment_required", return_value={"accepts": []}),
            patch("sign402_gateway.server.normalize_x402_payment_required", return_value=requirement),
            patch(
                "sign402_gateway.server.fetch_x402_paid_resource",
                return_value={
                    "status": 200,
                    "paymentResponse": {"transaction": "TXID"},
                },
            ),
        ):
            result = buyer("https://x402.goplausible.xyz/examples/weather")

        self.assertEqual(result["decision"], "approved_and_executed")
        firefly.approve_payment_hash.assert_called_once()
        self.assertEqual(
            firefly.approve_payment_hash.call_args.kwargs["context_lines"],
            ["x402 WEATHER", "0.01 USDC", "GoPlausible API"],
        )

    def test_external_x402_buyer_can_override_firefly_context_for_named_tool(self):
        policy_hash = "a" * 64
        payment_hash = "b" * 64
        policy = {
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
            "allowedPurpose": "x402_api_access",
            "maxPerPaymentAtomic": "10000",
            "maxBudgetAtomic": "30000",
        }
        requirement = {
            "network": "base-mainnet",
            "x402Network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amountAtomic": "5000",
            "receiver": "0x1111111111111111111111111111111111111111",
            "resource": "https://x402.twit.sh/users/by/username?username=jessepollak",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
            "extra": {"name": "USD Coin", "version": "2"},
        }
        firefly = Mock()
        firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": payment_hash,
        }
        event_store = Mock()
        agent_state_store = Mock()
        agent_state_store.read_policy.return_value = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        agent_state_store.read_policy_for_requirement.return_value = {
            "policy": policy,
            "policyHash": policy_hash,
            "firefly": {"approvedHash": policy_hash},
        }
        agent_state_store.remaining_budget.return_value = 25000
        cdp_buyer = Mock(
            return_value={
                "status": 200,
                "paymentResponse": {"transaction": "0xTX"},
            }
        )

        buyer = ExternalX402Buyer(
            firefly=firefly,
            payment_signature_builder=Mock(),
            base_payment_client=cdp_buyer,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with (
            patch("sign402_gateway.server.fetch_x402_payment_required", return_value={"accepts": []}),
            patch("sign402_gateway.server.normalize_x402_payment_required", return_value=requirement),
            patch(
                "sign402_gateway.server.build_payment_commitment",
                return_value={"paymentHash": payment_hash, "commitment": {"type": "sign402-payment"}},
            ),
        ):
            buyer(
                "https://x402.twit.sh/users/by/username?username=jessepollak",
                payment_context={"title": "X PROFILE", "subject": "@jessepollak"},
            )

        self.assertEqual(
            firefly.approve_payment_hash.call_args.kwargs["context_lines"],
            ["X PROFILE", "@jessepollak", "0.005 USDC"],
        )

    def test_external_x402_buyer_selects_policy_matching_requirement_asset(self):
        algo_hash = "a" * 64
        base_hash = "b" * 64
        requirement = {
            "network": "algorand-testnet",
            "x402Network": "algorand:testnet",
            "asset": "10458941",
            "amountAtomic": "10000",
            "receiver": "PAYEE",
            "resource": "https://x402.goplausible.xyz/examples/weather",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
            "extra": {"name": "USDC", "decimals": 6},
        }
        algo_policy_state = {
            "policy": {
                "asset": "10458941",
                "allowedPurpose": "x402_api_access",
                "maxBudgetAtomic": "100000",
                "maxPerPaymentAtomic": "10000",
            },
            "policyHash": algo_hash,
            "firefly": {"approvedHash": algo_hash},
        }
        base_policy_state = {
            "policy": {
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
                "allowedPurpose": "x402_api_access",
                "maxBudgetAtomic": "1000000",
                "maxPerPaymentAtomic": "10000",
            },
            "policyHash": base_hash,
            "firefly": {"approvedHash": base_hash},
        }
        firefly = Mock()
        firefly.approve_payment_hash.return_value = {
            "approved": True,
            "approvedHash": "c" * 64,
        }
        event_store = Mock()
        agent_state_store = Mock()
        agent_state_store.read_policy.return_value = base_policy_state
        agent_state_store.read_policy_for_requirement.return_value = algo_policy_state
        agent_state_store.remaining_budget.return_value = 90000
        payment_signature_builder = Mock(return_value={"headerValue": "PAYMENT-SIGNATURE token"})

        buyer = ExternalX402Buyer(
            firefly=firefly,
            payment_signature_builder=payment_signature_builder,
            event_store=event_store,
            agent_state_store=agent_state_store,
        )

        with (
            patch("sign402_gateway.server.fetch_x402_payment_required", return_value={"accepts": []}),
            patch("sign402_gateway.server.normalize_x402_payment_required", return_value=requirement),
            patch(
                "sign402_gateway.server.build_payment_commitment",
                return_value={"paymentHash": "c" * 64, "commitment": {"type": "sign402-payment"}},
            ),
            patch(
                "sign402_gateway.server.fetch_x402_paid_resource",
                return_value={
                    "status": 200,
                    "paymentResponse": {"transaction": "TXID"},
                },
            ),
        ):
            result = buyer("https://x402.goplausible.xyz/examples/weather")

        self.assertTrue(result["ok"])
        self.assertEqual(result["policyHash"], algo_hash)
        agent_state_store.read_policy_for_requirement.assert_called_once_with(requirement)
        agent_state_store.validate_policy_allows.assert_called_once_with(
            algo_policy_state["policy"],
            algo_hash,
            requirement,
        )
        agent_state_store.record_payment.assert_called_once_with(algo_hash, "intent-001", 10000)


class SpendReservationTests(unittest.TestCase):
    """A purchase must hold its budget while it waits for human approval.

    The cap is checked before the approval prompt and only recorded after the
    payment settles, so without a hold a second purchase started inside that
    window measures itself against a stale total and the daily cap is exceeded.
    """

    def make_store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return UserSpendLimitStore(Path(temp_dir.name) / "limits.json")

    def reserve(self, store, amount_atomic, *, daily_cap_atomic=1_000_000):
        return store.reserve_within_limits(
            "1045618308",
            amount_atomic=amount_atomic,
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            network="base-mainnet",
            max_per_tx_atomic=None,
            daily_cap_atomic=daily_cap_atomic,
        )

    def test_unsettled_reservation_still_consumes_the_daily_cap(self):
        store = self.make_store()

        first = self.reserve(store, 600_000)
        second = self.reserve(store, 600_000)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_released_reservation_frees_the_daily_cap_again(self):
        store = self.make_store()

        first = self.reserve(store, 600_000)
        store.release_reservation(first)
        second = self.reserve(store, 600_000)

        self.assertIsNotNone(second)

    def test_settled_reservation_keeps_consuming_the_daily_cap(self):
        store = self.make_store()

        first = self.reserve(store, 600_000)
        store.settle_reservation(first, tx_id="0xabc")
        second = self.reserve(store, 600_000)

        self.assertIsNone(second)
        self.assertEqual(
            store.spent_today_atomic(
                "1045618308",
                asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                network="base-mainnet",
            ),
            600_000,
        )

    def test_settling_a_reservation_does_not_double_count_it(self):
        store = self.make_store()

        handle = self.reserve(store, 400_000)
        store.settle_reservation(handle, tx_id="0xabc")

        self.assertEqual(
            store.spent_today_atomic(
                "1045618308",
                asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                network="base-mainnet",
            ),
            400_000,
        )

    def test_expired_reservation_stops_blocking_new_purchases(self):
        store = self.make_store()

        with patch("time.time", return_value=1_000.0):
            first = self.reserve(store, 600_000)
            self.assertIsNotNone(first)

        later = 1_000.0 + SPEND_RESERVATION_TTL_SECONDS + 1
        with patch("time.time", return_value=later):
            second = self.reserve(store, 600_000)

        self.assertIsNotNone(second)

    def test_per_transaction_cap_is_enforced_by_the_reservation(self):
        store = self.make_store()

        handle = store.reserve_within_limits(
            "1045618308",
            amount_atomic=200_000,
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            network="base-mainnet",
            max_per_tx_atomic=100_000,
            daily_cap_atomic=1_000_000,
        )

        self.assertIsNone(handle)

    def test_concurrent_reservations_cannot_both_win_the_last_slot(self):
        store = self.make_store()
        results = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            results.append(self.reserve(store, 600_000))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len([value for value in results if value is not None]), 1)

    def test_reservations_are_scoped_per_user(self):
        store = self.make_store()

        self.reserve(store, 600_000)
        other = store.reserve_within_limits(
            "2045618308",
            amount_atomic=600_000,
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            network="base-mainnet",
            max_per_tx_atomic=None,
            daily_cap_atomic=1_000_000,
        )

        self.assertIsNotNone(other)


class AgentStateStoreTests(unittest.TestCase):
    def make_store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return AgentStateStore(Path(temp_dir.name) / "agent-state.json")

    def test_validate_policy_allows_matching_payment_and_tracks_budget(self):
        store = self.make_store()
        policy = {
            "asset": "ALGO_TEST",
            "allowedPurpose": "x402_api_access",
            "maxBudgetAtomic": "100000",
            "maxPerPaymentAtomic": "50000",
        }
        policy_hash = "a" * 64
        requirement = {
            "asset": "ALGO_TEST",
            "purpose": "x402_api_access",
            "amountAtomic": "50000",
            "paymentIntent": "intent-001",
        }
        store.write_policy(
            {
                "policy": policy,
                "policyHash": policy_hash,
                "firefly": {"approvedHash": policy_hash},
            }
        )

        store.validate_policy_allows(policy, policy_hash, requirement)
        store.record_payment(policy_hash, "intent-001", 50000)

        self.assertEqual(store.remaining_budget(policy_hash), 50000)

    def test_validate_policy_rejects_replayed_intent(self):
        store = self.make_store()
        policy = {
            "asset": "ALGO_TEST",
            "allowedPurpose": "x402_api_access",
            "maxBudgetAtomic": "100000",
            "maxPerPaymentAtomic": "50000",
        }
        policy_hash = "a" * 64
        requirement = {
            "asset": "ALGO_TEST",
            "purpose": "x402_api_access",
            "amountAtomic": "50000",
            "paymentIntent": "intent-001",
        }
        store.write_policy(
            {
                "policy": policy,
                "policyHash": policy_hash,
                "firefly": {"approvedHash": policy_hash},
            }
        )
        store.record_payment(policy_hash, "intent-001", 50000)

        with self.assertRaisesRegex(ValueError, "paymentIntent already used"):
            store.validate_policy_allows(policy, policy_hash, requirement)

    def test_store_keeps_base_and_algorand_policies_and_resolves_by_requirement(self):
        store = self.make_store()
        algo_policy = {
            "asset": "10458941",
            "allowedPurpose": "x402_api_access",
            "maxBudgetAtomic": "100000",
            "maxPerPaymentAtomic": "10000",
        }
        base_policy = {
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
            "allowedPurpose": "x402_api_access",
            "maxBudgetAtomic": "1000000",
            "maxPerPaymentAtomic": "10000",
        }
        algo_hash = "a" * 64
        base_hash = "b" * 64
        store.write_policy(
            {
                "policy": algo_policy,
                "policyHash": algo_hash,
                "firefly": {"approvedHash": algo_hash},
            }
        )
        store.write_policy(
            {
                "policy": base_policy,
                "policyHash": base_hash,
                "firefly": {"approvedHash": base_hash},
            }
        )

        algo_state = store.read_policy_for_requirement(
            {
                "asset": "10458941",
                "purpose": "x402_api_access",
            }
        )
        base_state = store.read_policy_for_requirement(
            {
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "purpose": "x402_api_access",
            }
        )

        self.assertEqual(algo_state["policyHash"], algo_hash)
        self.assertEqual(base_state["policyHash"], base_hash)

        store.record_payment(algo_hash, "algo-intent", 10000)
        store.record_payment(base_hash, "base-intent", 1000)

        self.assertEqual(store.remaining_budget(algo_hash), 90000)
        self.assertEqual(store.remaining_budget(base_hash), 999000)


if __name__ == "__main__":
    unittest.main()
