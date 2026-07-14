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
| Python dependency consistency | Global interpreter reports `wheel 0.47.0` requires missing `packaging`; gateway direct dependencies are current | LIMITATION — repeat `pip check` in production venv |

## Findings

| ID | Severity | Component | Evidence | Exploit scenario | Status | Fix/mitigation | Verification | Residual risk |
|---|---|---|---|---|---|---|---|---|
| SEC-001 | High | CDP HTTP client dependency | `npm audit`: `form-data 4.0.5`, GHSA-hmw2-7cc7-3qxx / CWE-93 | If attacker-controlled multipart names reach the vulnerable encoder, CRLF injection can alter an outbound request | FIXED | Lock tree now resolves `form-data 4.0.6` | `npm audit`: zero; CDP `13/13`; gateway `431/431`; plugin `118/118`; risk-check `2/2` | Package remains transitive through Axios; future lock updates must retain `>=4.0.6` |
| SEC-002 | High | CDP WebSocket dependency | `npm audit`: nested `ws 8.20.1` through `viem 2.52.2`, GHSA-96hv-2xvq-fx4p / CWE-400 | A hostile WebSocket peer can exhaust memory with fragmented frames | FIXED | Lock tree now resolves `viem 2.55.2`; all `ws` instances dedupe to `8.21.0` | `npm audit`: zero; CDP `13/13`; gateway `431/431`; plugin `118/118`; risk-check `2/2` | CDP primarily uses HTTP; keep npm advisory checks in release gates |

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
