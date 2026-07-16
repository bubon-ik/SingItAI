# Sign402 security audit — 2026-07-14

## Scope

Code, Ubuntu VPS, Cloudflare Tunnel, Telegram/Hermes, Photon iMessage, Meta
WhatsApp Cloud, managed Base wallets, and Bitrefill.

## Baseline

| Check | Evidence | Result |
|---|---|---|
| Gateway unit tests | `444` tests, exit `0` | PASS |
| Hermes Sign402 plugin tests | `122` tests in a clean environment, exit `0` | PASS |
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
| SEC-005 | Low | Public WhatsApp tunnel | Public `/health` returns internal diagnostics including the Phone Number ID and configuration flags; webhook responses do not advertise HSTS | Reconnaissance reveals implementation metadata that is not needed by Meta | CONFIRMED | Restrict the Cloudflare published route to `/whatsapp/webhook` (or protect `/health`) and enable HSTS at the zone edge after validating all HTTPS hosts | External probe: TLS valid; `/health` `200`; root `404`; bad verify token `403`; unsigned POST `401` | Phone Number ID is not a credential; webhook signature and verify-token checks already fail closed |
| SEC-006 | Medium | Hermes WhatsApp listener | VPS socket audit showed Hermes on `0.0.0.0:8090`; UFW blocked the port | A future firewall rule or colocated untrusted process could reach an origin intended only for Cloudflare Tunnel | FIXED | Set `WHATSAPP_CLOUD_WEBHOOK_HOST=127.0.0.1` and restart with the readiness-aware Hermes CLI | `ss` shows only `127.0.0.1:8090`; local and tunneled health return `200`; Hermes log confirms the loopback listener | Cloudflare remains the only intended public ingress; retain UFW default-deny |
| SEC-007 | Medium | systemd service isolation | `sign402-gateway`, `hermes-gateway`, and `cloudflared` initially reported no sandboxing and unlimited memory | A compromised process retained a wider filesystem/kernel attack surface and could exhaust host memory | FIXED | Added compatible drop-ins: private tmp, no-new-privileges, read-only system paths, restrictive umask, task limits, and memory caps (1 GiB Sign402, 2 GiB Hermes, 256 MiB cloudflared); cloudflared additionally has strict system/home and empty capabilities | All services active; local and public health `200`; effective properties show the intended limits | Hermes user service cannot apply kernel capability directives, so those remain omitted there; home stays writable for state and Photon |
| SEC-008 | High | Internet-facing SSH | Effective SSH config reported `PermitRootLogin yes`, `PasswordAuthentication yes`, `MaxAuthTries 6`, and `X11Forwarding yes` on public port 22; logs counted thousands of failed attempts | Credential stuffing or a stolen root password can obtain direct host control | FIXED | Installed the existing Mac ED25519 public key for `hermes`; disabled root/password/interactive auth and X11; reduced attempts to 3; limited users to `hermes` | `sshd -t` passed; effective config reports all hardened values; a fresh key-only session returned `ssh_hardened_ok` | Protect and back up the Mac private key; keep provider console as recovery and fail2ban active |
| SEC-009 | High | Ubuntu kernel lifecycle | Host was running `6.8.0-106-generic` while `/var/run/reboot-required` existed; OpenSSH security updates were pending | Installed kernel/security fixes were not active until reboot | FIXED | Installed Ubuntu/OpenSSH updates and performed a controlled reboot after backup and service-enable checks | Host now runs `6.8.0-134-generic`; no reboot required; all three services active; internal/public health `200`; SSH hardening persisted | Continue unattended security updates and schedule reboot-required maintenance promptly |
| SEC-010 | High | Cloudflare Tunnel credential storage | `/etc/systemd/system/cloudflared.service` was `0644` and contained the remotely managed tunnel token in `ExecStart` | Any local account or local file-read primitive could copy the token and impersonate the connector | FIXED | Moved the token to `/etc/cloudflared/tunnel.token` with `0600 root:root`; changed the non-secret unit to `--token-file`; rotated the token in Cloudflare and restarted the connector | Unit contains no plaintext `--token`; token file is `0600`; after rotation/restart the tunnel reached public health `200` | A root compromise can still read service credentials; retain Cloudflare MFA and periodic rotation |
| SEC-011 | Low | Approval-channel selection | Existing Telegram `Connect iMessage` / `Connect WhatsApp` controls always entered pairing, even when that channel was already linked | A user with both channels linked could receive an “already linked” response instead of selecting the intended approval channel, causing approval availability and support failures | FIXED | Existing controls now select an already-linked channel first and start pairing only when no link exists; selection changes only the preference row and preserves both encrypted links | Gateway `444/444`; plugin `122/122`; production button selected WhatsApp, both links remained present, and no new pairing was created | The control label still says “Connect”; help text clarifies “Select or link” |

## Route/auth matrix

| Route group | Guard | Security property |
|---|---|---|
| `/health`, Bitrefill search/list/product details | Public-safe, loopback service | No wallet mutation, private key, redemption, or approval decision |
| `/agent/create-wallet` | Wallet service bearer | Creates/rotates per-user access material; private key fields are stripped |
| User wallet, balance, limits, withdrawal, LLM, user x402, quote and wallet-Bitrefill routes | Wallet bearer **and** `X-Sign402-User-Token` | Token resolves authoritative Telegram user; mismatched body identity is rejected |
| `/agent/approval-channel/select-existing` | Wallet service bearer | Selects only a channel already linked to the authoritative Telegram user; does not create, reveal, or replace link secrets |
| `/agent/imessage/*` | Independent Photon/approval bearer | Hermes transports are trusted to bind sender identity and approval channel |
| `/agent/test-imessage-approval` | Feature flag off by default plus Photon bearer | No-funds probe absent from normal production route list |
| `/internal/fulfill-bitrefill`, `/internal/prepare-bitrefill-settlement` | Independent service secret | Fulfillment token is additionally hash-bound to a stored quote; legacy fulfillment defaults off |
| Legacy Firefly/operator routes | Feature flag off by default plus independent operator bearer | Disabled routes return `404` in production |

## Approval-channel matrix

| Test | iMessage | WhatsApp | Evidence |
|---|---|---|---|
| Existing link retained | PASS | PASS | Production database retained both channel links after changing the preference |
| Existing Telegram connect control selects channel | PASS | PASS | iMessage was selected through the authenticated selector; the existing WhatsApp button recorded `channel_selected` without creating a new pairing |
| Fresh no-funds approval delivery | PASS | PASS | iMessage approval reached `approved`; WhatsApp template delivery succeeded |
| Decision callback | PASS | PASS | iMessage decision was accepted; WhatsApp quick-reply callbacks reached Hermes and stale decisions failed closed with `404` after the two-minute TTL |
| Webhook signature rejection | N/A | PASS | Invalid public signature returned `401`; `accepted` stayed `2`; `rejected_signature` increased from `1` to `2`; empty response contained no stack, path, or credential data |
| Production purchase approval | PASS | PASS | Previous production approvals and the USD `0.10` Bitrefill purchase completed through the configured approval paths |

## Production changes and rollback

| Change | Before | Command | Verification | Rollback |
|---|---|---|---|---|
| WhatsApp origin bind | `0.0.0.0:8090` | Set `WHATSAPP_CLOUD_WEBHOOK_HOST=127.0.0.1`; restart Hermes | Local and tunneled health `200`; `ss` shows loopback only | Restore the prior env value and restart Hermes |
| Cloudflare token storage | Token embedded in world-readable unit | Move token to root-only token file and use `--token-file`; rotate token | Tunnel healthy; token absent from unit; token file `0600` | Generate a new connector token and reinstall the service |
| SSH hardening | Root/password authentication enabled | Install ED25519 key; disable root/password/interactive auth; allow only `hermes` | Fresh key-only login succeeded; effective `sshd` configuration persisted after reboot | Use provider console to amend the hardening drop-in |
| Service isolation | No sandboxing or resource caps | Add compatible systemd security drop-ins | All services active with effective memory/task/umask protections | Remove the relevant drop-in and reload systemd |
| Existing approval-channel selection | Connect controls always started pairing | Deploy authenticated selector and update existing Telegram handlers | `444` gateway and `122` plugin tests; live selection preserved both links | Revert commits `fa8913f` and `e58e5ed`, redeploy plugin, restart services |

## Final gates

- [x] No open Critical findings
- [x] No open High findings
- [x] iMessage approval path verified
- [x] WhatsApp approval path verified
- [x] Public exposure verified
- [x] Secret and log redaction verified
- [x] Live purchase at or below USD 0.10 verified
