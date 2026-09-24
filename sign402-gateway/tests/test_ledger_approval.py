"""Readable EIP-191 approvals, signed with a real key rather than a stubbed recover.

`eth_account` signs here so the verification path is exercised end to end: a
mocked `recover_message` would let every one of these tests pass against a
function that checks nothing. The only thing a Ledger adds over this key is
where the private half lives and who saw the fields — neither of which changes
what the gateway has to verify.
"""

from __future__ import annotations

import time
import unittest
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from spending_memory import Action, Decision, Payment

from sign402_gateway.ledger_approval import (
    APPROVERS_ENV,
    DEFAULT_CHAIN_ID,
    ENABLED_ENV,
    LedgerApprovalError,
    MAX_LIFETIME_SECONDS,
    SpendingApproval,
    approval_enabled,
    approval_for,
    approver_addresses,
    verify_approval,
    display_text,
)

OWNER_KEY = Account.from_key("0x" + "11" * 32)
OTHER_KEY = Account.from_key("0x" + "22" * 32)

MERCHANT = "giftcards.example.com"
PAY_TO = "0x8f3a1c2b4d5e6f708192a3b4c5d6e7f809a1b2c3"
JOURNAL = "01JB8Z4A1B2C3D4E5F6G7H8J9K"


def payment(**overrides) -> Payment:
    fields = {
        "merchant": MERCHANT,
        "pay_to": PAY_TO,
        "amount_usd": Decimal("25.00"),
        "owner": "agent-7",
        "resource": "https://giftcards.example.com/item",
    }
    fields.update(overrides)
    return Payment(**fields)


def decision(journal_id: str = JOURNAL, rule: str = "unknown_merchant") -> Decision:
    return Decision(
        action=Action.ESCALATE,
        reason="I have never paid them before.",
        rule=rule,
        journal_id=journal_id,
    )


def env(approvers=(OWNER_KEY.address,), **extra) -> dict:
    values = {ENABLED_ENV: "1", APPROVERS_ENV: ",".join(approvers)}
    values.update(extra)
    return values


def sign(
    key=OWNER_KEY,
    *,
    pay=None,
    dec=None,
    expires_at=None,
    chain=DEFAULT_CHAIN_ID,
    **overrides,
) -> dict:
    pay = pay or payment()
    dec = dec or decision()
    expires_at = expires_at if expires_at is not None else int(time.time()) + 600
    approval = SpendingApproval(
        merchant=overrides.get("merchant", pay.merchant),
        pay_to=overrides.get("pay_to", pay.pay_to_normalised),
        amount_usd=overrides.get("amount_usd", str(pay.amount_usd)),
        owner=overrides.get("owner", pay.owner),
        rule=overrides.get("rule", dec.rule),
        journal_id=overrides.get("journal_id", dec.journal_id),
        expires_at=expires_at,
        purchase=overrides.get("purchase", pay.resource or pay.merchant),
        resource=overrides.get("resource", pay.resource or ""),
    )
    signed = key.sign_message(
        encode_defunct(text=display_text(approval.message(), chain=chain))
    )
    return {
        "signingMethod": "personal_sign",
        "signature": signed.signature.hex(),
        "expiresAt": expires_at,
        "journalId": approval.journal_id,
    }


class FlagTests(unittest.TestCase):
    def test_off_unless_set(self):
        self.assertFalse(approval_enabled({}))
        self.assertFalse(approval_enabled({ENABLED_ENV: "0"}))
        self.assertTrue(approval_enabled({ENABLED_ENV: "1"}))

    def test_approvers_are_read_case_insensitively_and_in_either_separator(self):
        found = approver_addresses({APPROVERS_ENV: f"{OWNER_KEY.address}; {OTHER_KEY.address}"})
        self.assertEqual(found, {OWNER_KEY.address.lower(), OTHER_KEY.address.lower()})


class VerifyTests(unittest.TestCase):
    def verify(self, submitted, *, values=None, pay=None, dec=None):
        return verify_approval(
            submitted,
            payment=pay or payment(),
            decision=dec or decision(),
            env=values or env(),
        )

    def test_a_valid_signature_names_its_signer(self):
        self.assertEqual(self.verify(sign()), OWNER_KEY.address.lower())

    def test_a_signature_from_another_device_is_refused(self):
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(OTHER_KEY))

    def test_an_expired_approval_is_refused(self):
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(expires_at=int(time.time()) - 1))

    def test_an_approval_valid_for_too_long_is_refused(self):
        """A signature good for a week is a bearer token for that payment."""
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(expires_at=int(time.time()) + MAX_LIFETIME_SECONDS + 60))

    def test_an_approval_for_another_decision_is_refused(self):
        """The property the whole design turns on: one approval, one payment."""
        other = sign(dec=decision(journal_id="a-different-journal-entry"))
        with self.assertRaises(LedgerApprovalError):
            self.verify(other)

    def test_reusing_an_approval_on_a_second_payment_is_refused(self):
        """The same signature, replayed against the next identical purchase.
        Same merchant, same address, same amount — a different journal entry,
        which is what makes it a different payment."""
        first = sign()
        self.assertEqual(self.verify(first), OWNER_KEY.address.lower())
        with self.assertRaises(LedgerApprovalError):
            self.verify(first, dec=decision(journal_id="the-next-escalation"))

    def test_a_changed_amount_breaks_the_signature(self):
        approved = sign()
        with self.assertRaises(LedgerApprovalError):
            self.verify(approved, pay=payment(amount_usd=Decimal("2500.00")))

    def test_a_changed_payout_address_breaks_the_signature(self):
        approved = sign()
        with self.assertRaises(LedgerApprovalError):
            self.verify(approved, pay=payment(pay_to="0x" + "ab" * 20))

    def test_a_changed_merchant_breaks_the_signature(self):
        approved = sign()
        with self.assertRaises(LedgerApprovalError):
            self.verify(approved, pay=payment(merchant="somewhere.else"))

    def test_purchase_name_and_resource_are_bound_to_the_signature(self):
        for change in ({"purchase": "A different product"}, {"resource": "https://giftcards.example.com/other"}):
            with self.subTest(change=change), self.assertRaises(LedgerApprovalError):
                self.verify(sign(**change))

    def test_edited_display_text_is_rejected_even_with_a_valid_owner_key(self):
        approval = approval_for(payment(), decision())
        text = display_text(approval.message(), chain=DEFAULT_CHAIN_ID).replace("25.00 USDC", "0.01 USDC")
        submitted = {"signingMethod": "personal_sign", "expiresAt": approval.expires_at,
                     "journalId": JOURNAL, "signature": OWNER_KEY.sign_message(encode_defunct(text=text)).signature.hex()}
        with self.assertRaises(LedgerApprovalError):
            self.verify(submitted)

    def test_typed_data_cannot_be_relabelled_as_a_text_approval(self):
        # A real EIP-712 signature from the allowed key still uses a different
        # digest/prefix; changing a transport field cannot upgrade it to v2.
        typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                           "SpendingApproval": [{"name": "merchant", "type": "string"}]},
                 "primaryType": "SpendingApproval", "domain": {"name": "SingIt Spending Approval"},
                 "message": {"merchant": MERCHANT}}
        submitted = sign()
        submitted["signature"] = OWNER_KEY.sign_message(encode_typed_data(full_message=typed)).signature.hex()
        with self.assertRaises(LedgerApprovalError):
            self.verify(submitted)
        submitted = sign()
        submitted.pop("signingMethod")
        with self.assertRaises(LedgerApprovalError):
            self.verify(submitted)

    def test_a_signature_for_another_chain_is_refused(self):
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(chain=1))

    def test_no_approvers_configured_refuses_rather_than_passing(self):
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(), values=env(approvers=()))

    def test_a_missing_or_malformed_submission_is_refused(self):
        for bad in (None, {}, {"signature": ""}, "not-a-mapping", {"signature": "0xdead"}):
            with self.subTest(submitted=bad):
                with self.assertRaises(LedgerApprovalError):
                    self.verify(bad)

    def test_an_approval_without_an_expiry_is_refused(self):
        approved = sign()
        approved.pop("expiresAt")
        with self.assertRaises(LedgerApprovalError):
            self.verify(approved)

    def test_a_decision_with_no_journal_entry_cannot_be_approved(self):
        with self.assertRaises(LedgerApprovalError):
            self.verify(sign(), dec=decision(journal_id=""))


class PayloadTests(unittest.TestCase):
    def test_readable_text_has_stable_labels_and_escapes_untrusted_values(self):
        msg = {"purchase": "News\nAmount: 0\u202e", "amountUsd": "0.001", "payTo": PAY_TO,
               "merchant": "example.com", "resource": "https://example.com/news", "owner": "7",
               "rule": "unknown_merchant", "journalId": "decision-1", "expiresAt": 1788912000}
        text = display_text(msg, chain=8453)
        self.assertEqual(text.splitlines()[:4], [
            "SingIt purchase v2", "Buy: News\\nAmount: 0\\u202e",
            "Pay: 0.001 USDC on Base", f"To: {PAY_TO}",
        ])
        self.assertEqual(len(text.splitlines()), 5)
        self.assertRegex(text.splitlines()[4], r"^Ref: [A-Za-z0-9_-]{43}$")
        for name, value in {"merchant": "other.example", "resource": "https://example.com/other",
                            "owner": "8", "rule": "over_budget", "journalId": "decision-2", "expiresAt": 1788912001}.items():
            changed = display_text({**msg, name: value}, chain=8453)
            self.assertEqual(changed.splitlines()[:4], text.splitlines()[:4])
            self.assertNotEqual(changed.splitlines()[4], text.splitlines()[4])
        self.assertTrue(text.isascii())
        with self.assertRaises(LedgerApprovalError):
            display_text({**msg, "purchase": "a" * 2001}, chain=8453)

    def test_the_payload_is_built_from_the_decision_not_the_request(self):
        """What the device renders is the gateway's account of the payment."""
        built = approval_for(payment(), decision())
        self.assertEqual(built.merchant, MERCHANT)
        self.assertEqual(built.pay_to, PAY_TO.lower())
        self.assertEqual(built.amount_usd, "25.00")
        self.assertEqual(built.journal_id, JOURNAL)
        self.assertGreater(built.expires_at, int(time.time()))

    def test_the_amount_is_a_string_so_the_screen_shows_what_was_agreed(self):
        message = approval_for(payment(), decision()).message()
        self.assertIsInstance(message["amountUsd"], str)
        self.assertEqual(message["amountUsd"], "25.00")

    def test_the_domain_names_the_chain_the_payment_settles_on(self):
        typed = approval_for(payment(), decision()).payload(chain=DEFAULT_CHAIN_ID)
        self.assertEqual(typed["domain"]["chainId"], 8453)
        self.assertEqual(typed["signingMethod"], "personal_sign")
        self.assertIn("USDC on Base", typed["displayText"])


if __name__ == "__main__":
    unittest.main()
