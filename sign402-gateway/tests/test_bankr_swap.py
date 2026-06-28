import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sign402_gateway.bankr_swap import (
    BankrWalletApiClient,
    BankrSwapClient,
    load_bankr_api_key,
    parse_bankr_swap_quote,
    parse_bankr_transaction_hash,
)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["bankr"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class FakeHttpResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class BankrSwapTests(unittest.TestCase):
    def test_load_bankr_api_key_reads_env_before_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"apiKey": "config_key"}), encoding="utf-8")

            key = load_bankr_api_key({"BANKR_API_KEY": "env_key"}, config_path=config)

        self.assertEqual(key, "env_key")

    def test_load_bankr_api_key_reads_bankr_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"apiKey": "config_key"}), encoding="utf-8")

            key = load_bankr_api_key({}, config_path=config)

        self.assertEqual(key, "config_key")

    def test_wallet_api_quote_posts_swap_quote(self):
        payload = {
            "from": {
                "amount": "100000",
                "formattedAmount": "100000",
                "symbol": "SINGIT",
            },
            "to": {
                "amount": "89892",
                "formattedAmount": "0.089892",
                "symbol": "USDC",
            },
            "minBuyAmount": "0.084807",
        }
        with patch("urllib.request.urlopen", return_value=FakeHttpResponse(payload)) as urlopen:
            client = BankrWalletApiClient(api_key="secret", base_url="https://api.bankr.bot")
            quote = client.quote(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="100000",
                chain="base",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.bankr.bot/wallet/swap-quote")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["toToken"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(quote["fromAmount"], "100000")
        self.assertEqual(quote["toAmount"], "0.089892")
        self.assertEqual(quote["minToAmount"], "0.084807")

    def test_wallet_api_swap_uses_quote_min_buy_amount(self):
        quote_payload = {
            "from": {
                "amount": "100000",
                "formattedAmount": "100000",
                "symbol": "SINGIT",
            },
            "to": {
                "amount": "89892",
                "formattedAmount": "0.089892",
                "symbol": "USDC",
            },
            "minBuyAmount": "0.084807",
        }
        swap_payload = {
            "success": True,
            "hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "amountSold": 100000,
            "amountReceived": 0.089892,
            "amountSoldRaw": "100000000000000000000000",
            "amountReceivedRaw": "89892",
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[FakeHttpResponse(quote_payload), FakeHttpResponse(swap_payload)],
        ) as urlopen:
            client = BankrWalletApiClient(api_key="secret")
            result = client.swap(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="100000",
                chain="base",
            )

        swap_request = urlopen.call_args_list[1].args[0]
        body = json.loads(swap_request.data.decode("utf-8"))
        self.assertEqual(swap_request.full_url, "https://api.bankr.bot/wallet/swap")
        self.assertEqual(body["minBuyAmount"], "0.084807")
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["txId"],
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_parse_quote_only_output(self):
        quote = parse_bankr_swap_quote(
            """
- Resolving 0xc2c1e0b7C401e6217193732272444D928646eba3 → USDC on base...
- Fetching quote for 11 0xc2c1…eba3 → USDC...

You pay:  11 SINGIT ($0.00)
You receive:  ~0.000004 USDC ($0.00)
  Min received:       0.000004 USDC
"""
        )

        self.assertEqual(quote["fromAmount"], "11")
        self.assertEqual(quote["fromToken"], "SINGIT")
        self.assertEqual(quote["toAmount"], "0.000004")
        self.assertEqual(quote["toToken"], "USDC")
        self.assertEqual(quote["minToAmount"], "0.000004")

    def test_parse_quote_only_output_with_thousands_separators(self):
        quote = parse_bankr_swap_quote(
            """
- Resolving 0xc2c1e0b7C401e6217193732272444D928646eba3 → USDC on base...
- Fetching quote for 262144 0xc2c1…eba3 → USDC...

You pay:  262,144 SINGIT ($0.23)
You receive:  ~0.2323 USDC ($0.23)
  Min received:       0.219167 USDC
"""
        )

        self.assertEqual(quote["fromAmount"], "262144")
        self.assertEqual(quote["toAmount"], "0.2323")
        self.assertEqual(quote["minToAmount"], "0.219167")

    def test_parse_transaction_hash_accepts_tx_hash_line(self):
        self.assertEqual(
            parse_bankr_transaction_hash(
                "Swap successful\nTx Hash:  "
                "0x8eb6fe0859bf2fe1726322e251c9bc18ef2033bc443285436ee33636d10b04d6"
            ),
            "0x8eb6fe0859bf2fe1726322e251c9bc18ef2033bc443285436ee33636d10b04d6",
        )

    def test_quote_runs_bankr_swap_quote_only(self):
        with patch(
            "subprocess.run",
            return_value=completed(
                stdout=(
                    "You pay:  25 SINGIT ($0.00)\n"
                    "You receive:  ~0.10 USDC ($0.10)\n"
                    "  Min received:       0.095 USDC"
                )
            ),
        ) as run:
            client = BankrSwapClient(bankr_cli="/tmp/bankr")
            quote = client.quote(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="25",
                chain="base",
            )

        self.assertEqual(quote["toAmount"], "0.10")
        self.assertIn("--quote-only", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][:3], ["/tmp/bankr", "wallet", "swap"])

    def test_swap_runs_bankr_swap_without_quote_only(self):
        with patch(
            "subprocess.run",
            return_value=completed(
                stdout=(
                    "Swap successful\n"
                    "Tx Hash:  0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
            ),
        ) as run:
            client = BankrSwapClient(bankr_cli="/tmp/bankr")
            result = client.swap(
                from_token="0xc2c1e0b7C401e6217193732272444D928646eba3",
                to_token="USDC",
                amount="25",
                chain="base",
            )

        self.assertEqual(
            result["txId"],
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertNotIn("--quote-only", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
