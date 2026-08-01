import json
import threading
import urllib.error
import urllib.request
from unittest import TestCase

from trezor_sidecar.remote_agent_server import RemoteAgentHttpServer


TOKEN = "agent-" + "x" * 32


class FakeController:
    def __init__(self):
        self.calls = []

    def pair(self, user_id):
        self.calls.append(("status", user_id))
        return "enrolled"

    def intent_test(self, user_id):
        self.calls.append(("test", user_id))
        return "no purchase"

    def prepare(self, *args):
        self.calls.append(("prepare", *args))
        return "exact receipt"

    def confirm(self, *args):
        self.calls.append(("confirm", *args))
        return "complete"

    def cancel(self, user_id):
        self.calls.append(("cancel", user_id))
        return "cancelled"


class RemoteAgentServerTests(TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.server = RemoteAgentHttpServer(
            ("127.0.0.1", 0), token=TOKEN, controller=self.controller
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, payload, token=TOKEN):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_routes_only_narrow_agent_operations(self):
        status, value = self.request(
            "/v1/prepare",
            {"userId": "123", "productId": "test", "packageId": "1", "country": "US"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(value["text"], "exact receipt")
        self.assertEqual(self.controller.calls, [("prepare", "123", "test", "1", "US")])

    def test_no_purchase_test_is_a_distinct_operation(self):
        status, value = self.request("/v1/test", {"userId": "123"})
        self.assertEqual(status, 200)
        self.assertEqual(value["text"], "no purchase")
        self.assertEqual(self.controller.calls, [("test", "123")])

    def test_authentication_and_unknown_route_fail_before_controller(self):
        status, value = self.request("/v1/status", {"userId": "123"}, token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(value["code"], "unauthorized")
        status, value = self.request("/v1/generic", {"userId": "123"})
        self.assertEqual(status, 404)
        self.assertEqual(self.controller.calls, [])
