import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trezor_sidecar.broker_server import BrokerHttpServer, BrokerSettings
from trezor_sidecar.broker_store import BrokerStore


ADDRESS = "0xB80b5Ca13583fB7E0236db4bD8834B9035654558"
INTERNAL = "internal-" + "x" * 32
NOW = 1_700_000_000


class BrokerServerTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BrokerStore(Path(self.temp.name) / "state.db")
        settings = BrokerSettings(True, INTERNAL, Path(self.temp.name) / "state.db", "127.0.0.1", 0)
        self.server = BrokerHttpServer(
            ("127.0.0.1", 0),
            settings=settings,
            store=self.store,
            clock=lambda: NOW,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, payload=None, token="", method="POST"):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                status, raw = error.code, error.read()
            finally:
                error.close()
        return status, (json.loads(raw) if raw else None)

    def test_full_enroll_claim_complete_round_trip(self):
        status, created = self.request(
            "/v1/internal/enrollments", {"userId": "12345"}, token=INTERNAL
        )
        self.assertEqual(status, 201)
        status, enrolled = self.request(
            "/v1/enroll",
            {"enrollmentCode": created["enrollmentCode"], "walletAddress": ADDRESS},
        )
        self.assertEqual(status, 201)
        token = enrolled["companion"]["token"]

        status, queued = self.request(
            "/v1/internal/jobs",
            {
                "userId": "12345",
                "kind": "purchase_intent",
                "idempotencyKey": "approve:12345678",
                "payload": {"intentId": "test"},
                "expiresAt": NOW + 60,
            },
            token=INTERNAL,
        )
        self.assertEqual(status, 202)
        job_id = queued["job"]["jobId"]
        status, claimed = self.request("/v1/companion/jobs/claim", {}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(claimed["job"]["jobId"], job_id)
        status, _ = self.request(
            f"/v1/companion/jobs/{job_id}/complete",
            {"result": {"ok": True}},
            token=token,
        )
        self.assertEqual(status, 200)
        status, completed = self.request(
            f"/v1/internal/jobs/{job_id}", token=INTERNAL, method="GET"
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["job"]["state"], "SUCCEEDED")

    def test_internal_routes_reject_companion_or_missing_token(self):
        status, body = self.request("/v1/internal/enrollments", {"userId": "12345"})
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "unauthorized")

    def test_unknown_methods_and_routes_return_fixed_json(self):
        status, body = self.request("/unknown", {}, method="PUT")
        self.assertEqual(status, 405)
        self.assertEqual(set(body), {"ok", "code", "message"})
        status, body = self.request("/unknown", method="GET")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
