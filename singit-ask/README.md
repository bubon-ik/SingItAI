# SingIt Ask

OpenAI-compatible model answers with direct USDC payments over x402.

- **Base metered endpoint:** `POST /v1/chat/completions/metered`.
  Actual Surplus token cost + 30% markup (rounded up to one USDC micro-unit)
  + a published 0.001 USDC settlement fee. The agent authorizes **at most 0.003
  USDC**, and only the final invoice is settled. No merchant credit deposit.
- **Solana metered endpoint:** `POST /v1/chat/completions/metered/solana`.
  Actual token cost + 30% markup + **0.002 USDC** settlement fee. CDP uses two
  on-chain transactions: a one-request escrow deposit (at most 0.003 USDC), then
  settlement and an automatic refund of the unused portion to the payer agent.
  This does not create prepaid merchant/chat credit or a reusable channel.
- **Example:** 0.0001 USDC model cost + 0.00003 markup costs **0.00113 USDC on Base** or **0.00213 USDC on Solana**, including the respective settlement fee.
- **Cost source:** Surplus `usage.buyer_cost_micro`, so the provider's actual
  input/output/cache pricing is reflected. Missing, invalid or over-ceiling cost
  fails without asking the facilitator to settle; no estimated-cost fallback.
- **Facilitator:** [Coinbase CDP](https://docs.cdp.coinbase.com/x402/seller/facilitator)
  for `upto` on both networks. Base uses sponsored, bounded EIP-2612 approval and Permit2 settlement; Solana uses the canonical payment-channels program with sponsored fees and rent.
  The published service fee also applies during CDP's promotional free allowance.
  It is not a claim that CDP charges each particular promotional transaction.
- **Legacy endpoint:** `POST /v1/chat/completions` remains PayAI `exact`, priced
  at 0.003 USDC per answer on Base or Solana. The manual test page uses this route.
- **Model:** `deepseek-v4.1-flash` via Surplus using the merchant's private API key.
- **Free endpoints:** `GET /health` and `GET /v1/models` expose both pricing modes.

## Automatic web-agent payments

**Deployment status, October 3:** both metered merchant endpoints, gateway adapters,
model selection and actual-spend ledgers are active. A real Base agent request
settled **0.001055 USDC** for 833 tokens; the native-USDC receipt, usage record and
allowance ledger agree. See [verification and transaction](CHECKS.md#october-3-first-verified-actual-usage-base-ask-payment).
The receiver's canonical Solana USDC account now exists and was verified with
1 USDC. A funded end-to-end Solana metered payment remains unverified.

Select **DeepSeek V4.1 Flash · SingIt Ask** in a Base or Solana
account with an existing approved allowance. The account's own network agent pays;
there is no fallback across networks and no per-answer wallet confirmation.
Solana `upto` requires the payer agent to own the escrow funds. The adapter reuses
that agent's existing USDC, including previous refunds; if needed it pulls only
the ceiling shortfall through the existing SPL delegate grant. The user's own
agent pays that funding transaction and any token-account rent from its SOL.
In **Allowance → Agent network fees**, the user explicitly signs a SOL transfer
from their wallet to their agent (default 0.005 SOL; editable). There is no
operator sponsorship or automatic gas top-up. An unpaid RPC check prices rent
and the network fee before funding; insufficient SOL releases the Ask hold
without sending anything. Refunds remain in the same
user's agent wallet. No new shared payer or merchant credit balance is created.
Existing Venice and Telegram selections stay independent.

The Solana allowance feature must be configured. The user pays approval/revoke
fees in SOL and explicitly funds their agent's network-fee balance. Existing
operator sponsor credentials are neither used by Ask funding nor used as a
fallback for wallet operations. Solana network fees and initial token-account
rent are separate from the published 0.002-USDC x402 settlement service fee.
The user sees and confirms the SOL transfer in their own wallet. Unknown gas
transfer outcomes prevent resubmission until reconciliation.

**Rollout:** this user-funded gas update is prepared and tested; activation still
requires the operator's sudo authentication. It has not made a real payment.

The client pins the HTTPS merchant, native Base USDC, recipient, maximum and
billing formula before signing. CDP advertises its settlement signer in the quote;
that signer can vary. The signature's witness binds the recipient and ceiling.
A sponsored Permit2 allowance is limited to the same 3000 micro-USDC ceiling.
The returned invoice and actual on-chain USDC transfer are checked independently
by the Node client and gateway. Solana additionally proves the canonical channel distribution, exact merchant payout and entire unused refund on chain. The ledger reserves the maximum, then atomically
records the actual charge and releases the unused portion. The UI shows six
fractional digits and exposes model cost, markup and settlement fee.

The signed request is submitted once. An uncertain payment leaves a private,
non-secret checkpoint in `~/.sign402/ask-metered/` and blocks subsequent Ask
payments for that account, including after restart. Base reservations retain the existing ledger TTL. Solana reserves are durable SQLite holds that also count against other Solana purchases until settlement or operator reconciliation; they have no automatic expiry. The durable Ask block does not expire automatically. An
operator must reconcile the nonce/transaction, ledger and memory claim before
clearing this block. Never remove it merely to retry a timed-out payment.

The scripts in `deploy/activate-agent.py`, `deploy/activate-context.py` and
`deploy/activate-metered-merchant.py` record the guarded rollout used on this VPS.
They create private backups and verify wallet identities, but their manifests pin
specific before/after hashes. They are historical rollout helpers, not idempotent
commands for deploying the current tree: review and refresh the manifests against
the target deployment before reusing them. Operator activation and the subsequent
buyer-only extension fix have already been applied; see [CHECKS.md](CHECKS.md).

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
