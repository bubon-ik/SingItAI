"""`POST /v1/decide` and `GET /v1/journal` — the spending decision, over HTTP.

This module is a translator and nothing else. It turns a JSON body into a
`Payment`, hands it to the `SpendingPolicy` the gateway already runs, and turns
the `Decision` back into JSON. Every rule that matters lives in the policy; if
you are looking for why a payment was refused, it is not in this file.

**No money moves through here.** The endpoint does not touch a wallet, does not
hold budget in `user_spend_limit_store`, and does not call anything on the
payment path. It answers a question. That boundary is the reason it can be
exposed publicly to agents that are not the gateway's own: the worst a caller
can do with it is learn what the gateway would say, and write a journal line
saying they asked.

It is deliberately *not* a second place where payments are decided. The gateway
has exactly one chokepoint — `_reserve_user_wallet_spend` — and it stays the
only one. This endpoint asks the same policy the same question, without being
in the path of the answer.

Names change at this boundary and only here: camelCase outside, snake_case in.
`payTo` is what the x402 protocol calls it and what an agent will send;
`pay_to` is what Python code reads. Translating in one place means neither side
has to know about the other's spelling.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from spending_memory import Action, Payment

DISABLED_ERROR = "spending-memory-disabled"

MERCHANT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(:[0-9]{1,5})?$")
"""A bare host, optionally with a port — `gateway.thegraph.com`, `bitrefill`.

Not a URL. A merchant is the identity a payout address is remembered against,
and `https://gateway.thegraph.com/api/x402/subgraphs/id/5zvR…` silently reduced
to a host is a guess about which part of the string was the seller. When the
guess is wrong the agent asks memory about a merchant nobody has ever paid,
gets told they are a stranger, and the answer is useless in the confident
direction. Rejecting is the only reading that cannot be quietly wrong.
"""

PAY_TO_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

AMOUNT_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")
"""Plain decimal digits. No sign, no exponent, no leading `.`.

Matches the pattern published in the OpenAPI document, so a request that
validates against the schema is a request this endpoint accepts.
"""

DEFAULT_JOURNAL_LIMIT = 50
MAX_JOURNAL_LIMIT = 200

JOURNAL_SCAN_MULTIPLIER = 20
JOURNAL_SCAN_FLOOR = 500
JOURNAL_SCAN_CEILING = 10_000
"""How far back to read before filtering to one owner.

The journal is shared by every owner, so returning *this* owner's newest 50
means reading more than 50 lines. The scan window is generous but bounded: an
owner who has been quiet while the fleet was busy sees fewer entries rather
than the service reading the whole table.
"""


class DecideError(ValueError):
    """A request that could not be read as a payment.

    Carries the machine-readable code the caller gets back, so the handler
    never has to map exception text onto an error string.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_text(payload: dict[str, Any], field: str, code: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DecideError(code)
    return value.strip()


def payment_from_request(payload: Any) -> Payment:
    """Read a `/v1/decide` body as a `Payment`, or say precisely what is wrong.

    Rejects rather than repairs. Each of these fields decides something, and a
    value quietly coerced into a plausible shape produces a confident answer to
    a question nobody asked.
    """
    if not isinstance(payload, dict):
        raise DecideError("invalid-request")

    merchant = _require_text(payload, "merchant", "invalid-merchant")
    if not MERCHANT_PATTERN.match(merchant):
        raise DecideError("invalid-merchant")

    pay_to = _require_text(payload, "payTo", "invalid-pay-to")
    if not PAY_TO_PATTERN.match(pay_to):
        raise DecideError("invalid-pay-to")

    owner = _require_text(payload, "owner", "missing-owner")

    raw_amount = payload.get("amountUsd")
    if not isinstance(raw_amount, str) or not AMOUNT_PATTERN.match(raw_amount):
        # A JSON number is refused on purpose, not out of pedantry. The amount
        # is compared against a limit, and a float cannot hold 0.1 exactly, so
        # `25.0` arriving as 25.000000000000004 decides a boundary case
        # differently from the same payment sent as "25.00". Booleans land here
        # too, which is the right answer for `"amountUsd": true`.
        raise DecideError("invalid-amount")
    try:
        amount_usd = Decimal(raw_amount)
    except InvalidOperation:
        raise DecideError("invalid-amount") from None
    if amount_usd <= 0:
        raise DecideError("invalid-amount")

    resource = payload.get("resource")
    if resource is not None and not isinstance(resource, str):
        raise DecideError("invalid-request")

    return Payment(
        merchant=merchant.lower(),
        pay_to=pay_to,
        amount_usd=amount_usd,
        owner=owner,
        resource=resource or None,
    )


def decide(payload: Any, policy: Any) -> tuple[int, dict[str, Any]]:
    """Answer one `/v1/decide` call. Returns `(status, body)`.

    A verdict always comes back as **200**, including `BLOCK`. The status code
    says whether the question was understood; `action` says what the answer
    was. Returning 4xx for a BLOCK would put the verdict in the one field
    every HTTP client already has a reflex about — retry it, or report it as an
    outage — and the whole point of a BLOCK is that it is neither.
    """
    if policy is None:
        # Memory is off. There is no verdict to give, and inventing one in
        # either direction is worse than admitting it: a PAY would be
        # permission nobody granted, an ESCALATE would look like a considered
        # refusal. 503 is the honest shape — the service is not answering.
        return 503, {"error": DISABLED_ERROR}

    try:
        payment = payment_from_request(payload)
    except DecideError as exc:
        return 400, {"error": exc.code}

    decision, claim_id = policy.authorise(payment)

    return 200, {
        "action": decision.action.value,
        "rule": decision.rule,
        "reason": decision.reason,
        "evidence": dict(decision.evidence or {}),
        "journalId": decision.journal_id,
        # Only a PAY holds a claim, and `authorise` guarantees that pairing:
        # if the claim could not be taken, the decision it returns is not a PAY
        # at all. Reading it off `claim_id` alone would be the same value today
        # and a lie the first time that changes.
        "claimId": claim_id if decision.action is Action.PAY else None,
    }


def journal(owner: Any, limit: Any, policy: Any) -> tuple[int, dict[str, Any]]:
    """Answer one `/v1/journal` call. Returns `(status, body)`.

    One owner per call, and no way to ask for everybody's. What the fleet
    learns about a merchant is shared; what one owner was asked, refused, and
    spent is not.
    """
    if policy is None:
        return 503, {"error": DISABLED_ERROR}

    if not isinstance(owner, str) or not owner.strip():
        return 400, {"error": "missing-owner"}
    owner = owner.strip()

    try:
        wanted = DEFAULT_JOURNAL_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        return 400, {"error": "invalid-request"}
    if wanted < 1:
        return 400, {"error": "invalid-request"}
    wanted = min(wanted, MAX_JOURNAL_LIMIT)

    scan = min(
        max(wanted * JOURNAL_SCAN_MULTIPLIER, JOURNAL_SCAN_FLOOR),
        JOURNAL_SCAN_CEILING,
    )

    entries = []
    for entry in policy.memory.journal(limit=scan):
        extra = entry.get("extra") or {}
        if extra.get("owner") != owner:
            continue
        # `rule`, `action`, `owner` and `merchant` are written after the
        # evidence by `record_decision`, so they are the keys to trust here.
        # Everything else in `extra` is whichever rule fired.
        evidence = {
            k: v
            for k, v in extra.items()
            if k not in {"rule", "action", "owner", "merchant"}
        }
        entries.append(
            {
                "journalId": entry.get("id"),
                "at": entry.get("ts"),
                "merchant": extra.get("merchant"),
                "action": extra.get("action"),
                "rule": extra.get("rule"),
                "evidence": evidence,
            }
        )
        if len(entries) >= wanted:
            break

    return 200, {"owner": owner, "entries": entries}
