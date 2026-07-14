import io
import unittest
import urllib.error
from unittest.mock import Mock

from sign402_gateway.goplausible import (
    ALGORAND_TESTNET_CAIP2,
    BASE_MAINNET_CAIP2,
    fetch_x402_payment_required,
    normalize_x402_payment_required,
)


class GoPlausibleAdapterTests(unittest.TestCase):
    def test_fetch_rejects_oversized_upstream_response(self):
        class OversizedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return b"x" * 1_048_577

        with self.assertRaisesRegex(ValueError, "response is too large"):
            fetch_x402_payment_required(
                "https://example.test/protected",
                opener=Mock(return_value=OversizedResponse()),
            )

    def test_normalizes_official_algorand_payment_requirements(self):
        payload = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": ALGORAND_TESTNET_CAIP2,
                    "amount": "10000",
                    "asset": "10458941",
                    "payTo": "PAYEEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "maxTimeoutSeconds": 60,
                    "extra": {
                        "decimals": 6,
                        "feePayer": "FEEPAYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABX",
                    },
                }
            ],
        }

        normalized = normalize_x402_payment_required(
            payload,
            resource_url="https://example.x402.goplausible.xyz/protected",
        )

        self.assertEqual(normalized["sourceFormat"], "x402-avm-v2")
        self.assertEqual(normalized["network"], "algorand-testnet")
        self.assertEqual(normalized["x402Network"], ALGORAND_TESTNET_CAIP2)
        self.assertEqual(normalized["amountAtomic"], "10000")
        self.assertEqual(normalized["asset"], "10458941")
        self.assertEqual(
            normalized["receiver"],
            "PAYEEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        self.assertEqual(
            normalized["resource"],
            "https://example.x402.goplausible.xyz/protected",
        )
        self.assertEqual(normalized["purpose"], "x402_api_access")
        self.assertTrue(normalized["paymentIntent"].startswith("x402-"))
        self.assertEqual(
            normalized["originalPaymentRequirements"],
            payload["accepts"][0],
        )

    def test_preserves_official_payment_intent_from_extra_when_present(self):
        payload = {
            "paymentRequirements": {
                "scheme": "exact",
                "network": ALGORAND_TESTNET_CAIP2,
                "amount": "50000",
                "asset": "10458941",
                "payTo": "PAYEE",
                "maxTimeoutSeconds": 60,
                "extra": {"paymentIntent": "intent-from-resource"},
            }
        }

        normalized = normalize_x402_payment_required(
            payload,
            resource_url="https://example.x402.goplausible.xyz/api",
        )

        self.assertEqual(normalized["paymentIntent"], "intent-from-resource")

    def test_generates_fresh_local_intent_when_external_resource_has_no_nonce(self):
        payload = {
            "paymentRequirements": {
                "scheme": "exact",
                "network": ALGORAND_TESTNET_CAIP2,
                "amount": "10000",
                "asset": "10458941",
                "payTo": "PAYEE",
                "maxTimeoutSeconds": 60,
                "extra": {"name": "USDC"},
            }
        }

        first = normalize_x402_payment_required(
            payload,
            resource_url="https://x402.goplausible.xyz/examples/weather",
        )
        second = normalize_x402_payment_required(
            payload,
            resource_url="https://x402.goplausible.xyz/examples/weather",
        )

        self.assertNotEqual(first["paymentIntent"], second["paymentIntent"])
        self.assertTrue(first["paymentIntent"].startswith("x402-local-"))
        self.assertTrue(second["paymentIntent"].startswith("x402-local-"))

    def test_rejects_non_algorand_x402_network(self):
        payload = {
            "paymentRequirements": {
                "scheme": "exact",
                "network": "base-sepolia",
                "amount": "100",
                "asset": "0xTOKEN",
                "payTo": "0xPAYEE",
            }
        }

        with self.assertRaisesRegex(ValueError, "Unsupported x402 network"):
            normalize_x402_payment_required(payload, resource_url="https://example.test")

    def test_normalizes_base_mainnet_payment_requirements(self):
        payload = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": BASE_MAINNET_CAIP2,
                    "amount": "10000",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
                    "payTo": "0x1111111111111111111111111111111111111111",
                    "maxTimeoutSeconds": 60,
                    "extra": {
                        "name": "USDC",
                        "decimals": 6,
                    },
                }
            ],
        }

        normalized = normalize_x402_payment_required(
            payload,
            resource_url="https://api.example.com/paid-report",
        )

        self.assertEqual(normalized["sourceFormat"], "x402-evm-v2")
        self.assertEqual(normalized["network"], "base-mainnet")
        self.assertEqual(normalized["x402Network"], BASE_MAINNET_CAIP2)
        self.assertEqual(normalized["amountAtomic"], "10000")
        self.assertEqual(
            normalized["asset"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
        )
        self.assertEqual(
            normalized["receiver"],
            "0x1111111111111111111111111111111111111111",
        )
        self.assertEqual(normalized["purpose"], "x402_api_access")
        self.assertEqual(normalized["extra"]["name"], "USDC")
        self.assertEqual(normalized["originalPaymentRequirements"], payload["accepts"][0])

    def test_fetch_sends_json_accept_and_user_agent_headers(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return b"{}"

        opener = Mock(return_value=FakeResponse())

        with self.assertRaisesRegex(ValueError, "Expected x402 resource to return HTTP 402"):
            fetch_x402_payment_required("https://example.test/protected", opener=opener)

        request = opener.call_args.args[0]
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertIn("Hermes-Sign402", request.headers["User-agent"])

    def test_fetch_decodes_payment_required_header(self):
        header_value = (
            "eyJ4NDAyVmVyc2lvbiI6MiwicmVzb3VyY2UiOnsidXJsIjoiaHR0cHM6Ly9leGFtcGxl"
            "LnRlc3QvYXZtL3dlYXRoZXIifSwiYWNjZXB0cyI6W3sic2NoZW1lIjoiZXhhY3QiLCJu"
            "ZXR3b3JrIjoiYWxnb3JhbmQ6U0dPMUdLU3p5RTdJRVBJdFR4Q0J5dzl4OEZtbnJDRGV4"
            "aTkvY09VSk9pST0iLCJhbW91bnQiOiIxMDAwMCIsImFzc2V0IjoiMTA0NTg5NDEiLCJw"
            "YXlUbyI6IlBBWUVFIiwibWF4VGltZW91dFNlY29uZHMiOjMwMCwiZXh0cmEiOnsibmFt"
            "ZSI6IlVTREMiLCJkZWNpbWFscyI6Nn19XX0="
        )
        error = urllib.error.HTTPError(
            url="https://example.test/avm/weather",
            code=402,
            msg="Payment Required",
            hdrs={"Payment-Required": header_value},
            fp=io.BytesIO(b"{}"),
        )
        opener = Mock(side_effect=error)

        payload = fetch_x402_payment_required("https://example.test/avm/weather", opener=opener)

        self.assertEqual(payload["x402Version"], 2)
        self.assertEqual(payload["accepts"][0]["payTo"], "PAYEE")


if __name__ == "__main__":
    unittest.main()
