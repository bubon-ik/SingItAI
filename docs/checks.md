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

## L3 — can the VPS, which has no USB port, be a ring member at all?

Not part of the original five. It should have been, and it is the check that
changed part 2 the most.

L1 proved decrypt needs no device **on the machine that ran `ring init`**. It
says nothing about a second machine, and the plan — "only `master-key.enc` goes
to the server" — assumed the answer without testing it.

`wallet-cli` 2.1.0 installs and runs fine on the VPS (Linux x86_64, node
installed in userspace, no sudo, nothing about the host changed):

```
$ node .../wallet-cli --version        →  {"ok": true, ... "version": "2.1.0"}
$ node .../wallet-cli ring keys
{"ok": false, "error": {"message": "Ledger Key Ring not initialized. Run `wallet-cli ring init` first.", "command": "keys"}}
```

So the server must be enrolled. Three facts close every route to that:

1. **`ring init` requires a physically attached device.** Its own help says
   "creating or recovering a trustchain (device required)", and L1 confirmed the
   device step is not skippable — `WALLET_CLI_MOCK=1` does not stand in for it.
   A cloud VPS has no USB port to attach one to.
2. **There is no export or import verb.** The `ring` subcommands are `init`,
   `encrypt`, `decrypt`, `keys`, `destroy`. Nothing moves a membership between
   machines.
3. **There is nowhere to put it even by hand.** The member private key is held
   by the OS secret service — macOS Keychain here, as
   `ledger-wallet-cli` / `member-private-key-8d7f63a1…`, 510 bytes,
   password-protected. The Linux build reaches for `libsecret` / `secret-tool` /
   `gnome-keyring` over DBus, and this host has none of them:

```
  secret-tool            ABSENT
  gnome-keyring-daemon   ABSENT
  dbus-launch            ABSENT
  libsecret lib          ABSENT
```

**Conclusion: FAILED. A host that cannot have a Ledger physically attached to it
cannot join the ring by any supported path.** This is precisely the case Ledger
advertises — Key Ring on hosts without USB ports — and in 2.1.0 there is no
route to it. It is the headline entry in `ledger-dx-notes.md`.

**What it changes.** Part 2's code is unaffected and every acceptance criterion
still holds: the key is ciphertext at rest, it is decrypted at boot through the
ring, and the gateway refuses to start when that fails. What changes is where it
can run — an enrolled host, which for now means one a device can reach. The
claim "this runs on a keyless VPS" is not made, because it is not true, and
making it to the company that built the product would be found out in a
sentence.

---

## L4 — `keyring.py` against the real `wallet-cli`, end to end

The 19 unit tests drive a stand-in binary, and L1 exercised the ring on its own.
Neither shows the two working *together*, which is the only thing that matters
on the morning of a demo. Run on the enrolled machine, 6 September, with a
**throwaway** Fernet key — the production master key was not involved.

```
== 1. throwaway Fernet key, encrypted through the ring ==
✔ Encrypted (72 bytes, AES-256-GCM)
== 2. the gateway loads it through the ring ==
   loaded a valid Fernet key, length 44 - value not printed
== 3. corrupt the ciphertext: it must refuse ==
   refused, as designed:
    `wallet-cli ring decrypt` failed with exit code 1 reading key 'sign402-master'
    in /var/folders/…/master-key.enc
== 4. ring off: unchanged behaviour ==
   read straight from the environment: not-a-real-key
```

**Conclusion: PASS, all four acceptance criteria of §4.** A 44-character Fernet
key becomes 72 bytes of ciphertext; `load_master_key` recovers it through the
real CLI and validates it as a Fernet key before returning; a single appended
byte makes the gateway refuse to start; and with the ring off the value comes
straight from the environment as it always did.

Criterion 3 is the one worth insisting on, and it was tested by actually
corrupting the file rather than by reading the code. The refusal names both the
key and the file, which is what turns "the wallets are undecryptable" into "the
service did not start and said why".

---

## L5 — a real Ledger signature, verified, and refused on replay

Part 3 end to end on the hardware, 6 September. Nothing was spent and no wallet
was touched: the payload is the one the gateway builds for an escalated payment,
and only the signature is real.

```
== 1. what the device is asked to show ==
   merchant   giftcards.example.com
   payTo      0x8f3a1c2b4d5e6f708192a3b4c5d6e7f809a1b2c3
   amountUsd  25.00
   owner      agent-7
   rule       unknown_merchant
   journalId  01JB8Z4A1B2C3D4E5F6G7H8J9K
== 2. signing on the Ledger ==
  … pending  … completed
   signature: 0xc96f07842d8760d002…9c988f1b
== 3. the gateway verifies it ==
   accepted, signed by 0x1388…d9fa
== 4. the same signature on the next payment ==
   refused, as designed: That approval was signed for a different decision.
```

**Conclusion: PASS.** Step 4 is the one worth the exercise. Same merchant, same
payout address, same amount, same signer — refused, because the journal entry is
a different one. That is the property a tap in a chat cannot have: an approval
is spent when the decision it names is spent.

Device: Ledger Nano S Plus, Ethereum app, derivation `44'/60'/0'/0/0`. Signed
through `@ledgerhq/device-signer-kit-ethereum` 1.18.0 on DMK 1.9.0, because L2
established `wallet-cli` cannot sign messages at all.

Three bugs stood between the device and this output, and all three were ours or
the SDK's rather than the hardware's. They are written up in
`ledger-dx-notes.md` because each one presents, from the caller's side, as a
device that never answered — which is the most expensive way for an integration
to fail.

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
| L1 | **pass** | decrypt needs neither device nor network — on the enrolled host |
| L3 | **failed** | a USB-less host cannot be enrolled at all; part 2 runs where a device can reach |
| L4 | **pass** | keyring.py drives the real wallet-cli; all four §4 criteria met |
| L5 | **pass** | a real Ledger signature authorises one payment and is refused on the next |
| L2 | **no message signing** | Part 3 (§5) needs DMK; its §5 cut line is live from day one |
| G1 | pass | header not body, `accepts[0]`, `amount`, $0.01, Base USDC |
| G2 | fails as predicted | `thegraph.py` adds a third spelling and the header decode |
| G3 | **failed, structurally** | Part 5 (§7) is cut |

Parts that survive and are fully unblocked: **1** (`/decide`), **2**
(`keyring.py`), **4** (The Graph adapter), **6** (SKILL.md).
