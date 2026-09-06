# Ledger Agent Stack — developer experience notes

Kept from the first command of Phase 0, not written from memory afterwards.
Submitted as the required DX feedback. Each note says what happened, what we
expected, and what we would change.

Versions: `@ledgerhq/wallet-cli` **2.1.0**, node v24.12.0, npm 11.6.2,
macOS (darwin arm64).

---

## 1. The headline use case — Key Ring on a host with no USB port — has no path

This is the most important note here, so it is first. Everything below it is
smaller.

The pitch that made us choose the Key Ring is "one device tap to set up, then
none", and hosts without USB ports called out specifically. Our gateway is a
cloud VPS. It has no USB port and never will.

That case does not work in 2.1.0, and the reason is not a bug — it is a missing
piece:

- `ring init` requires a physically attached device ("creating or recovering a
  trustchain (device required)"). A VPS cannot satisfy it.
- There is no `ring export` / `ring import`. The subcommands are `init`,
  `encrypt`, `decrypt`, `keys`, `destroy`. Nothing moves a membership.
- The member private key lives in the OS secret service — macOS Keychain as
  `ledger-wallet-cli` / `member-private-key-…`, password-protected; on Linux via
  `libsecret`/`secret-tool` over DBus. A headless server typically has no secret
  service at all. Ours has none of them installed.

So the machine that encrypts must be the machine that decrypts, and the useful
half of the feature — encrypt on a laptop with the device, decrypt on a fleet of
servers without one — cannot be assembled. We got as far as `ring init`,
verified decrypt works with the device unplugged **and** the network off, and
only then discovered that none of it transfers.

**Suggested fix**, in order of how much it would help:

1. `ring export-member` / `ring import-member`, exchanging the
   password-protected member key. It is already encrypted at rest with
   `WALLET_PASS`; a file is the same secret in a different container.
2. Failing that, let `ring init` enrol a new member using the device *once*, on
   a machine that has one, and emit credentials targeted at another host — the
   device tap you already require, producing something that can be deployed.
3. At minimum, say so in the docs. "The host that decrypts must itself have been
   enrolled with a physically attached device" is one sentence, and it would
   have changed our architecture on day one instead of on day two.

A note on what we did **not** do: we did not work around this by extracting the
key from the Keychain by hand. It would probably have worked. It would also mean
shipping a production payment system on an unsupported path through someone
else's security product, and the point of the exercise was the opposite of that.

---

## 2. `npm i -g` is the documented install and it fails on a default macOS

```
$ npm i -g @ledgerhq/wallet-cli
npm error code EACCES
npm error syscall mkdir
npm error path /usr/local/lib/node_modules/@ledgerhq
```

Nothing wrong with the package — this is npm's global prefix on a Mac where
`/usr/local/lib/node_modules` is root-owned, which is the out-of-the-box state
for a Homebrew-less node install. But it is the *first* command in the docs, and
a developer meeting it has to decide between `sudo npm i -g` (bad advice that
plenty of people take) and knowing about `npm prefix`.

A local install works and needs no privileges:

```
$ npm i @ledgerhq/wallet-cli && ./node_modules/.bin/wallet-cli --help
```

**Suggested fix:** offer the local-install line next to the global one, or ship
the `npx @ledgerhq/wallet-cli` form as the primary. Do not tell people to sudo.

---

## 3. `WALLET_CLI_MOCK=1` mocks the backend, not the device — and the docs do not say which

This is the note that cost the most time, so it is the one worth reading.

`WALLET_CLI_MOCK=1` reads like "run without hardware". It is not:

```
$ WALLET_CLI_MOCK=1 WALLET_PASS=… wallet-cli ring init
Generating member credentials…
Connect device, open Ledger Sync app — provisioning your Ledger Key Ring…
[✖] No Ledger device found. Unlock the device and try again.
```

The flag swaps the **trustchain API client** for an in-memory one. The device
transport is untouched, so every command that needs a device still needs a
device. A developer writing CI for anything downstream of `ring init` will set
this flag, watch it fail, and have no way to tell from the message whether the
flag is broken, ignored, or doing something else entirely.

**Suggested fix:** two lines in the docs — "`WALLET_CLI_MOCK=1` mocks the LKRP
backend. It does not emulate a device; commands marked *device required* still
need one." And if a device-free path is wanted, a separate `WALLET_CLI_MOCK_DEVICE`
would make the split explicit rather than implied.

---

## 4. The output format changes between commands, in the same invocation style

`wallet-cli --help` and every `ring …` command answer with a JSON envelope:

```json
{ "ok": true, "data": { "type": "help", "text": "Usage: …" } }
```

`wallet-cli send --help` and `wallet-cli account --help` answer with plain text:

```
Usage: wallet-cli send [options]
Sign and broadcast a transaction
```

Same binary, same `--help`, two shapes — and no `--output json` was passed in
either case. A script that parses one breaks on the other. Worse for an agent:
the help text is *inside a JSON string with escaped newlines* for half the
commands, so the thing an agent reads to learn the tool is the thing formatted
least like documentation.

**Suggested fix:** pick one. If the envelope is the format, apply it to every
command; `--output human` already exists to opt out.

---

## 5. Errors are well-shaped and say the next command — keep this

```json
{
  "ok": false,
  "error": {
    "kind": "command-execution",
    "message": "Ledger Key Ring not initialized. Run `wallet-cli ring init` first.",
    "command": "keys"
  }
}
```

Typed, machine-readable, and it names the command that fixes it. This is
better than most CLIs manage and it is what let an agent recover without a
human. Worth saying out loud so it does not get lost in a refactor.

---

## 6. `ring encrypt/decrypt` defaulting to stdin/stdout is the right default, and it is under-sold

```
--input, -i  Input file (default: stdin)
--out,   -o  Output file (default: stdout)
```

For our use — decrypting a wallet master key on a server — this is the
difference between a secret that touches the disk and one that does not. The
docs present `-i/-o` as the main form and stdin/stdout as the fallback. It is
the other way round for anything holding a secret.

**Suggested fix:** lead the `ring` docs with the piped form and one sentence on
why: `-o` writes plaintext to disk, and for a key that is usually the whole
threat you were trying to remove.

---

## 7. `ring` is encryption, and the name will keep costing people a day

"Key Ring" plus a hardware wallet reads as *signing*. It is not: `ring` derives
scoped AES-256-GCM keys and encrypts blobs. We checked the entire command tree
for a message-signing verb before believing it — `--help` was walked at every
level and grepped for `sign`, and the only hit is `send`, which signs a
*transaction* and broadcasts it.

That is a real gap for the agent use case the stack is aimed at: "agents that
hold secrets they cannot leak" is served, but "agents whose owner approves an
action" needs a detached signature over typed data, and the CLI has no verb for
it. You are pushed to `@ledgerhq/device-management-kit` and a bespoke Node
script.

**Suggested fix:** `wallet-cli sign-message --account <label>` (personal_sign)
and `--typed-data <file>` (EIP-712), printing the signature to stdout. It is the
same device flow `send` already drives, minus the broadcast, and it would let
the CLI cover approval as well as secrecy. Failing that, one line in the `ring`
docs — "for signing, see `send` or the Device Management Kit" — would save the
search.

---

## 8. The command table says `ring decrypt` needs network. It does not.

The documented requirements column marks `ring decrypt` as needing the network.
Measured, with the device unplugged and Wi-Fi off, it decrypts fine and exits 0.

This is not a nitpick about a table cell. We were deciding whether putting the
master key behind the ring would add a third-party dependency to the start-up
path of a payment system — whether an LKRP outage would stop the gateway
booting. The docs said yes, the behaviour says no, and the answer changes
whether the design is shippable. We only found out by unplugging the machine.

It also matters the other way, for anyone writing a threat model: if decrypt
needs neither device nor network, then after `init` the scoped key is derivable
from local credentials plus `WALLET_PASS`, and a reader should not be left to
infer that from a requirements table.

**Suggested fix:** mark `ring decrypt` as requiring neither, and add a sentence
saying what the device is a root *of* — enrolment, not per-use authorisation.
This is the single most consequential fact about the feature for anyone
deploying it on a server, and right now the docs point the wrong way.

---

## 9. `resource.url` in the x402 402 block points at an internal indexer host

Not a `wallet-cli` note, but a Graph one, filed here to keep the DX findings in
one place. See `checks.md` G1: the `payment-required` header names
`mainnet-thegraph-arbitrum-02-eu-west3.thegraph.com` as the resource, not the
`gateway.thegraph.com` URL the client called. A client that treats
`resource.url` as the seller's identity gets a different seller per region.
