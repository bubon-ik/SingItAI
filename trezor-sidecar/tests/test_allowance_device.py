"""Grant and revoke through the device: sidecar service, route, client, companion.

Design: docs/trezor-allowance-v1.md, product integration phase 2.
"""

import http.client
import json
import threading
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from eth_account import Account

from trezor_sidecar.base import BASE_USDC_ADDRESS, encode_usdc_approve
from trezor_sidecar.companion import CompanionWorker
from trezor_sidecar.config import SidecarSettings
from trezor_sidecar.errors import SafeError
from trezor_sidecar.limiter import _GETTERS, LimiterArtifact
from trezor_sidecar.server import build_server
from trezor_sidecar.service import TrezorSidecarService
from trezor_sidecar.sidecar_client import SidecarClient
from trezor_sidecar.store import SidecarStore

from tests.test_service import FIXED_PATH, FakeTrezor
from tests.test_sidecar_client import FakeResponse, build_client, encode

LIMITER = "0x1111111111111111111111111111111111111111"
NOW = 1_700_000_000
ROOT = Path(__file__).resolve().parents[2]


class LimiterRpc:
    """Base as the limiter check sees it: code, and the limiter's getters."""

    def __init__(self, owner):
        self.owner = owner
        self.code_hex = LimiterArtifact().deployed_bytecode
        self.values = {"token": int(BASE_USDC_ADDRESS, 16), "agent": 7, "guardian": 8, "dailyCap": 100,
                       "perPurchaseCap": 10, "expiry": NOW + 86400, "paused": 0, "remainingToday": 100}
        self.reads = []

    def code(self, address):
        self.reads.append(("code", address))
        return self.code_hex

    def has_code(self, address):
        return len(self.code_hex) > 2

    def call_word(self, address, data):
        self.reads.append(("call", address))
        name = next(k for k, v in _GETTERS.items() if v == data)
        if name == "owner":
            return int(self.owner, 16)
        return self.values[name]


class ServiceApproveAllowanceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.trezor = FakeTrezor()
        self.rpc = LimiterRpc(self.trezor.address)
        settings = SidecarSettings(
            enabled=True, mcp_token="m", api_token="a", max_usd=Decimal("300"),
            base_rpc_url="https://base.example.invalid",
            state_path=Path(self.temporary.name) / "state" / "sidecar.db", derivation_path=FIXED_PATH,
        )
        self.service = TrezorSidecarService(settings, self.trezor, SidecarStore(settings.state_path),
                                            clock=lambda: NOW, rpc=self.rpc)
        self.service.pair()

    def test_a_grant_to_our_limiter_is_signed_verified_and_returned_unbroadcast(self):
        result = self.service.approve_allowance(LIMITER, 250_000_000)

        self.assertEqual(self.trezor.sign_transaction_calls[-1]["to"], BASE_USDC_ADDRESS)
        self.assertEqual(self.trezor.sign_transaction_calls[-1]["data"], encode_usdc_approve(LIMITER, 250_000_000))
        self.assertEqual(self.trezor.push_transaction_calls, [])
        self.assertEqual(result["owner"], self.trezor.address)
        self.assertEqual(result["amountAtomic"], 250_000_000)
        self.assertEqual(Account.recover_transaction(result["signedTransaction"]), self.trezor.address)

    def test_a_grant_to_anything_but_our_limiter_never_reaches_the_device(self):
        other = Account.create().address
        cases = {
            "not the tested code": lambda: setattr(self.rpc, "code_hex", self.rpc.code_hex[:-2] + "00"),
            "no code at all": lambda: setattr(self.rpc, "code_hex", "0x"),
            "another owner": lambda: setattr(self.rpc, "owner", other),
            "another token": lambda: self.rpc.values.update(token=int(other, 16)),
            "paused": lambda: self.rpc.values.update(paused=1),
            "expired": lambda: self.rpc.values.update(expiry=NOW),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                self.rpc = LimiterRpc(self.trezor.address)
                self.service._rpc = self.rpc
                spoil()
                signed_before = len(self.trezor.sign_transaction_calls)
                with self.assertRaises(SafeError) as raised:
                    self.service.approve_allowance(LIMITER, 1_000_000)
                self.assertEqual(raised.exception.code, "limiter_invalid")
                self.assertEqual(len(self.trezor.sign_transaction_calls), signed_before)

    def test_a_revoke_checks_nothing_about_the_spender(self):
        self.rpc.code_hex = "0x"
        self.rpc.owner = Account.create().address
        result = self.service.approve_allowance(LIMITER, 0)
        self.assertEqual(result["amountAtomic"], 0)
        self.assertEqual(self.trezor.sign_transaction_calls[-1]["data"], encode_usdc_approve(LIMITER, 0))
        self.assertNotIn(("code", LIMITER), self.rpc.reads)

    def test_amounts_and_spenders_are_validated_before_anything(self):
        for spender, amount, code in (
            (LIMITER, 300_000_001, "limit_exceeded"),
            (LIMITER, -1, "invalid_request"),
            (LIMITER, 1.5, "invalid_request"),
            (LIMITER, True, "invalid_request"),
            ("0x1234", 1, "invalid_request"),
            ("0x" + "00" * 20, 1, "invalid_request"),
        ):
            with self.subTest(spender=spender, amount=amount):
                with self.assertRaises(SafeError) as raised:
                    self.service.approve_allowance(spender, amount)
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.trezor.sign_transaction_calls, [])

    def test_a_rejection_on_the_device_is_named(self):
        self.trezor.sign_transaction_failure = SafeError("device_rejected", "x", 400)
        with self.assertRaises(SafeError) as raised:
            self.service.approve_allowance(LIMITER, 1_000_000)
        self.assertEqual(raised.exception.code, "device_rejected")

    def test_a_signature_for_something_else_is_never_returned(self):
        for name, update in {
            "other amount": {"data": encode_usdc_approve(LIMITER, 2_000_000)},
            "other spender": {"data": encode_usdc_approve(Account.create().address, 1_000_000)},
            "other chain": {"chainId": 1},
        }.items():
            with self.subTest(name):
                self.trezor.transaction_updates = update
                with self.assertRaises(SafeError) as raised:
                    self.service.approve_allowance(LIMITER, 1_000_000)
                self.assertEqual(raised.exception.code, "invalid_signed_transaction")

    def test_an_unpaired_sidecar_refuses(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = SidecarSettings(enabled=True, mcp_token="m", api_token="a", max_usd=Decimal("5"),
                                   base_rpc_url="https://base.example.invalid",
                                   state_path=Path(temporary.name) / "s.db", derivation_path=FIXED_PATH)
        service = TrezorSidecarService(settings, FakeTrezor(), SidecarStore(settings.state_path), rpc=self.rpc)
        with self.assertRaises(SafeError) as raised:
            service.approve_allowance(LIMITER, 0)
        self.assertEqual(raised.exception.code, "not_paired")


class RouteTests(TestCase):
    class Service:
        settings = None
        health_status = "ready"

        def __init__(self):
            self.calls = []

        def approve_allowance(self, spender, amount):
            self.calls.append((spender, amount))
            return {"signedTransaction": "0x02ab", "transactionHash": "0x" + "cd" * 32,
                    "owner": "0x" + "ab" * 20, "spender": spender, "amountAtomic": amount}

    def setUp(self):
        settings = SidecarSettings(enabled=True, mcp_token="m", api_token="local-secret", max_usd=Decimal("10"),
                                   base_rpc_url="https://rpc.example.invalid", state_path=Path("unused.db"),
                                   host="127.0.0.1", port=0)
        self.service = self.Service()
        self.server = build_server(settings, self.service, clock=lambda: NOW, _allow_test_port=True,
                                   _test_only_connection_timeout=0.15)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        connection.request("POST", "/v1/allowance/approve", body=json.dumps(body), headers={
            "Authorization": "Bearer local-secret", "Content-Type": "application/json",
            "X-Sign402-Timestamp": str(NOW), "Idempotency-Key": "allowance:job_12345678"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_the_route_passes_exactly_spender_and_amount(self):
        status, payload = self.post({"spender": LIMITER, "amountAtomic": 5})
        self.assertEqual(status, 200)
        self.assertEqual(self.service.calls, [(LIMITER, 5)])
        self.assertEqual(payload["amountAtomic"], 5)

    def test_anything_else_is_refused(self):
        for body in ({"spender": LIMITER}, {"spender": LIMITER, "amountAtomic": "5"},
                     {"spender": LIMITER, "amountAtomic": 5, "extra": 1}):
            with self.subTest(body=body):
                status, payload = self.post(body)
                self.assertEqual((status, payload["code"]), (400, "invalid_request"))
        self.assertEqual(self.service.calls, [])


class ClientTests(TestCase):
    def signed(self, **overrides):
        body = {"ok": True, "signedTransaction": "0x02abcd", "transactionHash": "0x" + "cd" * 32,
                "owner": "0x" + "ab" * 20, "spender": LIMITER, "amountAtomic": 7}
        body.update(overrides)
        return FakeResponse(200, encode(body))

    def test_returns_the_signed_approve_for_exactly_what_was_asked(self):
        client, captured = build_client(self.signed())
        result = client.approve_allowance(LIMITER, 7, "allowance:job_12345678")
        self.assertEqual(captured["path"], "/v1/allowance/approve")
        self.assertEqual(json.loads(captured["body"]), {"spender": LIMITER, "amountAtomic": 7})
        self.assertEqual(result["signedTransaction"], "0x02abcd")

    def test_a_response_for_something_else_is_refused(self):
        for override in ({"amountAtomic": 8}, {"spender": "0x" + "22" * 20}, {"signedTransaction": "nope"},
                         {"extra": 1}):
            with self.subTest(override=override):
                client, _ = build_client(self.signed(**override))
                with self.assertRaises(SafeError):
                    client.approve_allowance(LIMITER, 7, "allowance:job_12345678")

    def test_a_refused_limiter_keeps_its_code(self):
        response = FakeResponse(409, encode({"ok": False, "code": "limiter_invalid", "message": "x"}))
        client, _ = build_client(response)
        with self.assertRaises(SafeError) as raised:
            client.approve_allowance(LIMITER, 7, "allowance:job_12345678")
        self.assertEqual(raised.exception.code, "limiter_invalid")


class CompanionTests(TestCase):
    class Broker:
        def __init__(self, job):
            self.job, self.completed, self.failed = job, [], []

        def claim(self):
            job, self.job = self.job, None
            return job

        def complete(self, job_id, result):
            self.completed.append((job_id, result))

        def fail(self, job_id, code):
            self.failed.append((job_id, code))

    class Sidecar:
        def __init__(self):
            self.calls = []

        def approve_allowance(self, spender, amount, key):
            self.calls.append((spender, amount, key))
            return {"signedTransaction": "0x02ab"}

    def run_job(self, payload):
        broker = self.Broker({"jobId": "job_12345678", "kind": "usdc_approve", "payload": payload,
                              "expiresAt": NOW + 600})
        sidecar = self.Sidecar()
        CompanionWorker(broker=broker, sidecar=sidecar, clock=lambda: NOW).run_once()
        return broker, sidecar

    def test_an_approve_job_reaches_the_sidecar_as_is(self):
        broker, sidecar = self.run_job({"spender": LIMITER, "amountAtomic": 5})
        self.assertEqual(sidecar.calls, [(LIMITER, 5, "allowance:job_12345678")])
        self.assertEqual(broker.completed, [("job_12345678", {"signedTransaction": "0x02ab"})])

    def test_a_malformed_approve_job_is_failed_not_guessed(self):
        for payload in ({"spender": LIMITER}, {"spender": LIMITER, "amountAtomic": "5"},
                        {"spender": LIMITER, "amountAtomic": 5, "to": LIMITER}):
            with self.subTest(payload=payload):
                broker, sidecar = self.run_job(payload)
                self.assertEqual(sidecar.calls, [])
                self.assertEqual(broker.failed, [("job_12345678", "broker_failed")])


class ArtifactCopyTests(TestCase):
    def test_the_sidecar_checks_against_the_same_code_the_gateway_deploys(self):
        sidecar = Path(__file__).resolve().parents[1] / "trezor_sidecar" / "agent_allowance.json"
        gateway = ROOT / "sign402-gateway" / "sign402_gateway" / "contracts" / "agent_allowance.json"
        self.assertEqual(sidecar.read_bytes(), gateway.read_bytes())
