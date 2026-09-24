#!/usr/bin/env bash
# Uses an ephemeral key; never reads or changes the production wallet key.
# Run on an enrolled host with WALLET_PASS set. Override SIGN402_PYTHON and
# SIGN402_LEDGER_WALLET_CLI if they are not on PATH.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SIGN402_TEST_PYTHON="${SIGN402_PYTHON:-python3}"
export PYTHONPATH="$GATEWAY_DIR${PYTHONPATH:+:$PYTHONPATH}"
"$SIGN402_TEST_PYTHON" - <<'PYTEST'
import os
import subprocess
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet
from sign402_gateway.keyring import LedgerKeyringError, load_master_key

if not os.environ.get("WALLET_PASS"):
    raise SystemExit("Set WALLET_PASS before the rehearsal.")
cli = os.environ.get("SIGN402_LEDGER_WALLET_CLI") or "wallet-cli"
with tempfile.TemporaryDirectory(prefix="ledger-rehearsal-") as temp:
    encrypted = Path(temp) / "master-key.enc"
    key = Fernet.generate_key().decode()
    result = subprocess.run(
        [cli, "ring", "encrypt", "--key", "sign402-rehearsal", "-o", str(encrypted)],
        input=key.encode(), capture_output=True, timeout=60,
    )
    if result.returncode:
        raise SystemExit("FAIL: wallet-cli could not encrypt the throwaway key.")
    values = dict(os.environ, SIGN402_LEDGER_KEYRING_ENABLED="1",
                  SIGN402_LEDGER_KEYRING_KEY="sign402-rehearsal",
                  SIGN402_LEDGER_KEYRING_FILE=str(encrypted), SIGN402_LEDGER_WALLET_CLI=cli)
    if load_master_key(values) != key:
        raise SystemExit("FAIL: decrypted key differs from the original.")
    print("PASS: the real CLI and gateway round-trip the exact throwaway key.")
    with encrypted.open("ab") as f:
        f.write(b"tampered")
    try:
        load_master_key(values)
    except LedgerKeyringError:
        print("PASS: corrupted ciphertext is refused.")
    else:
        raise SystemExit("FAIL: corrupted ciphertext was accepted.")
    values.update(SIGN402_LEDGER_KEYRING_ENABLED="0", SIGN402_WALLET_MASTER_KEY=key)
    if load_master_key(values) != key:
        raise SystemExit("FAIL: disabled mode did not preserve the environment key.")
    print("PASS: disabled mode is unchanged. No key value was printed.")
PYTEST
