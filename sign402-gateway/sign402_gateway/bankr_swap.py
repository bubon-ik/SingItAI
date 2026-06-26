import re
import subprocess
from typing import Any


TX_HASH_RE = re.compile(r"\bTx Hash:\s*(0x[a-fA-F0-9]{64})\b")
RECEIVE_RE = re.compile(r"You receive:\s*~?\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")
PAY_RE = re.compile(r"You pay:\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")
MIN_RE = re.compile(r"Min received:\s*([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z0-9_.$-]+)")


def parse_bankr_transaction_hash(stdout: str) -> str | None:
    match = TX_HASH_RE.search(stdout)
    return match.group(1) if match else None


def parse_bankr_swap_quote(stdout: str) -> dict[str, str]:
    pay = PAY_RE.search(stdout)
    receive = RECEIVE_RE.search(stdout)
    minimum = MIN_RE.search(stdout)
    if not pay or not receive:
        raise ValueError("Bankr swap quote output did not include pay/receive amounts")
    return {
        "fromAmount": pay.group(1),
        "fromToken": pay.group(2),
        "toAmount": receive.group(1),
        "toToken": receive.group(2),
        "minToAmount": minimum.group(1) if minimum else receive.group(1),
    }


class BankrSwapClient:
    def __init__(self, *, bankr_cli: str):
        self.bankr_cli = bankr_cli

    def quote(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        ) + ["--quote-only"]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip() or result.stdout.strip() or "Bankr swap quote failed"
            )
        quote = parse_bankr_swap_quote(result.stdout)
        quote.update(
            {
                "ok": True,
                "command": command,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
        return quote

    def swap(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: str,
        chain: str = "base",
    ) -> dict[str, Any]:
        command = self._command(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            chain=chain,
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        payload = {
            "ok": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "txId": parse_bankr_transaction_hash(result.stdout),
        }
        if result.returncode != 0:
            raise ValueError(payload["stderr"] or payload["stdout"] or "Bankr swap failed")
        return payload

    def _command(self, *, from_token: str, to_token: str, amount: str, chain: str) -> list[str]:
        return [
            self.bankr_cli,
            "wallet",
            "swap",
            "--from",
            str(from_token),
            "--to",
            str(to_token),
            "--amount",
            str(amount),
            "--chain",
            str(chain),
        ]
