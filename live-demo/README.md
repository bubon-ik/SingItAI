# Shared payment flow helpers

The gateway still imports `sign402_live.flow` and `sign402_live.http_resource`.
They provide payment commitments, proof encoding and the legacy HTTP resource
client. Keep this package available alongside the gateway.

The bundled demo resource server, HTML dashboard and presentation launcher
have been removed. The original presentation instructions remain in
[Git history](https://github.com/bubon-ik/SingItAI/tree/d4380f431b04633f27bc726178fd24a3836a7934/live-demo).

The retained `sign402-live-demo` CLI requires an independently supplied resource
compatible with the legacy probe protocol. Set `--resource-url` to that service;
there is no bundled server listening on port 8090 anymore. Payment execution
still requires the separately configured `payment-executor` package and its
runtime credentials. CLI execution can make a real payment.

For current service setup and unit tests, see
[operations](../docs/operations.md). For the maintained Graph/Ledger demo, see
[its runbook](../docs/telegram-graph-ledger-demo.md).
