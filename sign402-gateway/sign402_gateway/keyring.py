"""The wallet master key, decrypted through the Ledger Key Ring at start-up.

`SIGN402_WALLET_MASTER_KEY` encrypts every managed wallet's private key, every
buyer email, and every stored fulfilment token. Until now it sat in plaintext in
`/etc/sign402-gateway.env`: whoever read that file read every wallet. A stolen
disk, a leaked backup, a misconfigured log shipper — any one of them was the
whole custody system.

With the ring switched on, the file on disk is AES-256-GCM ciphertext produced
by a Ledger device, and the key exists in the clear only in this process's
memory, only after a successful decrypt, and only for as long as the gateway
runs.

## What this actually buys, and what it does not

**Gives:** the key is unreadable at rest; recovery is bound to the device that
provisioned the ring; a stolen disk without `WALLET_PASS` is ciphertext.

**Does not give:** `ring decrypt` works on a box with no device attached, and —
measured, not assumed — with no network either (`docs/checks.md`, L1b). That is
Ledger's intended design; it is the thing that makes a keyless VPS possible at
all. The device is therefore the root of *enrolment*, not a gate on each use,
and after `ring init` the scoped key is derivable on this host from the member
credentials and `WALLET_PASS` alone. So an attacker who already owns a *running*
host decrypts exactly as we do.

This raises the cost of a stolen disk and a stolen backup. It does not make a
fully compromised host safe, and describing it as though it did would be a
misdescription of Ledger's own product to the people who built it.

The absence of a network requirement is deliberate good news operationally: boot
does not depend on the LKRP service being reachable, so this change adds no
third-party dependency to the start-up path of a payment system.

## Why a failure here refuses to boot

There is no fallback to the plaintext environment variable when the ring is on.
A silent fallback is how you ship the *wrong* key: the gateway comes up, appears
healthy, and re-encrypts nothing — until a user tries to spend and their wallet
will not decrypt, which is the first anyone hears of it. The loud failure is a
service that does not start and says which file and which key name it could not
read.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Mapping, MutableMapping

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

MASTER_KEY_ENV = "SIGN402_WALLET_MASTER_KEY"

KEYRING_ENABLED_ENV = "SIGN402_LEDGER_KEYRING_ENABLED"
"""Off by default. A box that was never provisioned behaves exactly as before.

Defaulting this on would break every existing deployment on the next restart,
and the thing it would break is the ability to decrypt customer wallets.
"""

KEYRING_KEY_NAME_ENV = "SIGN402_LEDGER_KEYRING_KEY"
KEYRING_FILE_ENV = "SIGN402_LEDGER_KEYRING_FILE"
KEYRING_CLI_ENV = "SIGN402_LEDGER_WALLET_CLI"
KEYRING_TIMEOUT_ENV = "SIGN402_LEDGER_KEYRING_TIMEOUT_SECONDS"

DEFAULT_KEY_NAME = "sign402-master"
DEFAULT_CLI = "wallet-cli"
DEFAULT_TIMEOUT_SECONDS = 60

TRUTHY_OFF = {"0", "false", "no", "off"}


class LedgerKeyringError(RuntimeError):
    """The ring is on and the master key could not be recovered through it.

    Always fatal. Raised at start-up, never per request.
    """


def keyring_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = str(values.get(KEYRING_ENABLED_ENV, "0")).strip().lower()
    return raw not in TRUTHY_OFF and raw != ""


def _decrypt_command(values: Mapping[str, str]) -> tuple[list[str], str, str]:
    cli = str(values.get(KEYRING_CLI_ENV, "") or DEFAULT_CLI).strip() or DEFAULT_CLI
    key_name = (
        str(values.get(KEYRING_KEY_NAME_ENV, "") or DEFAULT_KEY_NAME).strip()
        or DEFAULT_KEY_NAME
    )
    path = str(values.get(KEYRING_FILE_ENV, "") or "").strip()
    if not path:
        raise LedgerKeyringError(
            f"{KEYRING_ENABLED_ENV} is on but {KEYRING_FILE_ENV} is not set, so "
            "there is no encrypted master key to read. Point it at the "
            "`.enc` file produced by `wallet-cli ring encrypt`."
        )
    # `-i` reads the ciphertext; there is deliberately no `-o`. Without it the
    # plaintext comes back on stdout and never touches the filesystem, which is
    # the entire point of moving the key off disk in the first place. A
    # decrypted master key written to a temporary file is the same
    # vulnerability with extra steps and a worse audit trail.
    return [cli, "ring", "decrypt", "--key", key_name, "-i", path], key_name, path


def load_master_key(env: Mapping[str, str] | None = None) -> str:
    """The master key, however this deployment is configured to get it.

    Ring off — the default — reads `SIGN402_WALLET_MASTER_KEY` exactly as the
    gateway always has. Ring on runs `wallet-cli ring decrypt` and returns what
    comes back on stdout, or raises.
    """
    values = os.environ if env is None else env

    if not keyring_enabled(values):
        return str(values.get(MASTER_KEY_ENV, "") or "")

    command, key_name, path = _decrypt_command(values)
    where = f"key {key_name!r} in {path}"

    try:
        timeout = int(
            str(values.get(KEYRING_TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_SECONDS)
        )
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS

    try:
        completed = subprocess.run(
            command,
            # The plaintext is the whole point, so it is captured and never
            # inherited: with `stdout=None` a decrypted master key would go to
            # the service's own stdout and straight into the journal.
            capture_output=True,
            # `WALLET_PASS` lives in the environment, which is how wallet-cli
            # runs unattended. Passing the mapping through is what lets a test
            # point at a stand-in binary without touching the real process env.
            env={**os.environ, **{k: str(v) for k, v in values.items()}},
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LedgerKeyringError(
            f"{KEYRING_ENABLED_ENV} is on but {command[0]!r} was not found on "
            f"PATH, so {where} cannot be decrypted. Install "
            "`@ledgerhq/wallet-cli`, or point "
            f"{KEYRING_CLI_ENV} at the binary."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LedgerKeyringError(
            f"`wallet-cli ring decrypt` did not finish within {timeout}s while "
            f"reading {where}."
        ) from exc

    if completed.returncode != 0:
        raise LedgerKeyringError(
            f"`wallet-cli ring decrypt` failed with exit code "
            f"{completed.returncode} reading {where}: "
            f"{_tail(completed.stderr)}"
        )

    master_key = completed.stdout.decode("utf-8", "replace").strip()
    if not master_key:
        raise LedgerKeyringError(
            f"`wallet-cli ring decrypt` returned nothing for {where}. The "
            "gateway will not start on an empty master key, because every "
            "managed wallet would silently fail to decrypt instead."
        )

    try:
        Fernet(master_key.encode("ascii"))
    except Exception as exc:
        # The same check `ManagedBaseWalletService` makes before using the key.
        # Making it here turns "the wallets are undecryptable" into "the
        # service did not start", which is the difference between finding out
        # now and finding out from a customer.
        raise LedgerKeyringError(
            f"the value decrypted from {where} is not a valid Fernet key. "
            "Nothing about it is echoed here on purpose. Check that the "
            "ciphertext was produced from the master key and not from "
            "something else."
        ) from exc

    logger.info(
        "master key recovered through the Ledger key ring (%s)", where
    )
    return master_key


def install_master_key(env: MutableMapping[str, str] | None = None) -> str:
    """Resolve the key once and put it where the rest of the gateway looks.

    Eight call sites already read `SIGN402_WALLET_MASTER_KEY` out of an
    environment mapping. Rewriting all eight to know about a key ring would put
    eight chances to forget into a code path whose failure mode is
    undecryptable customer wallets. Resolving once, at boot, before anything
    reads it, leaves every one of them exactly as it was — which is also what
    makes the ring switchable without touching them.
    """
    values = os.environ if env is None else env
    master_key = load_master_key(values)
    if master_key:
        values[MASTER_KEY_ENV] = master_key
    return master_key


def _tail(stream: bytes, limit: int = 400) -> str:
    """The end of the CLI's stderr, for the operator reading the boot failure.

    Only ever stderr. `wallet-cli` prints the plaintext to stdout, so stdout
    must never reach a log line, an exception message, or a crash report.
    """
    text = stream.decode("utf-8", "replace").strip()
    return text[-limit:] if len(text) > limit else text or "(no output)"
