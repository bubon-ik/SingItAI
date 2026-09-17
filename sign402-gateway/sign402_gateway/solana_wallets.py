"""Managed Solana wallets alongside the existing Base wallet service.

Only wallet creation and balance reads are exposed at this milestone. Existing
payment and withdrawal callers keep their explicit Base behavior.
"""

import json
import sqlite3
from typing import Any

from .solana_keys import generate_keypair, keypair_address
from .user_wallets import ManagedBaseWalletService, WalletEncryptionError, _require_telegram_user_id

SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_DISABLED = "Solana payments and withdrawals are not enabled yet."


def validate_wallet_chain(chain: object) -> str:
    if not isinstance(chain, str) or chain not in {"base", "solana"}:
        raise ValueError("Unsupported wallet network. Use base or solana.")
    return chain


class ManagedWalletService(ManagedBaseWalletService):
    def __init__(self, *, solana_balance_provider=None, **kwargs):
        super().__init__(**kwargs)
        self.solana_balance_provider = solana_balance_provider

    def create_wallet(self, telegram_user_id: str, telegram_username: str = "", *, chain: str = "base") -> dict[str, Any]:
        if validate_wallet_chain(chain) == "base":
            return super().create_wallet(telegram_user_id, telegram_username)
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id, chain="solana")
        created = False
        if wallet is None:
            cipher = self._fernet()
            address, keypair = generate_keypair()
            # Authenticate the identity and chain with the key so copying a
            # ciphertext to a different user's row cannot change its owner.
            envelope = json.dumps({"chain": "solana", "telegramUserId": user_id,
                                   "address": address, "keypair": keypair})
            encrypted = cipher.encrypt(envelope.encode()).decode("ascii")
            try:
                wallet = self.store.insert_wallet(
                    telegram_user_id=user_id, telegram_username=telegram_username,
                    chain="solana", wallet_address=address,
                    encrypted_private_key=encrypted, status="created",
                )
                created = True
            except sqlite3.IntegrityError:
                wallet = self.store.get_wallet_by_telegram_user_id(user_id, chain="solana")
                if wallet is None:
                    raise
        self.store.record_audit_event(
            telegram_user_id=user_id,
            event_type="solana_wallet_created" if created else "solana_wallet_create_idempotent",
            wallet_address=wallet["wallet_address"],
        )
        return {**_response(wallet, created=created), "accessToken": self.store.issue_access_token(user_id)}

    def wallet_status(self, telegram_user_id: str, *, chain: str = "base") -> dict[str, Any]:
        if validate_wallet_chain(chain) == "base":
            return super().wallet_status(telegram_user_id)
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id, chain="solana")
        if wallet is None:
            return {"ok": False, "wallet": None,
                    "telegramText": "No Solana agent wallet yet. Send /wallet solana to create one."}
        return _response(wallet, created=False)

    def wallet_balance(self, telegram_user_id: str, *, chain: str = "base") -> dict[str, Any]:
        if validate_wallet_chain(chain) == "base":
            return super().wallet_balance(telegram_user_id)
        result = self.wallet_status(telegram_user_id, chain="solana")
        if not result["ok"]:
            return {**result, "balanceUnavailable": True}
        address = result["wallet"]["address"]
        self.store.record_audit_event(telegram_user_id=str(telegram_user_id),
                                     event_type="solana_wallet_balance_read", wallet_address=address)
        try:
            if self.solana_balance_provider is None:
                raise RuntimeError("Balance provider is not configured")
            balances = self.solana_balance_provider(address)
        except Exception:
            # Provider errors may contain URLs with credentials. Never include
            # them in the chat or present an unavailable balance as zero.
            return {**result, "balanceUnavailable": True,
                    "telegramText": f"Solana mainnet wallet: {address}\n\nBalance lookup is unavailable. Try again later.\n{_DISABLED}"}
        return {**result, "balances": balances, "balanceUnavailable": False,
                "telegramText": f"Solana mainnet wallet: {address}\n\nBalances:\n- SOL: {balances['SOL']}\n- USDC: {balances['USDC']}\n\n{_DISABLED}"}

    def decrypt_private_key_for_future_signing(self, telegram_user_id: str, *, chain: str = "base") -> str:
        if validate_wallet_chain(chain) == "base":
            return super().decrypt_private_key_for_future_signing(telegram_user_id)
        user_id = _require_telegram_user_id(telegram_user_id)
        wallet = self.store.get_wallet_by_telegram_user_id(user_id, chain="solana")
        if wallet is None:
            raise ValueError("Solana wallet not found")
        cipher = self._fernet()
        try:
            envelope = json.loads(cipher.decrypt(wallet["encrypted_private_key"].encode("ascii")))
            if (envelope["chain"] != "solana" or envelope["telegramUserId"] != user_id
                    or envelope["address"] != wallet["wallet_address"]
                    or keypair_address(envelope["keypair"]) != wallet["wallet_address"]):
                raise ValueError("Wallet binding mismatch")
            return envelope["keypair"]
        except Exception:
            raise WalletEncryptionError("Solana wallet private key could not be decrypted") from None


def _response(wallet: dict, *, created: bool) -> dict:
    address = wallet["wallet_address"]
    return {"ok": True, "created": created,
            "wallet": {"chain": "solana", "network": SOLANA_NETWORK, "address": address,
                       "status": wallet["status"], "spendingEnabled": False},
            "telegramText": f"Your Solana mainnet agent wallet{' is ready' if created else ''}:\n{address}\n\n{_DISABLED}"}
