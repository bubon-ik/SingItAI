// Pay for actual usage: the payer authorizes a ceiling with x402 `upto`, and CDP settles only what the
// answer cost, after it came back. A request that ends in 400 or above is never charged.
//
// Buyers that only sign `exact` (no `upto` support) get the same per-question ceiling as a flat, fixed-amount
// offer instead: there is no partial settlement or refund for `exact` (the signature authorizes one amount,
// and a facilitator cannot settle an exact payment for less than what was signed), so it is priced at the full
// ceiling and that is disclosed in the offer's own `extra.billing.mode`. `upto` is unaffected and keeps paying
// only the answer's actual cost.
import { markUnservable } from "./catalogue.mjs";
import { ExpressAdapter, paymentMiddlewareFromHTTPServer, x402HTTPResourceServer, x402ResourceServer, setSettlementOverrides } from "@x402/express";
import { SETTLEMENT_OVERRIDES_HEADER } from "@x402/core/http";
import { bazaarResourceServerExtension } from "@x402/extensions/bazaar";
import { permitGuard as defaultPermitGuard } from "./permit-guard.mjs";
import { withBuilderCode } from "./builder-code.mjs";
import { acceptedOf, answerLogFor, recordWhenSettled } from "./answer-log.mjs";
import { UptoEvmScheme } from "@x402/evm/upto/server";
import { UptoSvmScheme } from "@x402/svm/upto/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { BASE, ASSET, SOLANA, SOLANA_ASSET, TERMS, SOLANA_TERMS, billUsage, ceilingAtomic, minDiscountFor, exactReserveTokens,
  DEFAULT_EXACT_OUTPUT_FRACTION } from "./pricing.mjs";

// Actual model cost + 30% + the network's settlement fee. Solana's upto escrows the ceiling, then
// settles the actual amount and refunds the rest: two transactions, hence its higher fee.
const TERMS_BY_NETWORK = { [BASE]: TERMS, [SOLANA]: SOLANA_TERMS };

// The ceiling is decided per question, from the model it asks for, so the offer's price is a function of the
// request. `quotes` remembers what each parsed body was offered: the middleware asks again when the payment
// arrives and must get the same answer.
//
// `fallback`, when given, replaces the standing ceiling (TERMS/SOLANA_TERMS.maxChargeAtomic) as what a
// GET/HEAD or empty-POST discovery probe is quoted (`quotes` is never populated for those: see `isDiscoveryProbe`
// below). It may return a Promise: @x402/core awaits an offer's `price` before using it (chunk-FV2JN4FK.mjs,
// `await option.price(context)`), so a route whose model is known ahead of time (model-routes.mjs) can quote that
// model's own ceiling instead of the generic one, without touching the metered payment path at all.
export const baseOffer = (config, quotes, fallback) => ({
  // 120 s covers the model and settlement; after it, a lost answer's signature is provably unusable.
  scheme: "upto", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120,
  price: async ctx => {
    const amount = quotes.get(ctx.adapter.getBody())?.upto?.[BASE] ?? (fallback ? await fallback(ctx) : TERMS.maxChargeAtomic);
    return { asset: ASSET, amount, extra: { name: "USD Coin", version: "2", billing: { ...TERMS, maxChargeAtomic: amount } } };
  },
});
export const solanaOffer = (config, quotes, fallback) => ({
  scheme: "upto", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 300,
  price: async ctx => {
    const amount = quotes.get(ctx.adapter.getBody())?.upto?.[SOLANA] ?? (fallback ? await fallback(ctx) : SOLANA_TERMS.maxChargeAtomic);
    return { asset: SOLANA_ASSET, amount, extra: { billing: { ...SOLANA_TERMS, maxChargeAtomic: amount } } };
  },
});

// `exact` at its own, usually smaller, per-question ceiling (config.exactOutputFraction of `upto`'s output
// budget: see `ceilings`), charged flat (see the file header for why). Reuses the same `quotes` lookup as the
// `upto` offers above, so both schemes are always quoted from the one choice made for this question, and
// `mode: "exact_ceiling"` in the offer's billing tells buyers this one is not metered.
export const exactBaseOffer = (config, quotes, fallback) => ({
  scheme: "exact", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120,
  price: async ctx => {
    const amount = quotes.get(ctx.adapter.getBody())?.exact?.[BASE] ?? (fallback ? await fallback(ctx) : TERMS.maxChargeAtomic);
    return { asset: ASSET, amount, extra: { name: "USD Coin", version: "2",
      billing: { ...TERMS, mode: "exact_ceiling", maxChargeAtomic: amount } } };
  },
});
export const exactSolanaOffer = (config, quotes, fallback) => ({
  scheme: "exact", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 300,
  price: async ctx => {
    const amount = quotes.get(ctx.adapter.getBody())?.exact?.[SOLANA] ?? (fallback ? await fallback(ctx) : SOLANA_TERMS.maxChargeAtomic);
    return { asset: SOLANA_ASSET, amount, extra: { billing: { ...SOLANA_TERMS, mode: "exact_ceiling", maxChargeAtomic: amount } } };
  },
});

// The ceiling each network's offer carries for this question, separately for `upto` (authorizes up to the whole
// answer budget, settles actual usage) and `exact` (charged flat, so priced at a fraction of that budget when
// configured: `upto` still authorizes and settles the full answer regardless). `info` null means no catalogue
// entry for the model (not on Surplus's list, or not looked up): the old standing ceiling, for both. `input.share`
// and `input.isDefault` come from `chooseModel`/`priceGuard` (app.mjs, catalogue.mjs), fixed at quote time so the
// paid call is billed exactly what was quoted; the default model's ceiling is clamped to the standing maximum so
// nothing is ever authorized above it, same as before this formula existed.
//
// `upto` authorizes with the loosest, one-token-per-character input estimate (see pricing.mjs's `ceilingAtomic`):
// it only ever settles actual usage, so a higher authorization costs the payer nothing, and a dense prompt (CJK,
// emoji, ...) can never tokenize to more than it authorized. `exact` is charged flat at the tighter ~4-chars-per-
// token estimate, since it already absorbs an overrun via `allowOverrun` instead of refusing the answer.
export function ceilings(info, input, exactOutputFraction = DEFAULT_EXACT_OUTPUT_FRACTION) {
  const size = { chars: input.chars, messageCount: input.messageCount };
  const build = (maxTokens, charsPerToken, reserveTokens = null) => Object.fromEntries(Object.entries(TERMS_BY_NETWORK).map(([net, terms]) => {
    if (!info) return [net, terms.maxChargeAtomic];
    const amount = ceilingAtomic(info, { ...size, maxTokens, charsPerToken, reserveTokens }, terms, input.share ?? 1);
    return [net, input.isDefault && BigInt(amount) > BigInt(terms.maxChargeAtomic) ? terms.maxChargeAtomic : amount];
  }));
  return { upto: build(input.maxTokens, 1), exact: build(Math.ceil(input.maxTokens * exactOutputFraction), undefined, exactReserveTokens(info?.id, input.maxTokens)) };
}

// The terms of the network the payer chose, with the ceiling this question was offered for the scheme they
// signed, and that scheme. The middleware has already matched the payment to an offer, so the network is one of
// ours; the ceiling is ours, never the payer's.
function paidTerms(req) {
  const header = req.get("payment-signature") ?? req.get("x-payment");
  const { network, scheme } = JSON.parse(Buffer.from(header, "base64").toString()).accepted ?? {};
  const terms = TERMS_BY_NETWORK[network];
  const bucket = scheme === "exact" ? req.askCeilings?.exact : req.askCeilings?.upto;
  return terms && bucket && { terms: { ...terms, maxChargeAtomic: bucket[network] }, scheme, network };
}

// A request with no body at all: what a directory's crawler sends to see whether a route takes payment.
export const isEmptyBody = body => body === undefined || body === null
  || (typeof body === "object" && !Array.isArray(body) && Object.keys(body).length === 0);

// x402scan registers a route only if an empty, unpaid request gets the 402 challenge (docs/DISCOVERY.md in
// Merit-Systems/x402scan), rather than the validation error a real, malformed request would get for having no
// model or no question. A request that carries a payment attempt is never treated as a probe, empty body or not:
// it must still be validated and rejected without charging, same as any other malformed paid request.
export const isDiscoveryProbe = req => isEmptyBody(req.body) && !(req.get("payment-signature") ?? req.get("x-payment"));

// A GET/HEAD request never carries a real x402 payment attempt here, no matter what headers it sends: hiding
// payment-signature/x-payment, rather than branching on them, is what guarantees `discoveryChallenge` below can
// never find a payload to verify or settle.
class DiscoveryAdapter extends ExpressAdapter {
  getHeader(name) {
    return ["payment-signature", "x-payment"].includes(name.toLowerCase()) ? undefined : super.getHeader(name);
  }
}

// x402scan, Coinbase's Bazaar and 402 Index probe a paid route with GET and HEAD before listing it, not just an
// empty POST; Express has no handler for those verbs on a POST-only route and answers with its own 404, which
// reads as "not for sale" to a crawler that only checks GET/HEAD. This answers them with the exact 402 an empty,
// unpaid POST gets from `httpServer`'s own route config: same offers, same Bazaar extension, same resource url.
// `context.method` is pinned to POST (never the request's real GET/HEAD) so the enriched extension and the
// resource both describe the route the one way it is actually called.
//
// The POST middleware loads the facilitator's supported kinds (fee payer, facilitator address) before its first
// challenge, and retries that load while it keeps failing; a GET that arrives first must not answer with a challenge
// missing those, so it waits for the same load. Once loaded, it is never reloaded from here: `initialize()` clears
// the kinds before fetching them again, which a concurrent POST must never see.
function discoveryChallenge(httpServer) {
  let loading = null;
  const ready = () => httpServer.ResourceServer.supportedResponsesMap?.size > 0 ? null
    : (loading ??= httpServer.initialize().finally(() => { loading = null; }));
  return async (req, res, next) => {
    const context = { adapter: new DiscoveryAdapter(req), path: req.path, decodedPath: req.path, method: "POST" };
    let result;
    try {
      await ready();
      result = await httpServer.processHTTPRequest(context);
    } catch {
      return res.status(503).json({ error: { message: "Try again shortly.", type: "upstream_error", code: "upstream_unavailable" } });
    }
    // With no payment header ever reaching it, processHTTPRequest never verifies or settles: anything but the
    // challenge (no route matched) is left to Express's own 404.
    if (result.type !== "payment-error") return next();
    const { response } = result;
    res.status(response.status);
    // Sent the way the POST middleware sends it, so a browser opening the URL gets the same paywall page.
    for (const [key, value] of Object.entries(response.headers)) res.setHeader(key, value);
    if (response.isHtml) res.send(response.body);
    else res.json(response.body || {});
  };
}

// One `x402HTTPResourceServer` per call, shared by the POST route(s) it pays for and their free GET/HEAD discovery
// challenge: `routes` is `paymentMiddleware`'s own shape, `{ "POST /the/route": { accepts, mimeType, ... } }`.
// `discoverable` should mirror whichever check already grants that route's empty-POST probe the same 402 (see
// `isDiscoveryProbe` above) — a route that does not offer that probe should not advertise itself over GET either.
const statsServers = new WeakSet(), statsRoutes = new WeakMap();
export function addPaymentRoute(app, routes, server,
  { discoverable = true, log = console.log, permitGuard = defaultPermitGuard } = {}) {
  const paths = statsRoutes.get(server) ?? new Set();
  for (const key of Object.keys(routes)) if (key.startsWith("POST ")) paths.add(key.slice(5));
  statsRoutes.set(server, paths);
  app.locals.paidRoutes ??= new Set();
  for (const path of paths) app.locals.paidRoutes.add(path);
  if (!statsServers.has(server)) {
    statsServers.add(server);
    const record = ok => ctx => {
      try {
        let route = null;
        try { const path = ctx.transportContext?.request?.path;
          if (statsRoutes.get(server).has(path)) route = path; } catch {}
        // Core 2.28 supplies effective requirements after overrides, but without an override
        // an upto amount is only the authorization ceiling, not observed settlement volume.
        let amountOverridden = false;
        try {
          const raw = Object.entries(ctx.transportContext?.responseHeaders ?? {})
            .find(([key]) => key.toLowerCase() === SETTLEMENT_OVERRIDES_HEADER.toLowerCase())?.[1];
          amountOverridden = JSON.parse(raw)?.amount !== undefined;
        } catch {}
        app.locals?.healthStats?.recordSettlement({ route, network: ctx.requirements?.network,
          scheme: ctx.requirements?.scheme, ok: ok && ctx.result?.success !== false,
          amount: ctx.result?.amount ?? ((ctx.requirements?.scheme === "exact" || amountOverridden) ? ctx.requirements?.amount : undefined),
          payer: ctx.result?.payer });
      } catch {}
    };
    server.onAfterSettle(record(true)); server.onSettleFailure(record(false));
  }
  server.onAfterSettle(ctx => { permitGuard.record(ctx.paymentPayload, ctx.requirements, ctx.result); });
  // Only diagnostic fields are logged: facilitator errors can embed payer addresses or signatures, so redact
  // long encoded values even in the reason string rather than serializing any part of the payment payload.
  const failure = phase => ctx => {
    const { network, scheme, amount } = ctx.requirements ?? {};
    const reason = String(ctx.error?.errorReason ?? ctx.error?.message ?? "unknown")
      .replace(/0x[a-fA-F0-9]+|[A-Za-z0-9+/=_-]{32,}/g, "[redacted]").replace(/[\r\n]/g, " ");
    log(`singit-ask: ${Object.keys(routes).join(",")} ${phase}`
      + ` network=${network} scheme=${scheme} amount=${amount} reason=${reason}`);
  };
  server.onSettleFailure(failure("settle failed"));
  server.onVerifyFailure(failure("verify failed"));
  if (!server.hasExtension("bazaar")) server.registerExtension(bazaarResourceServerExtension);
  routes = withBuilderCode(routes, server);
  const httpServer = new x402HTTPResourceServer(server, routes);
  for (const pattern of Object.keys(routes)) {
    const [verb, route] = pattern.split(" ");
    if (verb === "POST") app.post(route, permitGuard.middleware);
  }
  app.use(paymentMiddlewareFromHTTPServer(httpServer));
  if (discoverable) {
    for (const pattern of Object.keys(routes)) {
      const [verb, route] = pattern.split(" ");
      if (verb === "POST") app.get(route, discoveryChallenge(httpServer));
    }
  }
  return httpServer;
}

// What the discovery probes (GET/HEAD and the empty, unpaid POST) are quoted, per scheme and network: the ceilings a
// real POST of `probeBody` would be offered right now, through the very same `readRequest`, `choose` and
// `ceilings` a paid question goes through. `probeBody` is the example body the route declares in its Bazaar
// discovery extension, which is the body CDP's Bazaar sends to record the route's price; vet402 then compares that
// recorded price with what its own empty, unpaid POST is offered (price_mismatch when no accept carries it). The
// standing ceiling (the old probe price) is kept only when the example cannot be quoted right now. Probes never pay:
// a payment on an empty body is refused before settlement (see `isDiscoveryProbe` and the handlers below).
export function exampleProbe({ probeBody, readRequest, choose, exactOutputFraction = DEFAULT_EXACT_OUTPUT_FRACTION }) {
  const probe = perRequest(async () => {
    try {
      const input = await choose(readRequest(probeBody));
      return ceilings(input.info, input, exactOutputFraction);
    } catch {
      return ceilings(null, {}, exactOutputFraction);
    }
  });
  return (bucket, network) => async (ctx) => (await probe(ctx))[bucket][network];
}

// `quote` computed once per request, however many offers ask for it: @x402/core prices each offer in turn, so
// without this a 402 with four offers would look the example up four times (and, with the catalogue down, wait
// out four failed loads). Keyed by the request's adapter, which each request gets its own of; a call without one
// is just computed.
export function perRequest(quote) {
  const seen = new WeakMap();
  return (ctx) => {
    const key = ctx?.adapter;
    if (!key || typeof key !== "object") return quote();
    if (!seen.has(key)) seen.set(key, quote());
    return seen.get(key);
  };
}

// `makeOffers(quotes, probeFallback)`: `probeFallback(bucket, network)` is the discovery probes' price for that
// offer (see `exampleProbe`), present only when `probeBody` is given; without it the probes keep the standing ceiling.
export function addUsageRoute(app, route, makeOffers, details, { facilitatorClient, catalogue, config, readRequest,
  choose = async i => ({ ...i, model: config.model, info: null }), quotes = new WeakMap(), upstream, fetchImpl, log,
  offerEmptyProbe = false, probeBody }) {
  const probeFallback = probeBody === undefined ? undefined : exampleProbe({ probeBody, readRequest, choose,
    exactOutputFraction: config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION });
  const offers = makeOffers(quotes, probeFallback);
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new UptoEvmScheme())
    .register(BASE, new ExactEvmScheme())
    .register(SOLANA, new UptoSvmScheme({ withdrawDelay: 300 }))
    .register(SOLANA, new ExactSvmScheme());
  // An unreadable question, or a model that is not offered, is refused before any payment is asked for.
  const exactOutputFraction = config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION;
  const answerLog = answerLogFor(config, log);
  // Do not advertise payment for a known failure on pinned routes or default-model probes.
  app.all(route, (req, res, next) => {
    const model = probeBody?.model ?? config.model;
    if (!probeBody?.model && !["GET", "HEAD"].includes(req.method) && !isDiscoveryProbe(req)) return next();
    if (!catalogue?.isUnservable?.(model)) return next();
    res.set("Retry-After", String(catalogue.unservableRetryAfter(model))).status(503).json({
      error: { type: "upstream_error", code: "no_healthy_sellers",
        message: "No seller can serve that model right now. Nothing was paid; try again later." } });
  });
  app.post(route, async (req, res, next) => {
    // It gets the standing offer; paying it with no question is still refused below.
    if (offerEmptyProbe && isDiscoveryProbe(req)) {
      req.askCeilings = ceilings(null, {}, exactOutputFraction);
      return next();
    }
    try {
      req.askInput = await choose(readRequest(req.body));
      if (req.askInput.autoTier) {
        res.set("X-SingIt-Model", req.askInput.model);
        res.set("X-SingIt-Tier", req.askInput.autoTier);
        log(`singit-ask: auto tier=${req.askInput.autoTier} reason=${req.askInput.autoReason}`);
      }
      req.askCeilings = ceilings(req.askInput.info, req.askInput, exactOutputFraction);
      quotes.set(req.body, req.askCeilings);
      next();
    } catch (error) {
      if (error.retryAfter) res.set("Retry-After", String(error.retryAfter));
      if (!error.status) log(`singit-ask: model list failure (${error.name})`);
      res.status(error.status ?? 503).json({ error: { message: error.status ? error.message : "Try again shortly.",
        code: error.code ?? (error.retryAfter ? "no_healthy_sellers" : error.status === 404 ? "model_not_found" : error.status === 400 ? "invalid_request" : "upstream_unavailable"),
        type: error.status ? "invalid_request_error" : "upstream_error" } });
    }
  });
  addPaymentRoute(app, { [`POST ${route}`]: { accepts: offers, mimeType: "application/json", ...details } }, server,
    { discoverable: offerEmptyProbe, log });
  app.post(route, async (req, res) => {
    const started = Date.now();
    let retried = false;
    try {
      const paid = paidTerms(req);
      if (!paid) throw new Error("payment on an unknown network");
      const exact = paid.scheme === "exact";
      const deadline = started + 95_000;
      const attempt = input => upstream(config, input.messages, input.maxTokens, fetchImpl, input.model,
        { minDiscount: minDiscountFor(input, paid.terms), params: input.info?.params,
          timeoutMs: Math.max(1, deadline - Date.now()), deadline });
      let answer;
      try { answer = await attempt(req.askInput); }
      catch (error) {
        const bucket = exact ? "exact" : "upto";
        const alternate = error.name === "UpstreamError" && error.status >= 400  // refused before any answer; a 200 with no text (no_text, max_tokens_spent_reasoning) may already be billed
          && Date.now() < deadline && req.askInput.autoAlternates?.find(candidate =>
            BigInt(ceilings(candidate.info, candidate, exactOutputFraction)[bucket][paid.network]) <= BigInt(paid.terms.maxChargeAtomic));
        if (!alternate) throw error;
        markUnservable(catalogue, req.askInput.model, error, log);
        log(`singit-ask: auto fallback from=${req.askInput.model} to=${alternate.model} code=${error.code} status=${error.status}`);
        req.askInput = alternate;
        retried = true;
        answer = await attempt(alternate);
      }
      if (req.askInput.autoTier) {
        res.set("X-SingIt-Model", req.askInput.model);
        res.set("X-SingIt-Tier", req.askInput.autoTier);
      }
      // Auto must retain its margin against actual provider cost, even on a flat exact offer.
      const usage = billWithDiagnostics(answer.usage, paid.terms, exact, route, req.askInput.model, log);
      // `exact` already settled the full ceiling it was offered: there is no lower amount to settle to, and no
      // way to refund the unused part, so the override below is only ever for `upto`. Its actual cost may run
      // over that flat ceiling (usage.overrunAtomic): the payer is never charged more than they signed for, this
      // service absorbs the difference instead of discarding an otherwise-good answer.
      const billing = exact ? { ...usage, mode: "exact_ceiling", totalAtomic: paid.terms.maxChargeAtomic } : usage;
      if (exact && usage.overrunAtomic) log(`singit-ask: exact overrun ${usage.overrunAtomic} micro-USDC`);
      recordWhenSettled(res, answerLog, { route, model: req.askInput.model, ...acceptedOf(req), maxTokens: req.askInput.maxTokens, usage: answer.usage, billing });
      if (!exact) setSettlementOverrides(res, { amount: billing.totalAtomic });
      log(`singit-ask: answer ready in ${Date.now() - started} ms, settling ${billing.totalAtomic} micro-USDC`);
      res.locals.singitReceipt = { ref: `singit-ask-${started}`, chargedAtomic: billing.totalAtomic };
      res.json({ id: `singit-ask-${started}`, object: "chat.completion", created: Math.floor(started / 1000),
        model: "singit-ask", choices: [{ index: 0, message: { role: "assistant", content: answer.content },
          finish_reason: answer.finishReason }], usage: answer.usage, billing });
    } catch (error) {
      markUnservable(catalogue, req.askInput?.model, error, log);
      // Not settled (status >= 400): on Base nothing moves, on Solana the escrowed ceiling is refunded in full.
      // Never 502: Cloudflare swaps an origin 502/504 for its own HTML page, and passes a 503 through.
      log(`singit-ask: ${route} ${req.askInput?.model} no billable answer after ${Date.now() - started} ms (${error.message})`);
      if (!retried && error.code === "max_tokens_spent_reasoning") {
        return res.status(422).json({ error: { type: "invalid_request_error", code: "max_tokens_too_small",
          message: "This model spent the whole max_tokens budget reasoning and wrote no answer. Raise max_tokens. You were not charged." } });
      }
      res.set("Retry-After", "30").status(503).json({ error: { type: "upstream_error", code: "no_billable_answer",
        message: "No billable answer was produced. You were not charged; try again." } });
    }
  });
}

// Preserve the provider's usage only for an overrun diagnostic: no prompt, answer or payment payload belongs
// in this log. Centralizing it keeps both API formats equally observable without changing their pricing.
export function billWithDiagnostics(usage, terms, exact, route, model, log) {
  try { return billUsage(usage, terms, { allowOverrun: exact }); }
  catch (error) {
    if (error.code === "ceiling_exceeded") {
      log(`singit-ask: ${route} ${model} ceiling exceeded ${JSON.stringify({
        prompt_tokens: usage?.prompt_tokens, completion_tokens: usage?.completion_tokens,
        reasoning_tokens: usage?.completion_tokens_details?.reasoning_tokens, buyer_cost_micro: usage?.buyer_cost_micro,
        totalAtomic: error.totalAtomic, ceiling: terms.maxChargeAtomic })}`);
    }
    throw error;
  }
}
