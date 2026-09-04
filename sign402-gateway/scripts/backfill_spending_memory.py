#!/usr/bin/env python3
"""Seed Spending Memory from Bitrefill orders that were actually delivered.

Without this, memory starts empty and the agent asks about a merchant it has
paid for months. With it, the first decision after deploy is made against real
history: a real payout address, real prices, a real count.

Read-only against the commerce store. Writes only to Sibyl.

    python scripts/backfill_spending_memory.py --dry-run
    python scripts/backfill_spending_memory.py

Owner attribution: the commerce store does not record which telegram user made
which order (`UserPurchaseStore` keeps only each user's latest purchase), so
every backfilled settlement is attributed to a synthetic owner, `backfill` by
default. That costs nothing real. The merchant record — payout address, price
history, payment count — is shared across owners and is the part that matters,
and each daily total is written under the order's own date, so nothing lands on
today's autonomy budget.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spending_memory import Payment, SpendingMemory  # noqa: E402

MERCHANT = "bitrefill"
DELIVERED = "DELIVERED"


def settlement_address() -> str:
    """The same address the live path records, or the same stand-in.

    Kept identical to `_bitrefill_settlement_address` in the gateway on
    purpose: a backfill that writes a different counterparty would make the
    first real purchase look like the address had moved.
    """
    return (
        os.getenv("SIGN402_CDP_WALLET_ADDRESS", "").strip()
        or os.getenv("CDP_EVM_ACCOUNT_ADDRESS", "").strip()
        or "bitrefill:no-onchain-counterparty"
    )


def read_delivered(db_path: Path) -> list[dict]:
    """Delivered orders, oldest first.

    Raw SQL rather than `BitrefillCommerceStore`: only `quote_json` is needed
    and it is not the encrypted part — the recipient lives in `metadata_json`,
    which this never touches.
    """
    if not db_path.exists():
        raise SystemExit(f"commerce store not found: {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT quote_id, quote_json, created_at FROM bitrefill_orders "
            "WHERE state = ? ORDER BY created_at",
            (DELIVERED,),
        ).fetchall()
    finally:
        connection.close()

    orders = []
    for row in rows:
        try:
            quote = json.loads(row["quote_json"] or "{}")
        except ValueError:
            print(f"  ! {row['quote_id']}: unreadable quote, skipped")
            continue
        raw_amount = quote.get("totalUsd") or quote.get("priceUsd")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError):
            print(f"  ! {row['quote_id']}: no usable price, skipped")
            continue
        if amount <= 0:
            print(f"  ! {row['quote_id']}: non-positive price, skipped")
            continue
        orders.append(
            {
                "quote_id": row["quote_id"],
                "amount": amount,
                "day": datetime.fromtimestamp(
                    int(row["created_at"]), tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "product": quote.get("productName") or quote.get("productId") or "",
            }
        )
    return orders


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(
            os.getenv(
                "SIGN402_BITREFILL_COMMERCE_STORE_PATH",
                Path(__file__).resolve().parent.parent.parent
                / "demo-dashboard"
                / "bitrefill-orders.sqlite3",
            )
        ),
    )
    parser.add_argument("--owner", default="backfill")
    parser.add_argument("--pay-to", default=None, help="override the counterparty")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="write even though the merchant is already known (double-counts)",
    )
    args = parser.parse_args()

    orders = read_delivered(args.db)
    pay_to = args.pay_to or settlement_address()

    print(f"store      {args.db}")
    print(f"merchant   {MERCHANT}")
    print(f"pay_to     {pay_to}")
    print(f"delivered  {len(orders)} orders")
    if not orders:
        print("nothing to backfill")
        return

    total = sum(order["amount"] for order in orders)
    print(f"total      {total} USD")
    for order in orders:
        print(f"  {order['day']}  {order['amount']:>8} USD  {order['product'][:44]}")

    if args.dry_run:
        print("\ndry run, nothing written")
        return

    memory = SpendingMemory.local()

    known = memory.recall_merchant(MERCHANT)
    if known is not None and not args.force:
        # Running twice would inflate the payment count and the price history,
        # and an inflated count is what the agent trusts when it decides to pay
        # without asking. Refusing is the safe default.
        raise SystemExit(
            f"{MERCHANT} is already known: {known.payment_count} payments at "
            f"{known.pay_to}. Nothing written. Pass --force only if you are "
            "sure this history is missing."
        )

    for order in orders:
        memory.remember_settlement(
            Payment(
                merchant=MERCHANT,
                pay_to=pay_to,
                amount_usd=order["amount"],
                owner=args.owner,
                resource=MERCHANT,
            ),
            tx_id=f"backfill:{order['quote_id']}",
            day=order["day"],
        )

    written = memory.recall_merchant(MERCHANT)
    assert written is not None
    print(
        f"\nwrote {written.payment_count} payments, status {written.status}, "
        f"median {written.typical_usd} USD"
    )
    print(f"check it with:  sibyl memory recall merchant {MERCHANT}")


if __name__ == "__main__":
    main()
