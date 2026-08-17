"""Route tests for /agent/chat/*.

These exercise the handler directly, the way test_gateway_server.py does, so
nothing here binds a socket, calls Venice, or settles anything.
"""

import io
import json
import logging
import os
import unittest
from unittest.mock import Mock, patch

from sign402_gateway.server import Sign402GatewayHandler
from sign402_gateway.venice_chat import (
    ChatResult,
    MerchantChanged,
    WindowExhausted,
)


WALLET_TOKEN = "test-wallet-token"
USER_TOKEN = "test-user-token"
USER_ID = "1045618308"


class FakeSocket:
    def __init__(self, request: bytes):
        self.rfile = io.BytesIO(request)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)

    def close(self) -> None:
        pass


class ChatDummyServer:
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
        self.user_wallet_api_token = WALLET_TOKEN
        self.bankr_llm_purchase_service = Mock()
        self.imessage_approval_service = Mock()
        self.imessage_approval_api_token = "test-photon-token"
        self.user_x402_buyer = Mock()
        self.user_token_transfer_client = Mock()
        self.user_spend_limit_store = Mock()
        self.chat_service = Mock()


class ChatEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        # Routes are behind the flag; individual tests turn it off again.
        self.enable_flag("1")

    def enable_flag(self, value: str) -> None:
        patcher = patch.dict(os.environ, {"SIGN402_AI_CHAT_ENABLED": value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_handler(
        self,
        path: str,
        body: dict | None = None,
        *,
        method: str = "POST",
        server=None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {
            "Authorization": f"Bearer {WALLET_TOKEN}",
            "X-Sign402-User-Token": USER_TOKEN,
        }
        if headers is not None:
            request_headers = dict(headers)

        encoded = b"" if body is None else json.dumps(body).encode("utf-8")
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
        handler = Sign402GatewayHandler(
            socket, ("127.0.0.1", 12345), server or ChatDummyServer()
        )
        handler.response = socket.wfile
        return handler

    def response_text(self, handler) -> str:
        return handler.response.getvalue().decode("utf-8", "replace")

    def response_json(self, handler) -> dict:
        _, body = self.response_text(handler).split("\r\n\r\n", 1)
        return json.loads(body)

    def status_of(self, handler) -> int:
        return int(self.response_text(handler).split(" ", 2)[1])

    def authenticated(self, server):
        patcher = patch(
            "sign402_gateway.server._require_authenticated_user",
            return_value=USER_ID,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return server


class FlagOffTests(ChatEndpointTestCase):
    def test_every_chat_route_is_404_when_the_flag_is_unset(self):
        self.enable_flag("")
        for path in (
            "/agent/chat/start",
            "/agent/chat/message",
            "/agent/chat/end",
        ):
            with self.subTest(path=path):
                handler = self.make_handler(path, {"telegramUserId": USER_ID})
                self.assertEqual(self.status_of(handler), 404)
                self.assertEqual(self.response_json(handler)["error"], "not_found")

    def test_flag_off_does_not_touch_the_chat_service(self):
        self.enable_flag("")
        server = ChatDummyServer()
        self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "hi"},
            server=server,
        )
        server.chat_service.send.assert_not_called()

    def test_health_omits_chat_routes_when_the_flag_is_unset(self):
        self.enable_flag("")
        handler = self.make_handler("/health", method="GET")
        endpoints = self.response_json(handler)["endpoints"]
        self.assertNotIn("/agent/chat/start", endpoints)

    def test_health_lists_chat_routes_when_the_flag_is_set(self):
        handler = self.make_handler("/health", method="GET")
        endpoints = self.response_json(handler)["endpoints"]
        for path in (
            "/agent/chat/start",
            "/agent/chat/message",
            "/agent/chat/end",
        ):
            self.assertIn(path, endpoints)


class AuthTests(ChatEndpointTestCase):
    def test_missing_bearer_is_401(self):
        handler = self.make_handler(
            "/agent/chat/start", {"telegramUserId": USER_ID}, headers={}
        )
        self.assertEqual(self.status_of(handler), 401)

    def test_wrong_bearer_is_401(self):
        handler = self.make_handler(
            "/agent/chat/start",
            {"telegramUserId": USER_ID},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(self.status_of(handler), 401)

    def test_missing_bearer_never_reaches_the_chat_service(self):
        server = ChatDummyServer()
        self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "hi"},
            server=server,
            headers={},
        )
        server.chat_service.send.assert_not_called()


class StartTests(ChatEndpointTestCase):
    def test_start_reports_free_messages_policy_and_cap(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.start.return_value = {
            "ok": True,
            "freeMessagesRemaining": 5,
            "hasPolicy": False,
            "dailyCapAtomic": 5_000_000,
            "dailyCapUsdc": "5.00",
        }
        handler = self.make_handler(
            "/agent/chat/start", {"telegramUserId": USER_ID}, server=server
        )
        body = self.response_json(handler)
        self.assertEqual(self.status_of(handler), 200)
        self.assertEqual(body["freeMessagesRemaining"], 5)
        self.assertFalse(body["hasPolicy"])
        self.assertEqual(body["dailyCapUsdc"], "5.00")


class MessageTests(ChatEndpointTestCase):
    def make_result(self, **kwargs):
        defaults = dict(
            text="an answer",
            cost_atomic=3_000,
            prefunded=False,
            remaining_window_atomic=4_997_000,
            outstanding_atomic=4_997_000,
        )
        defaults.update(kwargs)
        return ChatResult(**defaults)

    def test_message_returns_answer_cost_and_remaining_window(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send.return_value = self.make_result()
        handler = self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "hi"},
            server=server,
        )
        body = self.response_json(handler)
        self.assertEqual(self.status_of(handler), 200)
        self.assertEqual(body["text"], "an answer")
        self.assertEqual(body["costAtomic"], 3_000)
        self.assertEqual(body["remainingWindowAtomic"], 4_997_000)

    def test_window_exhausted_is_a_clean_refusal_not_a_crash(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send.side_effect = WindowExhausted(
            "Today's budget is spent. It resets at 00:00 UTC."
        )
        handler = self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "hi"},
            server=server,
        )
        body = self.response_json(handler)
        self.assertFalse(body["ok"])
        self.assertEqual(body["state"], "WINDOW_EXHAUSTED")
        self.assertIn("00:00 UTC", body["telegramText"])

    def test_merchant_changed_is_surfaced_with_its_state(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send.side_effect = MerchantChanged(
            "Venice AI changed its payout details."
        )
        handler = self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "hi"},
            server=server,
        )
        self.assertEqual(self.response_json(handler)["state"], "MERCHANT_CHANGED")

    def test_global_pause_refuses_paid_messages(self):
        server = self.authenticated(ChatDummyServer())
        with patch.dict(os.environ, {"SIGN402_PURCHASES_PAUSED": "1"}):
            handler = self.make_handler(
                "/agent/chat/message",
                {"telegramUserId": USER_ID, "text": "hi"},
                server=server,
            )
        self.assertEqual(self.status_of(handler), 503)
        server.chat_service.send.assert_not_called()

    def test_global_pause_still_serves_a_free_message(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send_free.return_value = self.make_result(
            cost_atomic=0
        )
        with patch.dict(os.environ, {"SIGN402_PURCHASES_PAUSED": "1"}):
            handler = self.make_handler(
                "/agent/chat/message",
                {"telegramUserId": USER_ID, "text": "hi", "free": True},
                server=server,
            )
        self.assertEqual(self.status_of(handler), 200)
        self.assertEqual(self.response_json(handler)["costAtomic"], 0)


class PrivacyTests(ChatEndpointTestCase):
    def test_prompt_text_and_answer_never_reach_the_logs(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send.return_value = ChatResult(
            text="the model said something private",
            cost_atomic=3_000,
            prefunded=False,
            remaining_window_atomic=0,
            outstanding_atomic=0,
        )
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger().debug("probe")  # keep assertLogs happy
            self.make_handler(
                "/agent/chat/message",
                {"telegramUserId": USER_ID, "text": "my secret prompt"},
                server=server,
            )
        logged = "\n".join(captured.output)
        self.assertNotIn("my secret prompt", logged)
        self.assertNotIn("the model said something private", logged)

    def test_an_unexpected_error_does_not_echo_the_prompt(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.send.side_effect = RuntimeError(
            "boom while handling my secret prompt"
        )
        handler = self.make_handler(
            "/agent/chat/message",
            {"telegramUserId": USER_ID, "text": "my secret prompt"},
            server=server,
        )
        self.assertNotIn("my secret prompt", self.response_text(handler))


class EndTests(ChatEndpointTestCase):
    def test_end_clears_mode(self):
        server = self.authenticated(ChatDummyServer())
        server.chat_service.end.return_value = {"ok": True}
        handler = self.make_handler(
            "/agent/chat/end", {"telegramUserId": USER_ID}, server=server
        )
        self.assertEqual(self.status_of(handler), 200)
        server.chat_service.end.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class ApprovePolicyRouteTests(ChatEndpointTestCase):
    def server_with_policy(self, result=None, error=None):
        server = self.authenticated(ChatDummyServer())
        server.chat_policy_service = Mock()
        if error is not None:
            server.chat_policy_service.approve.side_effect = error
        else:
            server.chat_policy_service.approve.return_value = result or {
                "ok": True,
                "approved": True,
                "dailyCapAtomic": 5_000_000,
                "telegramText": "Chat approved: up to $5.00 a day.",
            }
        return server

    def test_it_asks_for_approval_with_the_requested_cap(self):
        server = self.server_with_policy()

        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 30},
            server=server,
        )

        self.assertEqual(self.status_of(handler), 200)
        server.chat_policy_service.approve.assert_called_once()
        kwargs = server.chat_policy_service.approve.call_args.kwargs
        self.assertEqual(kwargs["daily_cap_atomic"], 5_000_000)
        self.assertEqual(kwargs["days"], 30)

    def test_a_cap_below_the_providers_minimum_is_refused_without_asking(self):
        # A $1/day policy could never fund a $5 top-up: it would be approved
        # and then never work.
        server = self.server_with_policy()

        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 1_000_000, "days": 30},
            server=server,
        )

        body = self.response_json(handler)
        self.assertFalse(body["ok"])
        server.chat_policy_service.approve.assert_not_called()

    def test_a_missing_expiry_is_refused(self):
        server = self.server_with_policy()

        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 0},
            server=server,
        )

        self.assertFalse(self.response_json(handler)["ok"])
        server.chat_policy_service.approve.assert_not_called()

    def test_a_declined_approval_is_reported_not_crashed(self):
        server = self.server_with_policy(
            result={"ok": False, "approved": False, "telegramText": "Not confirmed."}
        )

        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 30},
            server=server,
        )

        body = self.response_json(handler)
        self.assertEqual(self.status_of(handler), 200)
        self.assertFalse(body["ok"])

    def test_the_route_is_404_when_the_flag_is_unset(self):
        self.enable_flag("")
        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 30},
        )
        self.assertEqual(self.status_of(handler), 404)

    def test_it_requires_the_bearer_token(self):
        handler = self.make_handler(
            "/agent/chat/approve-policy",
            {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 30},
            headers={},
        )
        self.assertEqual(self.status_of(handler), 401)

    def test_a_global_pause_refuses_a_new_budget(self):
        server = self.server_with_policy()
        with patch.dict(os.environ, {"SIGN402_PURCHASES_PAUSED": "1"}):
            handler = self.make_handler(
                "/agent/chat/approve-policy",
                {"telegramUserId": USER_ID, "dailyCapAtomic": 5_000_000, "days": 30},
                server=server,
            )
        self.assertEqual(self.status_of(handler), 503)
        server.chat_policy_service.approve.assert_not_called()
