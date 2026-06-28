# Bankr x402 → Bitrefill Automatic Reconciliation Design

## Goal

Make a Telegram-triggered Bitrefill purchase finish automatically when Bankr's
`x402 call --raw` response omits the settlement transaction hash, without ever
creating a second x402 charge or a duplicate Bitrefill invoice.

## Confirmed Failure Mode

Bankr successfully settled the SINGIT payment on Base, but the CLI returned
`transactionHash: null`. The gateway therefore moved the quote to
`RECONCILIATION_REQUIRED` and correctly refused to spend treasury USDC. A later
on-chain lookup proved the exact SINGIT transfer. After Bitrefill received USDC,
its first order response briefly reported `created` with `redemption_info: null`
before changing to `delivered` with a redemption code.

## Chosen Approach

Use strict, bounded on-chain transaction discovery as a fallback when Bankr
does not return a hash. The gateway records the Base block immediately before
the x402 call, parses `paymentMade.payTo`, and searches only the resulting block
window for an exact ERC-20 transfer:

- token: configured SINGIT contract;
- sender: configured Bankr payer wallet;
- recipient: the `payTo` address returned by Bankr;
- amount: the quote's exact `maxSingitAtomic` value;
- status: successful transaction receipt.

Fulfillment proceeds only when exactly one transaction satisfies every field.
Zero matches time out into `RECONCILIATION_REQUIRED`; multiple matches are
treated as ambiguous and also require reconciliation. The gateway never picks
the "closest" transaction.

This is preferred over trusting timing alone or bypassing settlement
verification. It preserves the safety gate while removing the manual step seen
in the live test.

## Components

### Bankr x402 client

Before invoking Bankr, capture the latest Base block. Preserve the parsed raw
`paymentMade` object in the returned result. Continue accepting a transaction
hash directly when Bankr provides one.

### SINGIT settlement resolver

If the Bankr result has no transaction hash, poll `eth_getLogs` over the bounded
block window using indexed Transfer topics for the configured payer and
`paymentMade.payTo`. Filter results by the exact atomic amount, then fetch and
validate the candidate receipt. Return the discovered transaction hash as the
normal settlement proof.

The existing verifier will also be tightened so a supplied transaction hash
must contain the exact token, sender, recipient, and amount—not merely any
large-enough SINGIT transfer in the receipt.

### Bitrefill delivery polling

After the invoice is complete and an order ID exists, poll the order until the
delivery is actually usable. Gift cards and eSIM products require non-null
`redemption_info`; other product types may complete with a terminal delivered
status. The stored provider result must use the refreshed order, so Telegram
receives the code rather than `null`.

If delivery remains pending after the bounded poll, persist the invoice and
order IDs and return a pending/reconciliation state. A retry refreshes that
existing order; it must not create another invoice or transfer USDC again.

### Bankr treasury transaction parsing

Recognize both a BaseScan transaction URL and the CLI's `Tx Hash: 0x…` output.
Persist the USDC transaction hash and verify its receipt before reporting the
treasury payment as confirmed.

## State and Idempotency

The commerce record stores these checkpoints separately:

1. x402 invocation start block and Bankr `paymentMade` metadata;
2. verified SINGIT settlement proof;
3. Bitrefill invoice ID before treasury transfer;
4. verified USDC treasury transaction;
5. Bitrefill order ID and delivery/redemption result.

Every retry begins by reading these checkpoints. Existing successful stages
are resumed, never repeated. `DELIVERED` remains terminal.

## Error Handling

- Base RPC errors retry within a bounded timeout and then leave the order in
  `RECONCILIATION_REQUIRED` without spending USDC.
- Missing payer, `payTo`, start block, or exact transfer match blocks
  fulfillment.
- An ambiguous on-chain match blocks fulfillment.
- A Bitrefill invoice above the configured USDC cap remains rejected before the
  treasury transfer.
- A paid but not-yet-delivered Bitrefill order is saved as pending and refreshed
  by order ID; no new invoice is created.
- Redemption values remain excluded from general logs and are returned only by
  the existing authorized order-retrieval path.

## Configuration

- `SIGN402_BANKR_WALLET_ADDRESS`: expected SINGIT payer on Base.
- `SIGN402_BASE_RPC_URL`: Base mainnet JSON-RPC endpoint.
- Existing SINGIT token, Bitrefill live cap, treasury refund address, and Bankr
  CLI settings remain in force.

Startup must fail in live mode when the Bankr payer address is missing or
invalid.

## Verification

- Unit test: parse `Tx Hash: 0x…` from Bankr treasury output.
- Unit test: preserve `paymentMade.payTo` and x402 start block.
- Unit test: discover one exact SINGIT transfer when the Bankr hash is missing.
- Unit test: reject wrong sender, recipient, token, amount, failed receipt, and
  ambiguous matches.
- Unit test: prefer a directly supplied transaction hash without log discovery.
- Unit test: wait through `created`/null redemption until `delivered` with code.
- Unit test: a pending paid order is resumed without a second invoice or USDC
  transfer.
- Full Python gateway and Node Bankr endpoint suites must pass.
- Final dry run must prove that a missing Bankr hash no longer causes manual
  reconciliation. A new live purchase still requires a fresh, exact user
  confirmation.

