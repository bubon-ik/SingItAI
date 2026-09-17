# Documentation map

Start with the root [README](../README.md) for the product and component map.
This index distinguishes operating instructions from design history; an old
plan is not evidence that a feature is deployed.

## Operations and current integrations

- [Operations](operations.md): observed server revision, deployment and tests.
- [Recovery](recovery-runbook.md) and [release checks](production-beta-checklist.md).
- [Verification record](checks.md): dated test and live-payment evidence.
- [Ledger scope](ledger-v1.md) and [Telegram / Graph / Ledger demo](telegram-graph-ledger-demo.md).
- [Decision API](decide-public-endpoint.md) and [Bazantic experiment](bazantic-experiment.md).
- [Production incident: repeated Crypto News purchases](incidents/2026-09-08-crypto-news-memory.md).

## Historical material

The 86 old implementation plans and design specifications have been removed
from the working tree. They remain available in
[Git history](https://github.com/bubon-ik/SingItAI/tree/29670c2ed644b584768dae7bb9dc629c26c52958/docs/superpowers).
Those documents contain superseded decisions and unfinished proposals; use the
current code, tests and operating instructions to establish feature status.

The original project specification, roadmap, hardware-consent pitch and
August proposal review are also archived in Git history. To restore any of
these files locally, use `git restore --source=84d8ee8 -- <path>`.

The original Firefly/Algorand prototype lives in `sign402-bridge/`,
`payment-executor/`, `demo-resource-server/` and `live-demo/`. Its instructions
include `DEMO_SCRIPT.md` and `scripts/start-local-demo.sh`. These are historical
reference material, not the current production deployment guide. The gateway
still imports shared code from all four directories at startup, so they remain
runtime dependencies even when the original demo routes are not used.
