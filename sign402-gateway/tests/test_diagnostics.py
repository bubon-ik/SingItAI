import hashlib
import io
import json
import logging
import unittest

from sign402_gateway.diagnostics import (
    bounded,
    configure_logging,
    describe_payload_shape,
    log_swallowed_failure,
    redact_secrets,
    safe_provider_diagnostic,
)


class RedactSecretsTests(unittest.TestCase):
    def test_secret_env_values_are_replaced_by_name(self):
        env = {
            "CDP_WALLET_SECRET": "super-secret-wallet-value",
            "HOME": "/home/hermes",
        }

        redacted = redact_secrets(
            "boom: super-secret-wallet-value at /home/hermes",
            env=env,
        )

        self.assertNotIn("super-secret-wallet-value", redacted)
        self.assertIn("<redacted:CDP_WALLET_SECRET>", redacted)
        self.assertIn("/home/hermes", redacted)

    def test_short_secret_values_are_not_used_as_patterns(self):
        env = {"API_KEY": "abc"}

        self.assertEqual(redact_secrets("abc def", env=env), "abc def")

    def test_longest_secret_is_redacted_first(self):
        env = {
            "BITREFILL_API_KEY": "key_abcdefgh",
            "SIGN402_WALLET_API_TOKEN": "key_abcdefgh_and_more",
        }

        redacted = redact_secrets("token key_abcdefgh_and_more used", env=env)

        self.assertIn("<redacted:SIGN402_WALLET_API_TOKEN>", redacted)
        self.assertNotIn("key_abcdefgh_and_more", redacted)

    def test_non_secret_names_are_left_alone(self):
        env = {"SIGN402_BITREFILL_MODE": "live-mode-value"}

        self.assertEqual(
            redact_secrets("mode live-mode-value", env=env),
            "mode live-mode-value",
        )


class BoundedTests(unittest.TestCase):
    def test_long_text_is_truncated_with_marker(self):
        self.assertEqual(bounded("abcdef", limit=4), "abcd…(truncated)")

    def test_short_text_is_returned_unchanged(self):
        self.assertEqual(bounded("abc", limit=8), "abc")


class SafeProviderDiagnosticTests(unittest.TestCase):
    def test_keeps_allowlisted_fields_and_filters_bearer_values(self):
        detail = json.dumps(
            {
                "error_code": "PACKAGE_VALUE_INVALID",
                "message": (
                    "invalid package; pay https://pay.example/inv "
                    "to 0x1111111111111111111111111111111111111111 "
                    "pin=1234 esim=LPA:1$secret"
                ),
                "status": 422,
                "trace_id": "trace_123",
                "payment_link": "https://pay.example/secret",
                "redemption": {"code": "GIFT-SECRET"},
            }
        )

        diagnostic = safe_provider_diagnostic(detail, env={})

        self.assertEqual(diagnostic["code"], "PACKAGE_VALUE_INVALID")
        self.assertEqual(diagnostic["status"], "422")
        self.assertEqual(diagnostic["requestId"], "trace_123")
        rendered = str(diagnostic)
        for secret in (
            "https://",
            "0x1111111111111111111111111111111111111111",
            "1234",
            "LPA:",
            "GIFT-SECRET",
        ):
            self.assertNotIn(secret, rendered)

    def test_filters_guest_checkout_bearer_values_and_the_buyer_email(self):
        # A guest invoice is readable by whoever holds its access token, and
        # the buyer's address is personal data we only hold to deliver codes.
        detail = json.dumps(
            {
                "error_code": "INVOICE_UNREADABLE",
                "message": (
                    "invoice_access_token=tok_live_secret_value "
                    "access_link=https://bitrefill.example/i/abc "
                    "email: buyer@example.com"
                ),
                "status": 403,
            }
        )

        diagnostic = safe_provider_diagnostic(detail, env={})

        self.assertEqual(diagnostic["code"], "INVOICE_UNREADABLE")
        rendered = str(diagnostic)
        for secret in (
            "tok_live_secret_value",
            "bitrefill.example",
            "buyer@example.com",
        ):
            self.assertNotIn(secret, rendered)

    def test_filters_a_buyer_email_that_carries_no_label(self):
        diagnostic = safe_provider_diagnostic(
            json.dumps(
                {
                    "code": "CART_REJECTED",
                    "message": "buyer@example.com is not deliverable",
                }
            ),
            env={},
        )

        self.assertNotIn("buyer@example.com", str(diagnostic))
        self.assertIn("<redacted:email>", str(diagnostic))

    def test_filters_secret_environment_values_from_allowlisted_fields(self):
        diagnostic = safe_provider_diagnostic(
            json.dumps(
                {
                    "code": "PROVIDER_REJECTED",
                    "message": "request used secret-provider-token",
                    "request_id": "secret-provider-token",
                }
            ),
            env={"BITREFILL_API_KEY": "secret-provider-token"},
        )

        rendered = str(diagnostic)
        self.assertNotIn("secret-provider-token", rendered)
        self.assertIn("<redacted:BITREFILL_API_KEY>", rendered)

    def test_unparseable_body_returns_only_length_and_fingerprint(self):
        detail = (
            "raw address 0x1111111111111111111111111111111111111111 "
            "https://pay.example/secret"
        )

        diagnostic = safe_provider_diagnostic(detail, env={})

        self.assertEqual(diagnostic["type"], "unparseable")
        self.assertEqual(diagnostic["bytes"], len(detail.encode("utf-8")))
        self.assertEqual(
            diagnostic["sha256"],
            hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(detail, str(diagnostic))

    def test_non_scalar_allowlisted_message_never_logs_nested_bearer_data(self):
        diagnostic = safe_provider_diagnostic(
            json.dumps(
                {
                    "code": "REJECTED",
                    "message": {
                        "redemption": {"code": "GIFT-SECRET"},
                        "payment_address": (
                            "0x1111111111111111111111111111111111111111"
                        ),
                    },
                }
            ),
            env={},
        )

        self.assertEqual(diagnostic["code"], "REJECTED")
        self.assertNotIn("GIFT-SECRET", str(diagnostic))
        self.assertNotIn("0x111111", str(diagnostic))


class LogSwallowedFailureTests(unittest.TestCase):
    def test_cause_is_logged_with_context(self):
        logger = logging.getLogger("sign402_gateway.test_target")

        with self.assertLogs(logger, level="ERROR") as captured:
            try:
                raise RuntimeError("REAL-CAUSE-MARKER")
            except RuntimeError as exc:
                log_swallowed_failure(
                    logger,
                    "bitrefill fulfillment failed",
                    exc,
                    quoteId="quote_1",
                )

        joined = "\n".join(captured.output)
        self.assertIn("REAL-CAUSE-MARKER", joined)
        self.assertIn("bitrefill fulfillment failed", joined)
        self.assertIn("quote_1", joined)
        self.assertIn("RuntimeError", joined)

    def test_secret_values_are_redacted_from_the_log(self):
        logger = logging.getLogger("sign402_gateway.test_target")
        env = {"CDP_API_KEY_SECRET": "leaked-secret-value"}

        with self.assertLogs(logger, level="ERROR") as captured:
            try:
                raise RuntimeError("boom leaked-secret-value")
            except RuntimeError as exc:
                log_swallowed_failure(logger, "cdp call failed", exc, env=env)

        joined = "\n".join(captured.output)
        self.assertNotIn("leaked-secret-value", joined)
        self.assertIn("<redacted:CDP_API_KEY_SECRET>", joined)

    def test_context_fields_use_the_same_bearer_filter(self):
        logger = logging.getLogger("sign402_gateway.test_target")
        address = "0x1111111111111111111111111111111111111111"

        with self.assertLogs(logger, level="ERROR") as captured:
            log_swallowed_failure(
                logger,
                "provider failed",
                RuntimeError("safe cause"),
                env={"API_KEY": "secret-provider-value"},
                request=f"https://pay.example/secret {address}",
                trace="secret-provider-value",
            )

        joined = "\n".join(captured.output)
        self.assertNotIn("https://", joined)
        self.assertNotIn(address, joined)
        self.assertNotIn("secret-provider-value", joined)


class ConfigureLoggingTests(unittest.TestCase):
    def test_transport_loggers_are_silenced_so_urls_stay_out_of_the_journal(self):
        # httpx logs every request URL at INFO, and the Bitrefill MCP URL
        # carries the API key in its path.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)

        configure_logging(level="INFO", stream=io.StringIO())

        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            self.assertGreaterEqual(logger.getEffectiveLevel(), logging.WARNING)
        self.assertEqual(
            logging.getLogger("sign402_gateway").getEffectiveLevel(),
            logging.INFO,
        )


class DescribePayloadShapeTests(unittest.TestCase):
    def test_shape_reports_key_names_and_types_but_never_values(self):
        shape = describe_payload_shape(
            {
                "result": {
                    "invoice_id": "inv_secret_value",
                    "payment_info": {"address": "0xSECRETADDRESS", "amount": 23.76},
                    "paid": False,
                }
            }
        )

        self.assertEqual(
            shape,
            {
                "result": {
                    "invoice_id": "str",
                    "paid": "bool",
                    "payment_info": {"address": "str", "amount": "float"},
                }
            },
        )
        rendered = str(shape)
        self.assertNotIn("inv_secret_value", rendered)
        self.assertNotIn("0xSECRETADDRESS", rendered)

    def test_lists_report_length_and_element_shape(self):
        shape = describe_payload_shape({"orders": [{"order_id": "ord_1"}]})

        self.assertEqual(shape, {"orders": {"list[1]": {"order_id": "str"}}})

    def test_empty_list_is_reported_without_element_shape(self):
        self.assertEqual(describe_payload_shape({"orders": []}), {"orders": "list[0]"})

    def test_depth_is_bounded(self):
        shape = describe_payload_shape({"a": {"b": {"c": {"d": "x"}}}}, depth=2)

        self.assertEqual(shape, {"a": {"b": "object(1 keys)"}})

    def test_key_count_is_bounded(self):
        payload = {f"key_{index}": index for index in range(5)}

        shape = describe_payload_shape(payload, max_keys=2)

        self.assertEqual(len(shape), 3)
        self.assertEqual(shape["…"], "+3 more")


if __name__ == "__main__":
    unittest.main()
