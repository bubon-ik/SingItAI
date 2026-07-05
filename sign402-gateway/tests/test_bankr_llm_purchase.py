import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import traceback
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet

from sign402_gateway.bankr_llm_purchase import (
    BankrIdentityClient,
    BankrLlmError,
    BankrLlmPurchaseService,
    BankrLlmStore,
    build_bankr_llm_purchase_service_from_env,
)


EVM_ADDRESS = "0x1111111111111111111111111111111111111111"
API_KEY = "bk_test_key"
IDENTITY_TOKEN = "identity-secret"


class FakeResponse:
    def __init__(self, payload):
        self.body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.closed = False
        self.read_size = None

    def read(self, size=-1):
        self.read_size = size
        return self.body if size < 0 else self.body[:size]

    def close(self):
        self.closed = True


class QueueOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.results:
            raise AssertionError(f"unexpected HTTP request: {request.full_url}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def json_response(payload):
    return FakeResponse(payload)


def http_error(status, payload):
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    error = HTTPError(
        "https://upstream.example/private",
        status,
        "private upstream reason",
        {},
        body,
    )
    error.tracked_body = body
    return error


def request_json(request):
    return json.loads(request.data.decode("utf-8"))


class BankrIdentityClientTests(unittest.TestCase):
    def test_rejects_non_https_service_urls_before_http(self):
        for argument, value in (
            ("api_url", "http://api.bankr.example"),
            ("llm_url", "http://llm.bankr.example"),
            ("api_url", "https:///missing-host"),
        ):
            with self.subTest(argument=argument, value=value):
                with self.assertRaises(BankrLlmError) as raised:
                    BankrIdentityClient(**{argument: value})
                self.assertEqual(
                    raised.exception.code,
                    "invalid_configuration",
                )
                self.assertEqual(
                    raised.exception.user_message,
                    "Bankr service URLs must use HTTPS.",
                )
                self.assertIsNone(raised.exception.__context__)
                self.assertIsNone(raised.exception.__cause__)

    def test_default_transport_rejects_redirects_without_forwarding_headers(self):
        client = BankrIdentityClient()
        opener = getattr(client.opener, "__self__", None)

        self.assertIsInstance(opener, urllib.request.OpenerDirector)
        redirect_handler = next(
            handler
            for handler in opener.handlers
            if type(handler).__name__ == "_RejectRedirectHandler"
        )
        request = urllib.request.Request(
            "https://api.bankr.bot/api-keys",
            headers={
                "X-API-Key": API_KEY,
                "Privy-Id-Token": IDENTITY_TOKEN,
            },
        )

        redirected = redirect_handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            {"Location": "https://attacker.example/steal"},
            "https://attacker.example/steal",
        )

        self.assertIsNone(redirected)

    def test_send_otp_uses_privy_configuration_and_closes_responses(self):
        config_response = json_response(
            {"privyAppId": "app-1", "privyClientId": "client-1"}
        )
        otp_response = json_response({"success": True})
        opener = QueueOpener(config_response, otp_response)
        client = BankrIdentityClient(opener=opener, timeout=7.5)

        result = client.send_otp("user@example.com")

        self.assertIsNone(result)
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://api.bankr.bot/cli/config",
                "https://auth.privy.io/api/v1/passwordless/init",
            ],
        )
        self.assertEqual(
            [request.method for request in opener.requests],
            ["GET", "POST"],
        )
        self.assertEqual(
            request_json(opener.requests[1]),
            {"email": "user@example.com", "type": "email"},
        )
        self.assertEqual(
            opener.requests[1].get_header("Privy-app-id"),
            "app-1",
        )
        self.assertEqual(
            opener.requests[1].get_header("Privy-client-id"),
            "client-1",
        )
        self.assertEqual(
            opener.requests[1].get_header("Content-type"),
            "application/json",
        )
        self.assertTrue(
            opener.requests[1].get_header("User-agent").startswith("Mozilla/5.0"),
        )
        self.assertEqual(
            opener.requests[1].get_header("Accept-language"),
            "en-US,en;q=0.9",
        )
        self.assertEqual(
            opener.requests[1].get_header("Origin"),
            "https://bankr.bot",
        )
        self.assertEqual(
            opener.requests[1].get_header("Referer"),
            "https://bankr.bot/",
        )
        self.assertEqual(opener.timeouts, [7.5, 7.5])
        self.assertTrue(config_response.closed)
        self.assertTrue(otp_response.closed)

    def test_verify_and_create_key_uses_minimum_capabilities(self):
        responses = [
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": True,
                    "isNewUser": False,
                }
            ),
            json_response(
                {
                    "id": "key-1",
                    "apiKey": API_KEY,
                    "name": "Sign402-123",
                    "walletApiEnabled": True,
                    "agentApiEnabled": False,
                    "readOnly": False,
                    "tokenLaunchApiEnabled": False,
                    "llmGatewayEnabled": True,
                    "allowedIps": [],
                    "allowedRecipients": {},
                }
            ),
        ]
        opener = QueueOpener(*responses)
        client = BankrIdentityClient(opener=opener)

        result = client.verify_and_create_key(
            email="user@example.com",
            code="123456",
            key_name="Sign402-123",
            accept_terms=False,
        )

        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://api.bankr.bot/cli/config",
                "https://auth.privy.io/api/v1/passwordless/authenticate",
                "https://api.bankr.bot/cli/generate-wallet",
                "https://api.bankr.bot/api-keys",
            ],
        )
        self.assertEqual(
            request_json(opener.requests[1]),
            {
                "email": "user@example.com",
                "code": "123456",
                "mode": "login-or-sign-up",
            },
        )
        self.assertEqual(
            opener.requests[1].get_header("Privy-app-id"),
            "app-1",
        )
        self.assertEqual(
            opener.requests[1].get_header("Privy-client-id"),
            "client-1",
        )
        self.assertEqual(
            opener.requests[2].get_header("Privy-id-token"),
            IDENTITY_TOKEN,
        )
        self.assertEqual(
            request_json(opener.requests[3]),
            {
                "name": "Sign402-123",
                "walletApiEnabled": True,
                "agentApiEnabled": False,
                "readOnly": False,
                "tokenLaunchApiEnabled": False,
                "llmGatewayEnabled": True,
                "allowedIps": [],
                "allowedRecipients": {},
            },
        )
        self.assertEqual(
            result,
            {
                "evmAddress": EVM_ADDRESS,
                "apiKey": API_KEY,
                "key": {
                    "id": "key-1",
                    "name": "Sign402-123",
                    "llmGatewayEnabled": True,
                    "requestedCapabilities": {
                        "walletApiEnabled": True,
                        "agentApiEnabled": False,
                        "readOnly": False,
                        "tokenLaunchApiEnabled": False,
                        "llmGatewayEnabled": True,
                        "allowedIps": [],
                        "allowedRecipients": {},
                    },
                },
            },
        )
        self.assertTrue(all(response.closed for response in responses))

    def test_verify_accepts_terms_only_when_requested_and_required(self):
        opener = QueueOpener(
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": False,
                }
            ),
            json_response({"success": True}),
            json_response(
                {
                    "apiKey": API_KEY,
                    "name": "Sign402-123",
                    "llmGatewayEnabled": True,
                }
            ),
        )
        client = BankrIdentityClient(opener=opener)

        client.verify_and_create_key(
            email="user@example.com",
            code="123456",
            key_name="Sign402-123",
            accept_terms=True,
        )

        self.assertEqual(
            opener.requests[3].full_url,
            "https://api.bankr.bot/user/accept-terms",
        )
        self.assertEqual(opener.requests[3].method, "POST")
        self.assertEqual(request_json(opener.requests[3]), {})
        self.assertEqual(
            opener.requests[3].get_header("Privy-id-token"),
            IDENTITY_TOKEN,
        )

    def test_normalized_key_metadata_cannot_mutate_later_requests(self):
        opener = QueueOpener(
            *(
                [
                    json_response(
                        {"privyAppId": "app-1", "privyClientId": "client-1"}
                    ),
                    json_response({"identity_token": IDENTITY_TOKEN}),
                    json_response(
                        {
                            "evmAddress": EVM_ADDRESS,
                            "hasAcceptedTerms": True,
                        }
                    ),
                    json_response(
                        {
                            "apiKey": API_KEY,
                            "name": "Sign402-123",
                            "llmGatewayEnabled": True,
                        }
                    ),
                ]
                * 2
            )
        )
        client = BankrIdentityClient(opener=opener)

        first = client.verify_and_create_key(
            email="user@example.com",
            code="123456",
            key_name="Sign402-123",
            accept_terms=False,
        )
        first["key"]["requestedCapabilities"]["allowedIps"].append(
            "private-mutation"
        )
        try:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )
            self.assertEqual(request_json(opener.requests[7])["allowedIps"], [])
        finally:
            first["key"]["requestedCapabilities"]["allowedIps"].clear()

    def test_verify_requires_terms_without_creating_a_key(self):
        opener = QueueOpener(
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": False,
                }
            ),
        )
        client = BankrIdentityClient(opener=opener)

        with self.assertRaises(BankrLlmError) as raised:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )

        self.assertEqual(raised.exception.code, "terms_required")
        self.assertEqual(
            raised.exception.user_message,
            "Accept Bankr's terms before continuing.",
        )
        self.assertEqual(len(opener.requests), 3)
        self.assertNotIn(IDENTITY_TOKEN, str(raised.exception))

    def test_validates_email_and_otp_before_http_requests(self):
        cases = (
            (
                lambda client: client.send_otp("not-an-email"),
                "invalid_email",
                "Enter a valid email address.",
            ),
            (
                lambda client: client.verify_and_create_key(
                    email="user@example.com",
                    code="12 456",
                    key_name="Sign402-123",
                    accept_terms=False,
                ),
                "invalid_otp",
                "Enter the six-digit verification code.",
            ),
        )

        for operation, code, message in cases:
            with self.subTest(code=code):
                opener = QueueOpener()
                client = BankrIdentityClient(opener=opener)
                with self.assertRaises(BankrLlmError) as raised:
                    operation(client)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.user_message, message)
                self.assertEqual(opener.requests, [])

    def test_rejects_invalid_wallet_and_key_responses_without_leaking_secrets(self):
        cases = (
            (
                [
                    json_response(
                        {"privyAppId": "app-1", "privyClientId": "client-1"}
                    ),
                    json_response({"identity_token": IDENTITY_TOKEN}),
                    json_response(
                        {
                            "evmAddress": "private-invalid-wallet",
                            "hasAcceptedTerms": True,
                        }
                    ),
                ],
                "invalid_response",
                "Bankr returned an invalid response. Please try again.",
            ),
            (
                [
                    json_response(
                        {"privyAppId": "app-1", "privyClientId": "client-1"}
                    ),
                    json_response({"identity_token": IDENTITY_TOKEN}),
                    json_response(
                        {
                            "evmAddress": EVM_ADDRESS,
                            "hasAcceptedTerms": True,
                        }
                    ),
                    json_response(
                        {
                            "apiKey": "private-invalid-key",
                            "name": "Sign402-123",
                        }
                    ),
                ],
                "bankr_key_creation_ambiguous",
                "Bankr API key creation result is unclear. Please check status before retrying.",
            ),
        )

        for responses, code, message in cases:
            with self.subTest(request_count=len(responses)):
                client = BankrIdentityClient(opener=QueueOpener(*responses))
                with self.assertRaises(BankrLlmError) as raised:
                    client.verify_and_create_key(
                        email="user@example.com",
                        code="123456",
                        key_name="Sign402-123",
                        accept_terms=False,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.user_message, message)
                error_text = str(raised.exception)
                self.assertNotIn("123456", error_text)
                self.assertNotIn(IDENTITY_TOKEN, error_text)
                self.assertNotIn("private-invalid", error_text)

    def test_rejects_key_response_without_confirmed_llm_gateway(self):
        responses = [
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": True,
                }
            ),
            json_response(
                {
                    "apiKey": API_KEY,
                    "name": "Sign402-123",
                }
            ),
        ]
        client = BankrIdentityClient(opener=QueueOpener(*responses))

        with self.assertRaises(BankrLlmError) as raised:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )

        self.assertEqual(raised.exception.code, "bankr_key_creation_ambiguous")
        self.assertEqual(
            raised.exception.user_message,
            "Bankr API key creation result is unclear. Please check status before retrying.",
        )
        self.assertNotIn(API_KEY, str(raised.exception))

    def test_key_creation_transport_error_is_ambiguous_and_redacted(self):
        upstream_secret = f"private failure for {IDENTITY_TOKEN}"
        responses = [
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": True,
                }
            ),
            URLError(upstream_secret),
        ]
        client = BankrIdentityClient(opener=QueueOpener(*responses))

        with self.assertRaises(BankrLlmError) as raised:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(raised.exception.code, "bankr_key_creation_ambiguous")
        self.assertEqual(
            raised.exception.user_message,
            "Bankr API key creation result is unclear. Please check status before retrying.",
        )
        self.assertNotIn(upstream_secret, rendered)
        self.assertNotIn(IDENTITY_TOKEN, rendered)

    def test_key_creation_rejection_includes_redacted_upstream_detail(self):
        error = http_error(
            400,
            {
                "message": "llmGatewayEnabled is not allowed",
                "apiKey": API_KEY,
                "identity_token": IDENTITY_TOKEN,
            },
        )
        responses = [
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": IDENTITY_TOKEN}),
            json_response(
                {
                    "evmAddress": EVM_ADDRESS,
                    "hasAcceptedTerms": True,
                }
            ),
            error,
        ]
        client = BankrIdentityClient(opener=QueueOpener(*responses))

        with self.assertRaises(BankrLlmError) as raised:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )

        self.assertEqual(raised.exception.code, "bankr_key_creation_rejected")
        error_text = str(raised.exception)
        self.assertIn("HTTP 400", error_text)
        self.assertIn("llmGatewayEnabled is not allowed", error_text)
        self.assertNotIn(API_KEY, error_text)
        self.assertNotIn(IDENTITY_TOKEN, error_text)
        self.assertTrue(error.tracked_body.closed)

    def test_rate_limit_error_is_stable_redacted_and_closed(self):
        error = http_error(429, {"secret": "private-upstream-body"})
        client = BankrIdentityClient(opener=QueueOpener(error))

        with self.assertRaises(BankrLlmError) as raised:
            client.send_otp("user@example.com")

        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(
            raised.exception.user_message,
            "Too many requests. Please try again later.",
        )
        self.assertNotIn("private-upstream-body", str(raised.exception))
        self.assertTrue(error.tracked_body.closed)
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_otp_http_error_is_stable_redacted_and_closed(self):
        error = http_error(
            401,
            {
                "error": "invalid code 123456",
                "identity_token": IDENTITY_TOKEN,
                "apiKey": API_KEY,
            },
        )
        opener = QueueOpener(
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            error,
        )
        client = BankrIdentityClient(opener=opener)

        with self.assertRaises(BankrLlmError) as raised:
            client.verify_and_create_key(
                email="user@example.com",
                code="123456",
                key_name="Sign402-123",
                accept_terms=False,
            )

        self.assertEqual(raised.exception.code, "invalid_otp")
        self.assertEqual(
            raised.exception.user_message,
            "That verification code is invalid or expired.",
        )
        error_text = str(raised.exception)
        self.assertNotIn("123456", error_text)
        self.assertNotIn(IDENTITY_TOKEN, error_text)
        self.assertNotIn(API_KEY, error_text)
        self.assertTrue(error.tracked_body.closed)

    def test_transport_error_traceback_discards_sensitive_exception(self):
        upstream_secret = f"connection failed for {API_KEY}"
        client = BankrIdentityClient(
            opener=QueueOpener(URLError(upstream_secret))
        )

        with self.assertRaises(BankrLlmError) as raised:
            client.credits(api_key=API_KEY)

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(upstream_secret, rendered)
        self.assertNotIn(API_KEY, rendered)
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_json_traceback_discards_sensitive_document(self):
        upstream_secret = f"{IDENTITY_TOKEN}-{API_KEY}"
        response = json_response(
            b'{"private":"' + upstream_secret.encode("utf-8")
        )
        client = BankrIdentityClient(opener=QueueOpener(response))

        with self.assertRaises(BankrLlmError) as raised:
            client.send_otp("user@example.com")

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertNotIn(upstream_secret, rendered)
        self.assertNotIn(IDENTITY_TOKEN, rendered)
        self.assertNotIn(API_KEY, rendered)
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(response.closed)

    def test_oversized_response_is_bounded_closed_and_redacted(self):
        upstream_secret = f"{IDENTITY_TOKEN}-{API_KEY}"
        response = json_response(
            upstream_secret.encode("utf-8") + b"x" * (64 * 1024)
        )
        client = BankrIdentityClient(opener=QueueOpener(response))

        with self.assertRaises(BankrLlmError) as raised:
            client.send_otp("user@example.com")

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(response.read_size, 64 * 1024 + 1)
        self.assertNotIn(upstream_secret, rendered)
        self.assertNotIn(IDENTITY_TOKEN, rendered)
        self.assertNotIn(API_KEY, rendered)
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(response.closed)

    def test_transport_error_is_stable_and_redacts_api_key(self):
        client = BankrIdentityClient(
            opener=QueueOpener(URLError(f"connection failed for {API_KEY}"))
        )

        with self.assertRaises(BankrLlmError) as raised:
            client.credits(api_key=API_KEY)

        self.assertEqual(raised.exception.code, "bankr_llm_unavailable")
        self.assertEqual(
            raised.exception.user_message,
            "Bankr LLM credits are unavailable. Please try again.",
        )
        self.assertNotIn(API_KEY, str(raised.exception))

    def test_top_up_posts_exact_payload_with_api_key(self):
        response = json_response(
            {"success": True, "credits": {"balanceUsd": "12.50"}}
        )
        opener = QueueOpener(response)
        client = BankrIdentityClient(
            api_url="https://bankr.example/",
            opener=opener,
            timeout=9,
        )

        result = client.top_up(
            api_key=API_KEY,
            amount_usd="10.00",
            source_token="0x2222222222222222222222222222222222222222",
        )

        request = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://bankr.example/llm/credits/topup",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("X-api-key"), API_KEY)
        self.assertEqual(
            request_json(request),
            {
                "amountUsd": 10,
                "chain": "base",
                "sourceToken": "0x2222222222222222222222222222222222222222",
            },
        )
        self.assertEqual(
            result,
            {"success": True, "credits": {"balanceUsd": "12.50"}},
        )
        self.assertEqual(opener.timeouts, [9])
        self.assertTrue(response.closed)

    def test_top_up_sends_amount_usd_as_json_number(self):
        for amount_text, expected in (("1", 1), ("10.00", 10), ("1.50", 1.5)):
            with self.subTest(amount=amount_text):
                response = json_response(
                    {"success": True, "credits": {"balanceUsd": "12.50"}}
                )
                opener = QueueOpener(response)
                client = BankrIdentityClient(opener=opener)

                client.top_up(
                    api_key=API_KEY,
                    amount_usd=amount_text,
                    source_token="0x2222222222222222222222222222222222222222",
                )

                sent = request_json(opener.requests[0])["amountUsd"]
                self.assertEqual(sent, expected)
                self.assertIsInstance(sent, type(expected))

    def test_top_up_rejects_invalid_amount_before_http(self):
        opener = QueueOpener()
        client = BankrIdentityClient(opener=opener)

        for bad_amount in ("", "NaN", "-1", "0", "ten"):
            with self.subTest(amount=bad_amount):
                with self.assertRaises(BankrLlmError) as raised:
                    client.top_up(
                        api_key=API_KEY,
                        amount_usd=bad_amount,
                        source_token="0x2222222222222222222222222222222222222222",
                    )
                self.assertEqual(raised.exception.code, "invalid_amount")
        self.assertEqual(opener.requests, [])

    def test_top_up_rejects_false_success_or_invalid_balance_as_ambiguous(self):
        cases = (
            {"success": False, "error": "private-no-credit"},
            {"success": True},
            {"success": True, "balanceUsd": False},
            {"success": True, "balanceUsd": "not-a-balance"},
            {"success": True, "balanceUsd": "NaN"},
            {"success": True, "balanceUsd": "-1"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                client = BankrIdentityClient(
                    opener=QueueOpener(json_response(payload))
                )

                with self.assertRaises(BankrLlmError) as raised:
                    client.top_up(
                        api_key=API_KEY,
                        amount_usd="10.00",
                        source_token="SINGIT",
                    )

                self.assertEqual(raised.exception.code, "bankr_topup_ambiguous")
                self.assertEqual(
                    raised.exception.user_message,
                    "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
                )
                self.assertNotIn("private-no-credit", str(raised.exception))

    def test_top_up_accepts_decimal_balance(self):
        for payload in (
            {"success": True, "balanceUsd": "12.50"},
            {"success": True, "credits": {"balanceUsd": 12.5}},
        ):
            with self.subTest(payload=payload):
                response = json_response(payload)
                client = BankrIdentityClient(opener=QueueOpener(response))

                result = client.top_up(
                    api_key=API_KEY,
                    amount_usd="10.00",
                    source_token="SINGIT",
                )

                self.assertEqual(result, payload)
                self.assertTrue(response.closed)

    def test_top_up_4xx_is_definitive_rejection(self):
        error = http_error(400, {"error": f"bad key {API_KEY}"})
        client = BankrIdentityClient(opener=QueueOpener(error))

        with self.assertRaises(BankrLlmError) as raised:
            client.top_up(
                api_key=API_KEY,
                amount_usd="10.00",
                source_token="SINGIT",
            )

        self.assertEqual(raised.exception.code, "bankr_topup_rejected")
        error_text = str(raised.exception)
        self.assertIn("Bankr rejected the LLM credit top-up.", error_text)
        self.assertIn("HTTP 400", error_text)
        self.assertIn("bad key bk_[redacted]", error_text)
        self.assertNotIn(API_KEY, error_text)
        self.assertTrue(error.tracked_body.closed)

    def test_top_up_5xx_or_transport_failure_is_ambiguous(self):
        cases = (
            http_error(503, {"error": f"maybe processed {API_KEY}"}),
            URLError(f"timeout after transfer {API_KEY}"),
        )
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                client = BankrIdentityClient(opener=QueueOpener(failure))

                with self.assertRaises(BankrLlmError) as raised:
                    client.top_up(
                        api_key=API_KEY,
                        amount_usd="10.00",
                        source_token="SINGIT",
                    )

                rendered = "".join(traceback.format_exception(raised.exception))
                self.assertEqual(raised.exception.code, "bankr_topup_ambiguous")
                self.assertEqual(
                    raised.exception.user_message,
                    "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
                )
                self.assertNotIn(API_KEY, rendered)

    def test_credits_gets_llm_gateway_balance_with_api_key(self):
        response = json_response(
            {"credits": "12.50", "currency": "USD", "usage": "1.25"}
        )
        opener = QueueOpener(response)
        client = BankrIdentityClient(
            llm_url="https://llm.example/",
            opener=opener,
        )

        result = client.credits(api_key=API_KEY)

        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://llm.example/v1/credits")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("X-api-key"), API_KEY)
        self.assertIsNone(request.data)
        self.assertEqual(
            result,
            {"credits": "12.50", "currency": "USD", "usage": "1.25"},
        )
        self.assertTrue(response.closed)

    def test_top_up_and_credits_reject_invalid_api_keys_without_http(self):
        for operation in (
            lambda client: client.top_up(
                api_key="private-key",
                amount_usd="10",
                source_token="SINGIT",
            ),
            lambda client: client.credits(api_key="private-key"),
        ):
            with self.subTest(operation=operation):
                opener = QueueOpener()
                client = BankrIdentityClient(opener=opener)
                with self.assertRaises(BankrLlmError) as raised:
                    operation(client)
                self.assertEqual(raised.exception.code, "invalid_api_key")
                self.assertEqual(
                    raised.exception.user_message,
                    "The Bankr API key is invalid.",
                )
                self.assertNotIn("private-key", str(raised.exception))
                self.assertEqual(opener.requests, [])


class BankrLlmStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode("ascii")
        self.path = Path(self.tempdir.name) / "nested" / "bankr-llm.db"
        self.store = BankrLlmStore(self.path, master_key=self.key)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_api_key_is_encrypted_at_rest(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=2000,
        )

        self.store.save_bankr_identity(
            purchase["purchaseId"],
            bankr_wallet_address=EVM_ADDRESS,
            api_key="bk_secret",
        )

        raw = self.path.read_bytes()
        loaded = self.store.get_active_purchase("123")
        self.assertNotIn(b"bk_secret", raw)
        self.assertEqual(self.store.decrypt_api_key(loaded), "bk_secret")
        self.assertEqual(
            loaded["apiKeyFingerprint"],
            hashlib.sha256(b"bk_secret").hexdigest()[:12],
        )

    def test_compare_and_set_rejects_duplicate_transfer_transition(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_IMESSAGE_APPROVAL",
            expires_at=2000,
        )

        first_store = BankrLlmStore(self.path, master_key=self.key)
        second_store = BankrLlmStore(self.path, master_key=self.key)

        self.assertTrue(
            first_store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
            )
        )
        self.assertFalse(
            second_store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
            )
        )
        loaded = self.store.get_purchase(purchase["purchaseId"])
        self.assertEqual(loaded["state"], "TRANSFERRING_SINGIT")
        self.assertEqual(
            self._audit_events(),
            ["purchase_created", "state_transition"],
        )

    def test_store_path_permissions_are_private(self):
        directory_mode = self.path.parent.stat().st_mode & 0o777
        db_mode = self.path.stat().st_mode & 0o777

        if os.name != "nt":
            self.assertEqual(directory_mode, 0o700)
            self.assertEqual(db_mode, 0o600)

    def test_store_fails_closed_when_permissions_cannot_be_hardened(self):
        if os.name == "nt":
            self.skipTest("POSIX permission hardening is not meaningful on Windows")

        with patch.object(Path, "chmod", side_effect=OSError("private fail")):
            with self.assertRaises(BankrLlmError) as raised:
                BankrLlmStore(
                    Path(self.tempdir.name) / "locked" / "bankr-llm.db",
                    master_key=self.key,
                )

        self.assertEqual(raised.exception.code, "invalid_configuration")
        self.assertEqual(
            raised.exception.user_message,
            "Bankr LLM store files must be private.",
        )

    def test_terms_acceptance_is_recorded_per_user(self):
        self.assertFalse(self.store.has_accepted_terms("123"))

        self.store.record_terms_acceptance("123", accepted_at=1700000000)

        self.assertTrue(self.store.has_accepted_terms("123"))
        self.assertFalse(self.store.has_accepted_terms("456"))
        self.assertEqual(self._audit_events(), ["terms_accepted"])

    def test_get_active_purchase_returns_latest_non_terminal_purchase(self):
        old = self.store.create_purchase(
            telegram_user_id="123",
            email="old@example.com",
            amount_usd="5",
            state="AWAITING_OTP",
            expires_at=1000,
        )
        self.assertTrue(
            self.store.transition(
                old["purchaseId"],
                expected_state="AWAITING_OTP",
                new_state="COMPLETED",
            )
        )
        active = self.store.create_purchase(
            telegram_user_id="123",
            email="new@example.com",
            amount_usd="10",
            state="AWAITING_TERMS",
            expires_at=2000,
        )

        loaded = self.store.get_active_purchase("123")

        self.assertEqual(loaded["purchaseId"], active["purchaseId"])
        self.assertEqual(loaded["email"], "new@example.com")

    def test_create_purchase_reuses_existing_active_purchase_across_store_instances(self):
        first = self.store.create_purchase(
            telegram_user_id="123",
            email="first@example.com",
            amount_usd="10",
            state="AWAITING_TERMS",
            expires_at=2000,
        )
        second_store = BankrLlmStore(self.path, master_key=self.key)

        second = second_store.create_purchase(
            telegram_user_id="123",
            email="second@example.com",
            amount_usd="20",
            state="AWAITING_TERMS",
            expires_at=3000,
        )

        self.assertEqual(second["purchaseId"], first["purchaseId"])
        self.assertEqual(second["email"], "first@example.com")
        self.assertEqual(
            second_store.get_active_purchase("123")["purchaseId"],
            first["purchaseId"],
        )

    def test_initialization_repairs_legacy_duplicate_active_purchases(self):
        with self._connect() as db:
            db.execute("DROP INDEX bankr_llm_one_active_purchase_per_user")
            db.execute(
                """
                INSERT INTO bankr_llm_purchases (
                    purchase_id, telegram_user_id, email, amount_usd, state,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("old", "123", "old@example.com", "5", "AWAITING_OTP", 2000, 1, 1),
            )
            db.execute(
                """
                INSERT INTO bankr_llm_purchases (
                    purchase_id, telegram_user_id, email, amount_usd, state,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("new", "123", "new@example.com", "10", "AWAITING_TERMS", 3000, 2, 2),
            )

        repaired_store = BankrLlmStore(self.path, master_key=self.key)

        active = repaired_store.get_active_purchase("123")
        old = repaired_store.get_purchase("old")
        self.assertEqual(active["purchaseId"], "new")
        self.assertEqual(old["state"], "EXPIRED")
        self.assertEqual(old["errorCode"], "duplicate_active_purchase")

    def test_get_active_purchase_ignores_spec_terminal_states(self):
        for state in (
            "COMPLETE",
            "REJECTED",
            "EXPIRED",
            "FAILED_BEFORE_TRANSFER",
            "RECONCILIATION_REQUIRED",
        ):
            self.store.create_purchase(
                telegram_user_id="123",
                email=f"{state.lower()}@example.com",
                amount_usd="10",
                state=state,
                expires_at=2000,
            )

        self.assertIsNone(self.store.get_active_purchase("123"))

    def test_safe_purchase_dict_never_exposes_ciphertext(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=2000,
        )
        self.store.save_bankr_identity(
            purchase["purchaseId"],
            bankr_wallet_address=EVM_ADDRESS,
            api_key="bk_secret",
        )

        loaded = self.store.get_purchase(purchase["purchaseId"])

        self.assertNotIn("encryptedApiKey", loaded)
        self.assertNotIn("encrypted_api_key", loaded)
        self.assertNotIn("bk_secret", repr(loaded))
        with self._connect() as db:
            encrypted_key = db.execute(
                """
                SELECT encrypted_api_key
                FROM bankr_llm_purchases
                WHERE purchase_id = ?
                """,
                (purchase["purchaseId"],),
            ).fetchone()["encrypted_api_key"]
        self.assertIsInstance(encrypted_key, str)
        self.assertNotEqual(encrypted_key, "bk_secret")

    def test_transition_updates_only_whitelisted_fields(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_IMESSAGE_APPROVAL",
            expires_at=2000,
        )

        self.assertTrue(
            self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
                fields={
                    "approvalRequestId": "approval-1",
                    "transferHash": "0xabc",
                },
            )
        )
        loaded = self.store.get_purchase(purchase["purchaseId"])

        self.assertEqual(loaded["approvalRequestId"], "approval-1")
        self.assertEqual(loaded["transferHash"], "0xabc")
        with self.assertRaises(ValueError):
            self.store.transition(
                purchase["purchaseId"],
                expected_state="TRANSFERRING_SINGIT",
                new_state="COMPLETED",
                fields={"encryptedApiKey": "plaintext"},
            )

    def test_failed_compare_and_set_does_not_write_audit(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=2000,
        )

        self.assertFalse(
            self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
            )
        )

        self.assertEqual(self._audit_events(), ["purchase_created"])

    def _audit_events(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_type FROM bankr_llm_audit ORDER BY id"
            ).fetchall()
        return [row["event_type"] for row in rows]

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


class FakeBankrForPurchase:
    def __init__(self):
        self.sent_otps = []
        self.created_key_count = 0
        self.topups = []
        self.credits_calls = []
        self.topup_error = None
        self.persistent_topup_error = None
        self.credits_result = {"credits": "10.00", "currency": "USD"}

    def send_otp(self, email):
        self.sent_otps.append(email)

    def verify_and_create_key(self, *, email, code, key_name, accept_terms):
        if code != "123456":
            raise BankrLlmError(
                "invalid_otp",
                "That verification code is invalid or expired.",
            )
        self.created_key_count += 1
        return {
            "evmAddress": EVM_ADDRESS,
            "apiKey": API_KEY,
            "key": {"id": "key-1", "name": key_name, "llmGatewayEnabled": True},
        }

    def top_up(self, *, api_key, amount_usd, source_token, chain="base"):
        self.topups.append(
            {
                "api_key": api_key,
                "amount_usd": amount_usd,
                "source_token": source_token,
                "chain": chain,
            }
        )
        if self.persistent_topup_error is not None:
            raise self.persistent_topup_error
        if self.topup_error is not None:
            error = self.topup_error
            self.topup_error = None
            raise error
        return {"success": True, "credits": {"balanceUsd": str(amount_usd)}}

    def credits(self, *, api_key):
        self.credits_calls.append({"api_key": api_key})
        return dict(self.credits_result)


class FakeWalletServiceForPurchase:
    def __init__(self):
        self.calls = []
        self.balance_calls = []
        self.decrypt_calls = []
        self.events = []
        self.private_key = "0xUSER_PRIVATE_KEY"
        self.result = {
            "ok": True,
            "wallet": {"address": "0x2222222222222222222222222222222222222222"},
        }
        self.balance_result = {
            "ok": True,
            "balanceUnavailable": False,
            "balances": {"SINGIT": "100"},
        }

    def wallet_status(self, telegram_user_id):
        self.calls.append(telegram_user_id)
        self.events.append("wallet_status")
        return self.result

    def wallet_balance(self, telegram_user_id):
        self.balance_calls.append(telegram_user_id)
        self.events.append("wallet_balance")
        return self.balance_result

    def decrypt_private_key_for_future_signing(self, telegram_user_id):
        self.decrypt_calls.append(telegram_user_id)
        self.events.append("decrypt")
        return self.private_key


class FakePricerForPurchase:
    def __init__(self):
        self.calls = []
        self.result = {
            "requiredSingit": "25",
            "requiredSingitAtomic": "25000000000000000000",
            "expectedUsdc": "11.00",
        }

    def price_for_usdc(self, amount_usd):
        self.calls.append(amount_usd)
        return dict(self.result)


class FakeApprovalServiceForPurchase:
    def __init__(self):
        self.calls = []
        self.result = {"ok": False, "status": "expired"}

    def request_hash_approval(
        self,
        *,
        telegram_user_id,
        action_type,
        commitment_hash,
        context_lines,
    ):
        self.calls.append(
            {
                "telegram_user_id": telegram_user_id,
                "action_type": action_type,
                "commitment_hash": commitment_hash,
                "context_lines": list(context_lines),
            }
        )
        return dict(self.result)


class FakeTransferForPurchase:
    def __init__(self):
        self.calls = []
        self.events = None
        self.result = {
            "ok": True,
            "txId": "0xTRANSFER",
            "transactionHash": "0xTRANSFER",
        }

    def transfer_token(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.events is not None:
            self.events.append("transfer")
        return dict(self.result)


class BankrLlmPurchaseServiceAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now_value = 1700000000
        self.store = BankrLlmStore(
            Path(self.tempdir.name) / "bankr-llm.db",
            master_key=Fernet.generate_key().decode("ascii"),
        )
        self.bankr = FakeBankrForPurchase()
        self.wallet = FakeWalletServiceForPurchase()
        self.pricer = FakePricerForPurchase()
        self.approval = FakeApprovalServiceForPurchase()
        self.enforced_spends = []
        self.recorded_spends = []
        self.service = BankrLlmPurchaseService(
            store=self.store,
            bankr=self.bankr,
            wallet_service=self.wallet,
            pricer=self.pricer,
            approval_service=self.approval,
            transfer_client=object(),
            enforce_spend=lambda user_id, metadata: self.enforced_spends.append(
                {"telegramUserId": user_id, "metadata": dict(metadata)}
            ),
            record_spend=lambda user_id, purchase, metadata: self.recorded_spends.append(
                {
                    "telegramUserId": user_id,
                    "purchase": dict(purchase),
                    "metadata": dict(metadata),
                }
            ),
            singit_token_address="0x3333333333333333333333333333333333333333",
            now=lambda: self.now_value,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_start_requires_terms_before_sending_otp(self):
        result = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.assertEqual(result["state"], "AWAITING_TERMS")
        self.assertEqual(self.bankr.sent_otps, [])
        self.assertIn("/llm_terms accept", result["telegramText"])

    def test_accept_terms_sends_otp(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        result = self.service.accept_terms("123")
        self.assertEqual(result["state"], "AWAITING_OTP")
        self.assertEqual(self.bankr.sent_otps, ["user@example.com"])

    def test_accept_terms_resends_otp_when_already_waiting_for_code(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.accept_terms("123")

        self.assertEqual(result["state"], "AWAITING_OTP")
        self.assertEqual(
            self.bankr.sent_otps,
            ["user@example.com", "user@example.com"],
        )

    def test_verify_creates_one_key_and_requests_approval(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        result = self.service.verify_otp(telegram_user_id="123", code="123456")
        repeated = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(repeated["purchaseId"], result["purchaseId"])
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(self.approval.calls[0]["action_type"], "sign402_bankr_llm")

    def test_verify_retry_after_pricing_failure_reuses_saved_bankr_key(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        should_fail = True

        def fail_once(user_id, metadata):
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise BankrLlmError("limit_exceeded", "Spend limit failed.")
            self.enforced_spends.append(
                {"telegramUserId": user_id, "metadata": dict(metadata)}
            )

        self.service.enforce_spend = fail_once
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        with self.assertRaises(BankrLlmError):
            self.service.verify_otp(telegram_user_id="123", code="123456")
        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(len(self.approval.calls), 1)

    def test_concurrent_verify_creates_only_one_bankr_key(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        original_verify = self.bankr.verify_and_create_key
        start = threading.Barrier(2)

        def slow_verify(**kwargs):
            time.sleep(0.05)
            return original_verify(**kwargs)

        self.bankr.verify_and_create_key = slow_verify
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        results = []
        errors = []

        def verify():
            try:
                start.wait(timeout=2)
                results.append(
                    self.service.verify_otp(
                        telegram_user_id="123",
                        code="123456",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=verify) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(len(self.approval.calls), 1)
        self.assertEqual(
            {result["purchaseId"] for result in results},
            {results[0]["purchaseId"]},
        )
        self.assertIn("REJECTED", {result["state"] for result in results})

    def test_ambiguous_key_creation_blocks_automatic_retry(self):
        def ambiguous_key_creation(**kwargs):
            self.bankr.created_key_count += 1
            raise BankrLlmError(
                "bankr_key_creation_ambiguous",
                "Bankr API key creation result is unclear.",
            )

        self.bankr.verify_and_create_key = ambiguous_key_creation
        started = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")
        repeated_start = self.service.start(
            telegram_user_id="123",
            email="other@example.com",
            amount_usd="20",
        )
        repeated_verify = self.service.verify_otp(
            telegram_user_id="123",
            code="123456",
        )

        self.assertEqual(result["state"], "BANKR_KEY_CREATION_UNCERTAIN")
        self.assertEqual(repeated_start["purchaseId"], started["purchaseId"])
        self.assertEqual(repeated_verify["purchaseId"], started["purchaseId"])
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(self.approval.calls, [])

    def test_rejected_key_creation_fails_before_transfer_and_allows_new_purchase(self):
        def rejected_key_creation(**kwargs):
            self.bankr.created_key_count += 1
            raise BankrLlmError(
                "bankr_key_creation_rejected",
                "Bankr rejected the API key creation request. HTTP 400: bad payload",
            )

        self.bankr.verify_and_create_key = rejected_key_creation
        started = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")
        restarted = self.service.start(
            telegram_user_id="123",
            email="other@example.com",
            amount_usd="20",
        )

        self.assertEqual(result["state"], "FAILED_BEFORE_TRANSFER")
        self.assertEqual(result["errorCode"], "bankr_key_creation_rejected")
        self.assertIn("HTTP 400: bad payload", result["errorMessage"])
        self.assertNotEqual(restarted["purchaseId"], started["purchaseId"])
        self.assertEqual(restarted["state"], "AWAITING_OTP")
        self.assertEqual(self.bankr.sent_otps[-1], "other@example.com")
        self.assertEqual(self.approval.calls, [])

    def test_malformed_key_creation_response_enters_uncertain_state(self):
        def malformed_key_creation(**kwargs):
            self.bankr.created_key_count += 1
            return {"evmAddress": "not-an-address", "apiKey": API_KEY}

        self.bankr.verify_and_create_key = malformed_key_creation
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "BANKR_KEY_CREATION_UNCERTAIN")
        self.assertEqual(result["errorCode"], "wallet_unavailable")
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(self.approval.calls, [])

    def test_wallet_unavailable_after_saved_key_does_not_silently_succeed(self):
        should_fail = True

        def fail_once(user_id, metadata):
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise BankrLlmError("limit_exceeded", "Spend limit failed.")

        self.service.enforce_spend = fail_once
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        with self.assertRaises(BankrLlmError):
            self.service.verify_otp(telegram_user_id="123", code="123456")
        self.wallet.result = {
            "ok": False,
            "telegramText": "Wallet provider unavailable.",
        }

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "BANKR_KEY_CREATED")
        self.assertEqual(result["errorCode"], "wallet_unavailable")
        self.assertIn("Wallet provider unavailable", result["errorMessage"])
        self.assertIn("Wallet provider unavailable", result["telegramText"])
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(self.approval.calls, [])

    def test_wallet_recovery_after_saved_key_clears_stale_error(self):
        should_fail = True

        def fail_once(user_id, metadata):
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise BankrLlmError("limit_exceeded", "Spend limit failed.")
            self.enforced_spends.append(
                {"telegramUserId": user_id, "metadata": dict(metadata)}
            )

        self.service.enforce_spend = fail_once
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        with self.assertRaises(BankrLlmError):
            self.service.verify_otp(telegram_user_id="123", code="123456")
        self.wallet.result = {
            "ok": False,
            "telegramText": "Wallet provider unavailable.",
        }
        unavailable = self.service.verify_otp(
            telegram_user_id="123",
            code="123456",
        )
        self.wallet.result = {
            "ok": True,
            "wallet": {"address": "0x2222222222222222222222222222222222222222"},
        }
        self.approval.result = {"ok": True, "status": "approved"}

        recovered = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertFalse(unavailable["ok"])
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["state"], "AWAITING_TRANSFER")
        self.assertNotIn("errorCode", recovered)
        self.assertIn("Approved", recovered["telegramText"])
        self.assertEqual(self.bankr.created_key_count, 1)

    def test_cas_failure_after_saving_key_stops_before_spend_checks(self):
        original_save = self.store.save_bankr_identity

        def save_and_expire(purchase_id, *, bankr_wallet_address, api_key):
            original_save(
                purchase_id,
                bankr_wallet_address=bankr_wallet_address,
                api_key=api_key,
            )
            self.store.transition(
                purchase_id,
                expected_state="CREATING_BANKR_KEY",
                new_state="EXPIRED",
                fields={
                    "errorCode": "operator_expired",
                    "errorMessage": "Expired by operator.",
                },
            )

        self.store.save_bankr_identity = save_and_expire
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "EXPIRED")
        self.assertEqual(self.enforced_spends, [])
        self.assertEqual(self.approval.calls, [])

    def test_verify_uses_canonical_commitment_and_safe_approval_context(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        started = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10.50",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        loaded = self.store.get_purchase(started["purchaseId"])
        commitment = {
            "purchaseId": started["purchaseId"],
            "amountUsd": "10.50",
            "singitAmountAtomic": "25000000000000000000",
            "sourceWalletAddress": "0x2222222222222222222222222222222222222222",
            "bankrWalletAddress": EVM_ADDRESS,
            "apiKeyFingerprint": hashlib.sha256(API_KEY.encode("utf-8")).hexdigest()[
                :12
            ],
            "expiresAt": started["expiresAt"],
        }
        canonical = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        self.assertEqual(result["commitmentHash"], expected_hash)
        self.assertEqual(loaded["commitmentHash"], expected_hash)
        self.assertEqual(self.approval.calls[0]["commitment_hash"], expected_hash)
        rendered_context = "\n".join(self.approval.calls[0]["context_lines"])
        self.assertIn(expected_hash, rendered_context)
        self.assertIn("10.50", rendered_context)
        self.assertNotIn(API_KEY, rendered_context)
        self.assertNotIn("bk_", repr(result))

    def test_invalid_otp_expires_after_three_attempts_without_creating_key(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        first = self.service.verify_otp(telegram_user_id="123", code="000000")
        second = self.service.verify_otp(telegram_user_id="123", code="000000")
        third = self.service.verify_otp(telegram_user_id="123", code="000000")

        self.assertEqual(first["state"], "AWAITING_OTP")
        self.assertEqual(second["state"], "AWAITING_OTP")
        self.assertEqual(third["state"], "EXPIRED")
        self.assertEqual(self.bankr.created_key_count, 0)

    def test_otp_expiration_blocks_verification(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        self.now_value += 601

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "EXPIRED")
        self.assertEqual(self.bankr.created_key_count, 0)

    def test_start_validates_amount_and_reuses_active_purchase(self):
        for amount in ("0.99", "1000.01", "NaN", "10.001"):
            with self.subTest(amount=amount):
                with self.assertRaises(BankrLlmError) as raised:
                    self.service.start(
                        telegram_user_id="123",
                        email="user@example.com",
                        amount_usd=amount,
                    )
                self.assertEqual(raised.exception.code, "invalid_amount")

        first = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        second = self.service.start(
            telegram_user_id="123",
            email="other@example.com",
            amount_usd="20",
        )

        self.assertEqual(second["purchaseId"], first["purchaseId"])
        self.assertEqual(second["state"], "AWAITING_TERMS")
        self.assertEqual(self.bankr.sent_otps, [])

    def test_approved_hash_moves_to_awaiting_transfer_without_revealing_key(self):
        self.approval.result = {"ok": True, "status": "approved"}
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")

        result = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "AWAITING_TRANSFER")
        self.assertIn("approved", result["telegramText"].lower())
        self.assertNotIn(API_KEY, repr(result))
        self.assertNotIn("bk_", repr(result))
        self.assertEqual(self.recorded_spends, [])

    def test_credits_reads_stored_key_without_returning_it(self):
        self.approval.result = {"ok": True, "status": "approved"}
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        self.service.verify_otp(telegram_user_id="123", code="123456")

        result = self.service.credits("123")

        self.assertEqual(result["state"], "AWAITING_TRANSFER")
        self.assertEqual(result["credits"], {"credits": "10.00", "currency": "USD"})
        self.assertNotIn(API_KEY, repr(result))


class BankrLlmPurchaseFactoryTests(unittest.TestCase):
    def test_factory_builds_service_from_environment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store_path = Path(tempdir) / "bankr-llm.db"
            wallet = FakeWalletServiceForPurchase()
            pricer = FakePricerForPurchase()
            approval = FakeApprovalServiceForPurchase()
            transfer = FakeTransferForPurchase()

            service = build_bankr_llm_purchase_service_from_env(
                env={
                    "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(
                        "ascii"
                    ),
                    "SIGN402_BANKR_API_URL": "https://api.example.test",
                    "SIGN402_BANKR_LLM_URL": "https://llm.example.test",
                    "SIGN402_BANKR_LLM_STORE_PATH": str(store_path),
                    "SIGN402_BANKR_HTTP_TIMEOUT_SECONDS": "12",
                    "SIGN402_BANKR_OTP_TTL_SECONDS": "420",
                    "SIGN402_BANKR_MAX_OTP_ATTEMPTS": "4",
                    "SIGN402_SINGIT_TOKEN_ADDRESS": (
                        "0x3333333333333333333333333333333333333333"
                    ),
                    "SIGN402_BANKR_TOPUP_SOURCE_TOKEN": (
                        "0xc2c1e0b7C401e6217193732272444D928646eba3"
                    ),
                    "SIGN402_BANKR_TOPUP_ATTEMPTS": "2",
                    "SIGN402_BANKR_TOPUP_RETRY_DELAY_SECONDS": "7.5",
                },
                wallet_service=wallet,
                pricer=pricer,
                approval_service=approval,
                transfer_client=transfer,
                enforce_spend=lambda _user_id, _requirement: None,
                record_spend=lambda _user_id, _purchase, _metadata: None,
            )

            self.assertIsInstance(service, BankrLlmPurchaseService)
            self.assertEqual(service.store.path, store_path)
            self.assertEqual(service.bankr.api_url, "https://api.example.test")
            self.assertEqual(service.bankr.llm_url, "https://llm.example.test")
            self.assertEqual(service.bankr.timeout, 12.0)
            self.assertEqual(service.otp_ttl_seconds, 420)
            self.assertEqual(service.max_otp_attempts, 4)
            self.assertEqual(
                service.topup_source_token,
                "0xc2c1e0b7C401e6217193732272444D928646eba3",
            )
            self.assertEqual(service.topup_attempts, 2)
            self.assertEqual(service.topup_retry_delay_seconds, 7.5)
            self.assertIs(service.wallet_service, wallet)
            self.assertIs(service.pricer, pricer)
            self.assertIs(service.transfer_client, transfer)

    def test_factory_defaults_topup_source_token_to_singit_address(self):
        with tempfile.TemporaryDirectory() as tempdir:
            service = build_bankr_llm_purchase_service_from_env(
                env={
                    "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(
                        "ascii"
                    ),
                    "SIGN402_BANKR_LLM_STORE_PATH": str(
                        Path(tempdir) / "bankr-llm.db"
                    ),
                    "SIGN402_SINGIT_TOKEN_ADDRESS": (
                        "0x3333333333333333333333333333333333333333"
                    ),
                },
                wallet_service=FakeWalletServiceForPurchase(),
                pricer=FakePricerForPurchase(),
                approval_service=FakeApprovalServiceForPurchase(),
                transfer_client=FakeTransferForPurchase(),
                enforce_spend=lambda _user_id, _requirement: None,
                record_spend=lambda _user_id, _purchase, _metadata: None,
            )

        self.assertEqual(
            service.topup_source_token,
            "0x3333333333333333333333333333333333333333",
        )

    def test_factory_requires_master_key_and_pricer(self):
        dependencies = {
            "wallet_service": FakeWalletServiceForPurchase(),
            "approval_service": FakeApprovalServiceForPurchase(),
            "transfer_client": FakeTransferForPurchase(),
            "enforce_spend": lambda _user_id, _requirement: None,
            "record_spend": lambda _user_id, _purchase, _metadata: None,
        }

        with self.assertRaises(BankrLlmError) as missing_key:
            build_bankr_llm_purchase_service_from_env(
                env={},
                pricer=FakePricerForPurchase(),
                **dependencies,
            )
        with self.assertRaises(BankrLlmError) as missing_pricer:
            build_bankr_llm_purchase_service_from_env(
                env={
                    "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(
                        "ascii"
                    )
                },
                pricer=None,
                **dependencies,
            )
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises(BankrLlmError) as invalid_timeout:
                build_bankr_llm_purchase_service_from_env(
                    env={
                        "SIGN402_WALLET_MASTER_KEY": Fernet.generate_key().decode(
                            "ascii"
                        ),
                        "SIGN402_BANKR_LLM_STORE_PATH": str(
                            Path(tempdir) / "bankr-llm.db"
                        ),
                        "SIGN402_BANKR_HTTP_TIMEOUT_SECONDS": "NaN",
                    },
                    pricer=FakePricerForPurchase(),
                    **dependencies,
                )

        self.assertEqual(missing_key.exception.code, "invalid_configuration")
        self.assertEqual(missing_pricer.exception.code, "invalid_configuration")
        self.assertEqual(invalid_timeout.exception.code, "invalid_configuration")


class BankrLlmPurchasePaymentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now_value = 1700000000
        self.store = BankrLlmStore(
            Path(self.tempdir.name) / "bankr-llm.db",
            master_key=Fernet.generate_key().decode("ascii"),
        )
        self.bankr = FakeBankrForPurchase()
        self.wallet = FakeWalletServiceForPurchase()
        self.pricer = FakePricerForPurchase()
        self.approval = FakeApprovalServiceForPurchase()
        self.approval.result = {"ok": True, "status": "approved"}
        self.transfer = FakeTransferForPurchase()
        self.transfer.events = self.wallet.events
        self.enforced_spends = []
        self.recorded_spends = []
        self.sleeps = []
        self.service = BankrLlmPurchaseService(
            store=self.store,
            bankr=self.bankr,
            wallet_service=self.wallet,
            pricer=self.pricer,
            approval_service=self.approval,
            transfer_client=self.transfer,
            enforce_spend=self._enforce_spend,
            record_spend=lambda user_id, purchase, metadata: self.recorded_spends.append(
                {
                    "telegramUserId": user_id,
                    "purchase": dict(purchase),
                    "metadata": dict(metadata),
                }
            ),
            singit_token_address="0x3333333333333333333333333333333333333333",
            now=lambda: self.now_value,
            sleep=self.sleeps.append,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_approved_purchase_transfers_user_singit_then_tops_up(self):
        result = self.complete_purchase()

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["apiKey"], API_KEY)
        self.assertEqual(self.transfer.calls[0]["private_key"], "0xUSER_PRIVATE_KEY")
        self.assertEqual(self.transfer.calls[0]["to_address"], EVM_ADDRESS)
        self.assertEqual(
            self.transfer.calls[0]["token_address"],
            "0x3333333333333333333333333333333333333333",
        )
        self.assertEqual(
            self.transfer.calls[0]["amount"],
            "25",
        )
        self.assertEqual(self.bankr.topups[0]["api_key"], API_KEY)
        self.assertEqual(self.bankr.topups[0]["amount_usd"], "10")
        self.assertEqual(self.bankr.topups[0]["source_token"], "SINGIT")
        self.assertEqual(len(self.recorded_spends), 1)
        self.assertEqual(self.recorded_spends[0]["telegramUserId"], "123")
        self.assertIn("10", result["telegramText"])

    def test_repeated_completion_does_not_transfer_twice_or_reveal_key(self):
        first = self.complete_purchase()
        second = self.service.resume(first["purchaseId"])

        self.assertEqual(first["state"], "COMPLETE")
        self.assertEqual(second["state"], "COMPLETE")
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 1)
        self.assertNotIn("apiKey", second)

    def test_topup_timeout_after_transfer_requires_reconciliation_without_second_transfer(self):
        self.bankr.topup_error = BankrLlmError(
            "bankr_topup_ambiguous",
            "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
        )

        result = self.complete_purchase()
        resumed = self.service.resume(result["purchaseId"])

        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(resumed["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 1)
        loaded = self.store.get_purchase(result["purchaseId"])
        self.assertEqual(loaded["transferHash"], "0xTRANSFER")
        self.assertNotIn("apiKey", result)

    def test_reconciliation_marks_complete_when_expected_credits_are_present(self):
        result = self.complete_purchase(reconcile_required=True)
        # Baseline at key creation was 10.00; the purchase adds another 10.
        self.bankr.credits_result = {"credits": "20.00", "currency": "USD"}

        reconciled = self.service.reconcile(result["purchaseId"])

        self.assertEqual(reconciled["state"], "COMPLETE")
        self.assertEqual(self.bankr.credits_calls[0]["api_key"], API_KEY)
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 1)
        self.assertEqual(reconciled["apiKey"], API_KEY)
        self.assertNotIn("apiKey", self.service.resume(result["purchaseId"]))

    def test_key_creation_snapshots_baseline_credits(self):
        result = self.complete_purchase()

        loaded = self.store.get_purchase(result["purchaseId"])
        self.assertEqual(loaded["baselineCreditsUsd"], "10.00")

    def test_reconciliation_ignores_preexisting_credits_balance(self):
        result = self.complete_purchase(reconcile_required=True)
        # Balance is unchanged since key creation, so the purchase was never
        # credited even though the absolute balance covers the amount.
        self.bankr.credits_result = {"credits": "10.00", "currency": "USD"}

        reconciled = self.service.reconcile(result["purchaseId"])

        self.assertEqual(reconciled["state"], "COMPLETE")
        self.assertEqual(len(self.bankr.topups), 2)

    def test_reconciliation_without_baseline_uses_absolute_balance(self):
        result = self.complete_purchase(reconcile_required=True)
        self.store.transition(
            result["purchaseId"],
            expected_state="RECONCILIATION_REQUIRED",
            new_state="RECONCILIATION_REQUIRED",
            fields={"baselineCreditsUsd": ""},
        )
        self.bankr.credits_result = {"credits": "10.00", "currency": "USD"}

        reconciled = self.service.reconcile(result["purchaseId"])

        self.assertEqual(reconciled["state"], "COMPLETE")
        self.assertEqual(len(self.bankr.topups), 1)

    def test_topup_rejection_is_retried_with_backoff_before_reconciliation(self):
        self.bankr.topup_error = BankrLlmError(
            "bankr_topup_rejected",
            "Bankr rejected the LLM credit top-up. HTTP 400: transient",
        )

        result = self.complete_purchase()

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 2)
        self.assertEqual(self.sleeps, [5.0])

    def test_topup_rejection_exhausts_retries_then_requires_reconciliation(self):
        self.bankr.persistent_topup_error = BankrLlmError(
            "bankr_topup_rejected",
            "Bankr rejected the LLM credit top-up. HTTP 400: unsupported token",
        )

        result = self.complete_purchase()

        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(result["errorCode"], "bankr_topup_rejected")
        self.assertIn("unsupported token", result["errorMessage"])
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 3)
        self.assertEqual(self.sleeps, [5.0, 10.0])

    def test_ambiguous_topup_error_is_never_retried(self):
        result = self.complete_purchase(reconcile_required=True)

        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(len(self.bankr.topups), 1)
        self.assertEqual(self.sleeps, [])

    def test_topup_uses_configured_source_token(self):
        self.service.topup_source_token = (
            "0xc2c1e0b7C401e6217193732272444D928646eba3"
        )

        result = self.complete_purchase()

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(
            self.bankr.topups[0]["source_token"],
            "0xc2c1e0b7C401e6217193732272444D928646eba3",
        )

    def test_reconcile_rejects_purchase_owned_by_another_user(self):
        result = self.complete_purchase(reconcile_required=True)
        credits_calls_before = len(self.bankr.credits_calls)

        with self.assertRaises(BankrLlmError) as raised:
            self.service.reconcile(
                result["purchaseId"],
                telegram_user_id="999",
            )

        self.assertEqual(raised.exception.code, "purchase_not_found")
        self.assertEqual(len(self.bankr.credits_calls), credits_calls_before)

    def test_reconcile_accepts_matching_owner(self):
        result = self.complete_purchase(reconcile_required=True)

        reconciled = self.service.reconcile(
            result["purchaseId"],
            telegram_user_id="123",
        )

        self.assertEqual(reconciled["state"], "COMPLETE")
        self.assertEqual(reconciled["apiKey"], API_KEY)

    def test_reconciliation_retries_only_bankr_topup_when_credits_are_missing(self):
        result = self.complete_purchase(reconcile_required=True)
        self.bankr.credits_result = {"credits": "0.00", "currency": "USD"}

        reconciled = self.service.reconcile(result["purchaseId"])

        self.assertEqual(reconciled["state"], "COMPLETE")
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(len(self.bankr.topups), 2)
        self.assertEqual(self.bankr.topups[1]["api_key"], API_KEY)
        self.assertEqual(self.bankr.topups[1]["source_token"], "SINGIT")
        self.assertEqual(len(self.recorded_spends), 1)
        self.assertEqual(reconciled["apiKey"], API_KEY)
        self.assertNotIn("apiKey", self.service.resume(result["purchaseId"]))

    def test_resume_reprices_before_transfer_and_rejects_above_approved_max(self):
        awaiting = self.approved_purchase()
        self.pricer.result = {
            "requiredSingit": "26",
            "requiredSingitAtomic": "26000000000000000000",
            "expectedUsdc": "11.00",
        }

        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "FAILED_BEFORE_TRANSFER")
        self.assertEqual(result["errorCode"], "price_exceeds_approved_max")
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.wallet.decrypt_calls, [])

    def test_resume_reruns_limits_and_balance_immediately_before_transfer(self):
        awaiting = self.approved_purchase()

        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(len(self.enforced_spends), 2)
        for enforced in self.enforced_spends:
            self.assertEqual(enforced["metadata"]["amountAtomic"], "10000000")
            self.assertEqual(
                enforced["metadata"]["asset"],
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            )
            self.assertEqual(enforced["metadata"]["network"], "base-mainnet")
            self.assertEqual(
                enforced["metadata"]["singitAmountAtomic"],
                "25000000000000000000",
            )
        self.assertEqual(
            self.recorded_spends[0]["metadata"]["amountAtomic"],
            "10000000",
        )
        self.assertEqual(
            self.recorded_spends[0]["metadata"]["asset"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        self.assertEqual(
            self.recorded_spends[0]["metadata"]["network"],
            "base-mainnet",
        )
        self.assertEqual(self.wallet.balance_calls, ["123"])
        self.assertLess(
            self.wallet.events.index("wallet_balance"),
            self.wallet.events.index("decrypt"),
        )
        self.assertLess(
            self.wallet.events.index("decrypt"),
            self.wallet.events.index("transfer"),
        )

    def test_resume_holds_when_managed_wallet_balance_is_too_low(self):
        awaiting = self.approved_purchase()
        self.wallet.balance_result = {
            "ok": True,
            "balanceUnavailable": False,
            "balances": {"SINGIT": "24.99"},
        }

        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "FAILED_BEFORE_TRANSFER")
        self.assertEqual(result["errorCode"], "insufficient_singit_balance")
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.wallet.decrypt_calls, [])

    def test_resume_transferring_without_persisted_hash_never_retransfers(self):
        awaiting = self.approved_purchase()
        transitioned = self.store.transition(
            awaiting["purchaseId"],
            expected_state="AWAITING_TRANSFER",
            new_state="TRANSFERRING_SINGIT",
        )

        result = self.service.resume(awaiting["purchaseId"])

        self.assertTrue(transitioned)
        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(result["errorCode"], "transfer_ambiguous")
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.bankr.topups, [])

    def test_generic_pre_transfer_failure_does_not_require_reconciliation(self):
        awaiting = self.approved_purchase()

        def fail_before_transfer(_user_id, _metadata):
            raise RuntimeError("local limit store failed")

        self.service.enforce_spend = fail_before_transfer

        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "FAILED_BEFORE_TRANSFER")
        self.assertEqual(result["errorCode"], "pre_transfer_failed")
        self.assertEqual(self.transfer.calls, [])
        self.assertEqual(self.wallet.decrypt_calls, [])
        self.assertNotIn("local limit store failed", repr(result))

    def test_failed_transfer_hash_persistence_never_starts_topup(self):
        awaiting = self.approved_purchase()
        original_transition = self.store.transition
        refused_once = False

        def refuse_transfer_checkpoint(
            purchase_id,
            *,
            expected_state,
            new_state,
            fields=None,
        ):
            nonlocal refused_once
            if (
                not refused_once
                and expected_state == "TRANSFERRING_SINGIT"
                and new_state == "TOPPING_UP_BANKR"
            ):
                refused_once = True
                return False
            return original_transition(
                purchase_id,
                expected_state=expected_state,
                new_state=new_state,
                fields=fields,
            )

        self.store.transition = refuse_transfer_checkpoint

        result = self.service.resume(awaiting["purchaseId"])

        self.assertTrue(refused_once)
        self.assertEqual(len(self.transfer.calls), 1)
        self.assertEqual(self.bankr.topups, [])
        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(result["transferHash"], "0xTRANSFER")

    def complete_purchase(self, *, reconcile_required=False):
        awaiting = self.approved_purchase()
        if reconcile_required:
            self.bankr.topup_error = BankrLlmError(
                "bankr_topup_ambiguous",
                "Bankr LLM credit top-up result is unclear. Do not retry automatically.",
            )
        return self.service.resume(awaiting["purchaseId"])

    def approved_purchase(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        return self.service.verify_otp(telegram_user_id="123", code="123456")

    def _enforce_spend(self, user_id, metadata):
        self.enforced_spends.append(
            {"telegramUserId": user_id, "metadata": dict(metadata)}
        )
        self.wallet.events.append("enforce_spend")


if __name__ == "__main__":
    unittest.main()
