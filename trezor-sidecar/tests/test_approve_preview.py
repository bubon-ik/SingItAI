import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from trezor_sidecar import approve_preview
from trezor_sidecar.base import BASE_USDC_ADDRESS, encode_usdc_approve
from trezor_sidecar.errors import SafeError


PAIRED = "0x2222222222222222222222222222222222222222"
SETTINGS = SimpleNamespace(derivation_path="m/44'/60'/0'/0/0", mcp_token="unused")


class FakeTrezor:
    def __init__(self):
        self.calls = []

    def sign_base_transaction(self, path, to, data):
        self.calls.append(("sign", path, to, data))
        return {"serializedTx": "0x" + "ab" * 10}

    def push_base_transaction(self, tx):
        self.calls.append(("push", tx))
        raise AssertionError("the preview must never broadcast")


class ApprovePreviewTests(TestCase):
    def run_preview(self, env):
        trezor = FakeTrezor()
        out = io.StringIO()
        with (
            patch.object(approve_preview, "_settings", return_value=SETTINGS),
            patch.object(approve_preview, "_paired_address", return_value=PAIRED),
            redirect_stdout(out),
        ):
            approve_preview.preview(env, client=trezor)
        return trezor, out.getvalue()

    def test_signs_one_approve_to_the_burn_address_and_never_pushes(self):
        trezor, _ = self.run_preview({})

        self.assertEqual(
            trezor.calls,
            [(
                "sign",
                "m/44'/60'/0'/0/0",
                BASE_USDC_ADDRESS,
                encode_usdc_approve(approve_preview.BURN_SPENDER, 1_000_000),
            )],
        )

    def test_amount_override_changes_only_the_allowance(self):
        trezor, out = self.run_preview(
            {"SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC": "300000000"}
        )

        self.assertEqual(
            trezor.calls[0][3],
            encode_usdc_approve(approve_preview.BURN_SPENDER, 300_000_000),
        )
        self.assertIn("300.00 USDC", out)

    def test_signature_value_is_never_printed(self):
        _, out = self.run_preview({})

        self.assertNotIn("ab" * 10, out)
        self.assertIn("serializedTx: string", out)

    def test_invalid_amount_is_refused_before_the_device(self):
        for raw in ("-1", "1.5", "abc", "١٢"):
            with self.subTest(raw=raw):
                trezor = FakeTrezor()
                with (
                    patch.object(approve_preview, "_settings", return_value=SETTINGS),
                    patch.object(approve_preview, "_paired_address", return_value=PAIRED),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SafeError),
                ):
                    approve_preview.preview(
                        {"SIGN402_TREZOR_PREVIEW_AMOUNT_ATOMIC": raw}, client=trezor
                    )
                self.assertEqual(trezor.calls, [])

    def test_burn_spender_is_the_canonical_dead_address(self):
        self.assertEqual(
            approve_preview.BURN_SPENDER.lower(),
            "0x000000000000000000000000000000000000dead",
        )
