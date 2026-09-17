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

- [ETHOnline 2026 submission](ethonline-submission.md): event scope, implementation links and dated verification results.

The 86 old implementation plans and design specifications have been removed
from the working tree. They remain available in
[Git history](https://github.com/bubon-ik/SingItAI/tree/29670c2ed644b584768dae7bb9dc629c26c52958/docs/superpowers).
Those documents contain superseded decisions and unfinished proposals; use the
current code, tests and operating instructions to establish feature status.

The original project specification, roadmap, hardware-consent pitch and
August proposal review are also archived in Git history. To restore any of
these files locally, use `git restore --source=84d8ee8 -- <path>`.

The demo resource server, HTML dashboard, presentation script and local demo
launcher have been removed. They remain in
[Git history](https://github.com/bubon-ik/SingItAI/tree/d4380f431b04633f27bc726178fd24a3836a7934).
The gateway still imports shared code from `sign402-bridge/`,
`payment-executor/` and `live-demo/`; these remain runtime dependencies.

Existing state in the historical `demo-dashboard/` directory is deliberately
preserved. Its database paths and backup/recovery commands are unchanged.
The removed dashboard HTML is unrelated to retention of order records.
