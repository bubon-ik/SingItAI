# Alchemy Base Wallet Balances Design

Date: 2026-07-01

## Goal

Make the managed wallet `/balance` command show the user's Base Mainnet
ETH and ERC-20 balances. USDC and SINGIT remain first-class trusted assets,
while Alchemy Token API discovery lets users see other ERC-20 tokens sent
to their managed wallet.

This is a read-only feature. Discovering or displaying a token never grants
the agent permission to transfer, swap, approve, or otherwise spend it.

## Scope

This slice includes:

- Base Mainnet chain verification.
- Native ETH balance lookup.
- Explicit USDC and SINGIT balance lookup.
- Discovery of additional non-zero ERC-20 balances through Alchemy.
- Token metadata lookup for a bounded number of discovered tokens.
- Exact decimal formatting without floating-point arithmetic.
- Safe fallback to the existing balance-unavailable response.
- Unit, integration, and regression tests.

This slice does not include:

- USD pricing or portfolio valuation.
- NFTs or ERC-1155 balances.
- Token transfers, swaps, approvals, or signing.
- Spend policies.
- iMessage approval.
- Cross-chain balances.
- A public RPC proxy.

## Decision

Use a hybrid provider:

1. Standard Ethereum JSON-RPC methods provide chain identity, ETH, USDC,
   and SINGIT balances.
2. Alchemy's `alchemy_getTokenBalances` discovers other ERC-20 balances.
3. Alchemy's `alchemy_getTokenMetadata` supplies symbol and decimals for a
   bounded set of non-zero discovered tokens.

This keeps the trusted asset path portable to any Base RPC provider while
using Alchemy's enhanced API only for optional token discovery. If enhanced
Alchemy methods are unavailable, ETH, USDC, and SINGIT still work.

`web3.py` is not added. The provider needs a small JSON-RPC surface and can
use the Python standard library.

## Components

Add:

```text
sign402-gateway/sign402_gateway/base_balances.py
sign402-gateway/tests/test_base_balances.py
```

Modify:

```text
sign402-gateway/sign402_gateway/user_wallets.py
sign402-gateway/tests/test_user_wallets.py
sign402-gateway/README.md
```

`base_balances.py` owns JSON-RPC transport, response validation, ERC-20
encoding, metadata validation, and human-readable amount formatting.

`user_wallets.py` remains responsible for wallet lookup, safe response
shaping, and audit events. Its environment factory wires the balance
provider only when `SIGN402_BASE_RPC_URL` is explicitly configured.

## Configuration

The Sign402 Gateway process receives:

```text
SIGN402_BASE_RPC_URL=<Alchemy Base Mainnet HTTPS endpoint>
```

The URL contains an Alchemy API key and is treated as a secret:

- It is not stored in the repository.
- It is not returned by any API.
- It is not written to Telegram.
- It is not included in logs or exception messages.
- It belongs in the `sign402-gateway` service environment, not in prompts
  or Hermes skills.

The provider is disabled when the variable is absent. It does not silently
use the public Base RPC for hosted user balances.

## Trusted Assets

The provider always queries these assets:

| Asset | Contract | Decimals |
| --- | --- | --- |
| ETH | Native | 18 |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913` | 6 |
| SINGIT | `0xc2c1e0b7C401e6217193732272444D928646eba3` | 18 |

The SINGIT address may follow the existing
`SIGN402_SINGIT_TOKEN_ADDRESS` override. USDC remains the canonical Base
Mainnet USDC contract.

Trusted asset balances are fetched with:

- `eth_getBalance` for ETH.
- `eth_call` using ERC-20 `balanceOf(address)` for USDC and SINGIT.

The provider checks `eth_chainId` and accepts only Base Mainnet chain ID
`8453` (`0x2105`). A wrong-network endpoint fails closed.

## Dynamic ERC-20 Discovery

After trusted balances succeed, the provider calls:

```json
{
  "method": "alchemy_getTokenBalances",
  "params": ["0xUSER", "erc20"]
}
```

The response is bounded before parsing. The provider:

- accepts only valid 20-byte contract addresses;
- accepts only non-negative hex balances;
- removes zero balances;
- skips canonical USDC and SINGIT duplicates;
- processes at most 100 returned balance rows;
- fetches metadata for at most 10 additional tokens;
- ignores additional rows rather than making the Telegram response
  unbounded.

Metadata requests use `alchemy_getTokenMetadata`. The provider accepts:

- a sanitized symbol of at most 16 ASCII letters, digits, `.`, `_`, or
  `-`;
- integer decimals from 0 through 36.

Invalid metadata causes that discovered token to be omitted. Duplicate
symbols include a shortened contract address in the display label.

Dynamic tokens are labeled as unverified in the Telegram response. The
contract address is included so identical or misleading symbols cannot
hide token identity.

Failure of Alchemy discovery or metadata does not hide trusted balances.
It only omits the optional unverified-token section.

## Transport

Use HTTPS JSON-RPC POST requests with:

- five-second request timeout;
- bounded response reads;
- JSON object or batch validation;
- monotonically assigned request IDs;
- fixed method names and parameter shapes;
- no retries inside the HTTP request path.

The trusted balance methods may be sent as one JSON-RPC batch. Metadata for
the bounded dynamic-token set may also use a batch, keeping `/balance` to a
small fixed number of HTTP round trips.

Transport errors expose only a typed error category. Logs may include the
RPC method and HTTP status category, but never the endpoint URL, response
body, wallet API token, or Alchemy API key.

## Amount Formatting

All atomic values are parsed as Python integers and scaled with
`decimal.Decimal`.

Formatting rules:

- zero is `0`;
- no scientific notation;
- no thousands separators;
- trailing fractional zeroes are removed;
- values smaller than one preserve meaningful precision up to the token's
  declared decimals.

No balance passes through `float`.

## Response Shape

The existing wallet service contract remains compatible:

```json
{
  "ok": true,
  "wallet": {
    "chain": "base",
    "address": "0x...",
    "status": "created",
    "spendingEnabled": false
  },
  "balanceUnavailable": false,
  "balances": {
    "ETH": "0.001",
    "USDC": "12.5",
    "SINGIT": "250"
  },
  "unverifiedTokens": [
    {
      "symbol": "TOKEN",
      "contractAddress": "0x...",
      "balance": "3"
    }
  ],
  "telegramText": "..."
}
```

ETH, USDC, and SINGIT appear first, including zero balances. The Telegram
text then shows at most 10 unverified ERC-20 tokens with shortened contract
addresses and a clear unverified label.

The wallet service accepts either the new structured provider response or
the existing simple balance dictionary used by tests and older callers.

## Error Handling

- Missing RPC URL: keep `balanceUnavailable: true`.
- Wrong chain ID: keep `balanceUnavailable: true`.
- Trusted balance RPC failure: keep `balanceUnavailable: true`.
- Invalid trusted response: keep `balanceUnavailable: true`.
- Alchemy discovery failure: return trusted balances only.
- Individual dynamic metadata failure: omit that token.
- Oversized response: reject that RPC call.
- More than 10 discovered non-zero tokens: show the first deterministic 10
  after canonical address sorting.

No error path changes wallet state or spending state.

## Testing

`test_base_balances.py` covers:

- Base chain verification.
- ETH, USDC, and SINGIT request construction.
- ERC-20 `balanceOf` calldata.
- Exact decimal formatting.
- Alchemy dynamic discovery.
- Zero and malformed token filtering.
- Metadata sanitization and decimal validation.
- Duplicate symbols.
- 10-token display cap.
- Discovery fallback while trusted balances succeed.
- Wrong-chain, HTTP, malformed JSON, JSON-RPC error, timeout, and oversized
  response failures.
- Secret URL and response-body redaction.

`test_user_wallets.py` covers:

- Environment factory wiring when RPC is configured.
- Disabled provider when RPC is absent.
- Structured provider response shaping.
- Trusted assets before unverified tokens in Telegram text.
- Existing balance-provider compatibility.

Run the complete Sign402 Gateway test suite after focused tests.

## Deployment

1. Push the implementation to `x402Bnkr`.
2. Pull it on the VPS.
3. Add the private Alchemy Base Mainnet URL as
   `SIGN402_BASE_RPC_URL` in the `sign402-gateway` service environment.
4. Restart `sign402-gateway`.
5. Verify its health endpoint.
6. Send `/balance` from the allowlisted Telegram account.
7. Confirm ETH, USDC, and SINGIT appear, with optional unverified tokens
   clearly separated.

The Alchemy URL must not be pasted into chat, screenshots, commits, or
shared logs.

## Success Criteria

- `/balance` shows exact Base Mainnet ETH, USDC, and SINGIT balances.
- Other non-zero ERC-20 balances can appear as unverified tokens.
- Unknown tokens do not gain spending permission.
- A wrong-chain endpoint fails closed.
- Alchemy discovery failure does not hide trusted balances.
- No secret RPC URL or upstream response body reaches Telegram or logs.
- Existing wallet creation and status behavior remains unchanged.
