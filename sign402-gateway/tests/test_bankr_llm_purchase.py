import hashlib
import io
import json
import os
import sqlite3
import tempfile
import traceback
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet

from sign402_gateway.bankr_llm_purchase import (
    BankrIdentityClient,
    BankrLlmError,
    BankrLlmStore,
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
                    "allowedRecipients": [],
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
                "allowedRecipients": [],
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
                        "allowedRecipients": [],
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
                "amountUsd": "10.00",
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
        self.assertEqual(
            raised.exception.user_message,
            "Bankr rejected the LLM credit top-up.",
        )
        self.assertNotIn(API_KEY, str(raised.exception))
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

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=5.0)
        db.row_factory = sqlite3.Row
        return db


if __name__ == "__main__":
    unittest.main()
