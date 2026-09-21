# Operations

How the running service is deployed, restarted and tested. For what the
product is, see the [README](../README.md). For incident recovery see
[recovery-runbook.md](recovery-runbook.md); for the pre-release gate see
[production-beta-checklist.md](production-beta-checklist.md).

## Production layout

**September 21, 2026 update:** the existing bot now runs
[`adca498`](https://github.com/bubon-ik/singit-solana/commit/adca498) on
`release/typesafe-20260921`, adding optional TypeSafe routing before Venice
onboarding. The operator enabled it in the private Hermes environment.
Only the Telegram service restarted; the payment gateway code and process
were unchanged. All 95 Base wallets, 1 Solana wallet, configuration values and
purchase history were verified intact after a private backup. All 375 plugin
tests passed in the server runtime, and seven live TypeSafe smoke cases passed.
See [deployment details and limits](natural-language-assistant.md#existing-bot-deployment--september-21-2026).
The following September 20 account records the underlying Solana-chat release.

The gateway checkout is `~/apps/sign402` on the VPS `hermes@164.68.104.44`.

Updated on 20 September 2026 at the owner's explicit request: the existing bot
runs this repository's `venice-solana` release at
`337e77785edd658146b42149f2068335ddef5cdf`. Later documentation commits do not
imply a new runtime deployment. Both service units are active, health returns
HTTP 200, and Telegram confirms the existing bot identity and native Menu.

Before switching, 1,289 gateway, 343 Telegram and 39 Solana tests passed on the
VPS. The dedicated Solana runtime is Node 24.21.0 at
`~/.local/share/singit-node24/bin/node`; Hermes retains its existing Node 22.
All 95 Base wallets, the Solana wallet and purchase history were preserved.
Authenticated AI network selection and a live Venice balance signed by the
owner's managed Solana wallet passed. No real Venice payment was submitted.
[Deployment evidence and remaining acceptance](venice-solana-agent.md#existing-vps-bot-updated--september-20-2026).

The checkout's `origin` is now `https://github.com/bubon-ik/singit-solana.git`;
the former remote is retained as `base-origin`. The previous deployment was
`fix-crypto-news-memory` at `21dc310f9b115198e4f320cf110add7ebb54f2e7`. Its local
Graph plugin changes are included in the new code. Its local lockfile only
removed peer metadata; the new lockfile was installed and tested separately.
The old edits remain in a named Git stash and a private deployment backup.

The existing bot credentials, gateway environment, encrypted Base wallet rows,
approval state and purchase history were retained. A private backup under
`~/sign402-backups/` includes the repository, local edits, runtime databases and
JSON files (including `user-purchases.json`), bot configuration and the gateway's
effective environment. Keep these backups private. This was a replacement of
the existing deployment, not a second instance sharing its wallet state.

For the earlier September 18 replacement, the VPS passed 1,245 gateway tests, 302 plugin tests with its
installed PTB 22.6, and 46 CDP helper tests. The existing `spending-memory`
installation already matched the pinned revision. After switching, the Base
wallet records and purchase history were verified unchanged, and the new
purchase-history endpoint rejected an unauthenticated request with HTTP 401.

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

Before updating, record the current commit and preserve local code changes.
Back up runtime state using the recovery runbook. Deploy a reviewed commit that
includes both the production fixes and the intended new features; do not replace
the checkout with the default branch solely because it is the GitHub default.
Install the dependencies from that commit with the interpreter used by the
service. In particular, `git pull` alone does not update the pinned
`spending-memory` package. Then restart the gateway during a quiet period.

`sudo systemctl restart sign402-gateway` requires a TTY when sudo needs a password;
use `ssh -t` for that operator step. Verify after restarting:

```bash
ssh hermes@164.68.104.44 'systemctl is-active sign402-gateway && curl --fail --max-time 5 -s -o /dev/null -w "health: HTTP %{http_code}\n" http://127.0.0.1:8099/health'
```

The September 18 replacement used SIGTERM on the gateway process owned by
`hermes`, after checking for active spend reservations and pending approvals and
stopping the Telegram bot. The unit's verified `Restart=always` policy restarted
it with the existing root-managed environment. No sudo policy or service unit
was changed. The original root-owned environment file was not modified; its
effective running environment was saved privately for recovery.

**Telegram plugin:** changes under `hermes-plugins/` need the bot restarted:

```bash
ssh hermes@164.68.104.44 'systemctl --user restart hermes-gateway'
```

Restarting the bot interrupts any purchase mid-flow. Prefer a quiet moment.

## Running the tests

Create an environment for this checkout and install its declared dependencies.
Use Python 3.12 to match CI. From the repository root:

```bash
python3.12 -m venv sign402-gateway/.venv
sign402-gateway/.venv/bin/python -m pip install -e ./sign402-gateway
(cd sign402-gateway && .venv/bin/python -m unittest discover -s tests)
(cd hermes-plugins/sign402-wallet && ../../sign402-gateway/.venv/bin/python -m unittest discover -s tests)
```

`unittest discover -s tests` works from each component directory; pytest is not
required. An existing sibling environment can be reused only after installing
this checkout's dependency versions into it.

For Node unit tests, from the repository root:

```bash
(cd cdp-x402-service && npm ci --ignore-scripts && npm test)
(cd singit-risk-check && npm ci --ignore-scripts && npm test)
(cd tools/ledger-approve && npm ci --ignore-scripts && npm test)
```

The Ledger unit tests mock the device and do not need native USB install scripts.
For actual device use, follow the Ledger runbook's full installation instructions.
CI runs Node tests independently of dependency audits so an advisory does not
hide the test results. The security gate runs on pushes to `main` and
`x402Bnkr`, on pull requests, weekly, and on manual dispatch.

A `RuntimeError: WALLET-FUNDING-SECRET-MARKER` in the output is a deliberate
fixture checking that secrets do not reach logs. It is not a failure.

## Where state lives

The retired dashboard HTML and demo resource server are no longer shipped.
Existing files in `~/apps/sign402/demo-dashboard/` remain gateway runtime state:
`bitrefill-orders.sqlite3`, `latest-run.json`, `agent-state.json` and
`user-purchases.json`. Their default paths have not changed, and a normal Git
update preserves these ignored files. Do not run `git clean -fdx` on a deployment.
Backup and recovery scripts continue to use the historical locations.

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
  .venv/bin/python -m sign402_gateway --port 8099
```

`SIGN402_APPROVAL_PROVIDER` defaults to `firefly`, which looks for a serial
device and fails without one. `disabled` makes
the legacy `/approve-*` endpoints refuse rather than reach for hardware.

To let the server-side bot reach a local gateway, expose only the gateway:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

If macOS refuses to run scripts or virtualenvs inside `Documents` with
`Operation not permitted`, grant the terminal Full Disk Access.
