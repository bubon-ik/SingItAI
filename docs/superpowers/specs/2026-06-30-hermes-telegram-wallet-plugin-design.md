# Hermes Telegram Wallet Plugin Design

Date: 2026-06-30

## Goal

Connect the existing managed Base wallet endpoints to the hosted Hermes
Telegram bot. An authorized Telegram user can run `/wallet`,
`/create_wallet`, or `/balance` and receive the gateway's safe Telegram
response without an LLM choosing or supplying the user's identity.

The first deployment remains restricted to the existing Telegram allowlist.
Opening the bot to additional users is an operational configuration change
after the three commands are verified with the operator account.

## Scope

This slice includes:

- A standalone Hermes plugin stored in this repository.
- Telegram slash commands for wallet status, creation, and balance.
- Trusted binding of the Telegram sender ID to each command invocation.
- Authenticated localhost calls to the existing Sign402 Gateway endpoints.
- An idempotent installation path for the VPS checkout.
- Focused unit tests and deployment documentation.

This slice does not include:

- Natural-language wallet actions.
- Private-key export or wallet import.
- Signing, transfers, swaps, or live spending.
- iMessage approval.
- Removing the Telegram allowlist.
- Changes to Hermes core.

## Decision

Use a standalone Hermes plugin rather than a skill, prompt instruction, or
Hermes core patch.

A skill or ordinary model tool would let the LLM construct
`telegramUserId`, which is not an acceptable identity boundary. A Hermes
core patch would work but would create an unnecessary fork and make Hermes
updates harder. The plugin API provides the two required extension points:

- `pre_gateway_dispatch`, which receives the trusted inbound
  `MessageEvent`.
- `register_command`, which exposes deterministic slash-command handlers
  that run without an LLM call.

## Repository Layout

Add the plugin under:

```text
hermes-plugins/sign402-wallet/
  plugin.yaml
  __init__.py
  client.py
  identity.py
  README.md
  tests/
    test_client.py
    test_plugin.py
```

Add an installer:

```text
scripts/install-hermes-wallet-plugin.sh
```

The installer links the repository plugin directory into
`~/.hermes/plugins/sign402-wallet`, enables the plugin through the Hermes
CLI, and leaves secrets untouched.

## Trusted Identity Binding

Hermes invokes `pre_gateway_dispatch` before its normal authorization and
command dispatch. The plugin hook performs no wallet operation at that
stage. It only inspects Telegram wallet commands and stores the trusted
message source in a Python `ContextVar`:

```text
platform
telegram_user_id
telegram_username
chat_id
```

`ContextVar` keeps concurrent Telegram updates isolated by asyncio task.
The command handler later reads that same task-local identity after Hermes
has completed its normal allowlist authorization. The handler refuses to
run when:

- No trusted gateway identity is bound.
- The platform is not Telegram.
- The Telegram user ID is missing or malformed.

The raw command arguments are ignored. A user cannot supply a different
Telegram ID, and the LLM never sees a wallet API parameter.

The binding is cleared after every command handler call, including errors.

## Commands

The plugin registers:

| Telegram command | Gateway endpoint |
| --- | --- |
| `/wallet` | `POST /agent/wallet` |
| `/create_wallet` | `POST /agent/create-wallet` |
| `/balance` | `POST /agent/wallet-balance` |

Hermes normalizes Telegram underscores and plugin command hyphens. The
plugin registers `create-wallet`, which appears as `/create_wallet` in
Telegram.

Each request body is derived only from the bound source:

```json
{
  "telegramUserId": "1045618308",
  "telegramUsername": "AlpskyKnedlik"
}
```

The command handler returns `telegramText` when present. It never returns
encrypted key material, authorization headers, raw exception details, or
an arbitrary upstream response body.

## Gateway Client

The plugin calls the gateway over localhost:

```text
SIGN402_GATEWAY_URL=http://127.0.0.1:8099
```

It authenticates with:

```text
Authorization: Bearer $SIGN402_WALLET_API_TOKEN
```

The client uses a short timeout and a bounded response size. It accepts
only JSON objects. Known failures become stable user-facing messages:

- Missing configuration: wallet service is not configured.
- Connection failure or timeout: wallet service is temporarily unavailable.
- HTTP 401/403: wallet service authentication failed; operator action is
  required.
- Other gateway errors: wallet request failed; details remain in server
  logs.
- Invalid response: wallet service returned an invalid response.

The token is read inside the plugin process and is never included in a
command, prompt, response, or log message.

## Configuration

The Hermes gateway process needs:

```text
SIGN402_GATEWAY_URL=http://127.0.0.1:8099
SIGN402_WALLET_API_TOKEN=<same token used by sign402-gateway>
```

These values belong in the Hermes gateway service environment. The user
must not paste the token into `SOUL.md`, a skill, a prompt, Telegram, or a
shell command that will be stored in chat history.

`plugin.yaml` declares both variables as required so Hermes reports a clear
disabled-plugin state when they are absent.

## Authorization

For the first deployment, the current Telegram allowlist remains:

```text
1045618308
```

The plugin does not replace or bypass Hermes authorization. Unauthorized
senders are rejected by Hermes before a registered wallet command handler
runs.

When the bot is opened later, every newly authorized Telegram user
automatically receives a separate wallet because the gateway's existing
SQLite constraint maps one wallet to each trusted `telegram_user_id`.

## Error Handling

Command handlers are deterministic and do not call the configured LLM.
Every failure returns short Telegram-safe text. Detailed diagnostics use
structured operator logs with:

- operation name;
- HTTP status category;
- exception class;
- no token;
- no private key;
- no full upstream body.

Wallet creation remains idempotent. Repeating `/create_wallet` returns the
existing wallet rather than creating another one.

## Testing

Use test-driven development for the plugin.

Identity tests cover:

- Telegram source binding.
- Rejection without a bound source.
- Rejection for non-Telegram sources.
- Isolation between concurrent asyncio tasks.
- Clearing identity after success and failure.

Client tests cover:

- Correct endpoint, trusted payload, and bearer header.
- `telegramText` extraction.
- Missing environment variables.
- Timeout and connection failures.
- Authorization failure.
- Invalid or oversized JSON responses.
- Redaction of token and upstream bodies from returned errors.

Plugin tests cover:

- Registration of all three commands and the dispatch hook.
- Raw command arguments cannot override the bound Telegram user ID.
- Each command maps to the expected endpoint operation.

After focused tests pass, run the complete Sign402 Gateway suite to ensure
the integration artifact did not disturb the existing wallet backend.

## Deployment

On the VPS:

1. Pull the repository branch.
2. Run `scripts/install-hermes-wallet-plugin.sh` as the `hermes` user.
3. Add `SIGN402_GATEWAY_URL` and `SIGN402_WALLET_API_TOKEN` to the Hermes
   gateway service environment without printing the token.
4. Restart the Hermes gateway.
5. Confirm the plugin is enabled with `hermes plugins list`.
6. Test `/wallet`, `/create_wallet`, and `/balance` from Telegram user
   `1045618308`.
7. Inspect redacted service logs only if a command fails.

The Sign402 Gateway remains bound to `127.0.0.1:8099`; no domain, public
tunnel, or public wallet API is required for this integration.

## Success Criteria

- `/wallet` returns the operator's existing Base address.
- `/create_wallet` is idempotent and returns the same address.
- `/balance` returns the address plus balances or the existing
  `balanceUnavailable` response.
- The three commands perform no LLM call.
- User-provided text cannot select another Telegram user ID.
- The wallet token and private key never appear in Telegram or plugin
  output.
- The Hermes and Sign402 services continue running after SSH logout and
  server reboot.
