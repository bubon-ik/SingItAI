import unittest
from unittest.mock import Mock

from sign402_live.flow import build_payment_commitment, encode_payment_proof, run_paid_probe_flow


class LiveFlowTests(unittest.TestCase):
    def test_payment_proof_preserves_legacy_wire_format(self):
        proof = {"txId": "TEST_TX", "paymentIntent": "probe-algorand-co-001"}
        expected = "eyJwYXltZW50SW50ZW50IjoicHJvYmUtYWxnb3JhbmQtY28tMDAxIiwidHhJZCI6IlRFU1RfVFgifQ"

        self.assertEqual(encode_payment_proof(proof), expected)
        self.assertEqual(encode_payment_proof(dict(reversed(list(proof.items())))), expected)

    def test_build_payment_commitment_is_stable(self):
        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }

        first = build_payment_commitment(requirement, "a" * 64)
        second = build_payment_commitment(dict(reversed(list(requirement.items()))), "a" * 64)

        self.assertEqual(first["paymentHash"], second["paymentHash"])
        self.assertEqual(len(first["paymentHash"]), 64)
        self.assertEqual(first["commitment"]["type"], "sign402-payment")

    def test_run_paid_probe_flow_requests_402_pays_and_retries(self):
        resource_client = Mock()
        payment_executor = Mock()
        proof_encoder = Mock(return_value="ENCODED_PAYMENT")

        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }
        resource_client.get_probe_without_payment.return_value = {
            "status": 402,
            "paymentRequirements": requirement,
        }
        payment_executor.return_value = {
            "txId": "TXID",
            "network": "algorand-testnet",
            "receiver": "MERCHANT",
            "amountAtomic": "50000",
            "asset": "ALGO_TEST",
            "paymentIntent": "intent-001",
            "policyHash": "a" * 64,
        }
        resource_client.get_probe_with_payment.return_value = {
            "target": "algorand.co",
            "result": "reachable",
            "paymentTx": "TXID",
        }

        result = run_paid_probe_flow(
            target="algorand.co",
            policy_hash="a" * 64,
            resource_client=resource_client,
            payment_executor=payment_executor,
            proof_encoder=proof_encoder,
        )

        payment_executor.assert_called_once_with(requirement, "a" * 64)
        proof_encoder.assert_called_once()
        resource_client.get_probe_with_payment.assert_called_once_with(
            "algorand.co",
            "ENCODED_PAYMENT",
        )
        self.assertEqual(result["status"], "access_granted")
        self.assertEqual(result["payment"]["txId"], "TXID")
        self.assertEqual(result["resourceResult"]["result"], "reachable")

    def test_run_paid_probe_flow_requires_payment_approval_before_payment(self):
        resource_client = Mock()
        payment_executor = Mock()
        proof_encoder = Mock(return_value="ENCODED_PAYMENT")
        payment_approver = Mock()

        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }
        resource_client.get_probe_without_payment.return_value = {
            "status": 402,
            "paymentRequirements": requirement,
        }
        payment_approver.return_value = {
            "approved": True,
            "paymentHash": build_payment_commitment(requirement, "a" * 64)["paymentHash"],
            "firefly": {
                "approved": True,
                "approvedHash": build_payment_commitment(requirement, "a" * 64)["paymentHash"],
            },
        }
        payment_executor.return_value = {
            "txId": "TXID",
            "network": "algorand-testnet",
            "receiver": "MERCHANT",
            "amountAtomic": "50000",
            "asset": "ALGO_TEST",
            "paymentIntent": "intent-001",
            "policyHash": "a" * 64,
        }
        resource_client.get_probe_with_payment.return_value = {
            "target": "algorand.co",
            "result": "reachable",
            "paymentTx": "TXID",
        }

        result = run_paid_probe_flow(
            target="algorand.co",
            policy_hash="a" * 64,
            resource_client=resource_client,
            payment_executor=payment_executor,
            proof_encoder=proof_encoder,
            payment_approver=payment_approver,
        )

        payment_approver.assert_called_once()
        payment_executor.assert_called_once()
        self.assertEqual(result["paymentApproval"]["approved"], True)
        self.assertEqual(result["paymentProof"]["paymentApprovalHash"], result["paymentApprovalHash"])

    def test_run_paid_probe_flow_stops_when_firefly_rejects_payment(self):
        resource_client = Mock()
        payment_executor = Mock()
        proof_encoder = Mock(return_value="ENCODED_PAYMENT")
        payment_approver = Mock(return_value={"approved": False, "error": "PAYMENT rejected by user"})

        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }
        resource_client.get_probe_without_payment.return_value = {
            "status": 402,
            "paymentRequirements": requirement,
        }

        result = run_paid_probe_flow(
            target="algorand.co",
            policy_hash="a" * 64,
            resource_client=resource_client,
            payment_executor=payment_executor,
            proof_encoder=proof_encoder,
            payment_approver=payment_approver,
        )

        payment_executor.assert_not_called()
        resource_client.get_probe_with_payment.assert_not_called()
        self.assertEqual(result["status"], "payment_rejected")

    def test_run_paid_probe_flow_retries_until_resource_access_is_granted(self):
        resource_client = Mock()
        payment_executor = Mock()
        proof_encoder = Mock(return_value="ENCODED_PAYMENT")

        requirement = {
            "network": "algorand-testnet",
            "asset": "ALGO_TEST",
            "amountAtomic": "50000",
            "receiver": "MERCHANT",
            "resource": "/probe?target=algorand.co",
            "paymentIntent": "intent-001",
            "purpose": "x402_api_access",
        }
        resource_client.get_probe_without_payment.return_value = {
            "status": 402,
            "paymentRequirements": requirement,
        }
        payment_executor.return_value = {
            "txId": "TXID",
            "network": "algorand-testnet",
            "receiver": "MERCHANT",
            "amountAtomic": "50000",
            "asset": "ALGO_TEST",
            "paymentIntent": "intent-001",
            "policyHash": "a" * 64,
        }
        resource_client.get_probe_with_payment.side_effect = [
            {"status": 402, "reason": "Algorand transaction lookup failed: HTTP 404"},
            {"target": "algorand.co", "result": "reachable", "paymentTx": "TXID"},
        ]

        result = run_paid_probe_flow(
            target="algorand.co",
            policy_hash="a" * 64,
            resource_client=resource_client,
            payment_executor=payment_executor,
            proof_encoder=proof_encoder,
            retry_delay_seconds=0,
            max_retries=2,
        )

        self.assertEqual(resource_client.get_probe_with_payment.call_count, 2)
        self.assertEqual(result["status"], "access_granted")


if __name__ == "__main__":
    unittest.main()
