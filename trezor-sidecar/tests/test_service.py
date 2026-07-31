import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_typed_data

import trezor_sidecar.service as service_module
from trezor_sidecar.config import SidecarSettings
from trezor_sidecar.errors import SafeError
from trezor_sidecar.intent import build_typed_data
from trezor_sidecar.models import Pairing, PaymentState, PurchaseIntent
from trezor_sidecar.service import TrezorSidecarService
from trezor_sidecar.store import SidecarStore


FIXED_PATH = "m/44'/60'/0'/0/0"


class FakeTrezor:
    def __init__(self, account=None):
        self.account = account or Account.create()
        self.address = self.account.address
        self.address_result = None
        self.address_failure = None
        self.sign_failure = None
        self.signature_result = None
        self.get_calls = []
        self.sign_calls = []
        self.sign_entered = None
        self.release_sign = None

    def get_base_address(self, path):
        self.get_calls.append(path)
        if self.address_failure is not None:
            raise self.address_failure
        if self.address_result is not None:
            return self.address_result
        return {"address": self.address}

    def sign_typed_data(self, path, data):
        self.sign_calls.append((path, data))
        if self.sign_entered is not None:
            self.sign_entered.set()
        if self.release_sign is not None:
            self.release_sign.wait(timeout=2)
        if self.sign_failure is not None:
            raise self.sign_failure
        if self.signature_result is not None:
            return self.signature_result
        signature = self.account.sign_message(
            encode_typed_data(full_message=data)
        ).signature.hex()
        return {"signature": signature}


class AlteredFixedFieldsIntent(PurchaseIntent):
    @property
    def payment_asset(self):
        return "ETH"

    @property
    def payment_network(self):
        return "Ethereum Mainnet"


def approve_in_process(database, private_key, intent, counter, start, results):
    account = Account.from_key(private_key)

    class ProcessTrezor:
        def sign_typed_data(self, path, data):
            with counter.get_lock():
                counter.value += 1
            time.sleep(0.2)
            return {"signature": account.sign_message(
                encode_typed_data(full_message=data)
            ).signature.hex()}

    settings = SidecarSettings(
        enabled=True,
        mcp_token="test-mcp-token",
        api_token="test-api-token",
        max_usd=Decimal("2"),
        base_rpc_url="https://base.example.invalid",
        state_path=Path(database),
    )
    service = TrezorSidecarService(
        settings,
        ProcessTrezor(),
        SidecarStore(Path(database)),
    )
    start.wait(timeout=2)
    try:
        approved = service.approve_intent(intent, 1_700_000_000)
        results.put(("ok", approved.intent_id))
    except Exception as error:
        results.put(("error", type(error).__name__, str(error)))


class TrezorSidecarServiceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "state" / "sidecar.db"

    def make_service(
        self,
        *,
        enabled=True,
        max_usd=Decimal("2"),
        derivation_path=FIXED_PATH,
        trezor=None,
        clock=lambda: 1_700_000_000,
    ):
        settings = SidecarSettings(
            enabled=enabled,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=max_usd,
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
            derivation_path=derivation_path,
        )
        store = SidecarStore(self.database)
        trezor = trezor or FakeTrezor()
        service = TrezorSidecarService(settings, trezor, store, clock=clock)
        return service, store, trezor

    @staticmethod
    def valid_intent(**changes):
        values = {
            "intent_id": "0x" + "11" * 32,
            "product_slug": "amazon-de",
            "package_id": "25",
            "denomination": "25 EUR",
            "quoted_total_usd_micros": 1_900_000,
            "max_payment_usdc_atomic": 2_000_000,
            "recipient_hash": "0x" + "22" * 32,
            "expires_at": 1_800_000_000,
        }
        values.update(changes)
        return PurchaseIntent(**values)

    def test_pairing_uses_only_fixed_path_and_refuses_silent_device_change(self):
        service, store, trezor = self.make_service()

        pairing = service.pair()
        trezor.address = "0x2222222222222222222222222222222222222222"
        with self.assertRaisesRegex(SafeError, "different Trezor") as raised:
            service.pair()

        self.assertEqual(pairing.address.lower(), store.get_pairing().address.lower())
        self.assertEqual(trezor.get_calls, [FIXED_PATH, FIXED_PATH])
        self.assertEqual(raised.exception.code, "pairing_mismatch")
        self.assertIsNone(raised.exception.__cause__)

    def test_pairing_repair_is_explicit_and_same_device_keeps_identity(self):
        ticks = iter((1_700_000_000, 1_700_000_001, 1_700_000_002))
        service, store, trezor = self.make_service(clock=lambda: next(ticks))

        first = service.pair()
        repeated = service.pair()
        trezor.address = "0x2222222222222222222222222222222222222222"
        repaired = service.pair(allow_repair=True)

        self.assertEqual(repeated.pairing_id, first.pairing_id)
        self.assertEqual(repeated.created_at, first.created_at)
        self.assertNotEqual(repaired.pairing_id, first.pairing_id)
        self.assertEqual(store.get_pairing(), repaired)

    def test_settings_snapshot_is_read_only_and_cannot_redirect_device_path(self):
        original = SidecarSettings(
            enabled=True,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=Decimal("2"),
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
        )
        store = SidecarStore(self.database)
        trezor = FakeTrezor()
        service = TrezorSidecarService(original, trezor, store)
        redirected = SidecarSettings(
            enabled=True,
            mcp_token="replacement-token",
            api_token="replacement-api-token",
            max_usd=Decimal("999"),
            base_rpc_url="https://replacement.example.invalid",
            state_path=self.database,
            derivation_path="m/44'/60'/0'/0/9",
        )

        with self.assertRaises(AttributeError):
            service.settings = redirected
        object.__setattr__(original, "derivation_path", "m/44'/60'/0'/0/8")
        service.pair()

        self.assertEqual(service.settings.derivation_path, FIXED_PATH)
        self.assertEqual(trezor.get_calls, [FIXED_PATH])
        self.assertEqual(stat.S_IMODE(service._device_lock_path.stat().st_mode), 0o600)

    def test_settings_property_returns_independent_defensive_copies(self):
        service, _, trezor = self.make_service()
        first = service.settings
        second = service.settings

        self.assertIsNot(first, second)
        object.__setattr__(first, "enabled", False)
        object.__setattr__(first, "chain_id", 1)
        object.__setattr__(first, "max_usd", Decimal("0.01"))
        object.__setattr__(first, "derivation_path", "m/44'/60'/0'/0/7")
        service.pair()
        service.approve_intent(self.valid_intent(), now=1_700_000_000)

        self.assertTrue(second.enabled)
        self.assertEqual(second.chain_id, 8453)
        self.assertEqual(second.max_usd, Decimal("2"))
        self.assertEqual(second.derivation_path, FIXED_PATH)
        self.assertEqual(service.settings.derivation_path, FIXED_PATH)
        self.assertEqual(trezor.get_calls, [FIXED_PATH])
        self.assertEqual(trezor.sign_calls[0][0], FIXED_PATH)

    def test_inflight_pairing_uses_fresh_snapshot_when_exposed_copy_is_mutated(self):
        clock_entered = threading.Event()
        release_clock = threading.Event()

        def controlled_clock():
            clock_entered.set()
            release_clock.wait(timeout=2)
            return 1_700_000_000

        service, _, trezor = self.make_service(clock=controlled_clock)
        exposed = service.settings
        with ThreadPoolExecutor(max_workers=1) as pool:
            pairing = pool.submit(service.pair)
            self.assertTrue(clock_entered.wait(timeout=2))
            object.__setattr__(exposed, "enabled", False)
            object.__setattr__(exposed, "chain_id", 1)
            object.__setattr__(exposed, "max_usd", Decimal("0.01"))
            object.__setattr__(exposed, "derivation_path", "m/44'/60'/0'/0/6")
            release_clock.set()
            result = pairing.result(timeout=2)

        self.assertEqual(result.derivation_path, FIXED_PATH)
        self.assertEqual(trezor.get_calls, [FIXED_PATH])

    def test_invalid_or_raising_pair_clock_fails_before_device(self):
        invalid_clocks = (
            lambda: float("nan"),
            lambda: float("inf"),
            lambda: -float("inf"),
            lambda: 1 << 63,
            lambda: (_ for _ in ()).throw(RuntimeError("canary clock detail")),
            lambda: (_ for _ in ()).throw(SafeError("canary", "canary safe detail")),
        )
        for clock in invalid_clocks:
            with self.subTest(clock=clock):
                service, _, trezor = self.make_service(clock=clock)
                with self.assertRaisesRegex(SafeError, "clock") as raised:
                    service.pair()
                self.assertEqual(raised.exception.code, "invalid_clock")
                self.assertNotIn("canary", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(trezor.get_calls, [])

    def test_unsafe_device_lock_path_fails_closed_before_device(self):
        service, _, trezor = self.make_service()
        target = Path(self.temporary.name) / "lock-target"
        target.touch(mode=0o600)
        service._device_lock_path.symlink_to(target)

        with self.assertRaisesRegex(SafeError, "device lock") as raised:
            service.pair()

        self.assertEqual(raised.exception.code, "device_lock_unavailable")
        self.assertEqual(trezor.get_calls, [])
        self.assertFalse(target.read_bytes())

    def test_guard_cleanup_preserves_operation_error_and_releases_thread_lock(self):
        service, _, _ = self.make_service()
        service.pair()
        original_error = SafeError("original", "original operation error")
        real_flock = service_module.fcntl.flock
        real_close = service_module.os.close
        close_calls = 0

        def failing_unlock(descriptor, operation):
            if operation == service_module.fcntl.LOCK_UN:
                raise OSError("canary unlock failure")
            return real_flock(descriptor, operation)

        def failing_lock_close(descriptor):
            nonlocal close_calls
            close_calls += 1
            real_close(descriptor)
            if close_calls == 2:
                raise OSError("canary close failure")

        caught = None
        with (
            patch.object(service_module.fcntl, "flock", side_effect=failing_unlock),
            patch.object(service_module.os, "close", side_effect=failing_lock_close),
        ):
            try:
                with service._device_guard():
                    raise original_error
            except BaseException as error:
                caught = error

        with service._device_guard(blocking=False) as acquired_after_cleanup:
            pass

        self.assertIs(caught, original_error)
        self.assertTrue(acquired_after_cleanup)

    def test_pairing_accepts_only_closed_address_result_paths(self):
        service, _, trezor = self.make_service()
        trezor.address_result = {"payload": {"address": trezor.address}}
        self.assertEqual(service.pair().address, trezor.address)

        invalid_results = (
            {"result": {"address": trezor.address}},
            {"address": trezor.address, "payload": {"address": trezor.address}},
            {"address": 123},
            {"payload": {"address": "not-an-address"}},
            [trezor.address],
        )
        for result in invalid_results:
            with self.subTest(result=result):
                trezor.address_result = result
                with self.assertRaisesRegex(SafeError, "Trezor Suite is unavailable") as raised:
                    service.pair()
                self.assertEqual(raised.exception.code, "trezor_unavailable")
                self.assertIsNone(raised.exception.__cause__)

    def test_disabled_or_nonfixed_pairing_fails_before_device(self):
        disabled, _, disabled_trezor = self.make_service(enabled=False)
        with self.assertRaisesRegex(SafeError, "disabled") as raised:
            disabled.pair()
        self.assertEqual(raised.exception.code, "disabled")
        self.assertEqual(disabled_trezor.get_calls, [])

        service, _, trezor = self.make_service(derivation_path="m/44'/60'/0'/0/1")
        with self.assertRaisesRegex(SafeError, "fixed Base"):
            service.pair()
        self.assertEqual(trezor.get_calls, [])

    def test_intent_is_approved_only_for_paired_signer(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent()

        approved = service.approve_intent(intent, now=1_700_000_000)

        self.assertEqual(approved, intent)
        record = store.get_intent(intent.intent_id)
        self.assertEqual(record.state, PaymentState.DEVICE_APPROVED)
        self.assertEqual(record.approved_at, 1_700_000_000)
        self.assertEqual(trezor.sign_calls[0][0], FIXED_PATH)
        self.assertEqual(trezor.sign_calls[0][1], build_typed_data(intent))
        self.assertEqual(trezor.sign_calls[0][1]["domain"]["chainId"], 8453)
        self.assertEqual(trezor.sign_calls[0][1]["message"]["paymentAsset"], "USDC")
        self.assertEqual(trezor.sign_calls[0][1]["message"]["paymentNetwork"], "Base Mainnet")

    def test_missing_pair_expiry_cap_and_changed_replay_fail_before_signing(self):
        service, store, trezor = self.make_service(max_usd=Decimal("2.0000001"))
        with self.assertRaisesRegex(SafeError, "paired") as missing:
            service.approve_intent(self.valid_intent(), now=1_700_000_000)
        self.assertEqual(missing.exception.code, "not_paired")

        service.pair()
        cases = (
            (self.valid_intent(expires_at=1_700_000_000), "expired"),
            (self.valid_intent(max_payment_usdc_atomic=2_000_001), "limit"),
        )
        for intent, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SafeError, message):
                    service.approve_intent(intent, now=1_700_000_000)
                self.assertEqual(trezor.sign_calls, [])

        existing = self.valid_intent()
        store.insert_intent(existing, created_at=1_699_999_999)
        changed = self.valid_intent(product_slug="different")
        with self.assertRaisesRegex(SafeError, "conflicts") as conflict:
            service.approve_intent(changed, now=1_700_000_000)
        self.assertEqual(conflict.exception.code, "intent_conflict")
        self.assertEqual(trezor.sign_calls, [])

    def test_cap_comparison_uses_decimal_atomic_boundary_without_float_rounding(self):
        service, _, trezor = self.make_service(max_usd=Decimal("2.0000009"))
        service.pair()

        service.approve_intent(
            self.valid_intent(max_payment_usdc_atomic=2_000_000),
            now=1_700_000_000,
        )

        self.assertEqual(len(trezor.sign_calls), 1)

    def test_fixed_field_or_pairing_path_mismatch_fails_before_signing(self):
        service, store, trezor = self.make_service()
        service.pair()
        values = self.valid_intent().__dict__
        altered = AlteredFixedFieldsIntent(**values)
        with self.assertRaisesRegex(SafeError, "fixed") as fixed:
            service.approve_intent(altered, now=1_700_000_000)
        self.assertEqual(fixed.exception.code, "invalid_intent")
        self.assertEqual(trezor.sign_calls, [])

        other_database = Path(self.temporary.name) / "other" / "sidecar.db"
        other_store = SidecarStore(other_database)
        other_store.save_pairing(Pairing(
            pairing_id="tampered",
            address=trezor.address,
            derivation_path="m/44'/60'/0'/0/1",
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        ))
        settings = service.settings
        mismatched = TrezorSidecarService(settings, trezor, other_store)
        with self.assertRaisesRegex(SafeError, "pairing"):
            mismatched.approve_intent(self.valid_intent(), now=1_700_000_000)
        self.assertEqual(trezor.sign_calls, [])

    def test_identical_approved_replay_returns_without_another_mcp_call(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent()
        first = service.approve_intent(intent, now=1_700_000_000)

        replay = service.approve_intent(intent, now=1_700_000_001)

        self.assertEqual(replay, first)
        self.assertEqual(len(trezor.sign_calls), 1)
        self.assertEqual(store.get_intent(intent.intent_id).approved_at, 1_700_000_000)

    def test_identical_approved_replay_survives_later_expiry_and_cap_change(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent(expires_at=1_700_000_001)
        service.approve_intent(intent, now=1_700_000_000)
        alternate_settings = SidecarSettings(
            enabled=True,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=Decimal("0.01"),
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
        )
        replay_service = TrezorSidecarService(alternate_settings, trezor, store)

        replay = replay_service.approve_intent(intent, now=1_700_000_002)

        self.assertEqual(replay, intent)
        self.assertEqual(len(trezor.sign_calls), 1)

    def test_signature_is_accepted_only_from_closed_paths_and_valid_format(self):
        account = Account.create()
        service, store, trezor = self.make_service(trezor=FakeTrezor(account))
        service.pair()
        for nested in (False, True):
            with self.subTest(nested=nested):
                intent = self.valid_intent(intent_id="0x" + ("33" if nested else "44") * 32)
                signature = account.sign_message(
                    encode_typed_data(full_message=build_typed_data(intent))
                ).signature.hex()
                trezor.signature_result = (
                    {"payload": {"signature": "0x" + signature}}
                    if nested else {"signature": signature}
                )
                self.assertEqual(service.approve_intent(intent, 1_700_000_000), intent)
                self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.DEVICE_APPROVED)

        invalid_results = (
            {"result": {"signature": "0x" + "11" * 65}},
            {"signature": "0x1234"},
            {"signature": 123},
            {"signature": "0x" + "11" * 65, "payload": {"signature": "0x" + "11" * 65}},
        )
        for index, result in enumerate(invalid_results, start=5):
            with self.subTest(result=result):
                intent = self.valid_intent(intent_id="0x" + f"{index:02x}" * 32)
                trezor.signature_result = result
                with self.assertRaisesRegex(SafeError, "valid approval") as raised:
                    service.approve_intent(intent, 1_700_000_000)
                self.assertEqual(raised.exception.code, "invalid_signature")
                self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.QUOTED)
                self.assertIsNone(raised.exception.__cause__)

    def test_mismatched_signer_fails_without_approval(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent()
        other = Account.create()
        trezor.signature_result = {"signature": other.sign_message(
            encode_typed_data(full_message=build_typed_data(intent))
        ).signature.hex()}

        with self.assertRaisesRegex(SafeError, "paired Trezor") as raised:
            service.approve_intent(intent, 1_700_000_000)

        self.assertEqual(raised.exception.code, "signer_mismatch")
        self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.QUOTED)

    def test_device_rejection_timeout_and_unexpected_failure_are_sanitized(self):
        service, store, trezor = self.make_service()
        service.pair()
        failures = (
            (
                SafeError("device_rejected", "canary upstream rejection"),
                "device_rejected",
                "Purchase approval was cancelled on Trezor.",
                400,
            ),
            (
                TimeoutError("canary timeout detail"),
                "device_timeout",
                "Trezor approval timed out.",
                504,
            ),
            (
                RuntimeError("canary unexpected detail"),
                "trezor_unavailable",
                "Trezor Suite is unavailable.",
                503,
            ),
        )
        for index, (failure, code, message, status) in enumerate(failures, start=8):
            with self.subTest(code=code):
                intent = self.valid_intent(intent_id="0x" + f"{index:02x}" * 32)
                trezor.sign_failure = failure
                with self.assertRaises(SafeError) as raised:
                    service.approve_intent(intent, 1_700_000_000)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.message, message)
                self.assertEqual(raised.exception.status, status)
                self.assertNotIn("canary", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.QUOTED)

    def test_approved_state_persists_no_signature_or_mcp_payload(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent()
        signature = trezor.account.sign_message(
            encode_typed_data(full_message=build_typed_data(intent))
        ).signature.hex()
        trezor.signature_result = {
            "signature": signature,
            "upstreamCanary": "never-persist-this-payload",
        }
        service.approve_intent(intent, 1_700_000_000)

        connection = sqlite3.connect(self.database)
        try:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(intents)")]
            row = connection.execute("SELECT * FROM intents WHERE intent_id = ?", (intent.intent_id,)).fetchone()
        finally:
            connection.close()
        self.assertNotIn("signature", columns)
        self.assertNotIn("payload", columns)
        self.assertNotIn(signature, repr(row))
        self.assertNotIn("never-persist-this-payload", repr(row))
        self.assertEqual(store.get_intent(intent.intent_id).approved_at, 1_700_000_000)

    def test_concurrent_identical_approvals_prompt_device_once(self):
        service, store, trezor = self.make_service()
        service.pair()
        intent = self.valid_intent()
        trezor.sign_entered = threading.Event()
        trezor.release_sign = threading.Event()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.approve_intent, intent, 1_700_000_000)
            self.assertTrue(trezor.sign_entered.wait(timeout=2))
            second = pool.submit(service.approve_intent, intent, 1_700_000_001)
            trezor.release_sign.set()
            results = (first.result(timeout=2), second.result(timeout=2))

        self.assertEqual(results, (intent, intent))
        self.assertEqual(len(trezor.sign_calls), 1)
        self.assertEqual(store.get_intent(intent.intent_id).approved_at, 1_700_000_000)

    def test_cross_process_identical_approvals_prompt_device_once(self):
        account = Account.create()
        service, store, _ = self.make_service(trezor=FakeTrezor(account))
        service.pair()
        intent = self.valid_intent()
        context = get_context("fork")
        counter = context.Value("i", 0)
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=approve_in_process,
                args=(self.database, account.key, intent, counter, start, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        outputs = sorted(results.get(timeout=2) for _ in processes)
        self.assertEqual(outputs, [("ok", intent.intent_id), ("ok", intent.intent_id)])
        self.assertEqual(counter.value, 1)
        self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.DEVICE_APPROVED)


if __name__ == "__main__":
    import unittest

    unittest.main()
