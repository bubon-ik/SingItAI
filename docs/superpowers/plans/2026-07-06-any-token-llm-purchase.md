# Any-Token Bankr LLM Purchase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Telegram user pay for a Bankr LLM key with any ERC-20 on their managed Base wallet: `/llm_buy <usd> <email> [token]` (symbol or contract address; omitted → SINGIT).

**Architecture:** Approach A from `docs/superpowers/specs/2026-07-06-any-token-llm-purchase-design.md`: the existing purchase pipeline (quote → approval → transfer → topup) is parameterised by a payment token resolved once at `start` and stored on the purchase row. Stables (USDC/USDT) skip the swap quote and the +5% buffer; other tokens reuse the generalized real-rate pricer whose CDP quote doubles as the liquidity pre-check.

**Tech Stack:** Python stdlib gateway (`sign402-gateway`), viem-based node helper (`cdp-x402-service`), Telegram plugin (`hermes-plugins/sign402-wallet`). Tests: `python3 -m unittest`.

**Working directory for all commands:** repo root `/Users/mp/Documents/Berlin Hack` unless stated otherwise.

---

### Task 1: Node commands `token-info`, `token-balance`, and `swap-price --decimals`

**Files:**
- Modify: `cdp-x402-service/src/index.mjs`

The file already imports `createPublicClient`, `http`, `erc20Abi` and helpers `viemChain`, `humanTokenAmountToAtomic` (used by `transferTokenFromUserWallet` / `getSwapPrice`). Verify the imports exist at the top of the file before starting; add any that are missing to the existing import lines.

- [ ] **Step 1: Add the two commands to the dispatcher**

In `main()`, after the `transfer-token-user` branch (`if (command === "transfer-token-user") { ... }`), add:

```js
  if (command === "token-info") {
    const result = await readTokenInfo(options);
    writeJson(result);
    return;
  }

  if (command === "token-balance") {
    const result = await readTokenBalance(options);
    writeJson(result);
    return;
  }
```

- [ ] **Step 2: Implement the read-only helpers**

Add next to `transferTokenFromUserWallet`:

```js
function basePublicClient(chainName) {
  const chain = viemChain(chainName || "base");
  const rpcUrl =
    process.env.SIGN402_BASE_RPC_URL ||
    process.env.BASE_RPC_URL ||
    chain.rpcUrls.default.http[0];
  return createPublicClient({ chain, transport: http(rpcUrl) });
}

async function readTokenInfo(options) {
  const token = requiredOption(options, "token");
  const publicClient = basePublicClient(options.chain);
  const [symbol, decimals] = await Promise.all([
    publicClient.readContract({ address: token, abi: erc20Abi, functionName: "symbol" }),
    publicClient.readContract({ address: token, abi: erc20Abi, functionName: "decimals" }),
  ]);
  return { ok: true, token, symbol: String(symbol), decimals: Number(decimals) };
}

async function readTokenBalance(options) {
  const token = requiredOption(options, "token");
  const owner = requiredOption(options, "owner");
  const publicClient = basePublicClient(options.chain);
  const balance = await publicClient.readContract({
    address: token,
    abi: erc20Abi,
    functionName: "balanceOf",
    args: [owner],
  });
  return { ok: true, token, owner, balanceAtomic: balance.toString() };
}
```

- [ ] **Step 3: Make `swap-price` decimals-aware**

In `getSwapPrice`, the from-amount is converted with a SINGIT-specific 18-decimals helper:

```js
const fromAmount = singitAmountToAtomic(requiredOption(options, "amount"));
```

Replace with:

```js
const fromAmount = humanTokenAmountToAtomic(
  requiredOption(options, "amount"),
  Number(options.decimals || "18"),
);
```

(`humanTokenAmountToAtomic` is already imported from `./user-token-transfer.mjs`.)

- [ ] **Step 4: Smoke-test against Base mainnet (read-only, no keys needed)**

Run from `cdp-x402-service/`:
```bash
node src/index.mjs token-info --token 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```
Expected: `{"ok":true,...,"symbol":"USDC","decimals":6}`.
```bash
node src/index.mjs token-balance --token 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 --owner 0x3b3e349e6cfee692b69d2c63ce86f7d444667d98
```
Expected: `{"ok":true,...,"balanceAtomic":"<digits>"}`.

- [ ] **Step 5: Commit**

```bash
git add cdp-x402-service/src/index.mjs
git commit -m "Add token-info/token-balance commands and decimals-aware swap-price"
```

---

### Task 2: Python client — read-only token calls and `--decimals` on transfer

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py` (class `UserWalletTokenTransferClient`, ~line 3235)
- Test: `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Write the failing tests**

Next to `test_user_wallet_token_transfer_client_passes_private_key_in_env_only` in `test_gateway_server.py`, using the same fake-runner pattern that test uses (a recorded `runner` returning a `subprocess.CompletedProcess`):

```python
    def test_user_wallet_token_transfer_client_passes_decimals(self):
        recorded = {}

        def runner(command, **kwargs):
            recorded["command"] = command
            return subprocess.CompletedProcess(
                command, 0, stdout='{"ok": true, "transactionHash": "0xabc"}', stderr=""
            )

        client = UserWalletTokenTransferClient(
            Path("cdp-x402-service"), runner=runner
        )
        client.transfer_token(
            private_key="0xkey",
            to_address="0x" + "1" * 40,
            token_address="0x" + "2" * 40,
            amount="1.5",
            decimals=6,
        )
        self.assertIn("--decimals", recorded["command"])
        self.assertEqual(
            recorded["command"][recorded["command"].index("--decimals") + 1], "6"
        )

    def test_user_wallet_token_transfer_client_reads_token_info_and_balance(self):
        recorded = []

        def runner(command, **kwargs):
            recorded.append(command)
            if "token-info" in command:
                body = '{"ok": true, "symbol": "USDC", "decimals": 6}'
            else:
                body = '{"ok": true, "balanceAtomic": "123"}'
            return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

        client = UserWalletTokenTransferClient(
            Path("cdp-x402-service"), runner=runner
        )
        info = client.token_info("0x" + "2" * 40)
        balance = client.token_balance("0x" + "2" * 40, "0x" + "3" * 40)
        self.assertEqual(info["symbol"], "USDC")
        self.assertEqual(info["decimals"], 6)
        self.assertEqual(balance, "123")
        self.assertIn("token-info", recorded[0])
        self.assertIn("token-balance", recorded[1])
```

Note: `transfer_token` raises if the script file does not exist — the existing private-key test shows how this is handled in this suite (it uses the real `cdp-x402-service` dir, which exists in the repo). Mirror whatever that test does.

- [ ] **Step 2: Run to verify failure**

```bash
cd sign402-gateway && python3 -m unittest tests.test_gateway_server -k token_transfer_client -v
```
Expected: FAIL (`unexpected keyword argument 'decimals'`, `no attribute 'token_info'`).

- [ ] **Step 3: Implement**

In `UserWalletTokenTransferClient.transfer_token`, add the parameter and CLI flag:

```python
    def transfer_token(
        self,
        *,
        private_key: str,
        to_address: str,
        token_address: str,
        amount: str,
        chain: str = "base",
        decimals: int = 18,
    ) -> dict[str, Any]:
```
and extend the command list (after `"--chain", str(chain),`):
```python
            "--decimals",
            str(int(decimals)),
```

Add two read-only methods to the same class:

```python
    def _run_read_only(self, args: list[str]) -> dict[str, Any]:
        script = self.service_dir / "src" / "index.mjs"
        if not script.exists():
            raise ValueError(f"CDP x402 service script not found: {script}")
        result = self.runner(
            ["node", str(script), *args],
            cwd=str(self.service_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip() or result.stdout.strip() or "token read failed"
            )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("token read returned an invalid payload")
        return payload

    def token_info(self, token_address: str, *, chain: str = "base") -> dict[str, Any]:
        return self._run_read_only(
            ["token-info", "--token", str(token_address), "--chain", str(chain)]
        )

    def token_balance(
        self, token_address: str, owner: str, *, chain: str = "base"
    ) -> str:
        payload = self._run_read_only(
            [
                "token-balance",
                "--token",
                str(token_address),
                "--owner",
                str(owner),
                "--chain",
                str(chain),
            ]
        )
        return str(payload.get("balanceAtomic") or "0")
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
cd sign402-gateway && python3 -m unittest tests.test_gateway_server -v 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "Add decimals flag and read-only token calls to user transfer client"
```

---

### Task 3: Purchase schema — payment token columns

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Test: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write the failing test**

In the store test class (the one exercising `BankrLlmStore` directly, `setUp` at ~line 900):

```python
    def test_purchase_rows_carry_payment_token_fields(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=1700000600,
            payment_token_address="0x" + "9" * 40,
            payment_token_symbol="WETH",
            payment_token_decimals="18",
        )
        loaded = self.store.get_purchase(purchase["purchaseId"])
        self.assertEqual(loaded["paymentTokenAddress"], "0x" + "9" * 40)
        self.assertEqual(loaded["paymentTokenSymbol"], "WETH")
        self.assertEqual(loaded["paymentTokenDecimals"], "18")

    def test_legacy_purchase_rows_default_to_blank_payment_token(self):
        purchase = self.store.create_purchase(
            telegram_user_id="124",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=1700000600,
        )
        loaded = self.store.get_purchase(purchase["purchaseId"])
        self.assertEqual(loaded["paymentTokenAddress"], "")
        self.assertEqual(loaded["paymentTokenSymbol"], "")
        self.assertEqual(loaded["paymentTokenDecimals"], "")
```

- [ ] **Step 2: Run to verify failure** (`unexpected keyword argument` / `KeyError`).

- [ ] **Step 3: Implement**

1. CREATE TABLE (in `_init_db`): after `baseline_credits_usd TEXT NOT NULL DEFAULT '',` add
   ```sql
                    payment_token_address TEXT NOT NULL DEFAULT '',
                    payment_token_symbol TEXT NOT NULL DEFAULT '',
                    payment_token_decimals TEXT NOT NULL DEFAULT '',
   ```
2. Migration: the `_init_db` PRAGMA block already computes `existing_columns`; extend it:
   ```python
            for column in (
                "baseline_credits_usd",
                "payment_token_address",
                "payment_token_symbol",
                "payment_token_decimals",
            ):
                if column not in existing_columns:
                    db.execute(
                        f"ALTER TABLE bankr_llm_purchases "
                        f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
   ```
   (replace the current single-column `if "baseline_credits_usd" not in existing_columns:` block with this loop).
3. `create_purchase` signature gains `payment_token_address: str = ""`, `payment_token_symbol: str = ""`, `payment_token_decimals: str = ""`; the INSERT adds the three columns and values (`str(...)` each).
4. `_purchase_row_to_dict` fields map: add
   ```python
        "payment_token_address": "paymentTokenAddress",
        "payment_token_symbol": "paymentTokenSymbol",
        "payment_token_decimals": "paymentTokenDecimals",
   ```
5. `TRANSITION_FIELDS`: add the same three camelCase→snake_case entries.

- [ ] **Step 4: Run the full gateway suite, expect PASS**

```bash
cd sign402-gateway && python3 -m unittest discover tests 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway
git commit -m "Store payment token metadata on Bankr LLM purchases"
```

---

### Task 4: Token registry, resolution, and `start(paymentToken)`

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Modify: `sign402-gateway/sign402_gateway/server.py` (llm-key start dispatch, ~line 807)
- Test: `sign402-gateway/tests/test_bankr_llm_purchase.py`, `sign402-gateway/tests/test_gateway_server.py`

- [ ] **Step 1: Write the failing service tests**

In the purchase-flow test class (setUp ~line 1912). First extend `FakeTransferForPurchase` with:

```python
    # in FakeTransferForPurchase.__init__
        self.token_info_result = {"ok": True, "symbol": "CUSTOM", "decimals": 8}
        self.token_info_calls = []
        self.token_balance_result = "999999999999999999999999"
        self.token_balance_calls = []

    def token_info(self, token_address, *, chain="base"):
        self.token_info_calls.append(token_address)
        if isinstance(self.token_info_result, Exception):
            raise self.token_info_result
        return dict(self.token_info_result)

    def token_balance(self, token_address, owner, *, chain="base"):
        self.token_balance_calls.append((token_address, owner))
        return self.token_balance_result
```

Then the tests:

```python
    def test_start_resolves_known_symbol_payment_token(self):
        result = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            payment_token="usdc",
        )
        loaded = self.store.get_purchase(result["purchaseId"])
        self.assertEqual(
            loaded["paymentTokenAddress"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        self.assertEqual(loaded["paymentTokenSymbol"], "USDC")
        self.assertEqual(loaded["paymentTokenDecimals"], "6")
        self.assertEqual(self.transfer.token_info_calls, [])

    def test_start_resolves_contract_address_via_token_info(self):
        custom = "0x" + "a" * 40
        result = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            payment_token=custom,
        )
        loaded = self.store.get_purchase(result["purchaseId"])
        self.assertEqual(loaded["paymentTokenAddress"], custom)
        self.assertEqual(loaded["paymentTokenSymbol"], "CUSTOM")
        self.assertEqual(loaded["paymentTokenDecimals"], "8")
        self.assertEqual(self.transfer.token_info_calls, [custom])

    def test_start_rejects_unknown_symbol_with_supported_list(self):
        with self.assertRaises(BankrLlmError) as raised:
            self.service.start(
                telegram_user_id="123",
                email="user@example.com",
                amount_usd="10",
                payment_token="DOGE",
            )
        self.assertEqual(raised.exception.code, "invalid_payment_token")
        self.assertIn("USDC", raised.exception.user_message)

    def test_start_rejects_unreadable_token_contract(self):
        self.transfer.token_info_result = RuntimeError("no contract")
        with self.assertRaises(BankrLlmError) as raised:
            self.service.start(
                telegram_user_id="123",
                email="user@example.com",
                amount_usd="10",
                payment_token="0x" + "b" * 40,
            )
        self.assertEqual(raised.exception.code, "invalid_payment_token")

    def test_start_without_token_defaults_to_singit(self):
        result = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        loaded = self.store.get_purchase(result["purchaseId"])
        self.assertEqual(
            loaded["paymentTokenAddress"],
            "0x3333333333333333333333333333333333333333",
        )
        self.assertEqual(loaded["paymentTokenSymbol"], "SINGIT")
        self.assertEqual(loaded["paymentTokenDecimals"], "18")
```

- [ ] **Step 2: Run to verify failure** (`unexpected keyword argument 'payment_token'`).

- [ ] **Step 3: Implement in `bankr_llm_purchase.py`**

Module-level constants (near `DEFAULT_SINGIT_TOKEN_ADDRESS`):

```python
BASE_MAINNET_PAYMENT_TOKENS: dict[str, tuple[str, int]] = {
    "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    "USDT": ("0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6),
    "WETH": ("0x4200000000000000000000000000000000000006", 18),
    "CBBTC": ("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8),
}
STABLE_PAYMENT_TOKEN_SYMBOLS = frozenset({"USDC", "USDT"})
```

Service methods:

```python
    def _resolve_payment_token(self, raw: Any) -> tuple[str, str, int]:
        text = str(raw or "").strip()
        if not text or text.upper() == "SINGIT":
            return self.singit_token_address, "SINGIT", 18
        known = BASE_MAINNET_PAYMENT_TOKENS.get(text.upper())
        if known is not None:
            return known[0], text.upper(), known[1]
        if EVM_ADDRESS_RE.fullmatch(text) is None:
            supported = ", ".join(["SINGIT", *sorted(BASE_MAINNET_PAYMENT_TOKENS)])
            raise BankrLlmError(
                "invalid_payment_token",
                f"Unknown payment token. Use one of {supported}, "
                "or pass the token contract address.",
            )
        token_info = getattr(self.transfer_client, "token_info", None)
        if not callable(token_info):
            raise BankrLlmError(
                "invalid_payment_token",
                "Custom token addresses are not supported right now.",
            )
        try:
            metadata = token_info(text)
            symbol = str(metadata.get("symbol") or "").strip() or "TOKEN"
            decimals = int(metadata.get("decimals"))
        except BankrLlmError:
            raise
        except Exception:
            raise BankrLlmError(
                "invalid_payment_token",
                "The token contract could not be read on Base. "
                "Check the address and try again.",
            )
        if not 0 <= decimals <= 36:
            raise BankrLlmError(
                "invalid_payment_token",
                "The token contract reports unsupported decimals.",
            )
        return text, symbol, decimals

    def _payment_token(self, purchase: Mapping[str, Any]) -> tuple[str, str, int]:
        address = str(purchase.get("paymentTokenAddress") or "").strip()
        if not address:
            return self.singit_token_address, "SINGIT", 18
        symbol = str(purchase.get("paymentTokenSymbol") or "").strip() or "TOKEN"
        try:
            decimals = int(str(purchase.get("paymentTokenDecimals") or "18"))
        except ValueError:
            decimals = 18
        return address, symbol, decimals

    def _payment_token_is_stable(self, purchase: Mapping[str, Any]) -> bool:
        _, symbol, _ = self._payment_token(purchase)
        return symbol.upper() in STABLE_PAYMENT_TOKEN_SYMBOLS
```

`start()` gains `payment_token: str = ""`, resolves BEFORE creating the purchase (so junk fails with nothing persisted), and passes fields into `create_purchase`:

```python
    def start(
        self,
        *,
        telegram_user_id: str,
        email: str,
        amount_usd: str,
        payment_token: str = "",
    ) -> dict[str, Any]:
        user_id = self._require_user_id(telegram_user_id)
        email_value = BankrIdentityClient._validate_email(email)
        amount_value = self._validate_amount(amount_usd)
        token_address, token_symbol, token_decimals = self._resolve_payment_token(
            payment_token
        )
        ...
        purchase = self.store.create_purchase(
            telegram_user_id=user_id,
            email=email_value,
            amount_usd=amount_value,
            state=state,
            expires_at=int(self._now()) + self.otp_ttl_seconds,
            payment_token_address=token_address,
            payment_token_symbol=token_symbol,
            payment_token_decimals=str(token_decimals),
        )
```

- [ ] **Step 4: Wire the HTTP payload** — in `server.py` `_handle_agent_llm_purchase`, the `start` branch:

```python
            if operation == "start":
                result = service.start(
                    telegram_user_id=user_id,
                    amount_usd=_read_required_text(payload, "amountUsd"),
                    email=_read_required_text(payload, "email"),
                    payment_token=str(payload.get("paymentToken") or ""),
                )
```

Add a server test next to `test_llm_key_start_dispatches_authenticated_user` asserting `start.assert_called_once_with(..., payment_token="USDC")` when the request body includes `"paymentToken": "USDC"`.

- [ ] **Step 5: Run both suites, expect PASS; commit**

```bash
cd sign402-gateway && python3 -m unittest discover tests 2>&1 | tail -3
git add sign402-gateway && git commit -m "Resolve and persist the payment token at Bankr LLM purchase start"
```

---

### Task 5: Decimals-aware pricing (pricer + quote clients)

**Files:**
- Modify: `sign402-gateway/sign402_gateway/real_rate_pricing.py`
- Modify: `sign402-gateway/sign402_gateway/server.py` (`CdpWalletClient.quote`, ~line 2736; `BankrSwapClient.quote` and `BankrWalletApiClient.quote` if present — add an ignored `decimals: int = 18` kwarg to keep call sites uniform)
- Test: `sign402-gateway/tests/test_real_rate_pricing.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_price_for_usdc_accepts_per_call_token_and_decimals(self):
        client = FakeQuoteClient(...)  # reuse this file's existing fake
        pricer = RealRateSingitPricer(
            quote_client=client, from_token="0xSINGIT", max_singit="1000000"
        )
        result = pricer.price_for_usdc(
            "10", from_token="0xCUSTOM", decimals=8
        )
        # every quote call used the per-call token
        self.assertTrue(all(c["from_token"] == "0xCUSTOM" for c in client.calls))
        # atomic uses 8 decimals: requiredSingit * 10^8
        self.assertEqual(
            result["requiredSingitAtomic"],
            str(int(Decimal(result["requiredSingit"]) * Decimal(10) ** 8)),
        )
```

Adapt fake/client-call recording to whatever `test_real_rate_pricing.py` already uses (it has a fake quote client; record `from_token` per call there).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement in `real_rate_pricing.py`**

Thread the token through the call chain (no instance mutation — the pricer is shared across requests):

- `price_for_usdc(self, target_usdc, *, from_token: str | None = None, decimals: int | None = None)`;
  at the top: `token = str(from_token or self.from_token)`,
  `token_decimals = int(decimals) if decimals is not None else SINGIT_DECIMALS`.
- `_quote(self, amount, token, token_decimals)` — pass `from_token=token` and `decimals=token_decimals` to `self.quote_client.quote(...)`; cache key becomes `f"{token}:{token_decimals}:{format_decimal(amount)}"`.
- `_quote_or_none` and `_minimize_integer_amount` take and forward the same two args.
- `requiredSingitAtomic` uses `Decimal(10) ** token_decimals`; `"fromToken": token` in the result.

In `server.py`, `CdpWalletClient.quote` gains `decimals: int = 18` and appends `"--decimals", str(int(decimals))` to the `swap-price` args. Other quote clients used by other funding modes just accept and ignore the kwarg (`decimals: int = 18` in the signature) so the pricer can pass it unconditionally.

- [ ] **Step 4: Run gateway suite, expect PASS; commit**

```bash
cd sign402-gateway && python3 -m unittest discover tests 2>&1 | tail -3
git add sign402-gateway && git commit -m "Support per-call token and decimals in real-rate pricing"
```

---

### Task 6: Purchase service uses the payment token end-to-end

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Test: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write the failing tests**

```python
    def approved_purchase_with_token(self, token):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            payment_token=token,
        )
        self.service.accept_terms("123")
        return self.service.verify_otp(telegram_user_id="123", code="123456")

    def test_usdc_purchase_skips_quote_and_buffer_and_pays_exact_amount(self):
        awaiting = self.approved_purchase_with_token("USDC")
        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(self.pricer.calls, [])            # no swap quote
        transfer = self.transfer.calls[0]
        self.assertEqual(transfer["amount"], "10")
        self.assertEqual(
            transfer["token_address"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        self.assertEqual(transfer["decimals"], 6)
        loaded = self.store.get_purchase(result["purchaseId"])
        # exact amount approved: 10 USDC = 10_000_000 atomic, no +5%
        self.assertEqual(loaded["singitAmountAtomic"], "10000000")
        self.assertEqual(
            self.bankr.topups[0]["source_token"],
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )

    def test_custom_token_purchase_quotes_with_token_and_decimals(self):
        custom = "0x" + "a" * 40
        awaiting = self.approved_purchase_with_token(custom)
        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(self.pricer.calls[0]["from_token"], custom)
        self.assertEqual(self.pricer.calls[0]["decimals"], 8)
        self.assertEqual(self.transfer.calls[0]["token_address"], custom)
        self.assertEqual(self.transfer.calls[0]["decimals"], 8)
        self.assertEqual(self.bankr.topups[0]["source_token"], custom)

    def test_custom_token_balance_checked_before_transfer(self):
        custom = "0x" + "a" * 40
        self.transfer.token_balance_result = "1"   # far below required
        awaiting = self.approved_purchase_with_token(custom)
        result = self.service.resume(awaiting["purchaseId"])

        self.assertEqual(result["state"], "FAILED_BEFORE_TRANSFER")
        self.assertEqual(result["errorCode"], "insufficient_token_balance")
        self.assertEqual(self.transfer.calls, [])

    def test_approval_context_names_the_payment_token(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            payment_token="USDC",
        )
        self.service.accept_terms("123")
        self.service.verify_otp(telegram_user_id="123", code="123456")
        rendered = "\n".join(self.approval.calls[0]["context_lines"])
        self.assertIn("USDC", rendered)
        self.assertIn("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", rendered)
```

Also update fakes:
- `FakePricerForPurchase.price_for_usdc(self, amount_usd, *, from_token=None, decimals=None)` records `{"amount_usd": ..., "from_token": from_token, "decimals": decimals}` in `self.calls` (existing tests that assert `self.pricer.calls == ["10"]`-style must be updated to the dict shape — grep for `pricer.calls`).
- `FakeTransferForPurchase.transfer_token` accepts and records `decimals=18`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement in the service**

1. New pricing helper (replaces the duplicated pricing blocks):

```python
    def _payment_pricing(self, purchase: Mapping[str, Any]) -> tuple[int, str]:
        """Return (required_atomic, human_transfer_amount) for the purchase."""
        address, symbol, decimals = self._payment_token(purchase)
        if symbol.upper() in STABLE_PAYMENT_TOKEN_SYMBOLS:
            amount = Decimal(str(purchase["amountUsd"]))
            atomic = int(amount * (Decimal(10) ** decimals))
            if atomic <= 0:
                raise BankrLlmError(
                    "invalid_pricing",
                    "Payment token pricing is unavailable. Please try again.",
                )
            return atomic, format(amount, "f")
        pricing = self.pricer.price_for_usdc(
            purchase["amountUsd"], from_token=address, decimals=decimals
        )
        atomic = self._pricing_atomic(pricing)
        amount = self._pricing_transfer_amount(
            pricing, expected_atomic=atomic, decimals=decimals
        )
        return atomic, amount
```

2. `_pricing_transfer_amount` gains `decimals: int = 18`; replace `Decimal("1000000000000000000")` with `Decimal(10) ** int(decimals)`.

3. `verify_otp` pricing block: replace the `pricing = self.pricer.price_for_usdc(...)` / `singit_atomic = ...` lines with

```python
        quoted_atomic, _ = self._payment_pricing(purchase)
        if self._payment_token_is_stable(purchase):
            approved_max_atomic = str(quoted_atomic)
        else:
            approved_max_atomic = str(
                self._approved_max_singit_atomic(quoted_atomic)
            )
```

4. `_execute_transfer`: replace the fresh-pricing block with

```python
            fresh_atomic, fresh_amount = self._payment_pricing(purchase)
```

replace `token_address=self.singit_token_address` in the transfer call with the purchase token and pass decimals:

```python
            token_address, _, token_decimals = self._payment_token(purchase)
            transfer = self.transfer_client.transfer_token(
                private_key=private_key,
                to_address=self._require_evm_address(purchase["bankrWalletAddress"]),
                token_address=token_address,
                amount=fresh_amount,
                chain="base",
                decimals=token_decimals,
            )
```

and change the balance check call to `self._balance_error(user_id, fresh_atomic, purchase, source_wallet)`.

5. `_balance_error` — new signature `(self, telegram_user_id, required_atomic, purchase, owner_address)`. Keep the existing body for SINGIT; add the generic lane first:

```python
        address, symbol, _ = self._payment_token(purchase)
        if symbol != "SINGIT":
            token_balance = getattr(self.transfer_client, "token_balance", None)
            if not callable(token_balance):
                return None
            try:
                available = int(str(token_balance(address, owner_address)))
            except Exception:
                return (
                    "wallet_balance_unavailable",
                    "Managed wallet balance is unavailable. Try again before transfer.",
                )
            if available < required_atomic:
                return (
                    "insufficient_token_balance",
                    f"Managed wallet does not have enough {symbol} for this purchase.",
                )
            return None
        # existing SINGIT path unchanged below
```

6. `_top_up_with_retries` — source token comes from the purchase, not the service constant. Change `_top_up_from_funded_wallet` to compute `source_token, _, _ = self._payment_token(purchase)` — but honour the env override for SINGIT: `source_token = self.topup_source_token if symbol == "SINGIT" else address`. Pass it into `_top_up_with_retries(api_key=..., amount_usd=..., source_token=source_token)` and use it there instead of `self.topup_source_token`.

7. `_approval_context_lines`: add after the amount line

```python
            f"Payment token: {commitment.get('paymentTokenSymbol', 'SINGIT')} "
            f"{commitment.get('paymentTokenAddress', '')}",
```

and `_commitment(...)` embeds `"paymentTokenAddress"` / `"paymentTokenSymbol"` from `self._payment_token(purchase)`. NOTE: this changes the commitment hash for new purchases only — old pending purchases keep their stored hash; no migration needed. The canonical-commitment test (`test_verify_uses_canonical_commitment_and_safe_approval_context`) must add the two new keys to its expected dict (`"paymentTokenAddress": "0x3333333333333333333333333333333333333333"`, `"paymentTokenSymbol": "SINGIT"`).

8. `_spend_context`: `"singitTokenAddress"` value becomes the purchase's payment token address (keep the key name); compute via `self._payment_token(purchase)`.

- [ ] **Step 4: Run the gateway suite; fix the pinned assertions the fakes change touched** (`pricer.calls` shapes, canonical commitment dict). Expect PASS.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway
git commit -m "Drive Bankr LLM transfer, pricing, and topup from the purchase payment token"
```

---

### Task 7: Bot — `/llm_buy <usd> <email> [token]`

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py` (`_parse_llm_buy_args` ~line 922, `_llm_operation_payload` ~line 942, `_LLM_BUY_USAGE` ~line 83)
- Test: `hermes-plugins/sign402-wallet/tests/` (the test module covering command parsing)

- [ ] **Step 1: Write the failing tests** (mirror this suite's existing parse tests):

```python
    def test_llm_buy_accepts_optional_token_symbol(self):
        payload = _llm_operation_payload("start", "1 user@example.com USDC")
        self.assertEqual(
            payload,
            {"amountUsd": "1", "email": "user@example.com", "paymentToken": "USDC"},
        )

    def test_llm_buy_accepts_token_address(self):
        token = "0x" + "a" * 40
        payload = _llm_operation_payload("start", f"1 user@example.com {token}")
        self.assertEqual(payload["paymentToken"], token)

    def test_llm_buy_without_token_omits_payment_token(self):
        payload = _llm_operation_payload("start", "1 user@example.com")
        self.assertEqual(payload, {"amountUsd": "1", "email": "user@example.com"})

    def test_llm_buy_rejects_four_args(self):
        self.assertIsNone(
            _llm_operation_payload("start", "1 user@example.com USDC extra")
        )
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
_LLM_BUY_USAGE = "Usage: /llm_buy <usd> <email> [token]"


def _parse_llm_buy_args(raw_args: str) -> tuple[str, str, str] | None:
    args = str(raw_args or "").strip().split()
    if len(args) not in {2, 3}:
        return None
    amount_text, email = args[0], args[1]
    token = args[2] if len(args) == 3 else ""
    if token and not re.fullmatch(r"[A-Za-z0-9]{2,12}|0x[a-fA-F0-9]{40}", token):
        return None
    try:
        amount = Decimal(amount_text)
    except (InvalidOperation, ValueError):
        return None
    if (
        not amount.is_finite()
        or amount < Decimal("1")
        or amount > Decimal("1000")
        or amount != amount.quantize(Decimal("0.01"))
        or _EMAIL_RE.fullmatch(email) is None
    ):
        return None
    return amount_text, email, token
```

and in `_llm_operation_payload`:

```python
    if operation == "start":
        parsed = _parse_llm_buy_args(raw_args)
        if parsed is None:
            return None
        amount, email, token = parsed
        payload = {"amountUsd": amount, "email": email}
        if token:
            payload["paymentToken"] = token
        return payload
```

(`execute_llm` in `client.py` already forwards arbitrary payload keys — no client change needed.)

- [ ] **Step 4: Run the plugin suite, expect PASS**

```bash
cd hermes-plugins/sign402-wallet && python3 -m unittest discover tests 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add hermes-plugins/sign402-wallet
git commit -m "Accept an optional payment token in /llm_buy"
```

---

### Task 8: Full verification and deploy notes

- [ ] **Step 1: Run everything**

```bash
cd sign402-gateway && python3 -m unittest discover tests 2>&1 | tail -3
cd ../hermes-plugins/sign402-wallet && python3 -m unittest discover tests 2>&1 | tail -3
```
Expected: OK / OK.

- [ ] **Step 2: Push**

```bash
git push
```

- [ ] **Step 3: Server deploy (user runs)**

```bash
cd ~/apps/sign402 && git pull
sudo systemctl restart sign402-gateway      # migrates the DB on start
systemctl --user restart hermes-gateway     # picks up the new /llm_buy parsing
```

E2E: `/llm_buy 1 <email> USDC` with USDC on the managed wallet — expect the fastest path (no swap, no buffer, instant topup). Then `/llm_buy 1 <email>` (SINGIT legacy) still works.
