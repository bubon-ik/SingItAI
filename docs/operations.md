# Operations

How the running service is deployed, restarted and tested. For what the
product is, see the [README](../README.md). For incident recovery see
[recovery-runbook.md](recovery-runbook.md); for the pre-release gate see
[production-beta-checklist.md](production-beta-checklist.md).

## Production layout

Everything runs on one VPS as `hermes@164.68.104.44`, from the checkout at
`~/apps/sign402` on branch `x402Bnkr`.

| Piece | How it runs | Notes |
| --- | --- | --- |
| `sign402-gateway` | system unit, port 8099 | Wallets, limits, approvals, orders. Needs `sudo` to restart |
| `hermes-gateway` | **user** unit | The Telegram bot. `systemctl --user`, no sudo |
| Website | Cloudflare | Deploys itself from GitHub; nothing to do on the VPS |

The site is not served from the VPS — nginx and caddy are installed but
inactive, and nothing listens on 80 or 443.

Hermes reads `~/.hermes/.env` and loads plugins listed in
`~/.hermes/config.yaml`. A plugin present in `~/.hermes/plugins/` but missing
from that list is silently ignored — no log line, no error.

## Deploying

**Website:** push to GitHub. Cloudflare rebuilds on its own. Verify by fetching
a string you just changed:

```bash
curl -s https://singitai.app | grep -c "some-string-from-your-change"
```

**Gateway:** changes under `sign402-gateway/` do not travel on their own.

```bash
ssh -t hermes@164.68.104.44 'cd ~/apps/sign402 && git pull && sudo systemctl restart sign402-gateway && sleep 5 && systemctl is-active sign402-gateway && curl -s -o /dev/null -w "health: HTTP %{http_code}\n" http://127.0.0.1:8099/health'
```

`ssh -t` is required: without a TTY `sudo` cannot prompt for the password and
the command fails with "a terminal is required to read the password".

**Telegram plugin:** changes under `hermes-plugins/` need the bot restarted:

```bash
ssh hermes@164.68.104.44 'systemctl --user restart hermes-gateway'
```

Restarting the bot interrupts any purchase mid-flow. Prefer a quiet moment.

## Running the tests

The gateway suite needs an interpreter that has `mcp`, `httpx`, `toons` and
`cryptography`. The system `python3` does not, and `sign402-gateway/` has no
virtualenv of its own — use the sibling project's:

```bash
cd sign402-gateway
../payment-executor/.venv/bin/python -m unittest \
  tests.test_bankr_llm_purchase tests.test_bankr_swap tests.test_base_balances \
  tests.test_bitrefill_client tests.test_bitrefill_config tests.test_bitrefill_mcp \
  tests.test_bitrefill_quote tests.test_bitrefill_runner tests.test_commerce_store \
  tests.test_diagnostics tests.test_discard_legacy_fulfillment_tokens \
  tests.test_gateway_server tests.test_goplausible_adapter tests.test_imessage_approvals \
  tests.test_real_rate_pricing tests.test_secure_state tests.test_user_wallets \
  tests.test_whatsapp_cloud
```

`unittest discover -s tests` fails with "Start directory is not importable";
list the modules explicitly. pytest is not installed.

A `RuntimeError: WALLET-FUNDING-SECRET-MARKER` in the output is a deliberate
fixture checking that secrets do not reach logs. It is not a failure.

## Where state lives

On the VPS, under `~/.sign402/`:

| File | Contents |
| --- | --- |
| `user-wallets.db` | Encrypted per-user Base wallet keys |
| `imessage-approvals.db` | Approval channel pairings and pending approvals |
| `bankr-llm.db` | LLM credit purchases |
| `user-spend-limits.json` | Per-user spending limits |

`sqlite3` is not installed on the server; read these with `python3` one-liners.
Back them up before anything that touches payment state — see the recovery
runbook.

## Third-party surfaces that have broken before

**Bitrefill ships breaking MCP changes without notice.** On 2026-07-30 the
key-in-path endpoint started returning HTTP 410; on 2026-08-03 `search-products`
gained a required `intent` field and every catalog call without it was
rejected. Both took the catalog down.

The second one surfaced as "Wallet request failed" for some countries while
others kept working, because cached catalog snapshots survived while live
fetches failed. If browsing breaks unevenly by country, read the tool's real
schema before anything else:

```python
tools = await session.list_tools()   # inspect tool.inputSchema["required"]
```

The wallet plugin logs only the HTTP status and shows the user "Please try
again or contact the operator", so the gateway's actual error message never
reaches the log. Expect to reproduce the call by hand to see the reason.

## Local development

The gateway runs locally against the same code:

```bash
cd sign402-gateway
SIGN402_APPROVAL_PROVIDER=disabled \
  ../payment-executor/.venv/bin/python -m sign402_gateway --port 8099
```

`SIGN402_APPROVAL_PROVIDER` defaults to `firefly`, which looks for a serial
device and fails without one. `disabled` is what production runs, and it makes
the legacy `/approve-*` endpoints refuse rather than reach for hardware.

To let the server-side bot reach a local gateway, expose only the gateway:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

If macOS refuses to run scripts or virtualenvs inside `Documents` with
`Operation not permitted`, grant the terminal Full Disk Access.
