# Exposing `/v1/decide` publicly, without exposing the wallets

Bazantic fetches the API Base URL itself during `Analyze`, so the endpoint has
to answer over public HTTPS. Today nothing on the VPS listens on 80 or 443 and
the gateway binds `127.0.0.1:8099` — which is the right posture for a service
holding custodial keys, and this document does not change it.

The whole point of what follows is that **one path becomes public and nothing
else does.**

---

## What is actually being exposed

`POST /v1/decide` and `GET /v1/journal`. Both are read-only with respect to
money: no wallet is touched, no budget is held, nothing on the payment path is
called.

They are unauthenticated on purpose. An agent has to be able to ask before it
spends, and a key would defeat that.

Everything else the gateway serves — `/agent/buy-tool`, `/agent/wallet`,
`/agent/withdraw`, the Bitrefill and iMessage routes — moves real USDC and
**must not be reachable**. The tunnel config below refuses them at Cloudflare,
before a request ever reaches the box. Step 5 verifies that rather than trusting
it.

## Why a tunnel and not caddy

Caddy is installed on the VPS and inactive. Turning it on means opening 80 and
443 on a host that holds customer wallet keys, and every path the gateway serves
becomes one misconfigured `reverse_proxy` line away from public.

`cloudflared` makes an outbound connection instead. No inbound port opens, the
firewall stays shut, and the path allow-list lives in Cloudflare's edge rather
than in a local file whose default is "forward everything".

---

## 1. Deploy the branch — read this before running it

Check what the box is actually on before reasoning about the risk. As of
6 September it runs the branch `spending-memory` at `0bae552` — **not**
`x402Bnkr`, which is what an earlier draft of this document assumed:

```bash
ssh hermes@164.68.104.44 'cd ~/apps/sign402 && git rev-parse --abbrev-ref HEAD && git log -1 --format="%h %s"'
```

That matters, because four of the five commits that change how live customer
payments are decided are **already in production**:

```
b92f904  memory: decide with Spending Memory at the spend chokepoint    on the box
751c1a1  backfill: write to the database the gateway actually reads     on the box
0bae552  telegram: stop promising an approval that may never come       on the box
72bc7c1  launcher: stop requiring FIREFLY_PORT to start                 on the box
1ca72b4  memory: honour the env mapping the caller passes, not the flag NOT on the box
```

So deploying `ethonline` does not ship the payment-decision change — that
shipped already. It adds `1ca72b4`, which only affects the path where an
explicit environment mapping is passed in, plus two new modules (`decide.py`,
`keyring.py`) and the routing for them. The diff to `server.py` is additive:
two routes, one policy built at start-up, and `install_master_key()`, which with
the key ring off returns the value that was already in the environment.

It is still a restart of a custodial payment system, and it should still be done
deliberately. It is not the much larger decision the first draft described.

The kill switch is the way back and it needs no deploy:

```bash
SIGN402_SPENDING_MEMORY_ENABLED=0   # every payment asks its owner, as before
```

```bash
ssh -t hermes@164.68.104.44 'cd ~/apps/sign402 && git fetch && git checkout ethonline && git pull --ff-only && sudo systemctl restart sign402-gateway && sleep 5 && systemctl is-active sign402-gateway && curl -s -o /dev/null -w "health: HTTP %{http_code}\n" http://127.0.0.1:8099/health'
```

## 2. Configure the decide endpoint

In `/etc/sign402-gateway.env`, mode `0600`:

```bash
# What an agent may spend per UTC day without asking. No default exists on
# purpose: a library that invents a spending limit invents it wrong.
SIGN402_DECIDE_AUTONOMY_CAP=5

# Its own database. Not the custodial one — see the warning below.
SIGN402_DECIDE_MEMORY_DB=/home/hermes/.sibyl-memory/decide.db
```

> **This must not be the same file as `SPENDING_MEMORY_DB`.** The endpoint is
> unauthenticated, and every call writes a journal line. Rule 6 counts a
> merchant's escalations across every owner out of that journal, so three
> anonymous requests quoting an absurd price for a real merchant block the next
> genuine customer for an hour. The gateway refuses to start if the two paths
> match, and there is a test for both halves of that in
> `tests/test_decide_endpoint.py`.

Without `SIGN402_DECIDE_AUTONOMY_CAP` the endpoint answers `503` and the rest of
the gateway runs normally. That is the safe failure: no verdict is better than a
guessed one.

Restart, then confirm it answers locally before anything is exposed:

```bash
ssh hermes@164.68.104.44 'curl -s -X POST http://127.0.0.1:8099/v1/decide -H "content-type: application/json" -d "{\"merchant\":\"gateway.thegraph.com\",\"payTo\":\"0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB\",\"amountUsd\":\"0.01\",\"owner\":\"smoke-test\"}"'
```

Expect `200` with `"action": "ESCALATE"` and `"rule": "unknown_merchant"`.

## 3. Create the tunnel

`singitai.app` is already on Cloudflare, so a named tunnel on a subdomain gives
a stable hostname that survives restarts. A quick tunnel
(`cloudflared tunnel --url ...`) works too, but its `*.trycloudflare.com`
hostname changes every restart — and Bazantic stores the URL, so a restart
mid-experiment invalidates the gateway and both arms have to be re-run.

```bash
ssh -t hermes@164.68.104.44
sudo apt-get install -y cloudflared     # or the .deb from Cloudflare
cloudflared tunnel login                # opens a URL; authorise singitai.app
cloudflared tunnel create sign402-decide
cloudflared tunnel route dns sign402-decide decide.singitai.app
```

`tunnel create` prints a UUID and writes credentials to
`~/.cloudflared/<UUID>.json`. Note the UUID for the next step.

## 4. The ingress allow-list

`~/.cloudflared/config.yml` — **order matters**, the first matching rule wins:

```yaml
tunnel: <UUID from step 3>
credentials-file: /home/hermes/.cloudflared/<UUID>.json

ingress:
  # The only two paths that reach the gateway.
  - hostname: decide.singitai.app
    path: ^/v1/(decide|journal)(\?.*)?$
    service: http://127.0.0.1:8099

  # Bazantic's Analyze step fetches the base URL. Answer it, from a route that
  # reveals nothing.
  - hostname: decide.singitai.app
    path: ^/health$
    service: http://127.0.0.1:8099

  # Everything else on this hostname is refused at the edge and never reaches
  # the box. This rule is the security control, not a tidiness measure.
  - hostname: decide.singitai.app
    service: http_status:404

  # cloudflared requires a catch-all last.
  - service: http_status:404
```

Run it as a service so it survives a reboot:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
systemctl is-active cloudflared
```

## 5. Verify the allow-list, do not assume it

Run all of these. The last three are the ones that matter.

```bash
# answers
curl -s -o /dev/null -w "decide:  %{http_code}\n" -X POST https://decide.singitai.app/v1/decide \
  -H 'content-type: application/json' \
  -d '{"merchant":"gateway.thegraph.com","payTo":"0x79DC34E41B2b591078d3dE222C43EcaaBD52FcCB","amountUsd":"0.01","owner":"smoke-test"}'
curl -s -o /dev/null -w "journal: %{http_code}\n" 'https://decide.singitai.app/v1/journal?owner=smoke-test'
curl -s -o /dev/null -w "health:  %{http_code}\n" https://decide.singitai.app/health

# must all be 404 — these move money
for p in /agent/wallet /agent/buy-tool /agent/withdraw /agent/spending-limits /internal/fulfill-bitrefill; do
  printf "%-28s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "https://decide.singitai.app$p" -d '{}')"
done
```

If any custodial path returns anything other than `404`, stop and fix the
ingress before going further. A `401` is **not** good enough here: it means the
request reached the gateway and only its own auth stopped it.

## 6. Rate limit at the edge

The endpoint is unauthenticated and writes a journal line per call. Its own
database means abuse cannot reach customer payments, but it can still fill a
disk.

In the Cloudflare dashboard, **Security → WAF → Rate limiting rules**:

- **Match:** hostname equals `decide.singitai.app`
- **Rate:** 60 requests per minute per IP
- **Action:** block for 1 minute

Sixty a minute is far above anything an agent doing real work needs and far
below what makes the journal a problem.

## 7. Give Bazantic the address

- **API Base URL:** `https://decide.singitai.app`
- **Spec URL:** PASTE — the whole of
  [`sign402-gateway/docs/decide-openapi.json`](../sign402-gateway/docs/decide-openapi.json),
  with `servers[0].url` changed to `https://decide.singitai.app`

That one edit is the only change to the document. Both experiment arms get it
byte for byte identical; see [bazantic-experiment.md](bazantic-experiment.md).

## Taking it down afterwards

```bash
ssh -t hermes@164.68.104.44 'sudo systemctl disable --now cloudflared'
```

The gateway keeps running and goes back to being reachable only on
`127.0.0.1:8099`. Nothing else needs undoing, which is the other reason for a
tunnel over a web server.
