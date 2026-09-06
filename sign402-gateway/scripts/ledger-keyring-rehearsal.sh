#!/bin/bash
# End-to-end rehearsal of the Ledger key ring path, against the real wallet-cli.
#
# The unit tests drive a stand-in binary and prove the code handles it
# correctly. This proves the code and the actual CLI work together, which is a
# different claim and the one that matters before a demo.
#
# Uses a THROWAWAY Fernet key, generated here and discarded with the temp
# directory. The production master key is never read, never transferred and
# never encrypted by this script.
#
# Needs: a machine already enrolled with `wallet-cli ring init`, and WALLET_PASS
# exported. No device needs to be attached — see docs/checks.md, L1.
#
#   export WALLET_PASS=...
#   bash scripts/ledger-keyring-rehearsal.sh
set -u
cd "$(dirname "$0")" 2>/dev/null || true
REPO="$HOME/Documents/Berlin Hack"
W="$HOME/Documents/ledger-cli/node_modules/.bin/wallet-cli"
PY="$REPO/sign402-gateway/.venv/bin/python"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

if [ -z "${WALLET_PASS:-}" ]; then echo "set WALLET_PASS first"; exit 1; fi

echo "== 1. throwaway Fernet key, encrypted through the ring =="
"$PY" -c 'from cryptography.fernet import Fernet;import sys;sys.stdout.write(Fernet.generate_key().decode())' \
  | "$W" ring encrypt --key sign402-master -o "$WORK/master-key.enc" >/dev/null
ls -l "$WORK/master-key.enc" | awk '{print "   ciphertext:", $5, "bytes"}'

echo "== 2. the gateway loads it through the ring =="
SIGN402_LEDGER_KEYRING_ENABLED=1 \
SIGN402_LEDGER_KEYRING_FILE="$WORK/master-key.enc" \
SIGN402_LEDGER_WALLET_CLI="$W" \
"$PY" -c '
import os
from sign402_gateway.keyring import load_master_key
k = load_master_key(dict(os.environ))
print("   loaded a valid Fernet key, length", len(k), "- value not printed")
' 2>&1 | tail -3

echo "== 3. corrupt the ciphertext: it must refuse =="
printf 'tampered' >> "$WORK/master-key.enc"
SIGN402_LEDGER_KEYRING_ENABLED=1 \
SIGN402_LEDGER_KEYRING_FILE="$WORK/master-key.enc" \
SIGN402_LEDGER_WALLET_CLI="$W" \
"$PY" -c '
import os
from sign402_gateway.keyring import load_master_key, LedgerKeyringError
try:
    load_master_key(dict(os.environ))
    print("   *** FAILED: it started anyway ***")
except LedgerKeyringError as e:
    print("   refused, as designed:"); print("   ", str(e)[:160])
' 2>&1 | tail -4

echo "== 4. ring off: unchanged behaviour =="
SIGN402_WALLET_MASTER_KEY=not-a-real-key "$PY" -c '
import os
from sign402_gateway.keyring import load_master_key
print("   read straight from the environment:", load_master_key(dict(os.environ)))
'
