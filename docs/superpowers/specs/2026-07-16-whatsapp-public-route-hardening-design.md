# WhatsApp public route hardening design

Date: 2026-07-16

## Goal

Reduce the public attack surface of `whatsapp.singitai.app` without changing
Hermes or interrupting Meta WhatsApp Cloud webhook delivery.

## Current state

- Cloudflare Tunnel publishes `whatsapp.singitai.app` to
  `http://localhost:8090`.
- The route currently matches every path.
- `/whatsapp/webhook` is required by Meta for verification and message events.
- `/health` is useful only to the operator and does not need to be public.
- Hermes listens on `127.0.0.1:8090`, so local health checks remain available
  after the public route is restricted.

## Design

### Tunnel route

Keep the existing hostname and service, but restrict the published application
route to this exact path expression:

```text
^/whatsapp/webhook$
```

The resulting route is:

```text
whatsapp.singitai.app + ^/whatsapp/webhook$
    -> http://localhost:8090
```

The existing tunnel catch-all rule continues to return `404` for unmatched
paths. Query parameters used by Meta during webhook verification do not change
the request path and therefore continue to match.

### HSTS

Create a Cloudflare response-header transform rule scoped only to:

```text
http.host eq "whatsapp.singitai.app"
```

Set:

```text
Strict-Transport-Security: max-age=2592000
```

Do not enable `includeSubDomains` or `preload`. This keeps the first rollout
reversible and avoids imposing policy on the future `singitai.app` website or
other subdomains.

## Security properties

- Public callers cannot reach `/health` or future accidental Hermes routes.
- Meta retains GET and POST access to the exact webhook endpoint.
- The origin remains loopback-only.
- Invalid webhook signatures continue to fail closed.
- HSTS is limited to the WhatsApp hostname.

## Verification

After changing the route:

1. `http://127.0.0.1:8090/health` returns `200` on the VPS.
2. `https://whatsapp.singitai.app/health` does not return `200`.
3. A Meta-style verification GET with the configured verify token returns the
   supplied challenge and `200`.
4. A POST with an invalid signature returns `401`, does not increment
   `accepted`, and increments `rejected_signature`.
5. A legitimate WhatsApp message reaches Hermes.
6. Webhook responses include the configured HSTS header.

## Rollback

Restore the published route path to empty (match all paths) and disable the
subdomain response-header transform rule. No server configuration or service
restart is required for either rollback.
