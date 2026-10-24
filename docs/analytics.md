# Analytics

Cookieless, privacy-first product analytics: a durable `events` row in `web.db` (the source of
truth, queryable and backfillable) and an optional mirror to a self-hosted Umami instance. No
cookies, no third-party script, no address/email/tx-hash/prompt content ever leaves the server
— see [`sign402_gateway/analytics.py`](../sign402-gateway/sign402_gateway/analytics.py).

## Identity and privacy

- A user is identified only by `uid = sha256(account_id + ANALYTICS_SALT)[:16]`, hex. The raw
  `account_id` (which embeds the wallet address) is never written to the `events` table or sent
  to Umami.
- Props are whitelisted before anything is stored or sent: a key is dropped if any whole
  snake_case word in it is sensitive (`address`, `owner`, `email`, `tx`, `hash`, `prompt`,
  `code`, `key`, `secret`, `token`, `signature`, `invoice`, `recipient`, `phone`), with a small
  explicit allow-list for safe-but-similar-looking keys (`tokens_in`, `tokens_out`, `cost_usd`,
  `amount_usd`, `daily_cap_usd`, `per_purchase_cap_usd`). A string value is also dropped if it is
  shaped like an EVM address, a tx hash, an email, or a base58 Solana address, regardless of its
  key.
- Amounts are sent as USD decimal strings (USDC atomic / 1e6), never raw atomic integers tied to
  a specific on-chain amount that could be correlated back to a transaction.
- `emit()` never raises into caller code: a broken analytics path must never break a sign-in or
  a payment. It also never blocks one noticeably — the events-table write uses a 1-second sqlite
  busy timeout, and the Umami mirror runs on a background thread with a 1-second timeout.

## Events

| Event | Side | Where |
|---|---|---|
| `landing_visit`, `cta_open_app` | Client | landing pages, once an Umami script tag exists (deferred) |
| `app_open` | Client | `website/app/main.js` `start()` |
| `wallet_connect` | Client | `main.js` `connectInjected()`, and the AppKit account-change handler in `start()` |
| `chat_message_sent` | Client | `main.js` `sendMessage()` |
| `pwa_installed`, `push_enabled` | Client | `main.js` `appinstalled` listener, `enablePush()` |
| `sign_in` | Server | `web_api.py` `POST /auth/verify`, after the session is durably created |
| `limits_set` | Server | `agent_allowance.py` / `solana_allowance.py` `setup()`, after the limiter/limits row is committed |
| `approval_signed` | Server | `agent_allowance.py` `_advance_op`/`advance()`, `solana_allowance.py` `submit_wallet()` — a GRANT operation reaching `DONE`/`FAILED` |
| `revoke` | Server | same choke points as `approval_signed`, for a REVOKE operation |
| `pause` | Server | `agent_allowance.py` `pause()` (`trigger=user` from the API route, `trigger=watcher_auto_pause` from `allowance_watcher.py`) |
| `purchase_completed` | Server | `agent_allowance.py` `count_settlement()` (Base), `solana_allowance.py` `spend()` (Solana) |
| `purchase_failed` | Server | `allowance_bitrefill.py` `buy()` (a refusal before paying), `solana_allowance.py` `spend()` (nothing sent) |
| `chat_answer_paid` | Server | `web_agent.py` `record_usage()` (web chat), `web_balance.py` `bill()` (API-key balance), `ask_metered.py`/`ask_solana.py` `pay()` (x402 metered Ask, Base and Solana) |
| `funds_added` | Server | `web_balance.py` `credit_topup()`, once its ledger row is committed |
| `api_key_created` | Server | `web_api_keys.py`, after the key is created |
| `allowance_setup_view`, `limits_saved`, `limits_failed`, `grant_prepared`, `grant_prepare_failed`, `wallet_sign_rejected`, `grant_result` | Client | `main.js` `renderAllowance()`, `createLimiter()`, `walletOperation()` — the setup funnel, both chains (SI-45) |

Setup funnel order: `allowance_setup_view` (once per page load per chain) → `limits_saved` (or `limits_failed` `reason=validation|server`) → `grant_prepared` (or `grant_prepare_failed` `reason=no_sol|other`) → `wallet_sign_rejected` (`reason=rejected|error`, `kind=grant|revoke|fund_gas`) → `grant_result` (`state` = final op state). All carry `chain`; the wallet/result events also carry `kind`. Server-side, a stale or superseded `PREPARED` Solana op now emits `approval_signed`/`revoke` with `state=EXPIRED`, and a refusal for lack of SOL emits them with `state=NO_SOL`.

`repeat_purchase` is not a separate event: filter `purchase_completed` by `purchase_n >= 2`.
`purchase_completed` is not emitted for a settlement of `https://ask.singitai.app/v1/credit` (a
balance top-up — `funds_added` covers it) or of a non-metered `.../v1/chat/completions` (would
double-count with `chat_answer_paid`); see `_settlement_category()` in `agent_allowance.py`.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANALYTICS_SALT` | Salts the account-id hash. **Must be set in production** — without it, `uid` is still one-way but guessable from a known account_id. A warning is logged once per process if it is missing. |
| `UMAMI_URL` | Base URL of the self-hosted Umami instance (e.g. `http://127.0.0.1:3000`). Leave unset to disable the Umami mirror entirely (no network calls, no background thread). |
| `UMAMI_WEBSITE_ID` | The Umami website id to send events under. Required alongside `UMAMI_URL`. |
| `UMAMI_HOSTNAME` | Optional override for the `hostname` field Umami receives (default `app.singitai.app`). |
| `SIGN402_ANALYTICS_DISABLED` | Set to `1` to turn `emit()` into a complete no-op (events table and Umami both skipped). |
| `SIGN402_WEB_DB` | Same variable the web account store already reads; `events` lives in this database. |

Server-side events carry the real client IP and User-Agent to Umami only (as `X-Forwarded-For`
/ `X-Real-IP` and `User-Agent` request headers) — never as stored props, and never into the
`events` table — so a sign-in from a browser does not collapse into one Umami session with every
other server event from the gateway's own loopback address. Payment-internal code never passes
these; only request handlers that already have them (e.g. `POST /auth/verify`) do.

## Running `daily-metrics.py`

```sh
.venv/bin/python scripts/daily-metrics.py --day 2026-10-17
.venv/bin/python scripts/daily-metrics.py --out ~/.sign402/metrics       # writes DIR/YYYY-MM-DD.json too
.venv/bin/python scripts/daily-metrics.py --text                        # short text summary
```

Read-only: every database is opened as `file:PATH?mode=ro` (same pattern as
`scripts/sign402-operator.py`'s `_connect_readonly`), and never written to. A missing database,
or a table an older deploy does not have yet, is reported as `null` for the fields it would have
answered rather than crashing the run. Output is aggregate counts and sums only — it never prints
a wallet address or an account id. The day defaults to yesterday in Europe/Prague (the cron below
runs at 00:10 there); `--day YYYY-MM-DD` overrides it.

DB paths default to `$SIGN402_STATE_DIR/<file>` (falling back to `~/.sign402/<file>`), each
overridable by the same environment variable the gateway itself reads, or by a `--*-db` flag —
see `--help`. The Telegram Bitrefill orders database (a separate demo-dashboard host path on
deployments that have it at all) has **no default**: it is only read when `--bitrefill-orders-db`
or `SIGN402_BITREFILL_COMMERCE_STORE_PATH` names it, and its section reports `null` otherwise.

A few fields need a reporting note, because the underlying tables were not designed for this:

- **`signed_in_today`** is counted from the `events` table (`sign_in`, distinct `uid`), not the
  `sessions` table — session rows are deleted once they expire, so a `sessions` count is only
  ever a lower bound for "today". When the `events` table is not there yet,
  `signed_in_today` is `null` and `signed_in_today_source` says `"unavailable"` rather than
  silently reporting the unreliable `sessions` count as truth (that count is still included, as
  `sessions_rows_today_unreliable`, for debugging).
- **Settlement categories** (`purchases`, `usdc_volume.base`, `active_spenders`,
  `first_purchases`, `repeat_buyers`, `settlements_by_lane`) all exclude the
  `https://ask.singitai.app/v1/credit` resource: it is a balance top-up, already counted in
  web.db's own `credit_topups`/`balance_ledger`, and would double-count as a "purchase"
  otherwise. It is reported on its own as `credit_topup_settlements`. A non-metered
  `.../v1/chat/completions` settlement counts as `chat_answer`, same as the metered one;
  anything else (`bitrefill:invoice:…`, `api.exa.ai/search`, a Bitrefill/tool/data-seller URL,
  …) is `data_tool`.
- **`settlements_by_lane`** splits Base settlements into `web` (`user_id` starts with
  `wallet:`, matching web.db's accounts) and `telegram` (a bare numeric id — the legacy
  Telegram-bot lane, which shares `allowance.db` with the web lane).
- **`abandoned_solana_setups`** (`today` / `total`, in the `solana_allowance` detail section as
  `abandoned_setups`) is account-level, not an `ops` row count: it counts accounts whose `limits`
  row is more than an hour old and that never reached a `GRANT`+`DONE` op. Counting `PREPARED`
  rows instead (as this used to) was misleading — the wallet prompt being closed/rejected or a
  blockhash expiring leaves several `PREPARED` rows for an account that granted moments later
  (SI-45); those now show up as `grant_retries` (`today` / `total`: `PREPARED`/`EXPIRED` `GRANT`
  rows from an account that did eventually grant), alongside `accounts_with_limits` and
  `accounts_granted`. None of these are counted as `errors.ops_failed`/`ops_expired` — nothing
  failed, the setup was simply never finished (or was retried until it was).
- **`errors.ops_failed`/`ops_expired`/`ops_uncertain`** only sum kinds that table actually has:
  Base's `operations.kind` is `GRANT`/`REVOKE`/`PAUSE` (no `FUND_GAS`, no `UNCERTAIN` state);
  Solana's `ops.kind` adds `FUND_GAS` and has no `PAUSE`.
- **`bitrefill_orders`** reports both the raw `by_state` counts and a `by_bucket` grouping
  (`delivered`, `user_rejected`, `pending`, `needs_reconciliation`, `failed`, `expired`); any
  state this script does not recognize yet lands under `other` instead of being dropped or
  raising.

Suggested cron (00:10 Europe/Prague, from the repo root):

```
10 0 * * * cd /path/to/SingItAI && .venv/bin/python scripts/daily-metrics.py --out ~/.sign402/metrics >/dev/null
```

## What is deferred to server-side ops

Setting up Umami itself (Docker container, Postgres, the Cloudflare tunnel hostname, the
`<script>` tag on the landing pages and in `website/app/index.html`, and configuring goals/funnels
in the Umami UI) is VPS/ops work, not code in this repository, and is not done here. Until then:
`UMAMI_URL`/`UMAMI_WEBSITE_ID` are unset, so the Umami mirror is a no-op and every event still
lands durably in the `events` table, queryable today and ready to backfill into Umami later.
