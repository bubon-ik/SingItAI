# SingIt Ask

OpenAI-compatible model answers with USDC payments over x402.

- **Web Ask on Base and Solana:** actual Surplus token cost + 30% markup
  (rounded up to one USDC micro-unit) + a published **0.001 USDC** settlement fee.
  The maximum allowed charge is **0.003 USDC**. This is a limit, not a fixed price.
- **Example:** 0.0001 USDC model cost + 0.00003 markup + 0.001 fee =
  **0.00113 USDC**, on either network's web Ask path.
- **Base:** `POST /v1/chat/completions/metered`, CDP `upto` with bounded
  sponsored approval and actual-amount settlement. Its existing limiter funding
  path is unchanged; that separate funding leg still uses the existing gas policy.
- **Solana:** authenticated `POST /v1/chat/completions/quoted/solana/prepare`
  prepares an answer and a measured invoice. `POST .../quoted/solana/:id`
  requires one CDP `exact` payment for that invoice before releasing the answer.
  The user's agent signs as the existing SPL delegate; USDC moves directly from
  the owner's token account to the merchant. CDP supplies the network fee payer.
  No agent USDC/SOL top-up, agent token account, escrow or refund is needed.
- **Legacy Solana `upto`:** `/v1/chat/completions/metered/solana` (0.002 USDC fee,
  two-step escrow/refund) is off in production. Nothing uses it; `SINGIT_ASK_METERED_SOLANA=1`
  turns it back on.
- **Legacy fixed-price API:** `/v1/chat/completions` remains PayAI `exact`,
  0.003 USDC on either network. The manual MetaMask test page uses this route.
- **Cost source:** Surplus `usage.buyer_cost_micro`, including its actual
  input/output/cache pricing. Missing, invalid or over-ceiling cost is refused.
- **Model:** `deepseek-v4.1-flash` via the merchant's private Surplus API key.
- **Facilitator:** [Coinbase CDP](https://docs.cdp.coinbase.com/x402/seller/facilitator).
  The published settlement service fee also applies during its free allowance;
  it does not assert CDP invoices every individual transaction during that allowance.

## Automatic web-agent payments

Select **DeepSeek V4.1 Flash · SingIt Ask** with an existing approved allowance.
The account's own network agent pays automatically within its limits. There is
no cross-network fallback or per-answer wallet confirmation. Existing Venice
and Telegram selections remain independent.

Solana users keep their USDC in their own wallet. They need SOL there for wallet
operations they explicitly sign, such as approving/revoking the SPL allowance;
ordinary Ask payments require no additional SOL funding. Existing agent SOL or
USDC from earlier flows is preserved, but the new Ask path does not use it.
The existing funding APIs remain for compatibility, without a funding prompt in
Ask's allowance UI. Unresolved older payments still require reconciliation.

The preparation route uses a dedicated server-only secret, never browser code.
It does not expose the answer. Request IDs bind owner, agent and normalized input;
a repeated preparation returns the same quote without invoking the model again.
Only one live quote per owner/agent exists: a new preparation replaces an unpaid
one, which can then no longer be paid. Quotes expire after five minutes. A
settlement is never resubmitted; an uncertain one stays as `unresolved` for review.
The merchant pays for inference before collecting payment on this private path:
a failed/abandoned quote therefore leaves that model cost with the merchant.
Do not expose the preparation secret to untrusted clients.

A private SQLite database records the generated answer, exact invoice and durable
settlement claim before settlement is requested. Concurrent/replayed requests
cannot settle again. A failed or unreachable verification marks the quote `failed`:
verification submits nothing. The buyer pins the merchant URL, native USDC mint, receiver,
owner, delegate, price, fee payer and request memo. Both the buyer and gateway
independently check the confirmed transaction's owner debit, merchant credit,
agent authority and memo before counting the actual charge.

The local allowance ledger reserves at most 0.003 USDC while a request runs,
then records actual spend and releases the difference. This reservation does not
move funds on Solana. A payment without a confirmed answer keeps its hold and a
private checkpoint in `~/.sign402/ask-metered/`, also after restarts, and blocks
further Ask and search payments for that account until the chain settles it up.
The next request does that automatically, and moves the files with their proof
to `reconciled/`:

- **No checkpoint:** no signature reached the merchant, so the hold is released.
- **Merchant refusal before settlement** (verification refused or unavailable, changed
  terms, replaced quote): the hold is released at once.
- **Solana:** the owner's USDC account is searched for the request memo at finalized
  commitment. A verified transfer is counted at its actual amount. Once the
  transaction's blockhash is past its last valid height without one, it is released.
  Until then, the user is asked to try again in a minute.
- **Base:** Permit2 decides. A nonce still unused after its deadline (120 s after
  signing) can never be spent, so the reservation is released. A used nonce means
  the payment went through without an answer: that one stays blocked for review.

**Deployment status, October 3:** the Base actual-usage payment was verified at
0.001055 USDC for 833 tokens; see [CHECKS.md](CHECKS.md). The previous user-funded
Solana implementation was activated at 11:25:35 UTC. The direct-payment update
was activated at 12:08:02 UTC with private rollback snapshot
`20261003T120802Z-before-direct-solana`. All nine deployed hashes match, all
three services are active, and the public Node client receives the new pricing
and updated UI. Wallet identities were preserved; no pending Solana Ask journal
remained at inspection. The activation manifest is now historical.

Verification for this update: 43 Node tests and 294 isolated web tests passed on
the VPS, including the real delegated transaction builder with simulated
facilitator settlement. CDP's live `verify` accepted a delegated owner-USDC exact
payment without submitting it. The owner subsequently completed a direct Solana payment: 0.001069 USDC for
1081 tokens, independently verified at finalized commitment. Its receipt and
allowance ledger agree; CDP paid the network fee. See [CHECKS.md](CHECKS.md).
Automated tests send no payments. The chat-context correction was activated at 12:20:36 UTC to prevent obsolete
agent-funding advice in generated answers; both deployed source hashes and all
three service health checks were verified.

Historical activation scripts have version-specific manifests; refresh them
against production before reusing them. Do not run old activation commands over
this update.

## Manual MetaMask test

Open `https://ask.singitai.app/test/?payer=YOUR_BASE_ADDRESS` in a browser with
MetaMask. The page connects the selected account, checks a fresh quote, and asks
the user to explicitly sign one payment of **0.003 native USDC on Base** to
`0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e`. It sends a fixed 32-token test
request to the model. Connecting alone does not sign or pay.

The test pins the recipient, token, network and amount, refuses changed terms,
and uses EIP-3009 `TransferWithAuthorization`, checked against the installed
`@x402/evm` SDK in offline tests. The signature expires after at most five minutes.
There is no automatic retry. After a signed request leaves the browser, the
page records only a submitted flag in session storage and prevents another
attempt in that tab; payment signatures remain in memory only. On an uncertain
result, check settlement before trying another payment. A successful response
shows the model answer and a BaseScan transaction link.

This is a one-request operator test, not a conversational chat UI. The normal
API still accepts x402 clients on both configured networks. The Solana receiver
must have its native USDC associated token account before real Solana payments.

## Run

```bash
npm ci
npm test
# Configure the Git-ignored .env with the variables below, then:
node --env-file=.env src/server.mjs
```

| Variable | Meaning |
| --- | --- |
| `SINGIT_ASK_PAY_TO_SOLANA` | Wallet that receives Solana payments. Its USDC token account must already exist. |
| `SINGIT_ASK_PAY_TO_BASE` | Address that receives Base payments. |
| `SURPLUS_API_KEY` | Surplus buyer key (`inf_…`) with access to the configured model. Fund its workspace's deposited USDC balance; USDC in the linked wallet alone is not a prepaid balance. |
| `SINGIT_ASK_PRICE_SOLANA`, `SINGIT_ASK_PRICE_BASE` | Legacy exact-route price per answer. Default: `$0.003` on both networks. |
| `SINGIT_ASK_METERED` | Set `1` to enable the additional Base `upto` route. Requires `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` in the private service environment. Reuses the existing `cdp-x402-service` installation of `@coinbase/x402`; no dependency installation was performed for this change. |
| `SINGIT_ASK_DIRECT_SOLANA` | Set `1` to enable the private direct Solana route with CDP metering. |
| `SINGIT_ASK_QUOTE_TOKEN` | Dedicated server-only authentication secret, shared by merchant and gateway (at least 32 characters). |
| `SINGIT_ASK_QUOTE_DB` | Private writable SQLite path for prepared answers and settlement claims. |
| `SINGIT_ASK_METERED_SOLANA` | Set `1` alongside `SINGIT_ASK_METERED=1` to enable the Solana metered route. Uses the same existing CDP credentials and installed `@x402/svm` 2.28.0. |
| `SINGIT_ASK_MODEL` | Upstream model. Default: `deepseek-v4.1-flash`. |
| `SINGIT_ASK_PORT` | Loopback port. Default: `8140`. |

The service listens on 127.0.0.1 only. Configure a Cloudflare tunnel hostname to publish it. Only loopback proxies are trusted for the forwarded HTTPS scheme.

## On the VPS

Use the dedicated user service in `deploy/singit-ask.service`. It runs as `hermes`,
independently of the gateway, from `/home/hermes/apps/sign402/singit-ask`.
The private configuration is `/home/hermes/.config/singit-ask/service.env`
(directory mode `0700`, file mode `0600`). It contains only this service's settings.

```bash
export PATH=/home/hermes/.hermes/node/bin:$PATH
export XDG_RUNTIME_DIR=/run/user/$(id -u)
cd /home/hermes/apps/sign402/singit-ask
npm ci --omit=dev --ignore-scripts --no-audit --no-fund
npm test
install -m 0644 deploy/singit-ask.service ~/.config/systemd/user/singit-ask.service
systemctl --user daemon-reload
systemctl --user enable --now singit-ask
curl --fail http://127.0.0.1:8140/health
```

The user manager requires lingering for startup after reboot. Verify it with
`loginctl show-user hermes -p Linger`; this VPS already has `Linger=yes`.

Node is the v22 build bundled with Hermes. In the existing Cloudflare tunnel, add:

- Hostname: `ask.singitai.app` (published; external health and unpaid x402 challenge verified).
- Service: `HTTP`, URL: `127.0.0.1:8140`.
- Leave the HTTP Host Header override empty to preserve the public hostname.

Public checks once the route exists:

```bash
curl --fail https://ask.singitai.app/health
curl -i https://ask.singitai.app/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}],"max_tokens":32}'
```

The second request must return `402` and `PAYMENT-REQUIRED`, advertising USDC
amount `3000` on both Solana and Base with the configured recipient addresses
and the public HTTPS resource URL. It does not call the model or pay anything.
Real signed payment and settlement require a separately approved live test.

Stop or roll back only this service with `systemctl --user disable --now singit-ask`.
Keep its private configuration out of Git, command output and published logs.
