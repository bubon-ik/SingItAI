# Phase 0 — the five checks

Every entry: the command, the output, and one line of conclusion. Parts 5 and 6
read this file. Nothing here is remembered, guessed, or reconstructed later —
it is written as each check runs.

Machine: darwin arm64, node v24.12.0, npm 11.6.2, Python 3.14.5.
`@ledgerhq/wallet-cli` v2.1.0.

---

## L1 — ring init → encrypt → decrypt, then unplug and decrypt again

**Status: PASS.** Run on the real device, 5 September.

```
$ wallet-cli ring init
✔ Member credentials created
✔ Ledger Key Ring ready

Member:  mymac.local (darwin)
Root ID: 001a903e…                      (truncated: this identifies the trustchain)
Encrypt/decrypt with: wallet-cli ring encrypt --key <name>
```

With the device connected:

```
$ printf 'phase-0-l1-canary' | wallet-cli ring encrypt --key l1-test > l1.enc \
    && wallet-cli ring decrypt --key l1-test < l1.enc
✔ Key retrieved
✔ Encrypted (45 bytes, AES-256-GCM)
✔ Key retrieved
✔ Decrypted
phase-0-l1-canary
```

Then the device was **physically disconnected**, and the same decrypt run again:

```
$ wallet-cli ring decrypt --key l1-test < l1.enc
✔ Key retrieved
✔ Decrypted
phase-0-l1-canary
```

**Conclusion: decrypt works with no device attached.** This is what makes §4
possible at all — the gateway runs on a VPS with no USB port, and it is Ledger's
own stated model, "one device tap to set up, then none", confirmed rather than
assumed.

It is also exactly the property the threat model in `keyring.py` has to state
plainly rather than talk around: an attacker who already holds a live,
compromised host holds the ring credentials and `WALLET_PASS` too, and will
decrypt. What this buys is that a stolen disk, a leaked backup or a copied
`/etc` is AES-256-GCM ciphertext instead of a key. That is a real and worthwhile
gain, and it is not the same thing as protecting a host that is already lost.

Note that 17 bytes of plaintext became 45 bytes of ciphertext — a 28-byte
overhead, consistent with a 12-byte GCM nonce and a 16-byte tag.

### L1b — and with the network off as well

The documented command table says `ring decrypt` requires network. It does not.
With Wi-Fi disabled **and** the device unplugged:

```
$ wallet-cli ring decrypt --key l1-test < l1.enc; echo "exit=$?"
✔ Key retrieved
✔ Decrypted
phase-0-l1-canary
exit=0
```

Two consequences, and they pull in opposite directions, so both get written
down.

**Operationally this is the good outcome.** Boot does not depend on Ledger's
LKRP service being up. Had it gone the other way, the gateway would refuse to
start whenever someone else's API had a bad afternoon, and a key-management
change would have quietly introduced a third-party dependency into the start-up
path of a payment system. That is worth knowing before shipping rather than
after.

**For the threat model it sharpens the claim, and downwards.** After `ring
init`, the scoped key is derivable on this host from the local member
credentials and `WALLET_PASS` alone — no device, no network, no Ledger. So the
device is the *enrolment* root, not a per-use gate, and what actually stands
between the encrypted file and plaintext on a running host is `WALLET_PASS` and
the credential store next to it.

That is still a real gain over a key in plaintext in `/etc`: a stolen disk, a
leaked backup, or a copied `/etc` without the passphrase is AES-256-GCM
ciphertext. It is not the stronger claim it would be easy to imply, and the
README says so in those words.

---

## L2 — does `wallet-cli` sign messages?

```
$ wallet-cli --help
wallet-cli v2.1.0
Ledger Wallet CLI

Commands:
  account        Account management commands
  assets         Crypto-assets store queries (resolve tokens by address or id)
  balances       Fetch native and token balances for an account (no device required)
  earn           Earn (staking & DeFi yield) commands
  genuine-check  Check whether the connected Ledger device is genuine
  operations     List operations for an account (no device required)
  receive        Get receive address for an account (optionally verify on device)
  ring           Ledger Key Ring — trustless, hardware-rooted encryption for files and text (LKRP)
  send           Sign and broadcast a transaction
  session        Session management commands
  skill          Ledger wallet-cli agent skills (list, retrieve, install, doctor)
  swap           Swap-related commands
```

Every subcommand's help was walked and grepped for `sign`. The only hits are
`send` — "Sign and broadcast a transaction". There is no `sign`, no
`sign-message`, no `personal-sign`, and no `--message` flag anywhere in the
tree. `send` signs a *transaction* and broadcasts it; it cannot produce a
detached signature over arbitrary typed data, and it moves money, which is
disqualifying on its own for an approval primitive.

**Conclusion: NO. `wallet-cli` cannot sign an EIP-712 message.** Part 3 (§5) must
go through the Device Management Kit — the day-instead-of-half-day branch. Its
cut line in §5 therefore applies from the start, not as a surprise late on.

### Side finding, useful for §8

`wallet-cli skill list | retrieve | install | doctor` ships agent SKILLs
embedded in the binary. That is a first-party example of the SKILL.md format
§8 has to match, and it comes from the same sponsor.

---

## G1 — The Graph's x402 endpoint, unpaid, verbatim

```
$ curl -s -i https://gateway.thegraph.com/api/x402/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV
HTTP/2 402
content-length: 0
payment-required: eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQYXltZW50LVNpZ25hdHVyZSBoZWFkZXIgaXMgcmVxdWlyZWQi…
allow: POST
```

Base64-decoded value of the `payment-required` header, verbatim:

```json
{
    "x402Version": 2,
    "error": "Payment-Signature header is required",
    "resource": {
        "url": "http://mainnet-thegraph-arbitrum-02-eu-west3.thegraph.com/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
    },
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "10000",
            "payTo": "0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB",
            "maxTimeoutSeconds": 300,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "extra": {
                "assetTransferMethod": "eip3009",
                "name": "USD Coin",
                "version": "2"
            }
        }
    ]
}
```

Four things the adapter has to know, and none of them were guessable:

1. **The block is not in the body.** The body is empty — `content-length: 0`.
   The requirements arrive base64-encoded in a `payment-required` **header**.
   An x402 client that parses the response body finds nothing at all.
2. **`x402Version` is 2**, and the requirements are nested under `accepts[]`
   rather than sitting at the top level.
3. **The amount field is `amount`.** Not `maxAmountRequired`, not
   `amountAtomic`. This is the third vocabulary the spec warned about, and G2
   confirms it breaks the existing adapter.
4. `payTo` **is** spelled `payTo`, `asset` is Base USDC
   (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`), `network` is `eip155:8453`.
   Six decimals, so `"10000"` atomic is **$0.01** per query.

`resource.url` points at an internal indexer hostname, not at the gateway URL
that was called. It is not the merchant identity — keying a merchant on it
would split one seller across regions and indexers. The merchant is
`gateway.thegraph.com`, taken from the URL the agent actually requested.

**Conclusion: PASS.** Field names captured exactly. The adapter reads the header,
not the body, and takes `accepts[0]`.

---

## G2 — does the existing adapter survive that block?

```
$ python -c 'from spending_memory.adapters.x402 import to_payment; to_payment(<G1 accepts[0]>, <url>)'
Traceback (most recent call last):
  File "<stdin>", line 12, in <module>
  File "spending_memory/adapters/x402.py", line 87, in to_payment
    raise ValueError(
        "payment requirements are missing maxAmountRequired (or amountAtomic)"
    )
ValueError: payment requirements are missing maxAmountRequired (or amountAtomic)
```

`payTo` was read fine. `amount` was not, because the adapter knows two spellings
and this is a third.

**Conclusion: FAILS, exactly where the spec predicted.** `thegraph.py` adds the
third spelling and the header decoding, and leaves the two existing spellings
untouched — the x402 adapter's tests must stay green without edits.

---

## G3 — is there a subgraph that gives an address's first-seen date and distinct counterparty count on Base?

The Subgraph MCP is live at `https://subgraphs.mcp.thegraph.com/sse` (legacy SSE
transport; the streamable-HTTP paths all 404). `initialize` reports
`subgraph-mcp 0.1.1` and nine tools, of which four matter here:
`search_subgraphs_by_keyword`, `get_top_subgraph_deployments`,
`get_schema_by_ipfs_hash`, `get_deployment_30day_query_counts`.

Two lines of attack were run.

**By keyword.** `base`, `transfers`, `usdc`, `erc20`, `token transfers`,
`holder`, `account`, `address activity`, `base transfers`, `wallet activity`.
The last three return `"returned": 0`. Everything that does return is
protocol-scoped: `uniswap-v4-base-3`, `uniswap-v2-base`, `Uniswap V3 Base`,
`Aerodrome Base Full`, `llens-aggs-base`, `base-derp-holders`.

The one name that looked exactly right, `base-usdc`
(`CpmUGfuDNbovon9Bruo4pXvNer4PAMiMyvX6HGasXJ7u`), is not a transfer index — its
whole schema is:

```graphql
type Wallet @entity { id: ID! vault: Bytes! owner: Bytes! deposited: BigInt! withdrawn: BigInt! }
type Variable @entity { id: ID! value: BigInt! }
```

and `get_deployment_30day_query_counts` reports `total_query_count: 0` for it
and for `base-usdc-optimized`. Dead, and not the thing anyway.

**By contract.** `get_top_subgraph_deployments(chain="base",
contract_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")` — Base USDC
itself — returns three live deployments with real query fees (121, 48 and 16
GRT). The top one, `QmWWa5bGmfqVD7C8mVSBQ5ct46Qseb6mnKYBjThLNrsSD3`, has a
schema of `Market`, `Pair`, `Position`, `PositionFee`, `CurrentPosition`. It is
a derivatives protocol that settles in USDC. It sees an address only if that
address traded on it.

That is the shape of the whole result, and it is structural rather than bad
luck: **a subgraph indexes a contract's events, so it knows the addresses that
touched that contract and no others.** "Every address on Base" is not a
contract, so nothing indexes it. A merchant payout address that has only ever
received USDC transfers appears in none of these.

The one product that would answer the question is the Token API, and
`thegraph.com/docs/en/token-api/quick-start/` now 301s to
`app.pinax.network/docs/api/`. It has moved off The Graph to Pinax and takes a
key — which breaks the exact property §6 is built on, that payment *is* the
authentication.

**Conclusion: FAILED.** Not "we could not find one in the time" — no such
subgraph exists, for a reason that will not change by looking harder. Per §2,
Part 5 (§7, onchain counterparty history as evidence) is **cut**, and §11
forbids writing our own subgraph to rescue it. Rule 1 keeps its current
behaviour: never paid this merchant before → ESCALATE.

The honest version of this belongs in the README rather than being quietly
dropped, because it is a genuine finding about the network: per-query x402
access is real and cheap, and the data behind it is protocol-shaped, so an
agent can buy an answer about *a protocol* for a cent and cannot buy one about
*an address* at all.

---

## What Phase 0 changed

| | Result | Effect on the plan |
|---|---|---|
| L1 | **pass** | decrypt works with no device attached, so the VPS design in §4 holds |
| L2 | **no message signing** | Part 3 (§5) needs DMK; its §5 cut line is live from day one |
| G1 | pass | header not body, `accepts[0]`, `amount`, $0.01, Base USDC |
| G2 | fails as predicted | `thegraph.py` adds a third spelling and the header decode |
| G3 | **failed, structurally** | Part 5 (§7) is cut |

Parts that survive and are fully unblocked: **1** (`/decide`), **2**
(`keyring.py`), **4** (The Graph adapter), **6** (SKILL.md).
