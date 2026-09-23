from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from eth_account import Account
from eth_utils import keccak

from trezor_sidecar import allowance
from trezor_sidecar.base import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    BaseBalances,
    encode_usdc_approve,
    encode_usdc_transfer,
)
from trezor_sidecar.errors import SafeError


LIMITER = "0x1111111111111111111111111111111111111111"
AGENT = "0x2222222222222222222222222222222222222222"
GUARDIAN = "0x3333333333333333333333333333333333333333"
OTHER = "0x4444444444444444444444444444444444444444"
NOW = 1_800_000_000
SETTINGS = SimpleNamespace(
    derivation_path="m/44'/60'/0'/0/0",
    mcp_token="unused",
    base_rpc_url="https://rpc.invalid",
    max_usd=Decimal("1.00"),
)


def word(address):
    return int(address, 16)


class FakeRpc:
    def __init__(self, owner, **limiter):
        self.code = True
        self.values = {
            "owner": word(owner),
            "token": word(BASE_USDC_ADDRESS),
            "agent": word(AGENT),
            "guardian": word(GUARDIAN),
            "dailyCap": 600_000,
            "perPurchaseCap": 300_000,
            "expiry": NOW + 7 * 86400,
            "paused": 0,
            "remainingToday": 600_000,
        }
        self.values.update(limiter)
        self.allowance = 0
        self.receipts = [None, 1]
        self.calls = []

    def has_code(self, address):
        return self.code

    def call_word(self, to, data):
        name = next(k for k, v in allowance._GETTERS.items() if v == data)
        return self.values[name]

    def usdc_allowance(self, owner, spender):
        return self.allowance

    def receipt_status(self, tx_hash):
        return self.receipts.pop(0) if self.receipts else 1

    def get_balances(self, address):
        return BaseBalances(eth_wei=1, usdc_atomic=6_526_336)


class FakeTrezor:
    """Signs a real EIP-1559 transaction, optionally not the one requested."""

    def __init__(self, account, rpc, *, tamper=None, signer=None, broadcast_hash=None):
        self.account = account
        self.rpc = rpc
        self.tamper = tamper or {}
        self.signer = signer or account
        self.broadcast_hash = broadcast_hash
        self.signed = []
        self.pushed = []

    def sign_base_transaction(self, path, to, data):
        self.signed.append((path, to, data))
        tx = {
            "type": 2, "chainId": BASE_CHAIN_ID, "nonce": 3,
            "maxPriorityFeePerGas": 1_000_000, "maxFeePerGas": 2_000_000,
            "gas": 60_000, "to": to, "value": 0, "data": data, "accessList": [],
        }
        tx.update(self.tamper)
        raw = Account.sign_transaction(tx, self.signer.key).raw_transaction.to_0x_hex()
        return {"serializedTx": raw, "r": "0x1", "s": "0x2", "v": "0x1"}

    def push_base_transaction(self, raw):
        self.pushed.append(raw)
        # The fake chain applies the approve once it is broadcast.
        self.rpc.allowance = self._amount(self.signed[-1][2])
        return {"txid": self.broadcast_hash or "0x" + keccak(bytes.fromhex(raw[2:])).hex()}

    @staticmethod
    def _amount(calldata):
        return int(calldata[-64:], 16)


class AllowanceTests(TestCase):
    def setUp(self):
        self.account = Account.create()
        self.rpc = FakeRpc(self.account.address)
        self.trezor = FakeTrezor(self.account, self.rpc)
        self.output = []
        self.clock = [NOW]
        patches = [
            patch.object(allowance, "_settings", return_value=SETTINGS),
            patch.object(allowance, "_paired_address", return_value=self.account.address),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def deps(self):
        def sleep(seconds):
            self.clock[0] += seconds

        return dict(
            env={}, trezor=self.trezor, rpc=self.rpc,
            now=lambda: self.clock[0], sleep=sleep, out=self.output.append,
        )

    def text(self):
        return "\n".join(self.output)

    # --- grant ---

    def test_grant_signs_verifies_and_broadcasts_exactly_the_requested_approve(self):
        tx_hash = allowance.grant(LIMITER, "1.00", **self.deps())

        self.assertEqual(
            self.trezor.signed,
            [(SETTINGS.derivation_path, BASE_USDC_ADDRESS, encode_usdc_approve(LIMITER, 1_000_000))],
        )
        self.assertEqual(len(self.trezor.pushed), 1)
        self.assertEqual(tx_hash, "0x" + keccak(bytes.fromhex(self.trezor.pushed[0][2:])).hex())
        self.assertIn("ALLOWANCE        1 USDC", self.text())
        self.assertIn(f"https://basescan.org/address/{LIMITER}#code", self.text())
        self.assertIn("Granted. Allowance now 1 USDC.", self.text())

    def test_grant_refuses_a_limiter_it_cannot_vouch_for_before_the_device(self):
        cases = {
            "no code": dict(code=False),
            "other owner": dict(owner=word(OTHER)),
            "other token": dict(token=word(OTHER)),
            "paused": dict(paused=1),
            "expired": dict(expiry=NOW),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.rpc = FakeRpc(self.account.address)
                if change.pop("code", True) is False:
                    self.rpc.code = False
                self.rpc.values.update(change)
                self.trezor = FakeTrezor(self.account, self.rpc)
                with self.assertRaises(SafeError) as raised:
                    allowance.grant(LIMITER, "1.00", **self.deps())
                self.assertEqual(raised.exception.code, "limiter_invalid")
                self.assertEqual(self.trezor.signed, [])

    def test_grant_refuses_bad_amounts_before_the_device(self):
        for text in ("0", "0.0", "-1", "1.0000001", "abc", "1e2", "1.01", "", " 1"):
            with self.subTest(text=text):
                with self.assertRaises(SafeError):
                    allowance.grant(LIMITER, text, **self.deps())
                self.assertEqual(self.trezor.signed, [])

    def test_grant_never_broadcasts_a_signature_for_something_else(self):
        tampered = {
            "other spender": dict(data=encode_usdc_approve(OTHER, 1_000_000)),
            "other amount": dict(data=encode_usdc_approve(LIMITER, 1_000_001)),
            "a transfer": dict(data=encode_usdc_transfer(LIMITER, 1_000_000)),
            "other contract": dict(to=OTHER),
            "with value": dict(value=1),
            "other chain": dict(chainId=1),
        }
        for name, change in tampered.items():
            with self.subTest(name):
                self.trezor = FakeTrezor(self.account, self.rpc, tamper=change)
                with self.assertRaises(SafeError) as raised:
                    allowance.grant(LIMITER, "1.00", **self.deps())
                self.assertEqual(raised.exception.code, "invalid_signed_transaction")
                self.assertEqual(self.trezor.pushed, [])

    def test_grant_never_broadcasts_a_signature_from_another_account(self):
        self.trezor = FakeTrezor(self.account, self.rpc, signer=Account.create())
        with self.assertRaises(SafeError) as raised:
            allowance.grant(LIMITER, "1.00", **self.deps())
        self.assertEqual(raised.exception.code, "invalid_signed_transaction")
        self.assertEqual(self.trezor.pushed, [])

    def test_a_different_broadcast_hash_is_reported_not_trusted(self):
        self.trezor = FakeTrezor(self.account, self.rpc, broadcast_hash="0x" + "ab" * 32)
        with self.assertRaises(SafeError) as raised:
            allowance.grant(LIMITER, "1.00", **self.deps())
        self.assertEqual(raised.exception.code, "broadcast_ambiguous")

    def test_a_reverted_approve_is_an_error(self):
        self.rpc.receipts = [0]
        with self.assertRaises(SafeError) as raised:
            allowance.grant(LIMITER, "1.00", **self.deps())
        self.assertEqual(raised.exception.code, "transaction_failed")

    def test_an_unmined_approve_says_not_to_sign_again(self):
        self.rpc.receipts = [None] * 1000
        with self.assertRaises(SafeError) as raised:
            allowance.grant(LIMITER, "1.00", **self.deps())
        self.assertEqual(raised.exception.code, "receipt_pending")
        self.assertIn("Do not sign again", raised.exception.message)
        self.assertEqual(len(self.trezor.pushed), 1)

    # --- revoke ---

    def test_revoke_works_for_any_spender_without_inspecting_it(self):
        self.rpc.code = False
        self.rpc.values["owner"] = word(OTHER)
        self.rpc.allowance = 1_000_000

        allowance.revoke(LIMITER, **self.deps())

        self.assertEqual(
            self.trezor.signed,
            [(SETTINGS.derivation_path, BASE_USDC_ADDRESS, encode_usdc_approve(LIMITER, 0))],
        )
        self.assertEqual(len(self.trezor.pushed), 1)
        self.assertIn("Revoked. Allowance now 0 USDC.", self.text())

    def test_revoke_never_broadcasts_a_nonzero_approve(self):
        self.trezor = FakeTrezor(
            self.account, self.rpc, tamper=dict(data=encode_usdc_approve(LIMITER, 1))
        )
        with self.assertRaises(SafeError):
            allowance.revoke(LIMITER, **self.deps())
        self.assertEqual(self.trezor.pushed, [])

    # --- status and parsing ---

    def test_status_reads_everything_and_signs_nothing(self):
        self.rpc.allowance = 400_000
        allowance.status(LIMITER, **self.deps())
        text = self.text()
        for expected in (
            "daily cap        0.6 USDC", "per purchase     0.3 USDC",
            "allowance left   0.4 USDC", "owner balance    6.526336 USDC",
            f"agent            {AGENT}",
        ):
            self.assertIn(expected, text)
        self.assertEqual(self.trezor.signed, [])

    def test_amounts_are_exact_atomic_units_up_to_the_configured_cap(self):
        cap = Decimal("1.00")
        self.assertEqual(allowance.parse_amount("1.00", cap), 1_000_000)
        self.assertEqual(allowance.parse_amount("0.3", cap), 300_000)
        self.assertEqual(allowance.parse_amount("0.000001", cap), 1)
        with self.assertRaises(SafeError) as raised:
            allowance.parse_amount("1.000001", cap)
        self.assertEqual(raised.exception.code, "limit_exceeded")

    def test_limiter_address_must_be_well_formed(self):
        for text in ("0x1234", "1111111111111111111111111111111111111111", "0x" + "00" * 20):
            with self.subTest(text=text):
                with self.assertRaises(SafeError):
                    allowance.revoke(text, **self.deps())
