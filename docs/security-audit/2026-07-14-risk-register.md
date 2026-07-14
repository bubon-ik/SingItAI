# Sign402 security audit — 2026-07-14

## Scope

Code, Ubuntu VPS, Cloudflare Tunnel, Telegram/Hermes, Photon iMessage, Meta
WhatsApp Cloud, managed Base wallets, and Bitrefill.

## Baseline

| Check | Evidence | Result |
|---|---|---|
| Gateway unit tests | `431` tests, Python 3.14, exit `0` | PASS |
| Hermes Sign402 plugin tests | `118` tests in a clean environment, exit `0` | PASS |
| CDP/x402 tests | `13` Node tests, exit `0` | PASS |
| Risk-check tests | `2` Node tests, exit `0` | PASS |
| Tracked secret-like paths | Only five `*.env.example` files | PASS |
| Secret content scan | Two placeholder assignments in README/tests; no credential value printed | PASS |
| Historical secret filenames | No `.env`, `.pem`, `.key`, Bitrefill-key, or mainnet-wallet file in Git history | PASS |
| Local secret-file ignore rules | Runtime `.env`, wallet, and Bitrefill files are ignored | PASS |
| CDP production dependency audit | `npm audit --omit=dev --audit-level=moderate`: zero vulnerabilities after lock update | PASS |
| Gateway Python vulnerability audit | `pip-audit 2.10.1` against `pyproject.toml`: no known vulnerabilities | PASS |
| Python static analysis | Bandit scanned `17,533` production lines: `0` High, `12` Medium; SQL-construction findings use constant allowlists/placeholders; URL findings use fixed/operator-configured upstreams | PASS WITH MEDIUM HARDENING |
| Python dependency consistency | Global interpreter reports `wheel 0.47.0` requires missing `packaging`; gateway direct dependencies are current | LIMITATION — repeat `pip check` in production venv |

## Findings

| ID | Severity | Component | Evidence | Exploit scenario | Status | Fix/mitigation | Verification | Residual risk |
|---|---|---|---|---|---|---|---|---|
| SEC-001 | High | CDP HTTP client dependency | `npm audit`: `form-data 4.0.5`, GHSA-hmw2-7cc7-3qxx / CWE-93 | If attacker-controlled multipart names reach the vulnerable encoder, CRLF injection can alter an outbound request | FIXED | Lock tree now resolves `form-data 4.0.6` | `npm audit`: zero; CDP `13/13`; gateway `438/438`; plugin `119/119`; risk-check `2/2` | Package remains transitive through Axios; future lock updates must retain `>=4.0.6` |
| SEC-002 | High | CDP WebSocket dependency | `npm audit`: nested `ws 8.20.1` through `viem 2.52.2`, GHSA-96hv-2xvq-fx4p / CWE-400 | A hostile WebSocket peer can exhaust memory with fragmented frames | FIXED | Lock tree now resolves `viem 2.55.2`; all `ws` instances dedupe to `8.21.0` | `npm audit`: zero; CDP `13/13`; gateway `438/438`; plugin `119/119`; risk-check `2/2` | CDP primarily uses HTTP; keep npm advisory checks in release gates |
| SEC-003 | Medium | Gateway HTTP parser | `_read_json` trusted arbitrary/negative `Content-Length` and could call `read(-1)` | A local caller, or a future accidental public route, can make the process buffer an unbounded request | FIXED | Reject malformed/negative lengths with `400`; reject bodies over 1 MiB with `413` before route dispatch | Three focused regressions plus gateway `438/438` | Gateway remains loopback-only; systemd memory limits are audited separately |
| SEC-004 | Medium | External HTTP clients | Photon, Bitrefill, Bankr, x402 resource, and Base RPC paths contained unbounded `response.read()` calls | A compromised/misbehaving upstream can exhaust gateway/Hermes memory | FIXED | Bound gateway upstream responses to 1 MiB and Photon responses to 256 KiB; oversized failures use stable errors without embedding the oversized body | Five focused regressions; gateway `438/438`; plugin `119/119`; CDP `13/13`; risk-check `2/2` | TLS and fixed/operator-configured upstream URLs remain required; operators must not raise limits without load testing |

## Route/auth matrix

| Route group | Guard | Security property |
|---|---|---|
| `/health`, Bitrefill search/list/product details | Public-safe, loopback service | No wallet mutation, private key, redemption, or approval decision |
| `/agent/create-wallet` | Wallet service bearer | Creates/rotates per-user access material; private key fields are stripped |
| User wallet, balance, limits, withdrawal, LLM, user x402, quote and wallet-Bitrefill routes | Wallet bearer **and** `X-Sign402-User-Token` | Token resolves authoritative Telegram user; mismatched body identity is rejected |
| `/agent/imessage/*` | Independent Photon/approval bearer | Hermes transports are trusted to bind sender identity and approval channel |
| `/agent/test-imessage-approval` | Feature flag off by default plus Photon bearer | No-funds probe absent from normal production route list |
| `/internal/fulfill-bitrefill`, `/internal/prepare-bitrefill-settlement` | Independent service secret | Fulfillment token is additionally hash-bound to a stored quote; legacy fulfillment defaults off |
| Legacy Firefly/operator routes | Feature flag off by default plus independent operator bearer | Disabled routes return `404` in production |

## Approval-channel matrix

| Test | iMessage | WhatsApp | Evidence |
|---|---|---|---|

## Production changes and rollback

| Change | Before | Command | Verification | Rollback |
|---|---|---|---|---|

## Final gates

- [ ] No open Critical findings
- [ ] No open High findings
- [ ] iMessage approval path verified
- [ ] WhatsApp approval path verified
- [ ] Public exposure verified
- [ ] Secret and log redaction verified
- [ ] Live purchase at or below USD 0.10 verified
