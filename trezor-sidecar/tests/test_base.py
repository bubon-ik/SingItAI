import json
from unittest import TestCase

import httpx
import rlp
from eth_account import Account
from eth_utils import keccak

from trezor_sidecar.base import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    EVM_DERIVATION_PATH,
    BaseBalances,
    BaseRpcClient,
    encode_usdc_transfer,
    verify_signed_usdc_transfer,
)
from trezor_sidecar.errors import SafeError


RECIPIENT = "0x1111111111111111111111111111111111111111"
AMOUNT = 1_250_000


class RpcQueue:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)


def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class BaseRpcTests(TestCase):
    def make_client(self, *replies, url="https://rpc.example"):
        queue = RpcQueue(replies)
        client = BaseRpcClient(url, transport=httpx.MockTransport(queue))
        return client, queue

    def test_balances_checks_chain_and_reads_eth_and_usdc(self):
        client, queue = self.make_client(
            rpc_result(1, "0x2105"),
            rpc_result(2, "0x10"),
            rpc_result(3, "0x" + (42).to_bytes(32, "big").hex()),
        )

        result = client.get_balances(RECIPIENT)

        self.assertEqual(result, BaseBalances(eth_wei=16, usdc_atomic=42))
        self.assertEqual(
            [request["method"] for request in queue.requests],
            ["eth_chainId", "eth_getBalance", "eth_call"],
        )
        self.assertEqual([request["id"] for request in queue.requests], [1, 2, 3])
        self.assertEqual(queue.requests[1]["params"], [RECIPIENT, "latest"])
        self.assertEqual(
            queue.requests[2]["params"],
            [{
                "to": BASE_USDC_ADDRESS,
                "data": "0x70a08231" + "0" * 24 + RECIPIENT[2:],
            }, "latest"],
        )

    def assert_rpc_refused(self, client):
        with self.assertRaisesRegex(SafeError, r"Base RPC is unavailable\.") as raised:
            client.get_balances(RECIPIENT)
        self.assertEqual(raised.exception.code, "base_rpc_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertIsNone(raised.exception.__cause__)

    def test_wrong_chain_is_refused_before_balance_reads(self):
        client, queue = self.make_client(rpc_result(1, "0x1"))

        self.assert_rpc_refused(client)

        self.assertEqual(len(queue.requests), 1)

    def test_redirect_is_refused(self):
        client, _ = self.make_client(
            httpx.Response(302, headers={"location": "https://canary.invalid"})
        )
        self.assert_rpc_refused(client)

    def test_response_over_64_kib_is_refused(self):
        client, _ = self.make_client(httpx.Response(200, content=b"x" * 65_537))
        self.assert_rpc_refused(client)

    def test_wrong_json_rpc_id_is_refused(self):
        client, _ = self.make_client(rpc_result(2, "0x2105"))
        self.assert_rpc_refused(client)

    def test_json_rpc_error_is_refused_without_provider_content(self):
        client, _ = self.make_client({
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "canary provider secret"},
        })

        with self.assertRaises(SafeError) as raised:
            client.get_balances(RECIPIENT)

        self.assertNotIn("canary", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_malformed_json_rpc_envelope_is_refused(self):
        for response in (
            [rpc_result(1, "0x2105")],
            {"jsonrpc": "1.0", "id": 1, "result": "0x2105"},
            {"jsonrpc": "2.0", "id": True, "result": "0x2105"},
            {"jsonrpc": "2.0", "id": 1},
        ):
            with self.subTest(response=response):
                client, _ = self.make_client(response)
                self.assert_rpc_refused(client)

    def test_duplicate_json_rpc_fields_are_refused(self):
        response = httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":999,"id":1,"result":"0x2105"}',
        )
        client, _ = self.make_client(
            response,
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )
        self.assert_rpc_refused(client)

    def test_malformed_quantities_are_refused(self):
        cases = (
            (rpc_result(1, "8453"),),
            (rpc_result(1, "0x02105"),),
            (rpc_result(1, "0x2105"), rpc_result(2, "0x00")),
            (
                rpc_result(1, "0x2105"),
                rpc_result(2, "0x1"),
                rpc_result(3, "0x1"),
            ),
            (
                rpc_result(1, "0x2105"),
                rpc_result(2, "0x1"),
                rpc_result(3, "0x" + "gg" * 32),
            ),
        )
        for replies in cases:
            with self.subTest(replies=replies):
                client, _ = self.make_client(*replies)
                self.assert_rpc_refused(client)

    def test_transport_failure_is_safe(self):
        def fail(_request):
            raise RuntimeError("canary transport detail")

        client = BaseRpcClient(
            "https://rpc.example", transport=httpx.MockTransport(fail)
        )
        self.assert_rpc_refused(client)

    def test_repr_does_not_disclose_rpc_url_credentials(self):
        client = BaseRpcClient("https://user:canary-secret@rpc.example/path")

        representation = repr(client)

        self.assertEqual(representation, "BaseRpcClient(timeout_seconds=10.0)")
        self.assertNotIn("canary-secret", representation)
        self.assertNotIn("rpc.example", representation)

    def test_invalid_balance_address_is_rejected_before_transport(self):
        client, queue = self.make_client()
        with self.assertRaisesRegex(ValueError, r"Invalid EVM address\."):
            client.get_balances("0x1234")
        self.assertEqual(queue.requests, [])


class BaseTransactionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.account = Account.create()

    def signed(self, account=None, **updates):
        transaction = {
            "type": 2,
            "chainId": BASE_CHAIN_ID,
            "nonce": 7,
            "maxPriorityFeePerGas": 1_000_000,
            "maxFeePerGas": 2_000_000,
            "gas": 80_000,
            "to": BASE_USDC_ADDRESS,
            "value": 0,
            "data": encode_usdc_transfer(RECIPIENT, AMOUNT),
            "accessList": [],
        }
        transaction.update(updates)
        signed = Account.sign_transaction(
            transaction, (account or self.account).key
        )
        return signed.raw_transaction.to_0x_hex()

    def mutate_rlp_field(self, raw_tx, index, value):
        raw = bytes.fromhex(raw_tx[2:])
        fields = rlp.decode(raw[1:], strict=True)
        fields[index] = value
        return "0x02" + rlp.encode(fields).hex()

    def assert_verification_refused(self, raw_tx, **expected_updates):
        expected = {
            "expected_signer": self.account.address,
            "expected_recipient": RECIPIENT,
            "expected_amount_atomic": AMOUNT,
        }
        expected.update(expected_updates)
        with self.assertRaisesRegex(
            SafeError, r"Signed transaction does not match the approved payment\."
        ) as raised:
            verify_signed_usdc_transfer(raw_tx, **expected)
        self.assertEqual(raised.exception.code, "invalid_signed_transaction")
        self.assertNotIn(raw_tx, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_usdc_transfer_calldata_is_exact(self):
        data = encode_usdc_transfer(RECIPIENT, AMOUNT)
        self.assertEqual(
            data,
            "0xa9059cbb"
            + "0" * 24
            + "11" * 20
            + "0" * 58
            + "1312d0",
        )

    def test_contract_chain_and_path_are_canonical(self):
        self.assertEqual(BASE_CHAIN_ID, 8453)
        self.assertEqual(
            BASE_USDC_ADDRESS,
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        self.assertEqual(EVM_DERIVATION_PATH, "m/44'/60'/0'/0/0")

    def test_calldata_rejects_invalid_types_addresses_and_uint256(self):
        for address, amount in (
            ("0x1234", AMOUNT),
            (RECIPIENT, True),
            (RECIPIENT, -1),
            (RECIPIENT, 1 << 256),
        ):
            with self.subTest(address=address, amount=amount):
                with self.assertRaises(ValueError):
                    encode_usdc_transfer(address, amount)

    def test_valid_signed_transfer_returns_exact_transaction_hash(self):
        raw_tx = self.signed()

        result = verify_signed_usdc_transfer(
            raw_tx, self.account.address, RECIPIENT, AMOUNT
        )

        self.assertEqual(result, "0x" + keccak(bytes.fromhex(raw_tx[2:])).hex())

    def test_each_approved_field_mismatch_is_refused(self):
        different_account = Account.create()
        cases = {
            "chain": self.signed(chainId=1),
            "signer": self.signed(account=different_account),
            "contract": self.signed(to="0x2222222222222222222222222222222222222222"),
            "native_value": self.signed(value=1),
            "recipient": self.signed(
                data=encode_usdc_transfer(
                    "0x3333333333333333333333333333333333333333", AMOUNT
                )
            ),
            "recipient_padding": self.signed(
                data=(
                    "0xa9059cbb"
                    + "ff" * 12
                    + RECIPIENT[2:]
                    + AMOUNT.to_bytes(32, "big").hex()
                )
            ),
            "amount": self.signed(data=encode_usdc_transfer(RECIPIENT, AMOUNT + 1)),
            "selector": self.signed(
                data="0xdeadbeef" + encode_usdc_transfer(RECIPIENT, AMOUNT)[10:]
            ),
            "trailing_calldata": self.signed(
                data=encode_usdc_transfer(RECIPIENT, AMOUNT) + "00"
            ),
        }
        for field, raw_tx in cases.items():
            with self.subTest(field=field):
                self.assert_verification_refused(raw_tx)

    def test_nonempty_access_list_zero_gas_and_zero_fees_are_refused(self):
        cases = {
            "access_list": self.signed(accessList=[{
                "address": RECIPIENT,
                "storageKeys": [],
            }]),
            "gas": self.signed(gas=0),
            "priority_fee": self.signed(maxPriorityFeePerGas=0),
            "max_fee": self.signed(maxFeePerGas=0),
            "fee_order": self.signed(
                maxPriorityFeePerGas=3_000_000, maxFeePerGas=2_000_000
            ),
        }
        for field, raw_tx in cases.items():
            with self.subTest(field=field):
                self.assert_verification_refused(raw_tx)

    def test_only_type_two_transactions_are_accepted(self):
        legacy = Account.sign_transaction({
            "chainId": BASE_CHAIN_ID,
            "nonce": 7,
            "gasPrice": 2_000_000,
            "gas": 80_000,
            "to": BASE_USDC_ADDRESS,
            "value": 0,
            "data": encode_usdc_transfer(RECIPIENT, AMOUNT),
        }, self.account.key).raw_transaction.to_0x_hex()
        self.assert_verification_refused(legacy)
        self.assert_verification_refused("0x03" + self.signed()[4:])

    def test_missing_trailing_and_malformed_rlp_fields_are_refused(self):
        raw_tx = self.signed()
        raw = bytes.fromhex(raw_tx[2:])
        fields = rlp.decode(raw[1:], strict=True)
        cases = {
            "missing": "0x02" + rlp.encode(fields[:-1]).hex(),
            "extra_field": "0x02" + rlp.encode(fields + [b""]).hex(),
            "trailing_rlp": raw_tx + "00",
            "malformed_destination": self.mutate_rlp_field(raw_tx, 5, b"\x11" * 19),
            "noncanonical_integer": self.mutate_rlp_field(raw_tx, 1, b"\x00"),
            "invalid_y_parity": self.mutate_rlp_field(raw_tx, 9, b"\x02"),
        }
        for shape, malformed in cases.items():
            with self.subTest(shape=shape):
                self.assert_verification_refused(malformed)

    def test_malformed_raw_and_expected_values_are_refused_safely(self):
        raw_tx = self.signed()
        cases = (
            ("02", self.account.address, RECIPIENT, AMOUNT),
            ("0x02zz", self.account.address, RECIPIENT, AMOUNT),
            (raw_tx, "0x1234", RECIPIENT, AMOUNT),
            (raw_tx, self.account.address, "0x1234", AMOUNT),
            (raw_tx, self.account.address, RECIPIENT, True),
        )
        for raw, signer, recipient, amount in cases:
            with self.subTest(raw=raw, signer=signer, recipient=recipient, amount=amount):
                self.assert_verification_refused(
                    raw,
                    expected_signer=signer,
                    expected_recipient=recipient,
                    expected_amount_atomic=amount,
                )
