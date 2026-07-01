# Alchemy Base Wallet Balances Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show exact Base Mainnet ETH, USDC, SINGIT, and bounded unverified ERC-20 balances through the existing Telegram `/balance` command.

**Architecture:** Add a standard-library JSON-RPC client and hybrid Alchemy balance provider. Standard Base methods fetch trusted balances and verify chain ID; optional Alchemy Token API calls discover other ERC-20 assets. Wire the provider through the existing wallet-service environment factory without changing wallet creation or spending state.

**Tech Stack:** Python 3.11+, `urllib`, JSON-RPC 2.0, `decimal.Decimal`, `unittest`.

---

## File Structure

- Create `sign402-gateway/sign402_gateway/base_balances.py`
  - JSON-RPC transport, trusted Base balance calls, Alchemy discovery,
    validation, and exact formatting.
- Create `sign402-gateway/tests/test_base_balances.py`
  - Focused provider and transport tests with fake HTTP responses.
- Modify `sign402-gateway/sign402_gateway/user_wallets.py`
  - Environment wiring and structured balance response rendering.
- Modify `sign402-gateway/tests/test_user_wallets.py`
  - Factory and Telegram response integration tests.
- Modify `sign402-gateway/README.md`
  - Private Alchemy endpoint deployment instructions.

### Task 1: JSON-RPC Transport And Exact Formatting

**Files:**
- Create: `sign402-gateway/tests/test_base_balances.py`
- Create: `sign402-gateway/sign402_gateway/base_balances.py`

- [ ] **Step 1: Write failing tests**

Add tests for:

```python
format_atomic_amount(0, 18) == "0"
format_atomic_amount(1, 18) == "0.000000000000000001"
format_atomic_amount(1_250_000, 6) == "1.25"
```

Use a recording opener to test `JsonRpcClient.call()` and
`JsonRpcClient.batch()`. Verify timeout, response-size cap, malformed JSON,
JSON-RPC errors, HTTP failures, missing batch IDs, and that errors never
contain the endpoint URL or upstream body.

- [ ] **Step 2: Verify RED**

```bash
cd sign402-gateway
python3 -m unittest tests/test_base_balances.py -v
```

Expected: import failure because `base_balances.py` does not exist.

- [ ] **Step 3: Implement transport and formatting**

Create:

```python
class BaseBalanceError(RuntimeError):
    pass


class JsonRpcClient:
    def call(self, method: str, params: list) -> object:
        ...

    def batch(
        self,
        calls: list[tuple[str, list]],
        *,
        allow_errors: bool = False,
    ) -> list[object | None]:
        ...


def format_atomic_amount(value: int, decimals: int) -> str:
    ...
```

Use fixed safe exception messages, five-second timeout, 256 KiB maximum
response, response closing, and request IDs local to the client.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 test command and expect all transport tests to pass.

### Task 2: Trusted Base Balances

**Files:**
- Modify: `sign402-gateway/tests/test_base_balances.py`
- Modify: `sign402-gateway/sign402_gateway/base_balances.py`

- [ ] **Step 1: Write failing trusted-balance tests**

Verify that `AlchemyBaseBalanceProvider` sends:

```text
eth_chainId
eth_getBalance
eth_call USDC balanceOf(address)
eth_call SINGIT balanceOf(address)
```

Assert Base chain ID `0x2105`, exact formatted values, canonical token
addresses, valid `balanceOf` calldata, wallet-address validation, and
wrong-chain rejection.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m unittest tests/test_base_balances.py -v
```

Expected: provider tests fail because `AlchemyBaseBalanceProvider` is
missing.

- [ ] **Step 3: Implement trusted balances**

Add:

```python
class AlchemyBaseBalanceProvider:
    def __call__(self, wallet_address: str) -> dict:
        return {
            "balances": {
                "ETH": ...,
                "USDC": ...,
                "SINGIT": ...,
            },
            "unverifiedTokens": [],
        }
```

Use one strict JSON-RPC batch, parse only valid uint256 hex values, and
fail closed when chain ID is not 8453.

- [ ] **Step 4: Verify GREEN**

Run the focused tests and expect trusted-balance tests to pass.

### Task 3: Bounded Alchemy ERC-20 Discovery

**Files:**
- Modify: `sign402-gateway/tests/test_base_balances.py`
- Modify: `sign402-gateway/sign402_gateway/base_balances.py`

- [ ] **Step 1: Write failing discovery tests**

Test:

- `alchemy_getTokenBalances(address, "erc20")`;
- non-zero filtering;
- valid contract-address filtering;
- canonical USDC/SINGIT deduplication;
- deterministic contract sorting;
- maximum 100 input rows and 10 metadata requests;
- batched `alchemy_getTokenMetadata`;
- symbol sanitization and decimals range 0 through 36;
- duplicate symbols retaining distinct contract addresses;
- discovery and metadata errors returning trusted balances.

- [ ] **Step 2: Verify RED**

Run the focused suite and confirm discovery assertions fail.

- [ ] **Step 3: Implement discovery**

After trusted balances succeed:

```python
try:
    discovery = rpc.call(
        "alchemy_getTokenBalances",
        [wallet_address, "erc20"],
    )
    unverified = _normalize_discovered_tokens(discovery)
except BaseBalanceError:
    unverified = []
```

Batch metadata requests with `allow_errors=True`, omit invalid rows, and
always include the contract address in unverified-token output.

- [ ] **Step 4: Verify GREEN**

Run the focused suite and expect all provider tests to pass.

### Task 4: Wallet Service Integration

**Files:**
- Modify: `sign402-gateway/tests/test_user_wallets.py`
- Modify: `sign402-gateway/sign402_gateway/user_wallets.py`

- [ ] **Step 1: Write failing integration tests**

Add tests that:

- `build_wallet_service_from_env` leaves the provider disabled without
  `SIGN402_BASE_RPC_URL`;
- it builds `AlchemyBaseBalanceProvider` when the URL exists;
- it passes the existing `SIGN402_SINGIT_TOKEN_ADDRESS` override;
- structured provider responses expose `balances` and
  `unverifiedTokens`;
- Telegram text orders ETH, USDC, SINGIT first;
- unverified tokens include a shortened contract and warning;
- legacy simple-dictionary providers still work.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests/test_user_wallets.py -v
```

Expected: new environment and structured-response assertions fail.

- [ ] **Step 3: Wire the provider and rendering**

Add:

```python
def build_base_balance_provider_from_env(env: Mapping[str, str]):
    rpc_url = str(env.get("SIGN402_BASE_RPC_URL", "")).strip()
    if not rpc_url:
        return None
    return AlchemyBaseBalanceProvider(
        rpc_url=rpc_url,
        singit_token_address=env.get(
            "SIGN402_SINGIT_TOKEN_ADDRESS",
            DEFAULT_SINGIT_TOKEN_ADDRESS,
        ),
    )
```

Normalize structured and legacy provider results in `wallet_balance`.
Render trusted balances first and unverified tokens in a separate section.

- [ ] **Step 4: Verify GREEN**

Run user-wallet tests, then the complete focused balance suites.

### Task 5: Documentation And Full Verification

**Files:**
- Modify: `sign402-gateway/README.md`

- [ ] **Step 1: Document server configuration**

Document `SIGN402_BASE_RPC_URL` as a secret Alchemy Base Mainnet HTTPS
endpoint. State that unknown tokens are display-only and that public Base
RPC is not used for hosted balances.

- [ ] **Step 2: Run fresh verification**

```bash
cd sign402-gateway
python3 -m unittest tests/test_base_balances.py tests/test_user_wallets.py -v
python3 -m unittest discover -s tests -v
```

Expected: all focused and regression tests pass.

- [ ] **Step 3: Check formatting and secrets**

```bash
git diff --check
rg -n 'base-mainnet\\.g\\.alchemy\\.com/v2/[A-Za-z0-9_-]{12,}' \
  sign402-gateway docs
```

Expected: no whitespace errors and no concrete Alchemy endpoint.

- [ ] **Step 4: Commit**

```bash
git add \
  docs/superpowers/specs/2026-07-01-alchemy-base-wallet-balances-design.md \
  docs/superpowers/plans/2026-07-01-alchemy-base-wallet-balances.md \
  sign402-gateway/sign402_gateway/base_balances.py \
  sign402-gateway/sign402_gateway/user_wallets.py \
  sign402-gateway/tests/test_base_balances.py \
  sign402-gateway/tests/test_user_wallets.py \
  sign402-gateway/README.md
git commit -m "Add Alchemy Base wallet balances"
```
