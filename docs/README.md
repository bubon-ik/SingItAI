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

`superpowers/plans/` and `superpowers/specs/` retain implementation and design
history. They can contain superseded decisions or unfinished proposals; use
the current code, tests and operating instructions to establish feature status.
Their paths are retained because other documents link to them.

The original Firefly/Algorand prototype lives in `sign402-bridge/`,
`payment-executor/`, `demo-resource-server/` and `live-demo/`. Its instructions
include the root `DEMO_SCRIPT.md`, `Hermes Sign402 - Project Spec.md`,
`Hermes Sign402 - Roadmap.md` and the local-demo scripts. These are historical
reference material, not the current production deployment guide. Keep the
modules in place until their imports and demo scripts have been separated.
