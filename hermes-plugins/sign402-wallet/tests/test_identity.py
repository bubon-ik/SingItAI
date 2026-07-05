import asyncio
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from identity import (  # noqa: E402
    TelegramIdentity,
    capture_gateway_identity,
    consume_gateway_identity,
)


@dataclass
class FakePlatform:
    value: str


@dataclass
class FakeSource:
    platform: FakePlatform
    user_id: str | None
    user_name: str | None = None
    chat_id: str = "chat-1"


class FakeEvent:
    def __init__(self, text: str, source: FakeSource):
        self.text = text
        self.source = source

    def get_command(self) -> str | None:
        if not self.text.startswith("/"):
            return None
        return self.text[1:].split(maxsplit=1)[0].split("@", maxsplit=1)[0]


def telegram_event(
    command: str,
    user_id: str | None = "1045618308",
    username: str | None = "AlpskyKnedlik",
) -> FakeEvent:
    return FakeEvent(
        command,
        FakeSource(FakePlatform("telegram"), user_id, username),
    )


class TelegramIdentityTests(unittest.TestCase):
    def tearDown(self):
        consume_gateway_identity()

    def test_captures_trusted_source_for_wallet_command(self):
        capture_gateway_identity(event=telegram_event("/wallet"))

        self.assertEqual(
            consume_gateway_identity(),
            TelegramIdentity(
                user_id="1045618308",
                username="AlpskyKnedlik",
                chat_id="chat-1",
            ),
        )

    def test_normalizes_telegram_create_wallet_command(self):
        capture_gateway_identity(event=telegram_event("/create_wallet ignored"))

        self.assertEqual(
            consume_gateway_identity(),
            TelegramIdentity(
                user_id="1045618308",
                username="AlpskyKnedlik",
                chat_id="chat-1",
            ),
        )

    def test_captures_trusted_source_for_limits_commands(self):
        for command in ("/limits", "/set_limits 0.005 0.05"):
            with self.subTest(command=command):
                capture_gateway_identity(event=telegram_event(command))

                self.assertEqual(
                    consume_gateway_identity(),
                    TelegramIdentity(
                        user_id="1045618308",
                        username="AlpskyKnedlik",
                        chat_id="chat-1",
                    ),
                )

    def test_captures_trusted_source_for_llm_commands(self):
        for command in (
            "/llm_buy 10 user@example.com",
            "/llm_terms accept",
            "/llm_code 123456",
            "/llm_credits",
        ):
            with self.subTest(command=command):
                capture_gateway_identity(event=telegram_event(command))

                self.assertEqual(
                    consume_gateway_identity(),
                    TelegramIdentity(
                        user_id="1045618308",
                        username="AlpskyKnedlik",
                        chat_id="chat-1",
                    ),
                )

    def test_ignores_non_wallet_command(self):
        capture_gateway_identity(event=telegram_event("/help"))

        self.assertIsNone(consume_gateway_identity())

    def test_ignores_non_telegram_source(self):
        event = FakeEvent(
            "/wallet",
            FakeSource(FakePlatform("discord"), "1045618308", "AlpskyKnedlik"),
        )

        capture_gateway_identity(event=event)

        self.assertIsNone(consume_gateway_identity())

    def test_ignores_missing_or_malformed_user_id(self):
        for user_id in (None, "", "abc", "-12"):
            with self.subTest(user_id=user_id):
                capture_gateway_identity(
                    event=telegram_event("/wallet", user_id=user_id)
                )
                self.assertIsNone(consume_gateway_identity())

    def test_identity_can_only_be_consumed_once(self):
        capture_gateway_identity(event=telegram_event("/balance"))

        self.assertIsNotNone(consume_gateway_identity())
        self.assertIsNone(consume_gateway_identity())

    def test_concurrent_tasks_keep_user_identities_isolated(self):
        async def worker(user_id: str):
            capture_gateway_identity(
                event=telegram_event("/wallet", user_id=user_id, username=None)
            )
            await asyncio.sleep(0)
            return consume_gateway_identity()

        async def run_workers():
            return await asyncio.gather(worker("111"), worker("222"))

        identities = asyncio.run(run_workers())

        self.assertEqual(
            identities,
            [
                TelegramIdentity(user_id="111", username=None, chat_id="chat-1"),
                TelegramIdentity(user_id="222", username=None, chat_id="chat-1"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
