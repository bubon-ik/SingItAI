import logging
import unittest

from sign402_gateway.diagnostics import (
    bounded,
    log_swallowed_failure,
    redact_secrets,
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


if __name__ == "__main__":
    unittest.main()
