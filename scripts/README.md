# Demo Scripts

## Start Local Demo

Run all local services in one terminal:

```bash
cd "/Users/mp/Documents/Berlin Hack"
bash scripts/start-local-demo.sh
```

The script starts:

- x402 resource server on `http://127.0.0.1:8090`;
- Sign402 Gateway on `http://127.0.0.1:8099`;
- dashboard server on `http://127.0.0.1:8100`.

It auto-detects a single `/dev/cu.usbmodem*` Firefly port. If more than one is connected, set it explicitly:

```bash
FIREFLY_PORT=/dev/cu.usbmodem11301 bash scripts/start-local-demo.sh
```

For server-side Hermes short mode, run one gateway tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8099
```

Then give Hermes the gateway tunnel URL and tell it to call `/agent/buy-probe`.

The resource server stays local. The gateway calls it directly.

For low-level protocol debugging, you can still expose the resource server separately:

```bash
cloudflared tunnel --url http://127.0.0.1:8090
```

Logs are written to:

```text
.demo-logs/
```

## Hosted Hermes Telegram Wallet Plugin

The VPS deployment can expose the managed Base wallet endpoints through
deterministic Hermes Telegram commands without giving the LLM control of a
Telegram user ID.

Configure `SIGN402_GATEWAY_URL` and `SIGN402_WALLET_API_TOKEN` in the
Hermes gateway environment, then install the repository plugin:

```bash
cd ~/apps/sign402
./scripts/install-hermes-wallet-plugin.sh
hermes gateway restart
```

See `hermes-plugins/sign402-wallet/README.md` for the secure configuration,
allowlist-first test flow, and diagnostics.
