import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from sign402_gateway.server import (
    AgentStateStore,
    BankrCliX402PaymentClient,
    BankrLlmCreditsTopUpRunner,
    DEFAULT_SINGIT_RISK_CHECK_URL,
    DEFAULT_SINGIT_TOKEN_ADDRESS,
    ExternalX402Buyer,
    Sign402GatewayHandler,
    _build_bankr_llm_topup_intent,
    _resolve_paid_tool,
    _tool_result,
)


def subprocess_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["bankr"], returncode=returncode, stdout=stdout, stderr=stderr)


class DummyServer:
    firefly = Mock()
    payment_executor = Mock()
    firefly_busy = False
    event_store = Mock()
    agent_state_store = Mock()
    agent_buy_probe = Mock()
    x402_inspector = Mock()
    x402_buyer = Mock()
    bankr_llm_topup_inspector = Mock()
    bankr_llm_topup = Mock()


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
    def make_handler(
        self,
        path: str,
        body: dict | None = None,
        method: str = "POST",
        server=None,
    ):
        encoded = b""
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")

        request = (
            f"{method} {path} HTTP/1.1\r\n".encode("ascii")
            + f"Content-Length: {len(encoded)}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + b"\r\n"
            + encoded
        )
        socket = FakeSocket(request)
        handler = Sign402GatewayHandler(socket, ("127.0.0.1", 12345), server or DummyServer())
        handler.response = socket.wfile
        return handler

    def response_text(self, handler) -> str:
        return handler.response.getvalue().decode("utf-8", "replace")

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

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/execute-payment",
                {
                    "policyHash": policy_hash,
                    "paymentApprovalHash": approval_hash,
                    "paymentRequirements": requirement,
                },
            )

        response = self.response_text(handler)

        self.assertIn("HTTP/1.0 200 OK", response)
        self.assertIn('"ok": true', response)
        self.assertIn('"txId": "TXID"', response)
        self.assertNotIn("private", response.lower())
        self.assertNotIn("mnemonic", response.lower())
        DummyServer.payment_executor.assert_called_once_with(requirement, policy_hash)

    def test_execute_payment_rejects_invalid_hash_before_executor(self):
        DummyServer.payment_executor.reset_mock()
        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "paymentIntent": "intent-001",
        }

        with patch("sys.stderr", io.StringIO()):
            handler = self.make_handler(
                "/execute-payment",
                {
                    "policyHash": "not-a-hash",
                    "paymentApprovalHash": "b" * 64,
                    "paymentRequirements": requirement,
                },
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
        self.assertIn("Access-Control-Allow-Origin: *", post_response)
        self.assertIn("HTTP/1.0 200 OK", get_response)
        self.assertIn('"decision": "APPROVED & EXECUTED"', get_response)
        DummyServer.event_store.write.assert_called_once_with(event)
        DummyServer.event_store.read.assert_called_once()

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
            client = BankrCliX402PaymentClient(bankr_cli="/tmp/bankr")
            result = client("https://x402.bankr.bot/0xabc/paid-risk-check")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["transactionHash"], "0xABC123")
        self.assertEqual(result["body"], {"ok": True, "riskLevel": "low"})
        self.assertEqual(run.call_args.args[0][:3], ["/tmp/bankr", "x402", "call"])
        self.assertIn("--raw", run.call_args.args[0])

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
