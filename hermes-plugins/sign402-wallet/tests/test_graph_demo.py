import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("graph_demo_plugin", Path(__file__).resolve().parents[1] / "graph_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class GraphDemoCommandTests(unittest.TestCase):
    def source(self, user="123", chat="123"):
        return SimpleNamespace(user_id=user, chat_id=chat, platform=SimpleNamespace(value="telegram"))

    def handle(self, text="/graph_demo", source=None):
        self.send = Mock()
        return demo.handle_graph_demo(event=SimpleNamespace(text=text, message_id="456"),
            source=source or self.source(), gateway=None, send=self.send, background=lambda work: work())

    def test_other_commands_are_unaffected(self):
        with patch.dict(os.environ, {"SIGN402_GRAPH_DEMO_OWNER": "123"}), patch.object(demo, "call_demo") as call:
            self.assertIsNone(self.handle("/balance"))
            call.assert_not_called()

    def test_other_users_and_group_chats_cannot_invoke_backend(self):
        with patch.dict(os.environ, {"SIGN402_GRAPH_DEMO_OWNER": "123"}), patch.object(demo, "call_demo") as call:
            self.handle(source=self.source(user="999"))
            self.handle(source=self.source(chat="-123"))
            call.assert_not_called()
            self.send.assert_not_called()

    def test_progress_and_final_result_reach_chat(self):
        responses = [
            {"requestId": "same-job", "status": "pending", "text": "Review on Ledger"},
            {"requestId": "same-job", "status": "succeeded", "text": "WETH 2500 USDC https://basescan.org/tx/abc"}]
        with patch.dict(os.environ, {"SIGN402_GRAPH_DEMO_OWNER": "123"}), \
                patch.object(demo, "call_demo", side_effect=responses) as call, patch.object(demo.time, "sleep"):
            self.handle()
            self.assertEqual(call.call_args_list[1].args, ("/status", {"owner": "123", "requestId": "same-job"}))
            self.assertEqual(self.send.call_args.args[2], responses[1]["text"])

    def test_status_never_starts_a_purchase(self):
        with patch.dict(os.environ, {"SIGN402_GRAPH_DEMO_OWNER": "123"}), \
                patch.object(demo, "call_demo", return_value={"status": "succeeded", "text": "saved"}) as call:
            self.handle("/graph_demo status")
            call.assert_called_once_with("/status", {"owner": "123"})


if __name__ == "__main__":
    unittest.main()
