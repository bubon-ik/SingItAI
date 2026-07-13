# Bitrefill Wallet Token Selection Design

## Goal

Require a Telegram user to choose the Base-wallet asset used for every
Bitrefill purchase. Reuse the token inventory already exposed by the wallet
balance and withdrawal flows. Do not silently default to SINGIT.

The selected asset must be priced, balance-checked, committed into the payment
approval, shown in WhatsApp, and used for the actual transfer and swap.

Every balance, token choice, signature, and debit belongs to the managed Base
wallet created for the authenticated Telegram user. A shared Hermes, Bankr, or
CDP wallet must never be used as the source wallet for a per-user purchase; CDP
may only receive the already approved user funds and perform settlement steps.

## User Experience

The existing Telegram Bitrefill wizard remains unchanged through product,
package, and recipient selection. Immediately before creating a quote, it adds
a `select-payment-token` stage:

1. The plugin requests the authenticated user's existing wallet token list.
2. The bot displays positive-balance assets and numbered Telegram reply
   buttons. Each line includes symbol and balance. Unverified ERC-20 assets also
   include a shortened contract address so duplicate or misleading symbols are
   distinguishable.
3. The user selects an asset with a button.
4. The gateway validates the selected asset against a fresh wallet token list,
   requests a token-to-USDC price route, and verifies that the wallet balance
   covers the quoted maximum.
5. Only after those checks succeed does the gateway persist the quote and send
   the WhatsApp approval request.

If the wallet has no spendable assets, or balance lookup is unavailable, the
bot stops before quote creation and explains the problem. If the selected asset
has insufficient balance or no usable liquidity route, the bot returns to the
token-selection stage so another asset can be chosen.

The direct command becomes:

```text
/bitrefill <productId> <packageId> <country> <token>
```

`token` is required. It may be a unique wallet symbol or a contract address.
Ambiguous symbols are rejected and the user is directed to the button flow.
The old three-argument form must show usage and must not create a quote.

## Token Inventory

The implementation reuses the existing authenticated wallet-token inventory
used by `Balance` and the withdrawal wizard; it does not introduce a second
balance source. Candidate assets must have a positive current balance.

Each candidate is represented by:

- `symbol`;
- `contractAddress` or the existing native-ETH asset identifier;
- `decimals`;
- `balance`;
- `verified`;
- `native`.

The Telegram session stores the complete selected token identity rather than
only its symbol. The gateway independently resolves and validates that identity
for the authenticated Telegram user, so plugin session data is not trusted as
authorization.

Both verified and unverified positive-balance ERC-20 assets may be displayed.
Unverified assets are identified by contract address and remain unusable unless
the pricing provider can quote them. Native ETH is displayed and supported by
the existing user-wallet native-transfer command; its funding path must reserve
enough ETH for gas before declaring the balance sufficient.

The wallet inventory keeps its existing `native` identity for ETH. At the CDP
pricing and swap boundary, the gateway maps it to Coinbase's native-token
sentinel `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE`.

## Gateway Contract

`POST /agent/quote-bitrefill` gains a required `paymentToken` object for
authenticated per-user purchases:

```json
{
  "productId": "bitrefill-giftcard-usd",
  "packageId": "0.1",
  "country": "US",
  "recipient": {},
  "telegramUserId": "1045618308",
  "paymentToken": {
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "symbol": "USDC",
    "decimals": 6,
    "native": false
  }
}
```

The authenticated user ID is authoritative. Before pricing, the gateway
reloads that user's token inventory and requires an exact address/native-asset
match. Symbol and decimals come from the server-side inventory, not the client
payload.

The persisted quote adds:

- `paymentTokenAddress`;
- `paymentTokenSymbol`;
- `paymentTokenDecimals`;
- `paymentTokenNative`;
- `paymentTokenAmount`;
- `maxPaymentTokenAtomic`;
- the existing USDC target, slippage/buffer, expiry, product, and recipient
  commitment fields.

New per-user quotes do not use `singitAmount` or `maxSingitAtomic` as generic
aliases. Legacy stored quotes retain their existing interpretation, but new
purchase commitments use the token-neutral fields.

## Pricing and Funding

The existing real-rate pricer already accepts a source token and decimals. Its
Bitrefill-facing result becomes token-neutral while retaining a compatibility
adapter for legacy SINGIT callers:

- source asset: the selected token address or native ETH identifier;
- target asset: USDC;
- target amount: Bitrefill USD price plus the configured buffer;
- result: exact maximum source-token amount and atomic amount.

Stablecoins still go through the same quote boundary so the committed maximum
and provider result are explicit. An implementation may use a direct 1:1 route
for a supported USDC source, but it must still return the same normalized quote
shape.

The user funding runner receives the selected token from the persisted quote.
It must never read `SIGN402_BANKR_SWAP_FROM_TOKEN` to decide a new per-user
purchase's source asset. ERC-20 assets use the existing signed token-transfer
path; native ETH uses the existing signed native-transfer path. The CDP/Bankr
swap then converts the received source asset to the USDC required by Bitrefill.

Immediately before transfer, the runner rechecks the user wallet balance
against `maxPaymentTokenAtomic`. Any mismatch, balance drop, or route failure
fails closed before fulfillment.

## Approval and WhatsApp

The payment commitment hash covers the selected token address, symbol,
decimals, native flag, maximum atomic amount, product, price, recipient
commitment, and expiry. A token substitution after approval therefore changes
the hash and cannot execute under the prior approval.

The WhatsApp template parameters describe:

- Bitrefill product and face value;
- USD price;
- selected payment token and maximum amount;
- abbreviated source wallet;
- request reference and expiry.

The existing approved WhatsApp template remains in use; these details are
packed into its first free-text parameter. No token address, amount, or product
may be changed after `Confirm`.

## Error Handling

- Missing token selection: no quote, no approval, no transfer.
- Token removed from wallet inventory: reject and reload token buttons.
- Duplicate symbol: require button selection or contract address.
- No liquidity/price route: reject before WhatsApp and offer another token.
- Insufficient token balance: reject before WhatsApp and offer another token.
- Insufficient native ETH for gas: reject before WhatsApp with a gas message.
- Quote expires while awaiting WhatsApp: do not execute; return the user to the
  amount/token selection flow.
- Balance changes after approval: fail closed and create no Bitrefill order.

Errors shown to Telegram must not include private keys, bearer tokens, provider
responses containing secrets, or full internal stack traces.

## Compatibility and Scope

- The existing `Balance` button, wallet creation, withdrawal flow, iMessage
  channel, and WhatsApp pairing remain unchanged.
- Existing stored Bitrefill quotes and reconciliation records remain readable.
- All newly created authenticated wallet Bitrefill purchases require explicit
  token selection.
- Operator-only legacy purchase endpoints keep their current SINGIT settlement
  contract and are outside this change.
- Automatic token choice, cross-chain assets, and tokens not present in the
  managed Base wallet are out of scope.

## Testing

Gateway tests cover token inventory validation, arbitrary ERC-20 pricing,
native ETH pricing and funding, balance checks, token-neutral quote fields,
commitment hashing, stale-token rejection, insufficient balance, no-liquidity
failure, and legacy quote compatibility.

Plugin tests cover the new token-selection stage, positive-balance buttons,
unverified-token address labels, selection persistence, retry after a safe
pricing error, mandatory fourth direct-command argument, and forwarding the
selected token to the quote request.

Integration tests verify that the token shown in WhatsApp is the token committed
in the quote and used by the funding runner. The complete gateway and plugin
test suites must pass before deployment.

## Completion Criteria

A Telegram user can select a Bitrefill product and amount, choose one of the
positive-balance assets already visible for their Base wallet, approve the exact
token spend in WhatsApp, and complete the purchase using that selected asset.
No Bitrefill purchase can begin without an explicit token choice, and no other
token can be substituted after approval.
