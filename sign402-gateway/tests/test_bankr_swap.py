import subprocess
import unittest
from unittest.mock import patch

from sign402_gateway.bankr_swap import (
    BankrSwapClient,
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


class BankrSwapTests(unittest.TestCase):
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
