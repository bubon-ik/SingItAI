# Bitrefill Wallet Token Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require every authenticated Telegram Bitrefill purchase to use a wallet asset explicitly selected by button, and bind that asset to pricing, WhatsApp approval, funding, and fulfillment.

**Architecture:** Reuse `/agent/withdraw/tokens` as the existing authenticated positive-balance token inventory. Generalize the Bitrefill quote and funding path from hard-coded SINGIT fields to a server-validated payment-token snapshot, while retaining legacy quote readers for reconciliation. Add a Telegram wizard stage that selects the token before quote creation; the selected address and amount become part of the immutable WhatsApp approval commitment.

**Tech Stack:** Python 3 standard library, `unittest`, Hermes plugin hooks, Telegram reply keyboards, SQLite quote storage, Bankr/CDP Base swap clients, Meta WhatsApp Cloud templates.

## Global Constraints

- Do not silently default a new authenticated Bitrefill purchase to SINGIT.
- Reuse the wallet token inventory already used by Balance and withdrawal flows.
- Require an explicit token button selection before quote creation and WhatsApp approval.
- Validate token identity and balance on the gateway using the authenticated Telegram user ID.
- Read, sign, and debit only the managed Base wallet belonging to that authenticated Telegram user; shared Hermes, Bankr, and CDP wallets are not per-user funding sources.
- Commit the selected token address and maximum atomic amount into the approval hash.
- Fail closed before fulfillment if price, liquidity, balance, gas, quote expiry, or token identity changes.
- Preserve readability of legacy stored Bitrefill quotes and operator-only SINGIT settlement records.
- Do not expose private keys, API tokens, full provider errors, or stack traces in Telegram or WhatsApp.
- Preserve unrelated untracked assets and the user-owned `cdp-x402-service/package-lock.json` server change.

---

### Task 1: Token-Neutral Bitrefill Pricing and Commitment

**Files:**
- Modify: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_quote.py`
- Test: `sign402-gateway/tests/test_real_rate_pricing.py`
- Test: `sign402-gateway/tests/test_bitrefill_quote.py`

**Interfaces:**
- Consumes: `RealRateSingitPricer.price_for_usdc(target_usdc, from_token, decimals)` and current Bitrefill product snapshots.
- Produces: `normalize_payment_token_pricing(pricing, token) -> dict[str, Any]`, token-neutral quote fields, and token-bound purchase commitments.

- [ ] **Step 1: Write failing pricing and quote tests**

Add tests asserting that an arbitrary six-decimal token produces `paymentTokenAmount` and `maxPaymentTokenAtomic`, while legacy SINGIT fields remain readable only for legacy input:

```python
def test_price_for_usdc_rounds_to_selected_token_precision(self):
    client = LinearQuoteClient(Decimal("1"))
    pricer = RealRateSingitPricer(
        quote_client=client,
        from_token="0x1111111111111111111111111111111111111111",
        buffer_bps=1000,
        max_singit="1000000",
    )
    result = pricer.price_for_usdc(
        "1.00",
        from_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        decimals=6,
        max_amount="5",
    )
    self.assertEqual(result["requiredAmount"], "1.1")
    self.assertEqual(result["requiredAmountAtomic"], "1100000")

def test_build_real_rate_quote_binds_selected_payment_token(self):
    quote = build_real_rate_quote(
        request={"productId": "gift", "packageId": "1", "country": "US"},
        product={
            "productId": "gift",
            "productName": "Gift",
            "name": "Gift",
            "productType": "gift_card",
            "packageId": "1",
            "packageValue": "1",
            "priceUsd": "1.00",
            "currency": "USD",
            "country": "US",
        },
        pricing={
            "targetUsdc": "1.00",
            "bufferedTargetUsdc": "1.10",
            "expectedUsdc": "1.10",
            "minUsdc": "1.08",
            "requiredAmount": "1.1",
            "requiredAmountAtomic": "1100000",
        },
        payment_token={
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "symbol": "USDC",
            "decimals": 6,
            "native": False,
        },
        quote_id="quote_token",
        now_epoch=100,
        ttl_seconds=120,
    )
    self.assertEqual(quote["paymentTokenSymbol"], "USDC")
    self.assertEqual(quote["paymentTokenAmount"], "1.1")
    self.assertEqual(quote["maxPaymentTokenAtomic"], "1100000")
    self.assertNotIn("maxSingitAtomic", quote)

def test_purchase_commitment_covers_payment_token(self):
    commitment = build_purchase_commitment(self._token_quote())
    self.assertEqual(commitment["paymentTokenAddress"], "0x1111111111111111111111111111111111111111")
    self.assertEqual(commitment["maxPaymentTokenAtomic"], "2500000")
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_bitrefill_quote tests.test_real_rate_pricing -v
```

Expected: failures because the pricer rounds to whole tokens, `build_real_rate_quote` has no `payment_token` argument, and the commitment still requires `maxSingitAtomic`.

- [ ] **Step 3: Implement token-neutral pricing normalization**

Add a focused helper and extend the quote builder:

```python
def normalize_payment_token_pricing(
    pricing: dict[str, Any],
    payment_token: dict[str, Any],
) -> dict[str, Any]:
    amount = format_decimal(Decimal(str(pricing["requiredAmount"])))
    atomic = str(pricing["requiredAmountAtomic"])
    return {
        "paymentTokenAddress": str(payment_token["address"]),
        "paymentTokenSymbol": str(payment_token["symbol"]),
        "paymentTokenDecimals": int(payment_token["decimals"]),
        "paymentTokenNative": bool(payment_token.get("native", False)),
        "paymentTokenAmount": amount,
        "maxPaymentTokenAtomic": atomic,
}
```

Generalize `price_for_usdc` with `max_amount: str | None = None`. Quantize the converged amount upward using `Decimal(1).scaleb(-token_decimals)`, minimize by the same quantum instead of subtracting one whole token, and return `requiredAmount` plus `requiredAmountAtomic`. Preserve `requiredSingit` aliases only when the call uses the constructor's legacy SINGIT token without a per-call token override.

Make `build_real_rate_quote` require `payment_token`, merge this normalized result, and build `quoteText` with the selected symbol. Update `build_purchase_commitment` to use token-neutral fields when present and to fall back to `maxSingitAtomic` only for legacy quotes.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 1 command. Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add sign402-gateway/sign402_gateway/real_rate_pricing.py sign402-gateway/sign402_gateway/bitrefill_quote.py sign402-gateway/tests/test_real_rate_pricing.py sign402-gateway/tests/test_bitrefill_quote.py
git commit -m "Generalize Bitrefill payment token quotes"
```

### Task 2: Gateway Wallet-Token Resolution and Balance Validation

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_gateway_server.py`

**Interfaces:**
- Consumes: authenticated `telegramUserId`, `UserWalletService.withdrawable_tokens(user_id)`, and request `paymentToken.address`.
- Produces: `WalletPaymentTokenResolver.resolve(user_id, raw_token) -> dict[str, Any]` and a quote containing a server-authoritative token snapshot.

- [ ] **Step 1: Write failing resolver and endpoint tests**

Cover exact ERC-20/native matching, stale token rejection, missing token rejection, server-owned decimals, and balance sufficiency:

```python
def test_quote_bitrefill_resolves_authenticated_wallet_token(self):
    request = {
        "productId": "gift",
        "packageId": "1",
        "country": "US",
        "telegramUserId": "1045618308",
        "paymentToken": {
            "address": "0x1111111111111111111111111111111111111111",
            "symbol": "FAKE",
            "decimals": 18,
            "native": False,
        },
    }
    result = self.quote_service(request)
    self.assertEqual(result["paymentTokenSymbol"], "USDC")
    self.assertEqual(result["paymentTokenDecimals"], 6)

def test_quote_bitrefill_rejects_token_not_in_wallet(self):
    with self.assertRaisesRegex(ValueError, "selected payment token is not available"):
        self.quote_service(self.request_with_address("0x2222222222222222222222222222222222222222"))
```

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_bitrefill_runner tests.test_gateway_server -v
```

Expected: new tests fail because the quote service ignores `paymentToken` and has no wallet resolver.

- [ ] **Step 3: Add the resolver and inject it into BitrefillQuoteService**

Implement a small resolver in `bitrefill_runner.py`:

```python
class WalletPaymentTokenResolver:
    def __init__(self, token_provider: Callable[[str], dict[str, Any]]):
        self.token_provider = token_provider

    def resolve(self, telegram_user_id: str, raw_token: Any) -> dict[str, Any]:
        user_id = str(telegram_user_id or "").strip()
        if not user_id:
            raise ValueError("telegramUserId is required for payment token selection")
        if not isinstance(raw_token, dict):
            raise ValueError("paymentToken is required")
        address = str(raw_token.get("address") or "").strip().lower()
        inventory = self.token_provider(user_id)
        for token in inventory.get("tokens", []):
            candidate = str(token.get("contractAddress") or "").strip().lower()
            if candidate == address:
                return {
                    "address": str(token["contractAddress"]),
                    "symbol": str(token["symbol"]),
                    "decimals": int(token["decimals"]),
                    "balance": str(token["balance"]),
                    "verified": bool(token.get("verified", False)),
                    "native": bool(token.get("native", False)),
                }
        raise ValueError("selected payment token is not available in this wallet")
```

Wire it from `build_server_from_env` using `user_wallet_service.withdrawable_tokens`. For native ETH, map inventory address `native` to Coinbase's swap sentinel `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE`; keep `native` in the committed wallet identity. Pass the pricing address and wallet balance to `real_rate_pricer.price_for_usdc(product["priceUsd"], from_token=pricing_address, decimals=decimals, max_amount=balance)` and pass the wallet token snapshot to `build_real_rate_quote`. Compare the normalized decimal balance to `paymentTokenAmount` before saving the quote.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_bitrefill_runner.py sign402-gateway/tests/test_gateway_server.py
git commit -m "Validate Bitrefill wallet payment tokens"
```

### Task 3: Selected-Token Funding for ERC-20 and Native ETH

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `cdp-x402-service/src/index.mjs`
- Test: `sign402-gateway/tests/test_gateway_server.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `cdp-x402-service/test/user-token-transfer.test.mjs`

**Interfaces:**
- Consumes: persisted quote fields from Task 1 and a freshly resolved wallet token from Task 2.
- Produces: `UserWalletTransferToCdpFundingRunner` transfers exactly the approved ERC-20 or native ETH amount to CDP, and `CdpWalletSwapFundingRunner` swaps that same committed source asset to USDC or performs an explicit USDC no-op.

- [ ] **Step 1: Write failing ERC-20 and native funding tests**

```python
def test_user_wallet_funding_uses_quote_payment_token(self):
    result = self.runner(
        telegram_user_id="1045618308",
        quote={
            "pricingMode": "bankr_real_rate",
            "paymentTokenAddress": "0x1111111111111111111111111111111111111111",
            "paymentTokenSymbol": "USDC",
            "paymentTokenDecimals": 6,
            "paymentTokenNative": False,
            "paymentTokenAmount": "1.10",
            "maxPaymentTokenAtomic": "1100000",
        },
        recipient={},
    )
    self.assertEqual(result["fromToken"], "0x1111111111111111111111111111111111111111")
    self.assertEqual(result["amount"], "1.10")

def test_user_wallet_funding_uses_native_transfer_for_eth(self):
    quote = self.native_quote(amount="0.001", atomic="1000000000000000")
    self.runner(telegram_user_id="1045618308", quote=quote, recipient={})
    self.transfer_client.transfer_native.assert_called_once()

def test_cdp_swap_uses_quote_payment_token_instead_of_env_default(self):
    quote = self.token_quote(
        address="0x1111111111111111111111111111111111111111",
        amount="2.5",
    )
    self.cdp_runner(quote)
    self.cdp_client.swap_singit_to_usdc.assert_called_once_with(
        amount="2.5",
        from_token="0x1111111111111111111111111111111111111111",
        min_usdc="1.00",
        chain="base",
        decimals=6,
    )

def test_cdp_funding_skips_swap_when_selected_token_is_usdc(self):
    result = self.cdp_runner(self.usdc_quote(amount="1.10"))
    self.assertEqual(result["mode"], "cdp_wallet_usdc_ready")
    self.cdp_client.swap_singit_to_usdc.assert_not_called()
```

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_gateway_server -v
cd ../cdp-x402-service
npm test
```

Expected: Python tests show the runner still reads configured SINGIT, does not select the existing native-transfer method, and omits source-token decimals from the CDP swap. Node regression tests remain green before the CLI behavior change.

- [ ] **Step 3: Implement selected-token transfer and pre-transfer recheck**

Extend `CdpWalletClient.swap_singit_to_usdc` with source-token decimals and forward them to the Node command:

```python
def swap_singit_to_usdc(
    self,
    *,
    amount: str,
    from_token: str,
    min_usdc: str,
    chain: str = "base",
    decimals: int = 18,
) -> dict[str, Any]:
    payload = self._run(
        [
            "swap",
            "--from-token",
            str(from_token),
            "--to-token",
            BASE_USDC_MAINNET,
            "--amount",
            str(amount),
            "--chain",
            str(chain),
            "--decimals",
            str(int(decimals)),
            "--min-usdc",
            str(min_usdc),
        ]
    )
    return self._with_tx_id(payload)
```

In `cdp-x402-service/src/index.mjs`, replace the fixed 18-decimal swap conversion with `humanTokenAmountToAtomic(requiredOption(options, "amount"), Number(options.decimals || "18"))`. The existing `user-token-transfer.test.mjs` already verifies six- and eighteen-decimal conversion; keep it in the regression run.

Update `UserWalletTransferToCdpFundingRunner` to read only the quote's payment-token fields for new quotes, re-resolve the per-user wallet inventory, compare current balance to the approved maximum, reserve native gas, and select the existing `transfer_native` or `transfer_token` method. Keep a legacy branch for stored quotes that contain only `singitAmount`.

Update `CdpWalletSwapFundingRunner` and the wallet fulfillment path to read `paymentTokenAddress`, `paymentTokenDecimals`, and `paymentTokenAmount` from the same quote. Map committed native ETH to `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE` at the CDP swap boundary. When the committed source is Base USDC, verify the transferred amount and return `mode="cdp_wallet_usdc_ready"` without requesting a USDC-to-USDC swap. Do not use the constructor's env `from_token` for a new token-neutral quote.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 3 commands. Expected: all focused tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/tests/test_bitrefill_runner.py cdp-x402-service/src/index.mjs cdp-x402-service/test/user-token-transfer.test.mjs
git commit -m "Fund Bitrefill with selected wallet assets"
```

### Task 4: Hermes Client and Mandatory Direct-Command Token

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_client.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: token objects returned by `withdraw_tokens` and the gateway request contract from Task 2.
- Produces: `execute_bitrefill_purchase(identity, product_id, package_id, country, recipient, payment_token, user_access_token)` and `_parse_bitrefill_args(raw) -> tuple[str, str, str, str] | None`.

- [ ] **Step 1: Write failing client and direct-command tests**

```python
def test_execute_bitrefill_purchase_posts_payment_token(self):
    self.client.execute_bitrefill_purchase(
        TelegramIdentity(user_id="1045618308"),
        product_id="bitrefill-giftcard-usd",
        package_id="0.1",
        country="US",
        payment_token={
            "address": "0x1111111111111111111111111111111111111111",
            "symbol": "USDC",
            "decimals": 6,
            "native": False,
        },
        user_access_token="user-token",
    )
    body = json.loads(self.opener.requests[0][0].data)
    self.assertEqual(body["paymentToken"]["symbol"], "USDC")

def test_bitrefill_direct_command_requires_token(self):
    self.assertIsNone(_parse_bitrefill_args("gift 1 US"))
    self.assertEqual(
        _parse_bitrefill_args("gift 1 US USDC"),
        ("gift", "1", "US", "USDC"),
    )
```

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
cd hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. python3 -m unittest tests.test_client tests.test_plugin -v
```

Expected: tests fail because the client and parser have no payment-token argument.

- [ ] **Step 3: Forward the selected token and enforce four direct arguments**

Change `execute_bitrefill_purchase` to require a normalized token dict and add this object to `quote_payload`:

```python
"paymentToken": {
    "address": str(payment_token["contractAddress"]),
    "symbol": str(payment_token["symbol"]),
    "decimals": int(payment_token["decimals"]),
    "native": bool(payment_token.get("native", False)),
}
```

For direct commands, resolve the fourth argument against `withdraw_tokens` before calling `execute_bitrefill_purchase`. Reject missing and ambiguous symbols without starting the background purchase.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 4 command. Expected: all client and plugin tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add hermes-plugins/sign402-wallet/client.py hermes-plugins/sign402-wallet/__init__.py hermes-plugins/sign402-wallet/tests/test_client.py hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Require Bitrefill payment token requests"
```

### Task 5: Telegram Bitrefill Token-Selection Buttons

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Test: `hermes-plugins/sign402-wallet/tests/test_plugin.py`

**Interfaces:**
- Consumes: existing `_normalize_withdraw_tokens`, `withdraw_tokens`, Bitrefill session state, and Task 4 client method.
- Produces: `select-payment-token` wizard stage and numbered reply buttons.

- [ ] **Step 1: Write failing wizard tests**

Cover gift cards with no recipient, products with required recipient fields, unverified duplicate symbols, Back, unavailable inventory, and retry after no liquidity:

```python
def test_bitrefill_wizard_requires_token_button_before_purchase(self):
    dispatch("Buy Bitrefill")
    dispatch("Search Products")
    dispatch("bitrefill gift card")
    dispatch("1")
    dispatch("1")
    self.assertIn("Choose a token to pay with", gateway.adapters["telegram"].sent[-1][1])
    self.assertEqual(client.bitrefill_calls, [])
    dispatch("2")
    self.assertEqual(client.bitrefill_calls[-1][-2]["symbol"], "USDC")

def test_bitrefill_token_buttons_label_unverified_contracts(self):
    text = _format_bitrefill_payment_tokens(self.wallet_tokens)
    self.assertIn("MEME", text)
    self.assertIn("0x2222", text)
```

- [ ] **Step 2: Run focused plugin tests and confirm RED**

```bash
cd hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. python3 -m unittest tests.test_plugin.PluginRegistrationTests -v
```

Expected: wizard purchases immediately after amount/recipient instead of entering token selection.

- [ ] **Step 3: Add the token-selection stage**

Replace direct purchase starts after amount/recipient with `_open_bitrefill_payment_token_selection`. Reuse `client.withdraw_tokens`, `_normalize_withdraw_tokens`, and numbered keyboards. Store these fields in `_BITREFILL_SESSIONS[user_id]` until selection:

```python
{
    "stage": "select-payment-token",
    "productId": product_id,
    "package": package,
    "country": country,
    "recipient": recipient,
    "paymentTokens": tokens,
}
```

On selection, call Task 4's execution method with the exact selected token object. On a safe pricing/balance error, retain the session and redraw token buttons.

- [ ] **Step 4: Run focused plugin tests and confirm GREEN**

Run the Task 5 command. Expected: all plugin tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add hermes-plugins/sign402-wallet/__init__.py hermes-plugins/sign402-wallet/tests/test_plugin.py
git commit -m "Add Bitrefill payment token buttons"
```

### Task 6: WhatsApp Approval Copy and Token-Bound Integration

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py`
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py`
- Test: `sign402-gateway/tests/test_bitrefill_runner.py`
- Test: `sign402-gateway/tests/test_imessage_approvals.py`

**Interfaces:**
- Consumes: token-neutral quote and commitment from Tasks 1–3.
- Produces: approval context containing product, USD price, exact selected-token maximum, wallet, reference, and expiry.

- [ ] **Step 1: Write failing approval-context tests**

```python
def test_bitrefill_approval_names_selected_token_and_amount(self):
    lines = _bitrefill_approval_context_lines(
        self.token_quote(symbol="USDC", amount="1.10"),
        source_wallet="0x1111111111111111111111111111111111111111",
        now_epoch_value=100,
    )
    text = "\n".join(lines)
    self.assertIn("Payment token: USDC", text)
    self.assertIn("Maximum spend: 1.10 USDC", text)

def test_whatsapp_template_parameter_contains_selected_token(self):
    payload = self.capture_template_payload(self.token_quote())
    body_text = payload["template"]["components"][0]["parameters"][0]["text"]
    self.assertIn("USDC", body_text)
```

- [ ] **Step 2: Run focused approval tests and confirm RED**

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_bitrefill_runner tests.test_imessage_approvals -v
```

Expected: context still says SINGIT or omits selected-token fields.

- [ ] **Step 3: Render token-neutral SingIt approval text**

Update `_bitrefill_approval_context_lines` to use `paymentTokenSymbol` and `paymentTokenAmount` for new quotes, with a legacy SINGIT fallback. Keep the approved `singit_payment_request` template and place the complete human-readable transaction summary in its first body parameter. Do not alter template name or language.

- [ ] **Step 4: Run focused approval tests and confirm GREEN**

Run the Task 6 command. Expected: all focused tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/sign402_gateway/imessage_approvals.py sign402-gateway/tests/test_bitrefill_runner.py sign402-gateway/tests/test_imessage_approvals.py
git commit -m "Show Bitrefill payment token in approvals"
```

### Task 7: Regression Verification and Deployment Documentation

**Files:**
- Modify: `hermes-plugins/sign402-wallet/README.md`
- Modify: `docs/production-beta-checklist.md`
- Test: all gateway, plugin, and CDP service tests.

**Interfaces:**
- Consumes: completed Tasks 1–6.
- Produces: deployable code and operator instructions for explicit Bitrefill token selection.

- [ ] **Step 1: Update user and operator documentation**

Document the button flow and mandatory direct syntax:

```text
/bitrefill <productId> <packageId> <country> <token>
```

Document that token selection is mandatory, WhatsApp shows the exact maximum source-token spend, and unavailable liquidity/balance stops before approval.

- [ ] **Step 2: Run the complete gateway suite**

```bash
cd sign402-gateway
PYTHONPATH=. ./.venv/bin/python -m unittest discover -s tests -q
```

Expected: all gateway tests pass.

- [ ] **Step 3: Run the complete plugin suite in a clean environment**

```bash
cd hermes-plugins/sign402-wallet
env -i HOME="$HOME" PATH="$PATH" PYTHONPATH=. python3 -m unittest discover -s tests -q
```

Expected: all plugin tests pass.

- [ ] **Step 4: Run the CDP service suite**

```bash
cd cdp-x402-service
npm test
```

Expected: all Node tests pass.

- [ ] **Step 5: Perform a no-spend staging check**

Use the local authenticated endpoints to list wallet tokens and create a `$0.10` Bitrefill quote, but reject the WhatsApp approval. Confirm the quote names the selected token, no transfer transaction is created, and no Bitrefill order exists.

- [ ] **Step 6: Commit documentation**

```bash
git add hermes-plugins/sign402-wallet/README.md docs/production-beta-checklist.md
git commit -m "Document Bitrefill wallet token selection"
```

- [ ] **Step 7: Prepare server deployment commands**

Provide commands that back up mutable Sign402 state, pull the exact commit, run gateway/plugin/CDP tests, copy the plugin, restart `sign402-gateway` and `hermes-gateway`, and verify both health endpoints. Do not modify the user's unrelated server `package-lock.json` change.
