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
from eth_utils import keccak

import trezor_sidecar.service as service_module
from trezor_sidecar.base import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    BaseBalances,
    encode_usdc_transfer,
)
from trezor_sidecar.config import SidecarSettings
from trezor_sidecar.errors import SafeError
from trezor_sidecar.intent import build_typed_data
from trezor_sidecar.models import (
    Pairing,
    PaymentRequest,
    PaymentState,
    PurchaseIntent,
)
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
        self.sign_transaction_calls = []
        self.push_transaction_calls = []
        self.sign_transaction_failure = None
        self.signed_transaction_result = None
        self.transaction_updates = {}
        self.transaction_account = None
        self.transaction_entered = None
        self.release_transaction = None
        self.push_transaction_failure = None
        self.push_transaction_result = None

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

    def sign_base_transaction(self, path, to, data):
        call = {
            "path": path,
            "to": to,
            "data": data,
            "chainId": BASE_CHAIN_ID,
            "value": "0",
            "broadcast": False,
        }
        self.sign_transaction_calls.append(call)
        if self.transaction_entered is not None:
            self.transaction_entered.set()
        if self.release_transaction is not None:
            self.release_transaction.wait(timeout=2)
        if self.sign_transaction_failure is not None:
            raise self.sign_transaction_failure
        if self.signed_transaction_result is not None:
            return self.signed_transaction_result
        transaction = {
            "type": 2,
            "chainId": BASE_CHAIN_ID,
            "nonce": 7,
            "maxPriorityFeePerGas": 1_000_000,
            "maxFeePerGas": 2_000_000,
            "gas": 80_000,
            "to": to,
            "value": 0,
            "data": data,
            "accessList": [],
        }
        transaction.update(self.transaction_updates)
        account = self.transaction_account or self.account
        signed = Account.sign_transaction(transaction, account.key)
        return {"payload": {"serializedTx": signed.raw_transaction.to_0x_hex()}}

    def push_base_transaction(self, tx):
        self.push_transaction_calls.append(tx)
        if self.push_transaction_failure is not None:
            raise self.push_transaction_failure
        if self.push_transaction_result is not None:
            if callable(self.push_transaction_result):
                return self.push_transaction_result(tx)
            return self.push_transaction_result
        return {"txid": "0x" + keccak(bytes.fromhex(tx[2:])).hex()}


class FakeRpc:
    def __init__(self):
        self.balances = BaseBalances(
            eth_wei=100_000_000_000_001,
            usdc_atomic=2_000_000,
        )
        self.calls = []
        self.failure = None
        self.result = None

    def get_balances(self, address):
        self.calls.append(address)
        if self.failure is not None:
            raise self.failure
        if self.result is not None:
            return self.result
        return self.balances


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


def run_payment_in_process(
    database,
    private_key,
    payment_id,
    role,
    transaction_entered,
    release_transaction,
    results,
):
    account = Account.from_key(private_key)

    class ProcessTrezor:
        def __init__(self):
            self.sign_calls = 0
            self.push_calls = 0

        def sign_base_transaction(self, path, to, data):
            self.sign_calls += 1
            if transaction_entered is not None:
                transaction_entered.set()
            if release_transaction is not None:
                release_transaction.wait(timeout=5)
            transaction = {
                "type": 2,
                "chainId": BASE_CHAIN_ID,
                "nonce": 7,
                "maxPriorityFeePerGas": 1_000_000,
                "maxFeePerGas": 2_000_000,
                "gas": 80_000,
                "to": to,
                "value": 0,
                "data": data,
                "accessList": [],
            }
            signed = Account.sign_transaction(transaction, account.key)
            return {"payload": {"serializedTx": signed.raw_transaction.to_0x_hex()}}

        def push_base_transaction(self, raw):
            self.push_calls += 1
            return {"txid": "0x" + keccak(bytes.fromhex(raw[2:])).hex()}

    class ProcessRpc:
        def __init__(self):
            self.calls = 0

        def get_balances(self, address):
            self.calls += 1
            return BaseBalances(
                eth_wei=100_000_000_000_001,
                usdc_atomic=2_000_000,
            )

    settings = SidecarSettings(
        enabled=True,
        mcp_token="test-mcp-token",
        api_token="test-api-token",
        max_usd=Decimal("2"),
        base_rpc_url="https://base.example.invalid",
        state_path=Path(database),
    )
    trezor = ProcessTrezor()
    rpc = ProcessRpc()
    service = TrezorSidecarService(
        settings,
        trezor,
        SidecarStore(Path(database)),
        rpc=rpc,
    )
    try:
        payment = service.run_payment(payment_id, now=lambda: 1_700_000_001)
        results.put(
            (
                role,
                "ok",
                payment.state.value,
                rpc.calls,
                trezor.sign_calls,
                trezor.push_calls,
            )
        )
    except SafeError as error:
        results.put(
            (
                role,
                "safe",
                error.code,
                error.message,
                error.status,
                rpc.calls,
                trezor.sign_calls,
                trezor.push_calls,
            )
        )
    except Exception as error:
        results.put(
            (
                role,
                "error",
                type(error).__name__,
                str(error),
                rpc.calls,
                trezor.sign_calls,
                trezor.push_calls,
            )
        )


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
        rpc=None,
        clock=lambda: 1_699_999_999,
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
        service = TrezorSidecarService(
            settings,
            trezor,
            store,
            clock=clock,
            rpc=rpc,
        )
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

    @staticmethod
    def valid_payment_request(**changes):
        values = {
            "intent_id": "0x" + "11" * 32,
            "invoice_id": "invoice-1",
            "pay_to": "0x1111111111111111111111111111111111111111",
            "amount_atomic": 1_900_000,
            "expires_at": 1_700_000_100,
        }
        values.update(changes)
        return PaymentRequest(**values)

    def make_approved_payment_service(self):
        rpc = FakeRpc()
        service, store, trezor = self.make_service(rpc=rpc)
        service.pair()
        service.approve_intent(self.valid_intent(), now=1_700_000_000)
        return service, store, trezor, rpc

    def create_additional_payment(
        self,
        service,
        *,
        ordinal,
        invoice_id,
        idempotency_key,
        **request_changes,
    ):
        intent_id = "0x" + f"{0x40 + ordinal:02x}" * 32
        service.approve_intent(
            self.valid_intent(intent_id=intent_id),
            now=1_700_000_000,
        )
        return service.create_payment(
            self.valid_payment_request(
                intent_id=intent_id,
                invoice_id=invoice_id,
                **request_changes,
            ),
            idempotency_key,
            1_700_000_000,
        )

    def test_payment_signs_verifies_then_broadcasts_once(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(),
            idempotency_key="pay-key-1",
            now=1_700_000_000,
        )

        completed = service.run_payment(
            payment.payment_id,
            now=lambda: 1_700_000_001,
        )

        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)
        self.assertEqual(trezor.sign_transaction_calls[0]["broadcast"], False)
        self.assertEqual(trezor.sign_transaction_calls[0]["path"], FIXED_PATH)
        self.assertEqual(trezor.sign_transaction_calls[0]["to"], BASE_USDC_ADDRESS)
        self.assertEqual(
            trezor.sign_transaction_calls[0]["data"],
            encode_usdc_transfer(
                self.valid_payment_request().pay_to,
                self.valid_payment_request().amount_atomic,
            ),
        )
        self.assertEqual(rpc.calls, [trezor.address])
        self.assertEqual(store.get_payment(payment.payment_id), completed)
        connection = sqlite3.connect(self.database)
        try:
            persisted = repr(connection.execute(
                "SELECT * FROM payments WHERE payment_id = ?",
                (payment.payment_id,),
            ).fetchone())
        finally:
            connection.close()
        self.assertNotIn(trezor.push_transaction_calls[0], persisted)

    def test_payment_creation_validates_request_limits_expiry_and_replays_strictly(self):
        service, store, _, _ = self.make_approved_payment_service()
        request = self.valid_payment_request()
        first = service.create_payment(request, "pay-key-1", 1_700_000_000)

        replay = service.create_payment(request, "pay-key-1", 1_700_000_001)

        self.assertEqual(replay, first)
        late_replay = service.create_payment(request, "pay-key-1", 1_800_000_000)
        self.assertEqual(late_replay, first)
        conflict_requests = (
            self.valid_payment_request(amount_atomic=1_800_000),
            self.valid_payment_request(invoice_id="invoice-2"),
        )
        for changed in conflict_requests:
            with self.subTest(changed=changed):
                with self.assertRaises(SafeError) as raised:
                    service.create_payment(changed, "pay-key-1", 1_700_000_001)
                self.assertEqual(raised.exception.code, "payment_conflict")
                self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(SafeError) as late_conflict:
            service.create_payment(
                self.valid_payment_request(amount_atomic=1_800_000),
                "pay-key-1",
                1_800_000_000,
            )
        self.assertEqual(late_conflict.exception.code, "payment_conflict")
        with self.assertRaises(SafeError) as reused_invoice:
            service.create_payment(request, "pay-key-2", 1_800_000_000)
        self.assertEqual(reused_invoice.exception.code, "payment_conflict")

        disabled_settings = SidecarSettings(
            enabled=False,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=Decimal("0.01"),
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
        )
        replay_service = TrezorSidecarService(
            disabled_settings,
            service.trezor,
            store,
            rpc=FakeRpc(),
        )
        self.assertEqual(
            replay_service.create_payment(request, "pay-key-1", 1_700_000_001),
            first,
        )

        invalid = (
            (object(), "key", 1_700_000_000, "invalid_request"),
            (request, "", 1_700_000_000, "invalid_request"),
            (request, "key", True, "invalid_request"),
            (request, "key", 1 << 63, "invalid_request"),
            (
                self.valid_payment_request(invoice_id="expired", expires_at=1_700_000_000),
                "expired-key",
                1_700_000_000,
                "invoice_expired",
            ),
            (
                self.valid_payment_request(invoice_id="too-high", amount_atomic=2_000_001),
                "high-key",
                1_700_000_000,
                "payment_limit_exceeded",
            ),
        )
        for candidate, key, timestamp, code in invalid:
            with self.subTest(code=code):
                with self.assertRaises(SafeError) as raised:
                    service.create_payment(candidate, key, timestamp)
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(store.get_payment(first.payment_id), first)

    def test_payment_replay_clock_cannot_precede_durable_creation(self):
        # Break caught: an idempotent replay accepts a clock older than the recorded payment.
        service, store, trezor, _ = self.make_approved_payment_service()
        request = self.valid_payment_request(
            invoice_id="clocked-replay",
            expires_at=1_700_001_000,
        )
        created = service.create_payment(
            request,
            "clocked-replay-key",
            1_700_000_200,
        )

        with self.assertRaises(SafeError) as backdated:
            service.create_payment(
                request,
                "clocked-replay-key",
                1_700_000_150,
            )
        boundary = service.create_payment(
            request,
            "clocked-replay-key",
            1_700_000_200,
        )

        self.assertEqual(backdated.exception.code, "invalid_clock")
        self.assertEqual(boundary, created)
        self.assertEqual(store.get_payment(created.payment_id), created)
        connection = store._connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payments WHERE invoice_id = ?",
                    (request.invoice_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_payment_creation_rechecks_enabled_fixed_config_and_decimal_cap(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        for enabled, path, code in (
            (False, FIXED_PATH, "disabled"),
            (True, "m/44'/60'/0'/0/9", "invalid_configuration"),
        ):
            settings = SidecarSettings(
                enabled=enabled,
                mcp_token="test-mcp-token",
                api_token="test-api-token",
                max_usd=Decimal("2"),
                base_rpc_url="https://base.example.invalid",
                state_path=self.database,
                derivation_path=path,
            )
            candidate = TrezorSidecarService(settings, trezor, store, rpc=rpc)
            with self.subTest(code=code), self.assertRaises(SafeError) as raised:
                candidate.create_payment(
                    self.valid_payment_request(invoice_id=f"invoice-{code}"),
                    f"key-{code}",
                    1_700_000_000,
                )
            self.assertEqual(raised.exception.code, code)

        capped_settings = SidecarSettings(
            enabled=True,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=Decimal("1.9000009"),
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
        )
        capped = TrezorSidecarService(capped_settings, trezor, store, rpc=rpc)
        accepted = capped.create_payment(
            self.valid_payment_request(invoice_id="cap-ok", amount_atomic=1_900_000),
            "cap-ok-key",
            1_700_000_000,
        )
        self.assertEqual(accepted.state, PaymentState.INVOICE_CREATED)
        with self.assertRaises(SafeError) as raised:
            capped.create_payment(
                self.valid_payment_request(invoice_id="cap-high", amount_atomic=1_900_001),
                "cap-high-key",
                1_700_000_000,
            )
        self.assertEqual(raised.exception.code, "payment_limit_exceeded")

    def test_new_payment_configuration_errors_precede_missing_pairing(self):
        # Break caught: deterministic configuration errors are masked by an absent pairing.
        for enabled, path, code in (
            (False, FIXED_PATH, "disabled"),
            (True, "m/44'/60'/0'/0/9", "invalid_configuration"),
        ):
            service, _, trezor = self.make_service(
                enabled=enabled,
                derivation_path=path,
                rpc=FakeRpc(),
            )
            with self.subTest(code=code), self.assertRaises(SafeError) as raised:
                service.create_payment(
                    self.valid_payment_request(invoice_id=f"no-pairing-{code}"),
                    f"no-pairing-key-{code}",
                    1_700_000_000,
                )
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(trezor.sign_calls, [])
            self.assertEqual(trezor.sign_transaction_calls, [])
            self.assertEqual(trezor.push_transaction_calls, [])

    def test_concurrent_identical_payment_creation_returns_one_recorded_view(self):
        service, store, _, _ = self.make_approved_payment_service()
        request = self.valid_payment_request(invoice_id="concurrent-create")
        real_create = store.create_payment
        both_ready = threading.Barrier(2)

        def synchronized_create(**arguments):
            both_ready.wait(timeout=2)
            return real_create(**arguments)

        with (
            patch.object(store, "create_payment", side_effect=synchronized_create),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = tuple(
                pool.submit(
                    service.create_payment,
                    request,
                    "concurrent-create-key",
                    1_700_000_000,
                )
                for _ in range(2)
            )
            results = tuple(future.result(timeout=2) for future in futures)

        self.assertEqual(results[0], results[1])
        self.assertEqual(store.get_payment(results[0].payment_id), results[0])
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM payments WHERE invoice_id = ?",
                (request.invoice_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_pre_sign_balance_and_expiry_failures_become_failed_without_device_calls(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        cases = (
            (BaseBalances(100_000_000_000_001, 1_899_999), 1_700_000_001, "insufficient_usdc"),
            (BaseBalances(100_000_000_000_000, 2_000_000), 1_700_000_001, "insufficient_eth"),
            (BaseBalances(99_999_999_999_999, 2_000_000), 1_700_000_001, "insufficient_eth"),
            (BaseBalances(100_000_000_000_001, 2_000_000), 1_700_000_100, "invoice_expired"),
        )
        for index, (balances, timestamp, code) in enumerate(cases):
            with self.subTest(code=code, index=index):
                payment = self.create_additional_payment(
                    service,
                    ordinal=index,
                    invoice_id=f"pre-{index}",
                    idempotency_key=f"pre-key-{index}",
                )
                rpc.balances = balances
                with self.assertRaises(SafeError) as raised:
                    service.run_payment(payment.payment_id, now=lambda: timestamp)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
                self.assertEqual(trezor.sign_transaction_calls, [])
                self.assertEqual(trezor.push_transaction_calls, [])

    def test_rpc_result_is_strict_and_failure_is_sanitized_before_signing(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        cases = (
            ({"eth_wei": 100_000_000_000_001, "usdc_atomic": 2_000_000}, None),
            (BaseBalances(True, 2_000_000), None),
            (BaseBalances(100_000_000_000_001, -1), None),
            (None, RuntimeError("canary rpc secret")),
        )
        for index, (result, failure) in enumerate(cases):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"rpc-{index}",
                idempotency_key=f"rpc-key-{index}",
            )
            rpc.result = result
            rpc.failure = failure
            with self.subTest(index=index), self.assertRaises(SafeError) as raised:
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
            self.assertEqual(raised.exception.code, "base_rpc_unavailable")
            self.assertNotIn("canary", str(raised.exception))
            self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_run_configuration_failure_becomes_failed_before_rpc_or_device(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(invoice_id="disabled-run"),
            "disabled-run-key",
            1_700_000_000,
        )
        disabled_settings = SidecarSettings(
            enabled=False,
            mcp_token="test-mcp-token",
            api_token="test-api-token",
            max_usd=Decimal("2"),
            base_rpc_url="https://base.example.invalid",
            state_path=self.database,
        )
        disabled = TrezorSidecarService(
            disabled_settings,
            trezor,
            store,
            rpc=rpc,
        )

        with self.assertRaises(SafeError) as raised:
            disabled.run_payment(payment.payment_id, now=lambda: 1_700_000_001)

        self.assertEqual(raised.exception.code, "disabled")
        self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
        self.assertEqual(rpc.calls, [])
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_run_rereads_durable_intent_and_pairing_before_rpc_or_device(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        changed_intent = service.create_payment(
            self.valid_payment_request(invoice_id="changed-intent"),
            "changed-intent-key",
            1_700_000_000,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE intents SET state = ? WHERE intent_id = ?",
                (PaymentState.QUOTED.value, changed_intent.intent_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(SafeError) as intent_error:
            service.run_payment(changed_intent.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(intent_error.exception.code, "intent_not_approved")
        self.assertEqual(store.get_payment(changed_intent.payment_id).state, PaymentState.FAILED)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE intents SET state = ? WHERE intent_id = ?",
                (PaymentState.DEVICE_APPROVED.value, changed_intent.intent_id),
            )
            connection.commit()
        finally:
            connection.close()
        changed_pairing = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="changed-pairing",
            idempotency_key="changed-pairing-key",
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE pairings SET derivation_path = ? WHERE singleton = 1",
                ("m/44'/60'/0'/0/9",),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(SafeError) as pairing_error:
            service.run_payment(changed_pairing.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(pairing_error.exception.code, "pairing_mismatch")
        self.assertEqual(store.get_payment(changed_pairing.payment_id).state, PaymentState.FAILED)
        self.assertEqual(rpc.calls, [])
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_intent_expiry_is_checked_at_creation_and_again_before_signing(self):
        rpc = FakeRpc()
        service, store, trezor = self.make_service(rpc=rpc)
        service.pair()
        first_intent = self.valid_intent(expires_at=1_700_000_002)
        service.approve_intent(first_intent, now=1_700_000_000)
        payment = service.create_payment(
            self.valid_payment_request(
                intent_id=first_intent.intent_id,
                invoice_id="intent-run-expiry",
            ),
            "intent-run-expiry-key",
            1_700_000_000,
        )
        with self.assertRaises(SafeError) as run_error:
            service.run_payment(payment.payment_id, now=lambda: 1_700_000_002)
        self.assertEqual(run_error.exception.code, "intent_expired")
        self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)

        second_intent = self.valid_intent(
            intent_id="0x" + "55" * 32,
            expires_at=1_700_000_002,
        )
        service.approve_intent(second_intent, now=1_700_000_000)
        with self.assertRaises(SafeError) as create_error:
            service.create_payment(
                self.valid_payment_request(
                    intent_id=second_intent.intent_id,
                    invoice_id="intent-create-expiry",
                ),
                "intent-create-expiry-key",
                1_700_000_002,
            )
        self.assertEqual(create_error.exception.code, "intent_expired")
        self.assertEqual(rpc.calls, [])
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_each_signed_chain_signer_contract_recipient_and_amount_mismatch_fails_closed(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        different_recipient = "0x2222222222222222222222222222222222222222"
        mutations = (
            ("chain", {"chainId": 1}, None),
            ("signer", {}, Account.create()),
            ("contract", {"to": "0x3333333333333333333333333333333333333333"}, None),
            ("recipient", {"data": encode_usdc_transfer(different_recipient, 1_900_000)}, None),
            (
                "amount",
                {"data": encode_usdc_transfer(self.valid_payment_request().pay_to, 1_900_001)},
                None,
            ),
        )
        for index, (field, updates, account) in enumerate(mutations, start=1):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"signed-{field}",
                idempotency_key=f"signed-key-{field}",
            )
            trezor.transaction_updates = updates
            trezor.transaction_account = account
            with self.subTest(field=field), self.assertRaises(SafeError) as raised:
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
            self.assertEqual(raised.exception.code, "invalid_signed_transaction")
            self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
            self.assertEqual(len(trezor.sign_transaction_calls), index)
            self.assertEqual(trezor.push_transaction_calls, [])

    def test_signing_result_accepts_one_closed_path_and_rejects_every_other_shape(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        valid_raw = trezor.sign_base_transaction(
            FIXED_PATH,
            BASE_USDC_ADDRESS,
            encode_usdc_transfer(
                self.valid_payment_request().pay_to,
                self.valid_payment_request().amount_atomic,
            ),
        )["payload"]["serializedTx"]
        trezor.sign_transaction_calls.clear()
        invalid_results = (
            {"serializedTx": valid_raw},
            {"payload": {"serializedTx": valid_raw, "signed": {"serializedTx": valid_raw}}},
            {"payload": {"signed": {"serializedTx": "0x123"}}},
            {"payload": {"serializedTx": 123}},
            {"payload": {"result": {"serializedTx": valid_raw}}},
        )
        for index, result in enumerate(invalid_results):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"shape-{index}",
                idempotency_key=f"shape-key-{index}",
            )
            trezor.signed_transaction_result = result
            before_signs = len(trezor.sign_transaction_calls)
            before_pushes = len(trezor.push_transaction_calls)
            with self.subTest(index=index), self.assertRaises(SafeError):
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
            self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
            self.assertEqual(len(trezor.sign_transaction_calls), before_signs + 1)
            self.assertEqual(len(trezor.push_transaction_calls), before_pushes)

        nested_payment = self.create_additional_payment(
            service,
            ordinal=20,
            invoice_id="shape-nested",
            idempotency_key="shape-nested-key",
        )
        trezor.signed_transaction_result = {
            "payload": {"signed": {"serializedTx": valid_raw}}
        }
        completed = service.run_payment(nested_payment.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)

    def test_device_rejection_timeout_and_unexpected_signing_failure_are_safe_and_failed(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        failures = (
            (
                SafeError("device_cancelled", "canary rejection"),
                "device_rejected",
                "Payment signing was cancelled on Trezor.",
                400,
            ),
            (
                TimeoutError("canary timeout"),
                "device_timeout",
                "Trezor payment signing timed out.",
                504,
            ),
            (
                RuntimeError("canary device detail"),
                "trezor_unavailable",
                "Trezor Suite is unavailable.",
                503,
            ),
        )
        for index, (failure, code, message, status) in enumerate(failures):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"device-{index}",
                idempotency_key=f"device-key-{index}",
            )
            trezor.sign_transaction_failure = failure
            before_signs = len(trezor.sign_transaction_calls)
            before_pushes = len(trezor.push_transaction_calls)
            with self.subTest(code=code), self.assertRaises(SafeError) as raised:
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.message, message)
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
            self.assertEqual(len(trezor.sign_transaction_calls), before_signs + 1)
            self.assertEqual(len(trezor.push_transaction_calls), before_pushes)

    def test_expiry_is_rechecked_after_signing_and_before_push(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        request = self.valid_payment_request(expires_at=1_700_000_002)
        payment = service.create_payment(request, "expiry-key", 1_700_000_000)
        ticks = iter((1_700_000_001, 1_700_000_002))

        with self.assertRaises(SafeError) as raised:
            service.run_payment(payment.payment_id, now=lambda: next(ticks))

        self.assertEqual(raised.exception.code, "invoice_expired")
        self.assertEqual(store.get_payment(payment.payment_id).state, PaymentState.FAILED)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_payment_clock_cannot_move_behind_durable_state_or_backward_before_push(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        before_created = service.create_payment(
            self.valid_payment_request(invoice_id="clock-before-created"),
            "clock-before-created-key",
            1_700_000_000,
        )
        with self.assertRaises(SafeError) as first_error:
            service.run_payment(
                before_created.payment_id,
                now=lambda: 1_699_999_999,
            )
        self.assertEqual(first_error.exception.code, "invalid_clock")
        self.assertEqual(store.get_payment(before_created.payment_id).state, PaymentState.FAILED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        rollback = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="clock-rollback",
            idempotency_key="clock-rollback-key",
        )
        ticks = iter((1_700_000_001, 1_700_000_000))
        with self.assertRaises(SafeError) as second_error:
            service.run_payment(rollback.payment_id, now=lambda: next(ticks))
        self.assertEqual(second_error.exception.code, "invalid_clock")
        self.assertEqual(store.get_payment(rollback.payment_id).state, PaymentState.FAILED)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_payment_clock_is_bound_to_durable_approval_time_at_create_and_run(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        with self.assertRaises(SafeError) as create_error:
            service.create_payment(
                self.valid_payment_request(invoice_id="backdated-create"),
                "backdated-create-key",
                1_699_999_999,
            )
        self.assertEqual(create_error.exception.code, "invalid_clock")
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        backdated_intent = self.valid_intent(intent_id="0x" + "66" * 32)
        service.approve_intent(backdated_intent, now=1_700_000_001)
        backdated_request = self.valid_payment_request(
            intent_id=backdated_intent.intent_id,
            invoice_id="backdated-run",
        )
        backdated = store.create_payment(
            payment_id="backdated-run-payment",
            intent_id=backdated_request.intent_id,
            invoice_id=backdated_request.invoice_id,
            idempotency_key="backdated-run-key",
            pay_to=backdated_request.pay_to,
            amount_atomic=backdated_request.amount_atomic,
            expires_at=backdated_request.expires_at,
            created_at=1_700_000_000,
        )
        with self.assertRaises(SafeError) as run_error:
            service.run_payment(backdated.payment_id, now=lambda: 1_700_000_000)
        self.assertEqual(run_error.exception.code, "invalid_clock")
        self.assertEqual(store.get_payment(backdated.payment_id).state, PaymentState.FAILED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        boundary_intent = self.valid_intent(intent_id="0x" + "67" * 32)
        service.approve_intent(boundary_intent, now=1_700_000_001)
        boundary_request = self.valid_payment_request(
            intent_id=boundary_intent.intent_id,
            invoice_id="approval-boundary",
        )
        boundary = service.create_payment(
            boundary_request,
            "approval-boundary-key",
            1_700_000_001,
        )
        completed = service.run_payment(boundary.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)

    def test_explicit_repair_invalidates_approvals_even_with_an_older_repair_clock(self):
        # Break caught: timestamp-only binding accepts device A after a clock-rollback repair to B.
        ticks = iter((100, 200))
        service, store, trezor = self.make_service(clock=lambda: next(ticks), rpc=FakeRpc())
        first_pairing = service.pair()
        old_run_intent = self.valid_intent()
        old_create_intent = self.valid_intent(intent_id="0x" + "33" * 32)
        service.approve_intent(old_run_intent, now=300)
        service.approve_intent(old_create_intent, now=300)
        old_payment = service.create_payment(
            self.valid_payment_request(),
            "old-payment-key",
            300,
        )
        old_approval_calls = len(trezor.sign_calls)

        replacement = Account.create()
        trezor.account = replacement
        trezor.address = replacement.address
        repaired = service.pair(allow_repair=True)
        self.assertNotEqual(repaired.pairing_id, first_pairing.pairing_id)
        self.assertEqual(repaired.created_at, 200)

        with self.assertRaises(SafeError) as replay_error:
            service.approve_intent(old_run_intent, now=301)
        self.assertEqual(replay_error.exception.code, "reapproval_required")
        self.assertEqual(replay_error.exception.status, 409)
        self.assertEqual(len(trezor.sign_calls), old_approval_calls)

        with self.assertRaises(SafeError) as create_replay_error:
            service.create_payment(
                self.valid_payment_request(),
                "old-payment-key",
                301,
            )
        self.assertEqual(create_replay_error.exception.code, "reapproval_required")

        with self.assertRaises(SafeError) as create_error:
            service.create_payment(
                self.valid_payment_request(
                    intent_id=old_create_intent.intent_id,
                    invoice_id="old-after-repair",
                ),
                "old-after-repair-key",
                301,
            )
        self.assertEqual(create_error.exception.code, "reapproval_required")

        with self.assertRaises(SafeError) as run_error:
            service.run_payment(old_payment.payment_id, now=lambda: 301)
        self.assertEqual(run_error.exception.code, "reapproval_required")
        self.assertEqual(store.get_payment(old_payment.payment_id).state, PaymentState.FAILED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        new_intent = self.valid_intent(intent_id="0x" + "44" * 32)
        service.approve_intent(new_intent, now=201)
        new_payment = service.create_payment(
            self.valid_payment_request(
                intent_id=new_intent.intent_id,
                invoice_id="new-after-repair",
            ),
            "new-after-repair-key",
            201,
        )
        completed = service.run_payment(new_payment.payment_id, now=lambda: 202)
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)

    def test_every_post_push_exception_or_ambiguous_hash_requires_reconciliation(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        cases = (
            (RuntimeError("canary push detail"), None),
            (None, {"txid": "not-a-hash"}),
            (None, {"txid": "0x" + "11" * 32}),
            (None, {"txid": "0x" + "11" * 32, "hash": "0x" + "11" * 32}),
            (None, {"payload": {"txId": "0x" + "11" * 32}, "hash": "0x" + "11" * 32}),
        )
        for index, (failure, result) in enumerate(cases, start=1):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"push-{index}",
                idempotency_key=f"push-key-{index}",
            )
            trezor.push_transaction_failure = failure
            trezor.push_transaction_result = result
            with self.subTest(index=index), self.assertRaises(SafeError) as raised:
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
            self.assertEqual(raised.exception.code, "reconciliation_required")
            self.assertNotIn("canary", str(raised.exception))
            self.assertEqual(
                store.get_payment(payment.payment_id).state,
                PaymentState.RECONCILIATION_REQUIRED,
            )
            self.assertEqual(
                store.get_payment(payment.payment_id).tx_hash,
                "0x" + keccak(bytes.fromhex(trezor.push_transaction_calls[-1][2:])).hex(),
            )
            self.assertEqual(len(trezor.sign_transaction_calls), index)
            self.assertEqual(len(trezor.push_transaction_calls), index)

    def test_each_exact_push_hash_field_is_accepted_and_normalized(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        paths = (
            (False, "txid"),
            (False, "txId"),
            (False, "hash"),
            (True, "txid"),
            (True, "txId"),
            (True, "hash"),
        )
        for index, (nested, field) in enumerate(paths, start=1):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"hash-{nested}-{field}",
                idempotency_key=f"hash-key-{nested}-{field}",
            )

            def response(raw, *, nested=nested, field=field, index=index):
                tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
                if index == len(paths):
                    tx_hash = tx_hash.upper().replace("0X", "0x")
                return {"payload": {field: tx_hash}} if nested else {field: tx_hash}

            trezor.push_transaction_result = response
            completed = service.run_payment(
                payment.payment_id,
                now=lambda: 1_700_000_001,
            )
            with self.subTest(nested=nested, field=field):
                self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
                self.assertRegex(completed.tx_hash, r"\A0x[0-9a-f]{64}\Z")
                self.assertEqual(store.get_payment(payment.payment_id), completed)
                self.assertEqual(len(trezor.sign_transaction_calls), index)
                self.assertEqual(len(trezor.push_transaction_calls), index)

    def test_transition_failures_preserve_prepush_error_and_classify_postpush_ambiguity(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        prepush = service.create_payment(
            self.valid_payment_request(invoice_id="transition-prepush"),
            "transition-prepush-key",
            1_700_000_000,
        )
        rpc.balances = BaseBalances(100_000_000_000_001, 0)
        real_transition = store.transition_payment

        def persist_failed_then_raise(**arguments):
            if arguments["target"] is PaymentState.FAILED:
                real_transition(**arguments)
                raise ValueError("canary state failure")
            return real_transition(**arguments)

        with patch.object(
            store,
            "transition_payment",
            side_effect=persist_failed_then_raise,
        ):
            with self.assertRaises(SafeError) as original:
                service.run_payment(prepush.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(original.exception.code, "insufficient_usdc")
        self.assertNotIn("canary", str(original.exception))
        self.assertEqual(store.get_payment(prepush.payment_id).state, PaymentState.FAILED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        unclassified = self.create_additional_payment(
            service,
            ordinal=2,
            invoice_id="transition-unclassified",
            idempotency_key="transition-unclassified-key",
        )

        def reject_failed(**arguments):
            if arguments["target"] is PaymentState.FAILED:
                raise ValueError("canary state failure")
            return real_transition(**arguments)

        with patch.object(store, "transition_payment", side_effect=reject_failed):
            with self.assertRaises(SafeError) as state_error:
                service.run_payment(unclassified.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(state_error.exception.code, "payment_state_unavailable")
        self.assertEqual(state_error.exception.status, 503)
        self.assertNotIn("canary", str(state_error.exception))
        self.assertEqual(
            store.get_payment(unclassified.payment_id).state,
            PaymentState.INVOICE_CREATED,
        )
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

        postpush = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="transition-postpush",
            idempotency_key="transition-postpush-key",
        )
        rpc.balances = BaseBalances(100_000_000_000_001, 2_000_000)

        def reject_broadcast(**arguments):
            if arguments["target"] is PaymentState.TX_BROADCAST:
                raise ValueError("canary state failure")
            return real_transition(**arguments)

        with patch.object(store, "transition_payment", side_effect=reject_broadcast):
            with self.assertRaises(SafeError) as ambiguous:
                service.run_payment(postpush.payment_id, now=lambda: 1_700_000_001)
        self.assertEqual(ambiguous.exception.code, "reconciliation_required")
        self.assertNotIn("canary", str(ambiguous.exception))
        self.assertEqual(
            store.get_payment(postpush.payment_id).state,
            PaymentState.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)

        committed = self.create_additional_payment(
            service,
            ordinal=3,
            invoice_id="transition-committed",
            idempotency_key="transition-committed-key",
        )

        def persist_broadcast_then_raise(**arguments):
            if arguments["target"] is PaymentState.TX_BROADCAST:
                real_transition(**arguments)
                raise ValueError("canary state failure")
            return real_transition(**arguments)

        with patch.object(
            store,
            "transition_payment",
            side_effect=persist_broadcast_then_raise,
        ):
            completed = service.run_payment(
                committed.payment_id,
                now=lambda: 1_700_000_001,
            )
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(completed, store.get_payment(committed.payment_id))
        self.assertEqual(len(trezor.sign_transaction_calls), 2)
        self.assertEqual(len(trezor.push_transaction_calls), 2)

    def test_post_push_reconciliation_storage_failures_are_always_fixed_and_safe(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        real_get = store.get_payment
        real_transition = store.transition_payment

        for index, failure_point in enumerate(
            ("initial_read", "write", "refetch", "broadcast_and_reconciliation"),
            start=1,
        ):
            payment = self.create_additional_payment(
                service,
                ordinal=index,
                invoice_id=f"reconcile-{failure_point}",
                idempotency_key=f"reconcile-key-{failure_point}",
            )
            reads = 0
            reconciliation_write_attempted = False

            def failing_get(payment_id):
                nonlocal reads
                reads += 1
                if failure_point == "initial_read" and reads == 4:
                    raise RuntimeError("canary reconciliation initial read")
                if failure_point == "refetch" and reconciliation_write_attempted:
                    raise RuntimeError("canary reconciliation refetch")
                return real_get(payment_id)

            def failing_transition(**arguments):
                nonlocal reconciliation_write_attempted
                if arguments["target"] is PaymentState.TX_BROADCAST:
                    raise RuntimeError("canary broadcast transition")
                if arguments["target"] is PaymentState.RECONCILIATION_REQUIRED:
                    reconciliation_write_attempted = True
                    if failure_point in {
                        "write",
                        "refetch",
                        "broadcast_and_reconciliation",
                    }:
                        raise RuntimeError("canary reconciliation transition")
                return real_transition(**arguments)

            before_signs = len(trezor.sign_transaction_calls)
            before_pushes = len(trezor.push_transaction_calls)
            with (
                patch.object(store, "get_payment", side_effect=failing_get),
                patch.object(store, "transition_payment", side_effect=failing_transition),
                self.subTest(failure_point=failure_point),
                self.assertRaises(SafeError) as raised,
            ):
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)

            self.assertEqual(raised.exception.code, "reconciliation_required")
            self.assertEqual(
                raised.exception.message,
                "Transaction broadcast outcome requires reconciliation.",
            )
            self.assertEqual(raised.exception.status, 409)
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(len(trezor.sign_transaction_calls), before_signs + 1)
            self.assertEqual(len(trezor.push_transaction_calls), before_pushes + 1)
            raw = trezor.push_transaction_calls[-1]
            self.assertNotIn(raw, str(raised.exception))

    def test_run_payment_store_read_failures_are_fixed_safe_before_any_device_work(self):
        # Break caught: SQLite details escape from either pre-device durable payment read.
        service, store, trezor, rpc = self.make_approved_payment_service()
        payments = (
            service.create_payment(
                self.valid_payment_request(invoice_id="read-failure-first"),
                "read-failure-first-key",
                1_700_000_000,
            ),
            self.create_additional_payment(
                service,
                ordinal=1,
                invoice_id="read-failure-second",
                idempotency_key="read-failure-second-key",
            ),
        )
        real_get = store.get_payment

        for failure_read, payment in enumerate(payments, start=1):
            reads = 0

            def fail_selected_read(payment_id):
                nonlocal reads
                reads += 1
                if reads == failure_read:
                    raise sqlite3.OperationalError(f"canary read {failure_read}")
                return real_get(payment_id)

            before_rpc = len(rpc.calls)
            before_signs = len(trezor.sign_transaction_calls)
            before_pushes = len(trezor.push_transaction_calls)
            with (
                self.subTest(failure_read=failure_read),
                patch.object(store, "get_payment", side_effect=fail_selected_read),
                self.assertRaises(BaseException) as raised,
            ):
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)

            self.assertIsInstance(raised.exception, SafeError)
            self.assertEqual(raised.exception.code, "reconciliation_required")
            self.assertEqual(
                raised.exception.message,
                "Transaction broadcast outcome requires reconciliation.",
            )
            self.assertEqual(raised.exception.status, 409)
            self.assertNotIn("canary", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(len(rpc.calls), before_rpc)
            self.assertEqual(len(trezor.sign_transaction_calls), before_signs)
            self.assertEqual(len(trezor.push_transaction_calls), before_pushes)
            self.assertEqual(real_get(payment.payment_id), payment)

    def test_run_payment_read_sanitizer_does_not_swallow_process_control(self):
        # Break caught: the storage boundary converts KeyboardInterrupt into an API error.
        service, store, trezor, rpc = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(invoice_id="read-exception-boundary"),
            "read-exception-boundary-key",
            1_700_000_000,
        )
        real_get = store.get_payment

        with (
            patch.object(
                store,
                "get_payment",
                side_effect=RuntimeError("canary ordinary exception"),
            ),
            self.assertRaises(BaseException) as ordinary,
        ):
            service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
        self.assertIsInstance(ordinary.exception, SafeError)
        self.assertEqual(ordinary.exception.code, "reconciliation_required")
        self.assertEqual(
            ordinary.exception.message,
            "Transaction broadcast outcome requires reconciliation.",
        )
        self.assertEqual(ordinary.exception.status, 409)
        self.assertNotIn("canary", str(ordinary.exception))
        self.assertIsNone(ordinary.exception.__cause__)

        with (
            patch.object(
                store,
                "get_payment",
                side_effect=KeyboardInterrupt("canary process control"),
            ),
            self.assertRaises(BaseException) as process_control,
        ):
            service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
        self.assertIsInstance(process_control.exception, KeyboardInterrupt)
        self.assertEqual(str(process_control.exception), "canary process control")

        self.assertEqual(rpc.calls, [])
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])
        self.assertEqual(real_get(payment.payment_id), payment)

    def test_orphaned_signed_reconciliation_write_failure_is_fixed_and_never_pushes(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(invoice_id="orphaned-write-failure"),
            "orphaned-write-failure-key",
            1_700_000_000,
        )
        store.transition_payment(
            payment_id=payment.payment_id,
            expected=PaymentState.INVOICE_CREATED,
            target=PaymentState.TX_SIGNED,
            updated_at=1_700_000_001,
        )
        real_transition = store.transition_payment

        def reject_reconciliation(**arguments):
            if arguments["target"] is PaymentState.RECONCILIATION_REQUIRED:
                raise RuntimeError("canary orphan reconciliation")
            return real_transition(**arguments)

        with (
            patch.object(store, "transition_payment", side_effect=reject_reconciliation),
            self.assertRaises(SafeError) as raised,
        ):
            service.run_payment(payment.payment_id, now=lambda: 1_700_000_002)

        self.assertEqual(raised.exception.code, "reconciliation_required")
        self.assertEqual(
            raised.exception.message,
            "Transaction broadcast outcome requires reconciliation.",
        )
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("canary", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_missing_state_during_failure_classification_is_not_reported_as_failed(self):
        service, store, trezor, rpc = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(invoice_id="missing-failure-state"),
            "missing-failure-state-key",
            1_700_000_000,
        )
        rpc.balances = BaseBalances(100_000_000_000_001, 0)
        real_get_payment = store.get_payment
        reads = 0

        def disappear_during_classification(payment_id):
            nonlocal reads
            reads += 1
            if reads == 3:
                return None
            return real_get_payment(payment_id)

        with patch.object(
            store,
            "get_payment",
            side_effect=disappear_during_classification,
        ):
            with self.assertRaises(SafeError) as raised:
                service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)

        self.assertEqual(raised.exception.code, "payment_state_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(real_get_payment(payment.payment_id).state, PaymentState.INVOICE_CREATED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_terminal_and_ambiguous_payment_replays_never_sign_or_push_again(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        completed_payment = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="replay-complete",
            idempotency_key="replay-complete-key",
        )
        completed = service.run_payment(completed_payment.payment_id, now=lambda: 1_700_000_001)
        failed_payment = self.create_additional_payment(
            service,
            ordinal=2,
            invoice_id="replay-failed",
            idempotency_key="replay-failed-key",
        )
        service._rpc.balances = BaseBalances(100_000_000_000_001, 0)
        with self.assertRaises(SafeError):
            service.run_payment(failed_payment.payment_id, now=lambda: 1_700_000_001)
        ambiguous_payment = self.create_additional_payment(
            service,
            ordinal=3,
            invoice_id="replay-ambiguous",
            idempotency_key="replay-ambiguous-key",
        )
        service._rpc.balances = BaseBalances(100_000_000_000_001, 2_000_000)
        trezor.push_transaction_failure = RuntimeError("canary")
        with self.assertRaises(SafeError):
            service.run_payment(ambiguous_payment.payment_id, now=lambda: 1_700_000_001)
        counts = (len(trezor.sign_transaction_calls), len(trezor.push_transaction_calls))
        trezor.push_transaction_failure = None

        replays = (
            service.run_payment(completed_payment.payment_id, now=lambda: 1_700_000_002),
            service.run_payment(failed_payment.payment_id, now=lambda: 1_700_000_002),
            service.run_payment(ambiguous_payment.payment_id, now=lambda: 1_700_000_002),
        )

        self.assertEqual(
            tuple(payment.state for payment in replays),
            (
                PaymentState.TX_BROADCAST,
                PaymentState.FAILED,
                PaymentState.RECONCILIATION_REQUIRED,
            ),
        )
        self.assertEqual(completed, replays[0])
        self.assertEqual(
            (len(trezor.sign_transaction_calls), len(trezor.push_transaction_calls)),
            counts,
        )

    def test_orphaned_signed_state_requires_reconciliation_without_replay(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        payment = service.create_payment(
            self.valid_payment_request(invoice_id="orphaned-signed"),
            "orphaned-signed-key",
            1_700_000_000,
        )
        store.transition_payment(
            payment_id=payment.payment_id,
            expected=PaymentState.INVOICE_CREATED,
            target=PaymentState.TX_SIGNED,
            updated_at=1_700_000_001,
        )

        replay = service.run_payment(payment.payment_id, now=lambda: 1_700_000_002)

        self.assertEqual(replay.state, PaymentState.RECONCILIATION_REQUIRED)
        self.assertEqual(trezor.sign_transaction_calls, [])
        self.assertEqual(trezor.push_transaction_calls, [])

    def test_concurrent_payment_jobs_overlap_and_second_gets_exact_device_busy(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        first_payment = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="concurrent-1",
            idempotency_key="concurrent-key-1",
        )
        second_payment = self.create_additional_payment(
            service,
            ordinal=2,
            invoice_id="concurrent-2",
            idempotency_key="concurrent-key-2",
        )
        trezor.transaction_entered = threading.Event()
        trezor.release_transaction = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                service.run_payment,
                first_payment.payment_id,
                lambda: 1_700_000_001,
            )
            self.assertTrue(trezor.transaction_entered.wait(timeout=2))
            second = pool.submit(
                service.run_payment,
                second_payment.payment_id,
                lambda: 1_700_000_001,
            )
            with self.assertRaises(SafeError) as raised:
                second.result(timeout=2)
            trezor.release_transaction.set()
            completed = first.result(timeout=2)

        self.assertEqual(raised.exception.code, "device_busy")
        self.assertEqual(raised.exception.message, "Another Trezor approval is active.")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(store.get_payment(second_payment.payment_id).state, PaymentState.INVOICE_CREATED)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)

    def test_terminal_payment_replays_while_an_unrelated_device_job_is_active(self):
        service, store, trezor, _ = self.make_approved_payment_service()
        terminal_payment = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="terminal-during-active",
            idempotency_key="terminal-during-active-key",
        )
        terminal = store.transition_payment(
            payment_id=terminal_payment.payment_id,
            expected=PaymentState.INVOICE_CREATED,
            target=PaymentState.FAILED,
            updated_at=1_700_000_001,
        )
        active_payment = self.create_additional_payment(
            service,
            ordinal=2,
            invoice_id="active-during-terminal-replay",
            idempotency_key="active-during-terminal-replay-key",
        )
        trezor.transaction_entered = threading.Event()
        trezor.release_transaction = threading.Event()

        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(
                service.run_payment,
                active_payment.payment_id,
                lambda: 1_700_000_002,
            )
            self.assertTrue(trezor.transaction_entered.wait(timeout=2))
            replay = service.run_payment(
                terminal.payment_id,
                now=lambda: 1_700_000_002,
            )
            trezor.release_transaction.set()
            completed = active.result(timeout=2)

        self.assertEqual(replay, terminal)
        self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
        self.assertEqual(len(trezor.sign_transaction_calls), 1)
        self.assertEqual(len(trezor.push_transaction_calls), 1)

    def test_forked_payment_jobs_share_the_process_lock_and_loser_does_no_work(self):
        account = Account.create()
        service, store, _ = self.make_service(
            trezor=FakeTrezor(account),
            rpc=FakeRpc(),
        )
        service.pair()
        service.approve_intent(self.valid_intent(), now=1_700_000_000)
        winner_payment = service.create_payment(
            self.valid_payment_request(invoice_id="forked-winner"),
            "forked-winner-key",
            1_700_000_000,
        )
        loser_payment = self.create_additional_payment(
            service,
            ordinal=1,
            invoice_id="forked-loser",
            idempotency_key="forked-loser-key",
        )

        context = get_context("fork")
        entered = context.Event()
        release = context.Event()
        results = context.Queue()
        winner = context.Process(
            target=run_payment_in_process,
            args=(
                self.database,
                account.key,
                winner_payment.payment_id,
                "winner",
                entered,
                release,
                results,
            ),
        )
        loser = context.Process(
            target=run_payment_in_process,
            args=(
                self.database,
                account.key,
                loser_payment.payment_id,
                "loser",
                None,
                None,
                results,
            ),
        )

        winner.start()
        loser_started = False
        try:
            self.assertTrue(entered.wait(timeout=3))
            loser.start()
            loser_started = True
            loser.join(timeout=5)
        finally:
            release.set()
            winner.join(timeout=5)
            if winner.is_alive():
                winner.terminate()
                winner.join(timeout=2)
            if loser_started and loser.is_alive():
                loser.terminate()
                loser.join(timeout=2)

        self.assertEqual(winner.exitcode, 0)
        self.assertEqual(loser.exitcode, 0)
        outputs = {
            output[0]: output
            for output in (results.get(timeout=2), results.get(timeout=2))
        }
        self.assertEqual(
            outputs["winner"],
            ("winner", "ok", PaymentState.TX_BROADCAST.value, 1, 1, 1),
        )
        self.assertEqual(
            outputs["loser"],
            (
                "loser",
                "safe",
                "device_busy",
                "Another Trezor approval is active.",
                409,
                0,
                0,
                0,
            ),
        )
        self.assertEqual(
            store.get_payment(winner_payment.payment_id).state,
            PaymentState.TX_BROADCAST,
        )
        self.assertEqual(
            store.get_payment(loser_payment.payment_id).state,
            PaymentState.INVOICE_CREATED,
        )

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
