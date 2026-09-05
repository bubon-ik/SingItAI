"""`/v1/decide` and `/v1/journal`, against a real policy on a real database.

Nothing here mocks `SpendingPolicy`. The endpoint's whole job is to be a
faithful translation of what the policy says, and a mocked policy would let it
be a faithful translation of what the test says instead. Each case builds a
memory of its own in a temporary directory and drives it into the state the
rule under test needs.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spending_memory import Payment, SpendingMemory, SpendingPolicy

from sign402_gateway.decide import decide, journal
from sign402_gateway.server import Sign402GatewayHandler
from tests.test_gateway_server import FakeSocket

KNOWN_ADDRESS = "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB"
OTHER_ADDRESS = "0xDEADbeefDEADbeefDEADbeefDEADbeefDEADbeef"
OWNER = "agent-7"


def build_policy(*, daily_cap_usd: str = "5") -> SpendingPolicy:
    return SpendingPolicy(
        SpendingMemory.local(str(Path(tempfile.mkdtemp()) / "memory.db")),
        daily_cap_usd=Decimal(daily_cap_usd),
    )


def request(**overrides) -> dict:
    body = {
        "merchant": "gateway.thegraph.com",
        "payTo": KNOWN_ADDRESS,
        "amountUsd": "0.01",
        "owner": OWNER,
    }
    body.update(overrides)
    return body


def remember_payment(
    policy: SpendingPolicy,
    *,
    pay_to: str = KNOWN_ADDRESS,
    amount_usd: str = "0.01",
    merchant: str = "gateway.thegraph.com",
) -> None:
    """Make the merchant known, the way a settled payment does."""
    policy.memory.remember_settlement(
        Payment(
            merchant=merchant,
            pay_to=pay_to,
            amount_usd=Decimal(amount_usd),
            owner=OWNER,
        ),
        tx_id="0xtest",
    )


class DecideTests(unittest.TestCase):
    def test_an_unknown_merchant_is_escalated_and_holds_no_claim(self):
        status, body = decide(request(), build_policy())

        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "ESCALATE")
        self.assertEqual(body["rule"], "unknown_merchant")
        self.assertIsNone(body["claimId"])
        self.assertTrue(body["journalId"])
        self.assertIn("gateway.thegraph.com", body["reason"])

    def test_a_known_merchant_inside_the_band_pays_and_holds_a_claim(self):
        policy = build_policy()
        remember_payment(policy)

        status, body = decide(request(), policy)

        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "PAY")
        self.assertEqual(body["rule"], "known_good")
        self.assertTrue(body["claimId"])

    def test_a_moved_payout_address_is_blocked_with_both_addresses_shown(self):
        """The verdict is BLOCK and the status is still 200.

        A caller has to read `action` to learn what happened. Putting the
        verdict in the HTTP status would hand it to the one field every client
        already has a reflex about, and the reflex — retry, or report an
        outage — is wrong for both of the things a BLOCK can mean.
        """
        policy = build_policy()
        remember_payment(policy)

        status, body = decide(request(payTo=OTHER_ADDRESS), policy)

        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "BLOCK")
        self.assertEqual(body["rule"], "payout_address_changed")
        self.assertIsNone(body["claimId"])
        self.assertEqual(
            body["evidence"]["remembered_pay_to"], KNOWN_ADDRESS.lower()
        )
        self.assertEqual(
            body["evidence"]["requested_pay_to"], OTHER_ADDRESS.lower()
        )

    def test_an_amount_sent_as_a_number_is_refused(self):
        """A float cannot hold 0.1, and this number is compared to a limit."""
        for amount in (25, 25.0, 0.1, True, None, "", "-1", "1e5", ".5", "abc"):
            with self.subTest(amount=amount):
                status, body = decide(
                    request(amountUsd=amount), build_policy()
                )
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "invalid-amount"})

    def test_a_zero_amount_is_refused_rather_than_decided(self):
        status, body = decide(request(amountUsd="0"), build_policy())

        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "invalid-amount"})

    def test_an_absent_owner_is_refused(self):
        """Every limit is per owner, so there is no useful default for it."""
        for owner in (None, "", "   ", 7):
            with self.subTest(owner=owner):
                payload = request()
                if owner is None:
                    payload.pop("owner")
                else:
                    payload["owner"] = owner
                status, body = decide(payload, build_policy())
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "missing-owner"})

    def test_a_pay_to_that_is_not_an_address_is_refused(self):
        for pay_to in (
            "0x79DC34E41B2b591078d3dE222C43EcaaBD52Fc",  # short
            "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCBAA",  # long
            "79DC34E41B2b591078d3dE222C43EcaaBD52FcCB",  # no 0x
            "0xZZDC34E41B2b591078d3dE222C43EcaaBD52FcCB",  # not hex
            "",
        ):
            with self.subTest(pay_to=pay_to):
                status, body = decide(request(payTo=pay_to), build_policy())
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "invalid-pay-to"})

    def test_a_body_that_is_not_an_object_is_refused(self):
        for payload in ([], "merchant", 3, None):
            with self.subTest(payload=payload):
                status, body = decide(payload, build_policy())
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "invalid-request"})

    def test_the_kill_switch_answers_503_rather_than_inventing_a_verdict(self):
        """No memory means no answer — in either direction.

        A PAY here would be permission nobody granted; an ESCALATE would look
        like a considered refusal. Neither is true, so neither is returned.
        """
        status, body = decide(request(), None)

        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "spending-memory-disabled"})

    def test_the_merchant_is_matched_regardless_of_case(self):
        policy = build_policy()
        remember_payment(policy)

        status, body = decide(request(merchant="Gateway.TheGraph.com"), policy)

        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "PAY")


class JournalTests(unittest.TestCase):
    def test_the_journal_returns_only_the_owner_that_was_asked_for(self):
        policy = build_policy()
        decide(request(owner="agent-7"), policy)
        decide(request(owner="agent-9", merchant="other.example.com"), policy)

        status, body = journal("agent-7", None, policy)

        self.assertEqual(status, 200)
        self.assertEqual(body["owner"], "agent-7")
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["entries"][0]["merchant"], "gateway.thegraph.com")
        self.assertEqual(body["entries"][0]["action"], "ESCALATE")
        self.assertEqual(body["entries"][0]["rule"], "unknown_merchant")
        self.assertTrue(body["entries"][0]["journalId"])
        self.assertTrue(body["entries"][0]["at"])

    def test_the_owner_and_rule_keys_are_not_repeated_inside_evidence(self):
        """`record_decision` writes them alongside the evidence, not in it."""
        policy = build_policy()
        decide(request(), policy)

        _, body = journal(OWNER, None, policy)

        self.assertNotIn("rule", body["entries"][0]["evidence"])
        self.assertNotIn("action", body["entries"][0]["evidence"])
        self.assertNotIn("owner", body["entries"][0]["evidence"])

    def test_the_limit_is_honoured_and_capped(self):
        policy = build_policy()
        for n in range(4):
            decide(request(merchant=f"m{n}.example.com"), policy)

        _, body = journal(OWNER, "2", policy)
        self.assertEqual(len(body["entries"]), 2)

        _, body = journal(OWNER, 10_000, policy)
        self.assertEqual(len(body["entries"]), 4)

    def test_an_absent_owner_is_refused(self):
        policy = build_policy()

        for owner in (None, "", "   "):
            with self.subTest(owner=owner):
                status, body = journal(owner, None, policy)
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "missing-owner"})

    def test_a_nonsense_limit_is_refused(self):
        policy = build_policy()

        for limit in ("banana", "0", "-1", []):
            with self.subTest(limit=limit):
                status, body = journal(OWNER, limit, policy)
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "invalid-request"})

    def test_the_kill_switch_answers_503(self):
        status, body = journal(OWNER, None, None)

        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "spending-memory-disabled"})


class DecideOverHttpTests(unittest.TestCase):
    """The two routes, driven through the real handler over a socket.

    The cases above prove the decision is translated correctly; these prove it
    is reachable, that the paths are spelled the way the OpenAPI document says,
    and that a body which is not JSON at all is refused before the policy is
    asked.
    """

    class Server:
        def __init__(self, policy):
            self.spending_policy = policy

    def call(self, request_bytes: bytes, policy):
        socket = FakeSocket(request_bytes)
        with patch("sys.stderr", io.StringIO()):
            handler = Sign402GatewayHandler(
                socket, ("127.0.0.1", 12345), self.Server(policy)
            )
        raw = socket.wfile.getvalue().decode("utf-8", "replace")
        head, _, body = raw.partition("\r\n\r\n")
        return head.splitlines()[0], json.loads(body)

    @staticmethod
    def post(path: str, payload: str) -> bytes:
        return (
            f"POST {path} HTTP/1.1\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            f"{payload}"
        ).encode()

    def test_decide_is_reachable_and_answers_a_verdict(self):
        status, body = self.call(
            self.post("/v1/decide", json.dumps(request())), build_policy()
        )

        self.assertIn(" 200 ", status)
        self.assertEqual(body["action"], "ESCALATE")

    def test_a_body_that_is_not_json_is_refused_without_asking_the_policy(self):
        policy = build_policy()

        status, body = self.call(self.post("/v1/decide", "not json"), policy)

        self.assertIn(" 400 ", status)
        self.assertEqual(body, {"error": "invalid-request"})
        self.assertEqual(policy.memory.journal(limit=50), [])

    def test_the_journal_route_reads_its_query_string(self):
        policy = build_policy()
        decide(request(), policy)

        status, body = self.call(
            f"GET /v1/journal?owner={OWNER}&limit=5 HTTP/1.1\r\n\r\n".encode(),
            policy,
        )

        self.assertIn(" 200 ", status)
        self.assertEqual(body["owner"], OWNER)
        self.assertEqual(len(body["entries"]), 1)

    def test_the_journal_route_refuses_a_request_with_no_owner(self):
        status, body = self.call(
            b"GET /v1/journal HTTP/1.1\r\n\r\n", build_policy()
        )

        self.assertIn(" 400 ", status)
        self.assertEqual(body, {"error": "missing-owner"})


if __name__ == "__main__":
    unittest.main()
