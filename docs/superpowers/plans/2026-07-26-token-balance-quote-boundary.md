# Token-Balance Quote Boundary Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Bitrefill real-rate pricing try the exact selected-token balance before rejecting a purchase, without ever quoting above that balance or weakening purchase safeguards.

**Architecture:** Keep the change inside `RealRateSingitPricer.price_for_usdc()`, where the wallet balance is already supplied as `max_amount`. Clamp the initial and growing quote probes to the active cap, make an unavailable or insufficient exact-cap quote terminal, and preserve the existing binary search, minimization, and successful-quote cache.

**Tech Stack:** Python 3.14, `decimal.Decimal`, standard-library `unittest`, fake Bankr quote clients, systemd deployment on the existing Sign402 VPS.

## Global Constraints

- Never request a quote greater than the active `max_amount`, including the initial probe.
- Request the exact `max_amount` at most once per pricing operation, including unsuccessful quote attempts.
- A successful exact-cap quote below the target must raise `selected payment token balance is insufficient at the current swap rate`.
- An unavailable exact-cap quote must raise `unable to obtain a swap quote for the selected payment token balance`.
- Non-skippable Bankr authentication, validation, and client errors must continue to propagate unchanged.
- Without a per-call `max_amount`, constructor-level `max_singit` exhaustion must continue to raise `required SINGIT exceeds configured maximum`.
- Preserve the 2% service fee, buffered target, Bitrefill product cap, user limits, platform ceilings, gas reservation, approval binding, replay protection, funding, settlement, and fulfillment behavior.
- Automated and deployment verification must not create an approval, swap, transfer, Bitrefill order, fulfillment, or charge.
- Do not add production secrets, wallet balances, recipient data, or redemption data to source, fixtures, logs, or commits.

---

## File Structure

- Modify `sign402-gateway/sign402_gateway/real_rate_pricing.py`: clamp quote discovery to the active cap and emit cap-source-specific terminal errors.
- Modify `sign402-gateway/tests/test_real_rate_pricing.py`: reproduce the production boundary with fake quote clients and cover success, insufficient balance, unavailable quote, exactly-once, and sub-unit-cap behavior.
- No runner, server, configuration, database, or Hermes plugin file changes are required.

### Task 1: Implement the hard quote boundary with regression tests

**Files:**
- Modify: `sign402-gateway/tests/test_real_rate_pricing.py`
- Modify: `sign402-gateway/sign402_gateway/real_rate_pricing.py:32-92`

**Interfaces:**
- Consumes: `RealRateSingitPricer.price_for_usdc(target_usdc: str, *, from_token: str | None = None, decimals: int | None = None, max_amount: str | None = None) -> dict[str, Any]`
- Produces: the same public signature and result dictionary; only quote-boundary control flow and failure messages change.

- [ ] **Step 1: Add the four failing regression tests**

Add these methods to `RealRatePricingTests` in
`sign402-gateway/tests/test_real_rate_pricing.py`:

```python
    def test_quotes_exact_wallet_balance_before_rejecting(self):
        balance = Decimal("20750000.123456789")
        client = MinAmountQuoteClient(
            rate=Decimal("0.0000014"),
            minimum=Decimal("20000000"),
        )
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xSINGIT",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
            max_singit="2000000000",
        )

        result = pricer.price_for_usdc(
            "24.48",
            max_amount=format(balance, "f"),
        )

        required = Decimal(result["requiredAmount"])
        self.assertLess(required, balance)
        self.assertGreaterEqual(Decimal(result["expectedUsdc"]), Decimal("24.48"))
        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_reports_insufficient_balance_after_exact_cap_quote(self):
        balance = Decimal("5")
        client = LinearQuoteClient(Decimal("0.01"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        with self.assertRaisesRegex(
            ValueError,
            "^selected payment token balance is insufficient at the current swap rate$",
        ):
            pricer.price_for_usdc("0.10", max_amount=format(balance, "f"))

        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_reports_unavailable_quote_after_exact_cap_attempt(self):
        balance = Decimal("5")
        client = WalletApiMinAmountQuoteClient(
            rate=Decimal("0.01"),
            minimum=Decimal("8"),
        )
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        with self.assertRaisesRegex(
            ValueError,
            "^unable to obtain a swap quote for the selected payment token balance$",
        ):
            pricer.price_for_usdc("0.10", max_amount=format(balance, "f"))

        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))

    def test_never_quotes_above_sub_unit_wallet_balance(self):
        balance = Decimal("0.5")
        client = LinearQuoteClient(Decimal("1"))
        pricer = RealRateSingitPricer(
            quote_client=client,
            from_token="0xTOKEN",
            to_token="USDC",
            chain="base",
            buffer_bps=0,
        )

        result = pricer.price_for_usdc("0.25", max_amount=format(balance, "f"))

        self.assertEqual(result["requiredAmount"], "0.25")
        self.assertEqual(client.amounts.count(balance), 1)
        self.assertTrue(all(amount <= balance for amount in client.amounts))
```

- [ ] **Step 2: Run the focused suite and verify the regressions fail**

Run from the isolated worktree:

```bash
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s sign402-gateway/tests \
  -p 'test_real_rate_pricing.py' \
  -q
```

Expected: the four new tests fail against the current implementation because
it either quotes the initial `1` above a sub-unit balance or raises the legacy
configured-maximum error before quoting the exact balance.

- [ ] **Step 3: Clamp the search and add terminal cap errors**

In `RealRateSingitPricer.price_for_usdc()`, record whether the cap came from
the selected token balance, define the applicable exhaustion message, clamp
the initial probe, and replace the exponential loop with this control flow:

```python
        balance_cap = max_amount is not None
        amount_cap = (
            Decimal(str(max_amount))
            if balance_cap
            else self.max_singit
        )
        cap_exhausted_message = (
            "selected payment token balance is insufficient at the current swap rate"
            if balance_cap
            else "required SINGIT exceeds configured maximum"
        )
```

After calculating `buffered_target`, replace the current `low`/`high` search
loop with:

```python
        low = Decimal("0")
        high = min(Decimal("1"), amount_cap)
        high_quote = self._quote_or_none(high, token, token_decimals)
        while high_quote is None or Decimal(high_quote["toAmount"]) < buffered_target:
            if high >= amount_cap:
                if balance_cap and high_quote is None:
                    raise ValueError(
                        "unable to obtain a swap quote for the selected payment token balance"
                    )
                raise ValueError(cap_exhausted_message)
            low = high
            high = min(
                self._next_high_amount(
                    current=high,
                    quote=high_quote,
                    buffered_target=buffered_target,
                ),
                amount_cap,
            )
            high_quote = self._quote_or_none(high, token, token_decimals)
```

Keep the binary search and `_minimize_amount()` unchanged. Replace the later
rounded-cap error:

```python
        if rounded_singit > amount_cap:
            raise ValueError(cap_exhausted_message)
```

This makes the unsuccessful exact-cap branch terminal, so it cannot loop or
repeat a failed quote that is absent from `_quote_cache`.

- [ ] **Step 4: Run the focused pricing suite and verify it passes**

Run:

```bash
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s sign402-gateway/tests \
  -p 'test_real_rate_pricing.py' \
  -q
```

Expected: `Ran 16 tests` and `OK`.

- [ ] **Step 5: Run the Bitrefill runner regression suite**

Run:

```bash
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s sign402-gateway/tests \
  -p 'test_bitrefill_runner.py' \
  -q
```

Expected: `Ran 56 tests` and `OK`.

- [ ] **Step 6: Commit the tested implementation**

Run:

```bash
git diff --check
git add \
  sign402-gateway/sign402_gateway/real_rate_pricing.py \
  sign402-gateway/tests/test_real_rate_pricing.py
git commit -m "fix: quote wallet balance at pricing boundary"
```

Expected: one commit containing only the pricer and its focused regression
tests.

### Task 2: Run the complete local quality gate

**Files:**
- Verify: `sign402-gateway/`
- Verify: commits after `90fdb7d`

**Interfaces:**
- Consumes: the Task 1 implementation commit.
- Produces: a reviewed, clean branch that is safe to fast-forward into `x402Bnkr`.

- [ ] **Step 1: Run the complete gateway test suite**

Run:

```bash
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s sign402-gateway/tests \
  -q
```

Expected: all discovered gateway tests finish with `OK`; no live network
purchase path is invoked.

- [ ] **Step 2: Verify scope and repository cleanliness**

Run:

```bash
git diff --check 0cc97d2..HEAD
git diff --stat 0cc97d2..HEAD
git status --short --branch
```

Expected: the branch contains the approved specification, implementation
plan, pricer change, and pricing tests only; the worktree is clean.

- [ ] **Step 3: Perform an independent code review**

Review `90fdb7d..HEAD` against
`docs/superpowers/specs/2026-07-26-token-balance-quote-boundary-design.md`
and require explicit confirmation of all of the following:

```text
- no quote can exceed max_amount
- exact max_amount is attempted at most once
- successful/insufficient/unavailable cap outcomes are distinct
- configured max_singit behavior is preserved without max_amount
- non-skippable quote errors still propagate
- no approval, transfer, order, or fulfillment behavior changed
- focused and full gateway tests pass
```

Expected: no unresolved P0, P1, or P2 findings before integration.

### Task 3: Fast-forward, push, and deploy the reviewed fix

**Files:**
- Integrate in: `/Users/mp/Documents/Berlin Hack` on `x402Bnkr`
- Deploy in: `/home/hermes/apps/sign402` on `hermes@164.68.104.44`
- Restart: `sign402-gateway.service`

**Interfaces:**
- Consumes: the clean, reviewed `codex/token-balance-quote-boundary` branch.
- Produces: `singitai/x402Bnkr` and the production gateway running the same reviewed commit.

- [ ] **Step 1: Verify the main worktree has no tracked modifications**

Run:

```bash
git -C '/Users/mp/Documents/Berlin Hack' branch --show-current
git -C '/Users/mp/Documents/Berlin Hack' diff --quiet
git -C '/Users/mp/Documents/Berlin Hack' diff --cached --quiet
git -C '/Users/mp/Documents/Berlin Hack' status --short
```

Expected: branch `x402Bnkr`; no tracked or staged changes. Existing unrelated
untracked user files may remain and must not be added, moved, or deleted.

- [ ] **Step 2: Fast-forward the local integration branch**

Run:

```bash
git -C '/Users/mp/Documents/Berlin Hack' merge \
  --ff-only codex/token-balance-quote-boundary
```

Expected: `x402Bnkr` advances without a merge commit and preserves all
untracked user files.

- [ ] **Step 3: Re-run the focused pricing test after integration**

Run:

```bash
cd '/Users/mp/Documents/Berlin Hack'
'/Users/mp/Documents/Berlin Hack/payment-executor/.venv/bin/python' \
  -m unittest discover \
  -s sign402-gateway/tests \
  -p 'test_real_rate_pricing.py' \
  -q
```

Expected: `Ran 16 tests` and `OK`.

- [ ] **Step 4: Push `x402Bnkr` to its configured upstream**

Run:

```bash
git -C '/Users/mp/Documents/Berlin Hack' push singitai x402Bnkr
git -C '/Users/mp/Documents/Berlin Hack' status --short --branch
```

Expected: local `x402Bnkr` and `singitai/x402Bnkr` point to the same reviewed
commit; unrelated untracked files remain untouched.

- [ ] **Step 5: Fast-forward and test the production checkout**

Run:

```bash
ssh hermes@164.68.104.44 '
set -eu
cd /home/hermes/apps/sign402
test -z "$(git status --porcelain)"
git fetch origin x402Bnkr
git merge --ff-only origin/x402Bnkr
sign402-gateway/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests \
  -p "test_real_rate_pricing.py" \
  -q
sign402-gateway/.venv/bin/python -m unittest discover \
  -s sign402-gateway/tests \
  -p "test_bitrefill_runner.py" \
  -q
git rev-parse HEAD
'
```

Expected: clean fast-forward, 16 pricing tests pass, 56 runner tests pass, and
the reported server commit matches pushed `singitai/x402Bnkr`.

- [ ] **Step 6: Restart only the gateway and verify health**

Run interactively on the server:

```bash
sudo systemctl restart sign402-gateway
sleep 3
systemctl show sign402-gateway \
  -p ActiveState \
  -p SubState \
  -p MainPID \
  -p NRestarts
curl -fsS http://127.0.0.1:8099/health |
python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"]; print("health ok")'
```

Expected:

```text
ActiveState=active
SubState=running
NRestarts=0
health ok
```

Do not restart `hermes-gateway`: no Hermes plugin code changes. Do not perform
a live Bitrefill purchase as a deployment test.
