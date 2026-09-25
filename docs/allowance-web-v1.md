# Allowance lane on the web — design, v1

Status: draft, 25 September 2026. Builds on [trezor-allowance-v1.md](trezor-allowance-v1.md);
everything verified there on Base mainnet (T4, T6, T7, T8) is kept.

## Goal

Anyone opens the SingIt web page, connects the wallet they already use —
Rabby, MetaMask or Phantom, with or without a Trezor or Ledger behind it — sets
limits, and signs **once**. From then on the agent buys (x402 tools, Bitrefill)
from the web page or the Telegram bot without asking the wallet again, inside
those limits. Money stays in the user's wallet until a purchase needs it.
Revoking is one more signature.

Nothing is installed on the user's computer. The tunnel, sidecar and companion
remain an option for owners who want the limiter checked on their own machine
before their device shows anything (see "Two signing paths").

## What stays and what changes

| Part | v1 (owner only, Telegram) | Web v1 (everyone) |
| --- | --- | --- |
| Limiter contract | `AgentAllowance`, one per user, deployed by us | unchanged |
| Agent key, x402 payments, Bitrefill x402, float, watcher | as verified on mainnet | unchanged |
| Who is an owner | `SIGN402_ALLOWANCE_OWNERS` in the server env | anyone who proves an address with Sign-In with Ethereum |
| How the owner signs the grant | broker → tunnel → companion → sidecar → Trezor Suite | the page hands the transaction to the connected wallet |
| Revoke | same device path | the same, from the wallet; also works from revoke.cash or any wallet without us |
| Where purchases start | bot | bot and web page, one account |

## Two signing paths

1. **Wallet (default).** The page builds `approve(limiter, amount)` — or a
   gasless `permit`, below — and the user's wallet shows and signs it. The
   wallet, and the hardware device behind it if any, displays the spender and
   amount. The limiter address comes from our server.
2. **Companion (optional, advanced).** The v1 path. The owner's own machine
   checks that the spender is the tested limiter owned by this address before
   the device shows anything, so even a compromised server cannot substitute a
   different spender. Needs the macOS launch agents
   (`trezor-sidecar/macos/install-launch-agents.sh`) and, for other users, a
   packaged app and an HTTPS broker endpoint instead of SSH. Not in web v1.

## Accounts and identity

- **Sign-In with Ethereum (EIP-4361).** The server issues a nonce (single use,
  5 minutes); the wallet signs the message; the server checks it and opens a
  session bound to that address. Domain and URI are the page's own; chain id
  8453. Session: HttpOnly, Secure, SameSite=Strict cookie, 12 hours, rotated on
  sign-in.
- **One account, several identities.** An account holds one owner address and,
  optionally, a Telegram id. Web-only accounts get the user id `wallet:0x…`
  (checksummed); the existing Telegram accounts keep theirs.
- **Linking Telegram.** Signed in on the web, the user presses "Link Telegram";
  the page shows a one-time code (6 digits, 10 minutes); the user sends
  `/link <code>` to the bot. The bot account and the address become one
  account; watcher notices go to Telegram from then on.
- **Changing the owner address** means a new limiter: the address is
  immutable in the contract. The old one keeps its allowance until revoked, and
  the page says so (as `/allowance_setup` already does).
- **Smart-contract wallets** (Coinbase Smart Wallet, Safe) need ERC-1271 /
  ERC-6492 signature checks and send approvals through their own batching. Not
  in v1; the page refuses them with a clear message.

## Flows

### 1. Connect and sign in

Two transports, one flow: browser extensions found by EIP-6963 (Rabby,
MetaMask, Phantom on desktop) and **WalletConnect** (QR code or deep link, for
mobile wallets). Use a kit that offers both behind one "Connect" button — Reown
AppKit, RainbowKit or ConnectKit on wagmi + viem; WalletConnect needs a
`projectId` from Reown Cloud. The backend does not know or care which
transport was used: it receives the same SIWE signature, transaction hash or
permit signature and checks them against the chain. `SIGN402_WEB_DOMAIN` must be
the page's host, or wallets (WalletConnect's domain verification especially)
flag the sign-in as suspicious. If the wallet is not on Base, ask it to switch
(`wallet_switchEthereumChain`, 8453). Phantom is used in its EVM mode. Then SIWE.

### 2. Limits and the limiter

The user picks a daily cap, a per-purchase cap and a lifetime (presets:
$5/$1/30 days, $20/$5/30 days, $100/$10/90 days; custom within the server's
ceilings). The page explains in one line that nothing leaves the wallet yet.

"Create my limiter" deploys `AgentAllowance(USDC, owner, agent, guardian,
caps, expiry)` from the user's agent key, gas paid by our gas funder — the v1
`setup`, unchanged: code checked against the tested artifact, every immutable
read back, source published to Sourcify. The page shows the limiter with a
Blockscout link and the caps read from the contract, not from our database.

### 3. Grant: approve or permit

The page offers the amount (default: one day's cap; ceiling from the server)
and one of two ways, chosen by what the wallet holds:

**a. `approve` (the user has ETH on Base).** The server returns the exact
transaction; the page sends it with `eth_sendTransaction`:

```json
{ "from": "<owner>", "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "data": "0x095ea7b3<limiter><amount>", "value": "0x0", "chainId": "0x2105" }
```

Rabby and MetaMask show it as "approve / spending cap: N USDC to 0x…". The
page then reports the transaction hash; the server verifies it (below).

**b. `permit` (no ETH needed).** USDC on Base supports EIP-2612 (checked on
chain: name "USD Coin", version "2", domain separator
`0x02fa7265…834f`). The page asks the wallet for `eth_signTypedData_v4`:

```json
{ "domain": { "name": "USD Coin", "version": "2", "chainId": 8453,
              "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" },
  "primaryType": "Permit",
  "message": { "owner": "<owner>", "spender": "<limiter>", "value": "<amount>",
               "nonce": "<USDC.nonces(owner)>", "deadline": "<now + 15 min>" } }
```

The server submits `permit(...)` from the gas funder and the allowance appears
without the user paying gas. Wallets warn about permits because they are a
common phishing vector; the page says, before asking, exactly what the wallet
will show and why. Hardware wallets display the typed fields (owner, spender,
value, deadline). Default is `approve` when the wallet holds at least 0.00005
ETH on Base, otherwise `permit`.

### 4. Purchases

Unchanged from v1 and the same for the web page and the bot: the gateway picks
the lane, the limiter funds the agent (float refill or exact), the agent pays
by x402, settlement is read from the chain. The web shop calls the same
Bitrefill quote → confirm → buy sequence; the confirmation screen shows
product, denomination, price, network and recipient before anything is paid.

### 5. Revoke and pause

- **Revoke:** `approve(limiter, 0)` from the wallet, or `permit` with value 0
  (gasless). When it is mined and nothing stays granted, the agent's float goes
  back to the owner (as in v1, 254cef8).
- **Pause:** permanent; the owner can call `pause()` from the wallet, and the
  guardian (our watcher) pauses on the v1 anomalies. After a pause, "create a
  new limiter" is the only way forward.
- The page always links to the limiter on revoke.cash, so users know they can
  revoke without us.

## API

Base path `/web/v1`, JSON, session cookie from SIWE, CSRF header
`X-SingIt-CSRF` on every POST. Public through the reverse proxy with TLS; CORS
only for the page's origin. Amounts are strings of USDC atomic units (6
decimals); every response that changes something carries `state`.

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /auth/nonce` | `{address}` | `{nonce, message, expiresAt}` — the exact EIP-4361 text to sign; `smart_wallet_unsupported` for contract wallets |
| `POST /auth/verify` | `{message, signature}` | session cookie, `{account, address, csrfToken, expiresAt, telegramLinked}` |
| `POST /auth/logout` | — | clears the cookie |
| `GET /session` | — | `{account, address, telegramLinked}` |
| `GET /allowance` | — | `configured`, and with a limiter: `limiter, owner, agent, guardian, dailyCapAtomic, perPurchaseCapAtomic, expiry, paused, allowanceAtomic, remainingTodayAtomic, floatAtomic, ownerUsdcAtomic, ownerEthWei, source, state, operations[], alerts[]`; `state` is `waiting_for_grant`, `granted`, `paused` or `expired` |
| `POST /allowance/setup` | `{dailyCap, perPurchaseCap, days}` | the new limiter as above, `created` |
| `POST /allowance/grant/prepare` | `{amount, method: "approve" \| "permit"}` | `{operation, kind, method, state, limiter, amountAtomic, expiresAt, walletShows}` and `tx` (approve) or `typedData` (permit) |
| `POST /allowance/grant/submit` | `{operation, txHash}` (approve) or `{operation, signature}` (permit) | the operation |
| `POST /allowance/revoke/prepare` | `{method, limiter?}` | as grant, amount 0 |
| `POST /allowance/revoke/submit` | as grant | the operation |
| `GET /allowance/operations/{id}` | — | `{operation, kind, method, state, limiter, amountAtomic, txHash, detail, createdAt, updatedAt}`; poll every 2 s |
| `POST /allowance/pause` | — | the limiter, paused for good by our guardian (the panic button; no wallet needed) |
| `POST /link/telegram` | — | `{code, expiresAt, text}`: send `/link <code>` to the bot within 10 minutes |
| `POST /link/telegram/remove` | — | `{telegramLinked: false}` |
| `GET /shop/tools` | — | `{tools: [{id, name, description, source, resourceUrl, inputSchema}]}` |
| `POST /shop/tools/quote` | `{tool, …template fields}` | `{quoteId, tool, priceAtomic, priceUsd, payTo, network, resourceUrl, expiresAt, text}`, 10 minutes |
| `POST /shop/tools/buy` | `{quoteId}` | the purchase: `ok`, `text`, `txId`, the tool's result |
| `POST /shop/bitrefill/search` | `{query, country?, kind?}` | `{products, text}` |
| `POST /shop/bitrefill/quote` | `{productId, package}` | `{quoteId, name, package, priceUsd, priceAtomic, expiresAt, text}` — show product, denomination, price, network and recipient before buying |
| `POST /shop/bitrefill/buy` | `{quoteId}` | `{invoiceId, delivered, text}` (no code) |
| `GET /purchases?offset=` | — | `{purchases, hasNext}`, 20 at a time, no codes |
| `POST /purchases/reveal` | `{purchaseId}` | a Bitrefill code, shown once |

Errors are `{ok: false, error, message}` (or `text` from the shop) with 400
(refused, and the reason says why), 401 (sign in / CSRF), 403 (not enabled), 404,
413, 415 (JSON only), 429 (rate limited) or 503 (shop or deployment budget).
Every POST needs `Content-Type: application/json` and, once signed in,
`X-SingIt-CSRF: <csrfToken>`; send cookies (`credentials: "include"`).

Operation states: `PREPARED → SUBMITTED → DONE | FAILED | EXPIRED`. The server
moves them on by reading the chain, not by trusting the page, and a new
prepare first moves the open ones on (the T8 lesson, 2ba7762).

## Running it

`scripts/enable-web-page.sh <beta addresses>` on the VPS: it writes the
`SIGN402_WEB_*` settings (domain `app.singitai.app`, the beta allowlist, the
internal token, `SIGN402_WEB_STATIC_DIR` pointing at `website/`), starts
`sign402-web-api` on `127.0.0.1:8130` and checks it. The web API serves the page
too (`/app/`, `/assets/`; `/` redirects to `/app/`; nothing else of the disk),
so page and API share one origin: the SameSite=Strict cookie works and no CORS
is involved.

Nothing opens on the host. The page goes public through the existing Cloudflare
Tunnel, as `decide.singitai.app` does (docs/decide-public-endpoint.md): one
Public Hostname, `app.singitai.app` → `http://127.0.0.1:8130`. Only the web API
listens there, and it answers only `/app/`, `/assets/` and `/web/v1/*`. It
takes the client address from `Cf-Connecting-Ip` (or `X-Forwarded-For`) only
when the connection comes from this host. Optional settings:
`SIGN402_WEB_MIN_OWNER_USDC` (1), `SIGN402_WEB_MAX_LIMITERS_PER_30_DAYS` (3),
`SIGN402_WEB_MAX_DEPLOYS_PER_DAY` (50), `SIGN402_WEB_DB` (`~/.sign402/web.db`).

## What the server verifies

Before `prepare`:
- the session's address is the limiter's `owner` on chain;
- the limiter runs the tested code (immutables masked), is neither paused nor
  expired, and its `agent` and `guardian` are ours for this account;
- the amount is within the grant ceiling.

On `submit` with a transaction hash:
- the transaction is from the owner, to USDC, with exactly the prepared
  `approve(limiter, amount)` calldata, on chain 8453;
- the receipt succeeded; the allowance reads back (waiting out a lagging node
  only while fresh).

On `submit` with a permit signature:
- it recovers to the owner over exactly the prepared typed data, the nonce is
  current and the deadline in the future;
- only then the gas funder sends `permit`; then as above.

A hash or signature for anything else is refused and never broadcast.

## What the user sees

Screens, in order: Connect → Sign in → Limits → Create limiter → Grant →
Dashboard. The dashboard shows: allowance left, spent today of the daily cap,
the agent's float, recent spends with transaction links, watcher alerts, and
the actions Grant more / Revoke / Pause / Link Telegram / Shop.

Rules for the copy:
- Addresses are shown the way wallets show them, `0x4F35…a46B`, with the full
  address one tap away, so the user can match the wallet prompt.
- Before every wallet prompt, one sentence saying what the wallet will show:
  "Your wallet will ask you to approve up to 10 USDC for 0x4F35…a46B."
- Every limit is shown as read from the contract, with its Blockscout link.
- Never show redemption codes except on explicit "Reveal", once, as in v1.

Errors the page must name: wrong network; not enough USDC for the grant (allowed
but explained); no ETH for approve (offer permit); rejected in the wallet;
reverted on chain; limiter paused or expired (offer a new one); prepare expired;
rate limited.

## Security model

What changes from v1: in the wallet path **the spender address comes from our
server**. A compromised server could hand the page a malicious spender. Limits
on that:
- the wallet shows the spender; the page shows the same short address with a
  link to its verified code and read-only values (owner, agent, caps) on
  Blockscout — a user who checks sees a mismatch;
- grants are small by default (one day's cap) and capped server-side;
- the watcher's rule 1 (a spend to anyone but the agent pauses at once) runs
  against every limiter;
- the page bundle is served with Subresource Integrity and a strict CSP; a
  later step is an IPFS-pinned build whose hash is published;
- the companion path stays available for owners who want the local check.

Unchanged from v1: the user's keys never touch our server; the limiter caps
what any compromise of the agent key or the server can take per day and per
purchase; revoke works without us.

## Cost and abuse

Each account costs us gas: one limiter deployment (≈0.00001 ETH today), agent
gas top-ups, and a permit submission per gasless grant or revoke. Limits:
- one active limiter per address; a new one only after the old is revoked,
  paused or expired, at most three per address per 30 days;
- setup requires at least 1 USDC at the owner address (a cheap sybil filter);
- per-IP and per-address rate limits on `auth` and `setup`;
- a global daily deployment budget with an alert when it is reached;
- the watcher gets its own RPC without the 10-block `eth_getLogs` limit.

## Data model

New tables beside the v1 `allowance.db`:
- `accounts(account_id, owner_address, telegram_user_id NULL, created_at)`;
- `auth_nonces(nonce, address, expires_at, used_at)`;
- `sessions(session_hash, account_id, address, expires_at)` — the cookie holds
  the token, the table only its hash;
- `link_codes(code_hash, account_id, expires_at, used_at)`.

`operations` gains `method` (`device` | `approve` | `permit`) and
`prepared` (the permit nonce and deadline). `SIGN402_ALLOWANCE_OWNERS` stays as an allowlist during the
rollout and is removed at general availability.

## Rollout

1. Backend: SIWE, accounts, sessions, status and setup for web accounts,
   behind `SIGN402_WEB_ENABLED` and an address allowlist.
2. `approve` prepare/submit with server-side verification.
3. `permit` for grant and revoke.
4. Web shop (x402 tools, Bitrefill) on the web account.
5. Link Telegram.
6. The page (owner's design), then a private beta, then general availability
   with the abuse limits on.

**Step 1 is built** (`sign402_gateway/web_accounts.py`, `web_api.py`): SIWE
nonce → verify → session cookie + CSRF token, logout, `GET /session`,
`GET /allowance`, `POST /allowance/setup` with the USDC minimum, the 30-day
limiter cap and rate limits. Web accounts are owners through
`AllowanceService.owner_lookup`, so the watcher, `lane_for` and later purchases
see them like the env allowlist. Rehearsed over HTTP on a Base mainnet fork: a
fresh wallet signed in, created a limiter whose `owner`, `agent` and caps read
back on chain, and the deployed code matched the tested artifact; setup without
the CSRF header, a wallet outside the beta and a logged-out cookie were refused.
Run it with `python -m sign402_gateway.web_api` behind a TLS reverse proxy that
forwards `/web/v1` to `127.0.0.1:8130`. Messages from the lane still name the
Trezor and bot commands; step 2 makes them neutral for the web.

**Step 2 is built** (`AllowanceService.prepare_wallet`, `submit_wallet`,
`operation`; routes `grant|revoke/prepare`, `grant|revoke/submit`,
`GET /allowance/operations/{id}`). Prepare checks the limiter on chain (not
paused or expired, tested code, every immutable) and returns the exact approve;
a newer prepare replaces an unsubmitted one, anything already submitted blocks.
Submit takes only a transaction hash. The server then reads the transaction
from Base and counts it only if it is from the owner, to USDC, on Base, with no
value, an `approve` of this limiter, an amount in (0, grant ceiling] for a grant
(the amount the wallet actually signed is recorded, so a user who lowers the cap
in Rabby or MetaMask is respected) or exactly 0 for a revoke, mined no earlier
than the request, and not already counted for another request. A revoke that
closes the lane returns the agent's float, as in v1. `operations` gained the
states PREPARED, SUBMITTED and EXPIRED and a `method` column; an existing v1
table is rebuilt with every row kept (checked against the production schema).
Rehearsed on a Base mainnet fork with real signed transactions: grant 3 USDC →
DONE, allowance 3 on chain; an approve to another spender → FAILED, not counted;
revoke → DONE, allowance 0.

**Step 3 is built** (`method: "permit"` on prepare; `{operation, signature}` on
submit). Prepare reads `USDC.nonces(owner)` and returns EIP-2612 typed data with
a 15-minute deadline. Submit recovers the signer over exactly that typed data
(it must be the owner), checks the nonce is still current and the deadline in
the future, and only then sends `permit(owner, limiter, value, deadline, v, r,
s)` from the gas funder. If someone else submits the same signed permit first,
ours reverts but the allowance already reads what the owner signed, and that
counts as done. Permits cost us gas, so each account may submit six a day.
Rehearsed on a Base mainnet fork against the real USDC contract: permit grant
2 USDC → allowance 2, permit revoke → 0, the user's ETH untouched.

**Steps 4 and 5 are built.** The shop runs in the gateway
(`sign402_gateway/web_internal.py`) behind `/internal/web/*`, which answers only
loopback callers presenting `SIGN402_WEB_INTERNAL_TOKEN`; the web API forwards
`/shop/*` and `/purchases` there as the signed-in account (the page cannot name
another). A web account pays only from its own limiter. Tools are bought by
quote → buy: the quote fixes price and recipient, and a seller asking more, or
asking to be paid elsewhere, at buy time is refused with nothing paid. The
page's "Buy" on a quote is the human approval spending memory may ask for
(Bitrefill too), so no iMessage is involved; memory blocks, rate limits and the
purchase pause still apply. A web account's gateway spending limits are set
from its limiter's caps (under the operator's hard ceilings), because the page
has no `/set_limits` and a second set of limits the user never chose would
refuse what their limiter allows. History and the one-time code reveal are per
account. Linking: `POST /link/telegram` returns a 6-digit code (10 minutes, a
new one kills the old), `/link <code>` in the bot joins the chat to the account
(5 tries per 10 minutes), after which the bot's allowance commands, purchases
and `/limits` use the web account's limiter and agent, the chat keeps its own
purchase history, and watcher notices go to the chat. Smart-contract wallets
are refused at sign-in (EIP-7702 accounts are not), and at most
`SIGN402_WEB_MAX_DEPLOYS_PER_DAY` (50) limiters are deployed for everyone per day.

**The backend is complete** (steps 1–5 and the deployment): the whole chain —
sign-in over HTTP, a quote, a purchase forwarded by the web API to the gateway
over HTTP and paid from the account's lane — runs in one end-to-end test, and
a live quote from Otto's crypto-news endpoint read price and recipient
correctly. What remains is step 6: the page itself (the owner's design), a
domain for `SIGN402_WEB_DOMAIN`, the reverse proxy, then a mainnet check with a
real wallet recorded as T9.

Each step with unit tests and a mainnet check recorded in
[trezor-allowance-checks.md](trezor-allowance-checks.md), as T4–T8 were.

## Open questions

- Default grant: one day's cap, or the whole lifetime budget? Smaller is safer,
  larger means fewer signatures.
- Should the web page also allow `increaseAllowance`-style top-ups, or always a
  fresh approve of the new total?
- Service fee on Bitrefill x402 orders, still not collected (open since v1).
- Phantom's EVM support on Base is assumed; test it before promising it.
- Solana users (Phantom's default chain) need a different lane; out of scope.
