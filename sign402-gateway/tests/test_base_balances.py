import io
import json
import unittest
from urllib.error import HTTPError, URLError

from sign402_gateway.base_balances import (
    BASE_CHAIN_ID,
    BASE_USDC_ADDRESS,
    DEFAULT_SINGIT_TOKEN_ADDRESS,
    AlchemyBaseBalanceProvider,
    BaseBalanceError,
    JsonRpcClient,
    build_base_balance_provider_from_env,
    encode_erc20_balance_of,
    format_atomic_amount,
)


WALLET = "0xAc4aCb03cAdaFE1d68262cf94cD5E8B56d9bf45C"


class FakeResponse:
    def __init__(self, payload):
        self.body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.closed = False
        self.read_size = None

    def read(self, size=-1):
        self.read_size = size
        return self.body if size < 0 else self.body[:size]

    def close(self):
        self.closed = True


class QueueOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubRpc:
    def __init__(self, *, batch_results=None, call_results=None):
        self.batch_results = list(batch_results or [])
        self.call_results = list(call_results or [])
        self.batch_calls = []
        self.calls = []

    def batch(self, calls, *, allow_errors=False):
        self.batch_calls.append((calls, allow_errors))
        result = self.batch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def call(self, method, params):
        self.calls.append((method, params))
        result = self.call_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def trusted_batch(
    *,
    chain_id=BASE_CHAIN_ID,
    eth_atomic=10**15,
    usdc_atomic=12_500_000,
    singit_atomic=250 * 10**18,
):
    return [
        hex(chain_id),
        hex(eth_atomic),
        hex(usdc_atomic),
        hex(singit_atomic),
    ]


class JsonRpcClientTests(unittest.TestCase):
    def test_formats_atomic_values_exactly(self):
        self.assertEqual(format_atomic_amount(0, 18), "0")
        self.assertEqual(format_atomic_amount(1, 18), "0.000000000000000001")
        self.assertEqual(format_atomic_amount(1_250_000, 6), "1.25")
        self.assertEqual(format_atomic_amount(250 * 10**18, 18), "250")

    def test_call_posts_json_rpc_and_closes_response(self):
        response = FakeResponse({"jsonrpc": "2.0", "id": 1, "result": "0x2105"})
        opener = QueueOpener(response)
        client = JsonRpcClient(
            endpoint_url="https://base-mainnet.g.alchemy.com/v2/private-key",
            opener=opener,
        )

        result = client.call("eth_chainId", [])

        self.assertEqual(result, "0x2105")
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 5.0)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_chainId",
                "params": [],
            },
        )
        self.assertTrue(response.closed)
        self.assertEqual(response.read_size, 262145)

    def test_batch_returns_results_in_request_order(self):
        response = FakeResponse(
            [
                {"jsonrpc": "2.0", "id": 2, "result": "second"},
                {"jsonrpc": "2.0", "id": 1, "result": "first"},
            ]
        )
        client = JsonRpcClient(
            endpoint_url="https://base-mainnet.g.alchemy.com/v2/private-key",
            opener=QueueOpener(response),
        )

        result = client.batch(
            [
                ("eth_chainId", []),
                ("eth_getBalance", [WALLET, "latest"]),
            ]
        )

        self.assertEqual(result, ["first", "second"])

    def test_batch_can_tolerate_individual_json_rpc_errors(self):
        response = FakeResponse(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"symbol": "AAA"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32000, "message": "private upstream body"},
                },
            ]
        )
        client = JsonRpcClient(
            endpoint_url="https://base-mainnet.g.alchemy.com/v2/private-key",
            opener=QueueOpener(response),
        )

        result = client.batch(
            [
                ("alchemy_getTokenMetadata", ["0x" + "11" * 20]),
                ("alchemy_getTokenMetadata", ["0x" + "22" * 20]),
            ],
            allow_errors=True,
        )

        self.assertEqual(result, [{"symbol": "AAA"}, None])

    def test_transport_errors_redact_endpoint_and_response_body(self):
        cases = (
            FakeResponse(b"not-json"),
            FakeResponse(b"x" * 262145),
            HTTPError(
                "https://base-mainnet.g.alchemy.com/v2/private-key",
                401,
                "Unauthorized private upstream body",
                {},
                io.BytesIO(b"private upstream body"),
            ),
            URLError("private endpoint unavailable"),
        )

        for result in cases:
            with self.subTest(result=type(result).__name__):
                client = JsonRpcClient(
                    endpoint_url=(
                        "https://base-mainnet.g.alchemy.com/v2/private-key"
                    ),
                    opener=QueueOpener(result),
                )
                with self.assertRaises(BaseBalanceError) as caught:
                    client.call("eth_chainId", [])
                message = str(caught.exception)
                self.assertNotIn("private-key", message)
                self.assertNotIn("private upstream", message)

    def test_strict_batch_rejects_missing_id_and_rpc_error(self):
        cases = (
            [{"jsonrpc": "2.0", "id": 99, "result": "wrong"}],
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "secret body"},
                }
            ],
        )

        for payload in cases:
            with self.subTest(payload=payload):
                client = JsonRpcClient(
                    endpoint_url=(
                        "https://base-mainnet.g.alchemy.com/v2/private-key"
                    ),
                    opener=QueueOpener(FakeResponse(payload)),
                )
                with self.assertRaises(BaseBalanceError):
                    client.batch([("eth_chainId", [])])


class AlchemyBaseBalanceProviderTests(unittest.TestCase):
    def test_reads_trusted_base_balances_with_standard_rpc(self):
        rpc = StubRpc(
            batch_results=[trusted_batch()],
            call_results=[BaseBalanceError("discovery unavailable")],
        )
        provider = AlchemyBaseBalanceProvider(rpc_client=rpc)

        result = provider(WALLET)

        self.assertEqual(
            result["balances"],
            {"ETH": "0.001", "USDC": "12.5", "SINGIT": "250"},
        )
        self.assertEqual(result["unverifiedTokens"], [])
        calls, allow_errors = rpc.batch_calls[0]
        self.assertFalse(allow_errors)
        self.assertEqual(
            [method for method, _params in calls],
            ["eth_chainId", "eth_getBalance", "eth_call", "eth_call"],
        )
        self.assertEqual(calls[1][1], [WALLET, "latest"])
        self.assertEqual(calls[2][1][0]["to"], BASE_USDC_ADDRESS)
        self.assertEqual(
            calls[3][1][0]["to"],
            DEFAULT_SINGIT_TOKEN_ADDRESS,
        )
        expected_calldata = encode_erc20_balance_of(WALLET)
        self.assertEqual(calls[2][1][0]["data"], expected_calldata)
        self.assertEqual(calls[3][1][0]["data"], expected_calldata)

    def test_rejects_wrong_chain_and_invalid_wallet(self):
        wrong_chain = AlchemyBaseBalanceProvider(
            rpc_client=StubRpc(batch_results=[trusted_batch(chain_id=1)])
        )

        with self.assertRaisesRegex(BaseBalanceError, "Base Mainnet"):
            wrong_chain(WALLET)
        with self.assertRaisesRegex(BaseBalanceError, "wallet address"):
            wrong_chain("not-an-address")

    def test_discovers_nonzero_unverified_tokens(self):
        token_a = "0x" + "11" * 20
        token_b = "0x" + "22" * 20
        rpc = StubRpc(
            batch_results=[
                trusted_batch(),
                [
                    {"symbol": "AAA", "decimals": 18},
                    {"symbol": "B/A D<script>", "decimals": 6},
                ],
            ],
            call_results=[
                {
                    "address": WALLET,
                    "tokenBalances": [
                        {
                            "contractAddress": token_b,
                            "tokenBalance": hex(1_250_000),
                        },
                        {
                            "contractAddress": token_a,
                            "tokenBalance": hex(3 * 10**18),
                        },
                        {
                            "contractAddress": "0x" + "33" * 20,
                            "tokenBalance": "0x0",
                        },
                        {
                            "contractAddress": BASE_USDC_ADDRESS,
                            "tokenBalance": hex(12_500_000),
                        },
                        {
                            "contractAddress": "bad-address",
                            "tokenBalance": "0x1",
                        },
                    ],
                }
            ],
        )

        result = AlchemyBaseBalanceProvider(rpc_client=rpc)(WALLET)

        self.assertEqual(
            result["unverifiedTokens"],
            [
                {
                    "symbol": "AAA",
                    "contractAddress": token_a,
                    "balance": "3",
                    "decimals": 18,
                },
                {
                    "symbol": "BADscript",
                    "contractAddress": token_b,
                    "balance": "1.25",
                    "decimals": 6,
                },
            ],
        )
        self.assertEqual(
            rpc.calls,
            [
                (
                    "alchemy_getTokenBalances",
                    [WALLET, "erc20", {"maxCount": 100}],
                )
            ],
        )
        metadata_calls, allow_errors = rpc.batch_calls[1]
        self.assertTrue(allow_errors)
        self.assertEqual(
            metadata_calls,
            [
                ("alchemy_getTokenMetadata", [token_a]),
                ("alchemy_getTokenMetadata", [token_b]),
            ],
        )

    def test_caps_dynamic_metadata_and_omits_invalid_metadata(self):
        token_rows = [
            {
                "contractAddress": f"0x{index:040x}",
                "tokenBalance": "0x1",
            }
            for index in range(1, 13)
        ]
        metadata = [
            {"symbol": f"T{index}", "decimals": 0}
            for index in range(1, 10)
        ] + [{"symbol": "BAD", "decimals": 99}]
        rpc = StubRpc(
            batch_results=[trusted_batch(), metadata],
            call_results=[{"tokenBalances": token_rows}],
        )

        result = AlchemyBaseBalanceProvider(rpc_client=rpc)(WALLET)

        metadata_calls, _allow_errors = rpc.batch_calls[1]
        self.assertEqual(len(metadata_calls), 10)
        self.assertEqual(len(result["unverifiedTokens"]), 9)
        self.assertEqual(
            [token["contractAddress"] for token in result["unverifiedTokens"]],
            [f"0x{index:040x}" for index in range(1, 10)],
        )

    def test_discovery_failure_does_not_hide_trusted_balances(self):
        rpc = StubRpc(
            batch_results=[trusted_batch()],
            call_results=[BaseBalanceError("token api failed")],
        )

        result = AlchemyBaseBalanceProvider(rpc_client=rpc)(WALLET)

        self.assertEqual(result["balances"]["USDC"], "12.5")
        self.assertEqual(result["unverifiedTokens"], [])

    def test_factory_requires_explicit_rpc_url(self):
        self.assertIsNone(build_base_balance_provider_from_env({}))

        provider = build_base_balance_provider_from_env(
            {
                "SIGN402_BASE_RPC_URL": (
                    "https://base-mainnet.g.alchemy.com/v2/private-key"
                ),
                "SIGN402_SINGIT_TOKEN_ADDRESS": "0x" + "44" * 20,
            }
        )

        self.assertIsInstance(provider, AlchemyBaseBalanceProvider)
        self.assertEqual(provider.singit_token_address, "0x" + "44" * 20)


if __name__ == "__main__":
    unittest.main()
