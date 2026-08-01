import json
from unittest import TestCase
from unittest.mock import patch

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
        self.http_requests = []

    def __call__(self, request):
        self.http_requests.append(request)
        self.requests.append(json.loads(request.content))
        if not self.replies:
            raise UnexpectedRpcRequest(
                f"unexpected JSON-RPC request: {self.requests[-1]['method']}"
            )
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)


class UnexpectedRpcRequest(BaseException):
    pass


class NeverReadStream(httpx.SyncByteStream):
    def __init__(self):
        self.read_attempted = False

    def __iter__(self):
        self.read_attempted = True
        raise AssertionError("encoded response body must not be consumed")
        yield b""  # pragma: no cover


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, content, chunk_size=1024):
        self.content = content
        self.chunk_size = chunk_size

    def __iter__(self):
        for offset in range(0, len(self.content), self.chunk_size):
            yield self.content[offset:offset + self.chunk_size]


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
        self.assertEqual(
            [request.headers["accept-encoding"] for request in queue.http_requests],
            ["identity", "identity", "identity"],
        )

    def assert_rpc_refused(self, client, queue=None, expected_methods=None):
        with self.assertRaisesRegex(SafeError, r"Base RPC is unavailable\.") as raised:
            client.get_balances(RECIPIENT)
        self.assertEqual(raised.exception.code, "base_rpc_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertIsNone(raised.exception.__cause__)
        if queue is not None:
            self.assertEqual(
                [request["method"] for request in queue.requests],
                expected_methods,
            )

    def test_wrong_chain_is_refused_before_balance_reads(self):
        client, queue = self.make_client(
            rpc_result(1, "0x1"),
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )

        self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_redirect_is_refused(self):
        client, queue = self.make_client(
            httpx.Response(
                302,
                headers={"location": "https://canary.invalid"},
                json=rpc_result(1, "0x2105"),
            ),
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )
        self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_valid_streamed_response_over_64_kib_without_length_is_refused(self):
        oversized = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": "0x2105",
            "padding": "x" * 65_536,
        }).encode()
        response = httpx.Response(200, stream=ChunkedStream(oversized))
        self.assertIsNone(response.headers.get("content-length"))
        client, queue = self.make_client(
            response,
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )
        self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_encoded_response_is_refused_before_body_is_consumed(self):
        for encoding in ("gzip", "deflate", "br"):
            with self.subTest(encoding=encoding):
                stream = NeverReadStream()
                response = httpx.Response(
                    200,
                    headers={"content-encoding": encoding},
                    stream=stream,
                )
                client, queue = self.make_client(
                    response,
                    rpc_result(2, "0x1"),
                    rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
                )

                self.assert_rpc_refused(client, queue, ["eth_chainId"])
                self.assertFalse(stream.read_attempted)

    def test_wrong_json_rpc_id_is_refused(self):
        client, queue = self.make_client(
            rpc_result(2, "0x2105"),
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )
        self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_json_rpc_error_is_refused_without_provider_content(self):
        client, queue = self.make_client(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "canary provider secret"},
                "result": "0x2105",
            },
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )

        with self.assertRaises(SafeError) as raised:
            client.get_balances(RECIPIENT)

        self.assertNotIn("canary", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(
            [request["method"] for request in queue.requests], ["eth_chainId"]
        )

    def test_malformed_json_rpc_envelope_is_refused(self):
        for response in (
            [rpc_result(1, "0x2105")],
            {"jsonrpc": "1.0", "id": 1, "result": "0x2105"},
            {"jsonrpc": "2.0", "id": True, "result": "0x2105"},
            {"jsonrpc": "2.0", "id": 1},
        ):
            with self.subTest(response=response):
                client, queue = self.make_client(
                    response,
                    rpc_result(2, "0x1"),
                    rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
                )
                self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_duplicate_json_rpc_fields_are_refused(self):
        response = httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":999,"id":1,"result":"0x2105"}',
        )
        client, queue = self.make_client(
            response,
            rpc_result(2, "0x1"),
            rpc_result(3, "0x" + (1).to_bytes(32, "big").hex()),
        )
        self.assert_rpc_refused(client, queue, ["eth_chainId"])

    def test_malformed_quantities_are_refused(self):
        word = rpc_result(3, "0x" + (1).to_bytes(32, "big").hex())
        cases = (
            (
                (rpc_result(1, "8453"), rpc_result(2, "0x1"), word),
                ["eth_chainId"],
            ),
            (
                (rpc_result(1, "0x02105"), rpc_result(2, "0x1"), word),
                ["eth_chainId"],
            ),
            (
                (rpc_result(1, "0x2105"), rpc_result(2, "0x00"), word),
                ["eth_chainId", "eth_getBalance"],
            ),
            ((
                rpc_result(1, "0x2105"),
                rpc_result(2, "0x1"),
                rpc_result(3, "0x1"),
            ), ["eth_chainId", "eth_getBalance", "eth_call"]),
            ((
                rpc_result(1, "0x2105"),
                rpc_result(2, "0x1"),
                rpc_result(3, "0x" + "gg" * 32),
            ), ["eth_chainId", "eth_getBalance", "eth_call"]),
        )
        for replies, expected_methods in cases:
            with self.subTest(replies=replies):
                client, queue = self.make_client(*replies)
                self.assert_rpc_refused(client, queue, expected_methods)

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
            ("0x" + "00" * 20, AMOUNT),
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

    def test_oversized_structurally_valid_transaction_is_refused_before_decoding(self):
        raw = bytes.fromhex(self.signed()[2:])
        fields = rlp.decode(raw[1:], strict=True)
        fields[7] += b"\x00" * 131_072
        oversized_raw = b"\x02" + rlp.encode(fields)
        oversized = "0x" + oversized_raw.hex()

        decoded = rlp.decode(oversized_raw[1:], strict=True)
        self.assertEqual(len(decoded), 12)
        self.assertGreater(len(oversized_raw), 131_072)
        self.assertEqual(len(decoded[7]), 68 + 131_072)

        class RlpDecodeReached(BaseException):
            pass

        with patch(
            "trezor_sidecar.base.rlp.decode", side_effect=RlpDecodeReached
        ):
            self.assert_verification_refused(oversized)

        class SignerRecoveryReached(BaseException):
            pass

        with (
            patch(
                "trezor_sidecar.base._MAX_RAW_TRANSACTION_BYTES",
                len(oversized_raw),
            ),
            patch(
                "trezor_sidecar.base.Account.recover_transaction",
                side_effect=SignerRecoveryReached,
            ),
        ):
            self.assert_verification_refused(oversized)

    def test_nonminimal_rlp_list_framing_is_refused(self):
        raw = bytes.fromhex(self.signed()[2:])
        self.assertEqual(raw[1], 0xF8)
        nonminimal = raw[:1] + b"\xf9\x00" + raw[2:]
        self.assert_verification_refused("0x" + nonminimal.hex())

    def test_high_s_signature_with_adjusted_parity_is_refused(self):
        curve_order = int(
            "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141",
            16,
        )
        raw_tx = self.signed()
        raw = bytes.fromhex(raw_tx[2:])
        fields = rlp.decode(raw[1:], strict=True)
        parity = int.from_bytes(fields[9], "big")
        low_s = int.from_bytes(fields[11], "big")
        fields[9] = (1 - parity).to_bytes(1, "big") if parity == 0 else b""
        fields[11] = (curve_order - low_s).to_bytes(32, "big")
        high_s = "0x02" + rlp.encode(fields).hex()

        self.assertEqual(
            Account.recover_transaction(high_s).lower(), self.account.address.lower()
        )
        self.assert_verification_refused(high_s)
