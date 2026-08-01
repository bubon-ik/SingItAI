# Local Trezor purchase proof

This is an isolated proof for one operator and one Trezor. It runs beside the
working sign402 services; it is not imported, started, or routed by the
production gateway, Hermes, iMessage, or WhatsApp flows. The sidecar listens
only on `127.0.0.1:8111`, uses its own SQLite file, and has no private-key input.
Trezor remains the signer for the fixed Base account at `m/44'/60'/0'/0/0`.

The automated checks use fakes only. They do not contact Trezor Suite,
Bitrefill, Base RPC, or a hardware wallet and do not create an invoice or
broadcast a transaction.

## One-time local setup

From the repository root, create a virtual environment dedicated to this
proof. Do not reuse a production environment:

```bash
cd trezor-sidecar
python3 -m venv .venv
.venv/bin/python -m pip install -e . -e ../sign402-gateway
```

In Trezor Suite Desktop, follow the official
[Suite Desktop MCP instructions](https://docs.trezor.io/trezor-suite/packages/suite-desktop/mcp.html),
enable MCP, and copy its local bearer token. The sidecar uses the fixed local
endpoint `http://127.0.0.1:21340/mcp`; do not expose that endpoint to the
network.

Create private configuration files outside this repository before inserting
any real values:

```bash
mkdir -p "$HOME/.config/sign402-trezor-poc"
cp .env.sidecar.example "$HOME/.config/sign402-trezor-poc/sidecar.env"
cp .env.runner.example "$HOME/.config/sign402-trezor-poc/runner.env"
chmod 600 "$HOME/.config/sign402-trezor-poc/sidecar.env"
chmod 600 "$HOME/.config/sign402-trezor-poc/runner.env"
```

Edit those two private copies, never the tracked examples. Generate an
independent random sidecar token and put the same value in both private files.
Put the Trezor Suite MCP token and a private HTTPS Base RPC URL only in
`sidecar.env`. Keep the Bitrefill operator key only in `runner.env`. Set
`SIGN402_TREZOR_POC_ENABLED=1` in each private file only while running this
proof. Keep `SIGN402_TREZOR_POC_MAX_USD=1.00` for the first manual test and use
an absolute proof-only state path outside the repository.

Never paste tokens, recipient data, payment links, redemption values, or eSIM
activation data into a command, log, issue, chat, or repository file.

## Safe local proof (no purchase)

These steps pair the displayed Base address and approve a reserved test intent.
The reserved intent cannot be converted into a payment. Neither command creates
a Bitrefill client, invoice, payment, or redemption value.

Terminal 1 — start only the isolated loopback sidecar:

```bash
cd trezor-sidecar
set -a
source "$HOME/.config/sign402-trezor-poc/sidecar.env"
set +a
.venv/bin/sign402-trezor-sidecar
```

Terminal 2 — load only the runner configuration, then pair. Confirm on the
Trezor that the displayed address is the dedicated account you intend to use:

```bash
cd trezor-sidecar
set -a
source "$HOME/.config/sign402-trezor-poc/runner.env"
set +a
.venv/bin/sign402-trezor-poc pair
```

Still in Terminal 2, perform the non-spendable typed-intent signature test:

```bash
.venv/bin/sign402-trezor-poc intent-test
```

Stop the sidecar with `Ctrl-C` when finished and return both private env files
to `SIGN402_TREZOR_POC_ENABLED=0`. This procedure does not require or authorize
restarting, reconfiguring, or stopping any production process.

## Operator-only live purchase

> [!WARNING]
> **PRODUCTION SERVICES MUST NOT BE RESTARTED, STOPPED, OR RECONFIGURED.**
> This is a real Base Mainnet USDC payment and may be non-refundable. Use only
> a dedicated low-balance account and a deliberately selected low-value item.
> The live command must display the exact purchase summary before device approval.
> Abort with `Ctrl-C` if the product, package/denomination, quoted
> total, maximum USDC, Base Mainnet network, recipient fields, or expiration is
> not exactly what the operator chose.

Do not run this section until every automated sidecar, production gateway, and
Hermes regression test has passed unchanged, the safe proof above succeeds,
and the operator has separately decided to spend real funds. Do not use a
production user, production database, production wallet, or a production
message conversation for this proof.

1. Fund only the paired dedicated account with slightly more than the selected
   amount in Base USDC and enough Base ETH for gas. Keep the configured cap at
   `1.00` USD for the first test.
2. In Terminal 1, start the isolated sidecar exactly as in the safe procedure.
3. In Terminal 2, load the private runner env exactly as above. Ensure its
   Bitrefill key belongs to the test operator and is not shared with production.
4. Run one explicitly selected catalog item:

   ```bash
   .venv/bin/sign402-trezor-poc buy \
     --product-id REPLACE_WITH_PRODUCT_ID \
     --package-id REPLACE_WITH_PACKAGE_ID \
     --country REPLACE_WITH_COUNTRY_CODE
   ```

5. Enter requested recipient fields only at the hidden prompts. Read the exact
   purchase summary. Continue only if every field matches the intended purchase.
   The first Trezor approval binds that summary; the later Trezor transaction
   screen must also show the exact Base USDC transfer. Reject either device
   prompt on any mismatch.
6. Treat any returned redemption or activation value as bearer value. Save it
   only in the operator's intended secure destination; it is printed once and
   is never written to the proof database.
7. Stop the sidecar, set both private env files back to
   `SIGN402_TREZOR_POC_ENABLED=0`, and retain only the non-secret purchase record
   (invoice ID, product slug, amount, `usdc_base`, and timestamp).

Failures are fail-closed. If a broadcast outcome is reported as ambiguous or
requiring reconciliation, do not retry the purchase or create another invoice;
inspect the dedicated address and existing invoice manually first.

## Automated verification

These commands are safe: the tests inject transports and never use the private
env files.

```bash
cd trezor-sidecar
PYTHONPATH=../sign402-gateway .venv/bin/python -m unittest discover \
  -s tests -p 'test_*.py' -v
```

Production regression suites are run from their existing directories without
changing configuration. A clean isolation check must show no changed path
outside `trezor-sidecar/` and this proof's plan document.
