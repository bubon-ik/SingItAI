// The other two ways agents ask a model, besides OpenAI's chat completions: Anthropic's Messages (POST /v1/messages,
// what Claude Code and the Anthropic SDK speak) and OpenAI's Responses (POST /v1/responses). Sold over x402 like the
// chat route: the payer authorizes a ceiling with `upto`, worked out from the model's list price and the size of the
// request, and is charged what the answer actually cost + 30% + the network's settlement fee, only once it came back.
//
// The request goes to Surplus as it came, with this service's key, apart from the model's name (Anthropic's dated
// names become Surplus's) and a bounded answer length. Streaming is not offered here: an answer is paid for whole.
import { x402ResourceServer, setSettlementOverrides } from "@x402/express";
import { UptoEvmScheme } from "@x402/evm/upto/server";
import { UptoSvmScheme } from "@x402/svm/upto/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { declareEip2612GasSponsoringExtension } from "@x402/extensions";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { UnknownModel, priceGuard } from "./catalogue.mjs";
import { addPaymentRoute, baseOffer, ceilings, exactBaseOffer, exactSolanaOffer, isDiscoveryProbe, solanaOffer } from "./metered.mjs";
import { BASE, SOLANA, TERMS, SOLANA_TERMS, billUsage, DEFAULT_EXACT_OUTPUT_FRACTION, SERVICE_NAME, ICON_URL } from "./pricing.mjs";

export const FORMAT_ROUTES = { "/v1/messages": "/anthropic/v1/messages", "/v1/responses": "/v1/responses" };
export const FORMAT_LIMITS = Object.freeze({ chars: 100_000, maxTokens: 32_000, defaultTokens: 4_096, timeoutMs: 120_000 });
const TERMS_BY_NETWORK = { [BASE]: TERMS, [SOLANA]: SOLANA_TERMS };
// Anthropic's SDK and Claude Code name models as Anthropic does (claude-sonnet-5-5, claude-fable-5-1, maybe dated); Surplus as claude-sonnet-5.5.
const ANTHROPIC_ID = /^(claude-(?:opus|sonnet|haiku|fable))-(\d+)-(\d+)(?:-\d{8})?$/;
const UPSTREAM_FIELDS = ["provider", "cost"]; // the seller, and its own price with the seller's unit
const UPSTREAM_USAGE_FIELDS = ["cost", "is_byok", "cost_details", "buyer_cost_micro"];

export class FormatError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export const surplusModel = (name) => {
  const m = ANTHROPIC_ID.exec(name);
  return m ? `${m[1]}-${m[2]}.${m[3]}` : name;
};

// The request sent on, its model, its size in characters and the longest answer it can get.
export function readFormatRequest(route, body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new FormatError(400, "Send a JSON body.");
  if (body.stream) throw new FormatError(400, "Streaming is not offered over x402: an answer is paid for whole. Send stream: false.");
  if (typeof body.model !== "string" || !body.model.trim() || body.model.length > 120) {
    throw new FormatError(400, "Name a model from GET /v1/models.");
  }
  const request = { ...body, model: surplusModel(body.model.trim()) };
  let maxTokens, sized, messageCount;
  if (route === "/v1/messages") {
    if (!Array.isArray(body.messages) || body.messages.length < 1 || body.messages.length > 200) {
      throw new FormatError(400, "Send 1 to 200 messages.");
    }
    if (!Number.isInteger(body.max_tokens) || body.max_tokens < 1 || body.max_tokens > FORMAT_LIMITS.maxTokens) {
      throw new FormatError(400, `max_tokens must be 1 to ${FORMAT_LIMITS.maxTokens}.`);
    }
    maxTokens = body.max_tokens;
    sized = [body.messages, body.system ?? "", body.tools ?? ""];
    messageCount = body.messages.length;
  } else {
    if (!(typeof body.input === "string" && body.input.trim()) && !(Array.isArray(body.input) && body.input.length)) {
      throw new FormatError(400, "Send input: text or a list of items.");
    }
    if (body.max_output_tokens !== undefined && (!Number.isInteger(body.max_output_tokens) || body.max_output_tokens < 1
        || body.max_output_tokens > FORMAT_LIMITS.maxTokens)) {
      throw new FormatError(400, `max_output_tokens must be 1 to ${FORMAT_LIMITS.maxTokens}.`);
    }
    // An answer with no stated length could cost more than the ceiling the payer signed: it gets one.
    maxTokens = body.max_output_tokens ?? FORMAT_LIMITS.defaultTokens;
    request.max_output_tokens = maxTokens;
    sized = [body.input, body.instructions ?? "", body.tools ?? ""];
    messageCount = Array.isArray(body.input) ? body.input.length : 1;
  }
  const chars = sized.reduce((n, part) => n + (typeof part === "string" ? part.length : JSON.stringify(part).length), 0);
  if (chars > FORMAT_LIMITS.chars) throw new FormatError(400, `Keep the request under ${FORMAT_LIMITS.chars} characters.`);
  return { request, model: request.model, chars, maxTokens, messageCount };
}

// An answer as the format has it, without Surplus's own fields.
export function scrub(data) {
  for (const field of UPSTREAM_FIELDS) delete data[field];
  if (data.usage && typeof data.usage === "object") for (const field of UPSTREAM_USAGE_FIELDS) delete data.usage[field];
  return data;
}

const refusal = (route, status, message) => (route === "/v1/messages"
  ? { type: "error", error: { type: status === 400 ? "invalid_request_error" : status === 404 ? "not_found_error" : "api_error", message } }
  : { error: { type: status >= 500 ? "upstream_error" : "invalid_request_error", message } });

// <=300 characters with the "SingIt Ask: " prefix (see app.mjs's DISCOVERY_DESCRIPTION for why): what each format
// does and when to call it, not what it costs.
const DESCRIPTIONS = {
  "/v1/messages": "Anthropic's Messages API (what Claude Code and the Anthropic SDK speak), any model listed at GET "
    + "/v1/models, Claude included. Call it when an agent needs a Claude-format answer with no API key or account, "
    + "or wants to try another model the same way.",
  "/v1/responses": "OpenAI's Responses API, any model listed at GET /v1/models (GPT, Claude, Gemini, DeepSeek and more). "
    + "Call it when an agent needs an answer in OpenAI's Responses format with no API key or account.",
};
const EXAMPLES = {
  "/v1/messages": { model: "claude-sonnet-5.5", max_tokens: 300, messages: [{ role: "user", content: "What is x402?" }] },
  "/v1/responses": { model: "gpt-5.5", input: "What is x402?", max_output_tokens: 300 },
};

export function addFormatRoutes(app, config, { facilitatorClient, catalogue, fetchImpl = fetch, log = console.error }) {
  const exactOutputFraction = config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION;
  const bufferPp = config.priceBufferPp ?? 1;
  for (const [route, upstreamPath] of Object.entries(FORMAT_ROUTES)) {
    const quotes = new WeakMap();
    const server = new x402ResourceServer(facilitatorClient)
      .register(BASE, new UptoEvmScheme())
      .register(BASE, new ExactEvmScheme())
      .register(SOLANA, new UptoSvmScheme({ withdrawDelay: 300 }))
      .register(SOLANA, new ExactSvmScheme());
    app.post(route, async (req, res, next) => {
      // x402scan's empty, unpaid probe gets the standing offer, not a "Send a JSON body"/"Name a model" error.
      if (isDiscoveryProbe(req)) { req.askCeilings = ceilings(null, {}, exactOutputFraction); return next(); }
      try {
        const input = readFormatRequest(route, req.body);
        const info = await catalogue.resolve(input.model);
        if (catalogue.canAnswer && !(await catalogue.canAnswer(info.id))) {
          throw new FormatError(400, "No trusted seller offers that model right now. Pick another; nothing was paid.");
        }
        req.format = { ...input, ...priceGuard(catalogue, info.id, bufferPp) };
        req.askCeilings = ceilings(info, req.format, exactOutputFraction);
        quotes.set(req.body, req.askCeilings);
        next();
      } catch (error) {
        const status = error instanceof UnknownModel ? 404 : error.status ?? 503;
        res.status(status).json(refusal(route, status, status === 503 ? "Try again shortly." : error.message));
      }
    });
    addPaymentRoute(app, { [`POST ${route}`]: {
      // exact on Base first: the simplest offer for a buyer that only inspects accepts[0].
      accepts: [exactBaseOffer(config, quotes), solanaOffer(config, quotes), baseOffer(config, quotes), exactSolanaOffer(config, quotes)],
      mimeType: "application/json",
      description: `SingIt Ask: ${DESCRIPTIONS[route]}`, serviceName: SERVICE_NAME, iconUrl: ICON_URL,
      extensions: { ...declareEip2612GasSponsoringExtension(),
        ...declareDiscoveryExtension({ bodyType: "json", input: EXAMPLES[route] }) },
    } }, server);
    app.post(route, async (req, res) => {
      const started = Date.now();
      try {
        const header = req.get("payment-signature") ?? req.get("x-payment");
        const accepted = JSON.parse(Buffer.from(header, "base64").toString()).accepted ?? {};
        const exact = accepted.scheme === "exact";
        const bucket = exact ? req.askCeilings?.exact : req.askCeilings?.upto;
        const terms = TERMS_BY_NETWORK[accepted.network] && bucket && { ...TERMS_BY_NETWORK[accepted.network], maxChargeAtomic: bucket[accepted.network] };
        if (!terms) throw new Error("payment on an unknown network");
        const response = await fetchImpl(`${config.surplusUrl}${upstreamPath}`, { method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}`,
                     ...(req.format.minDiscount != null ? { "X-Min-Discount": String(req.format.minDiscount) } : {}),
                     ...(route === "/v1/messages" ? { "anthropic-version": req.get("anthropic-version") ?? "2023-06-01" } : {}) },
          body: JSON.stringify(req.format.request), signal: AbortSignal.timeout(FORMAT_LIMITS.timeoutMs) });
        const data = await response.json().catch(() => null);
        if (!response.ok || !data || typeof data !== "object") throw new Error(`model answered HTTP ${response.status}`);
        const reported = data.usage?.buyer_cost_micro ?? response.headers.get("x-si-buyer-cost-micro");
        const usage = billUsage({ buyer_cost_micro: reported }, terms, { allowOverrun: exact });
        // `exact` already settled the full ceiling it was offered; see metered.mjs for why there is nothing to override,
        // and for why going over it (usage.overrunAtomic) still answers instead of refusing.
        const billing = exact ? { ...usage, mode: "exact_ceiling", totalAtomic: terms.maxChargeAtomic } : usage;
        if (exact && usage.overrunAtomic) log(`singit-ask: ${route} exact overrun ${usage.overrunAtomic} micro-USDC`);
        if (!exact) setSettlementOverrides(res, { amount: billing.totalAtomic });
        log(`singit-ask: ${route} answered by ${req.format.model} in ${Date.now() - started} ms for ${billing.totalAtomic} micro-USDC`);
        res.json({ ...scrub(data), billing });
      } catch (error) {
        // Not settled: on Base nothing moves, on Solana the escrowed ceiling is refunded in full.
        log(`singit-ask: ${route} no billable answer after ${Date.now() - started} ms (${error.message})`);
        res.status(502).json(refusal(route, 502, "No billable answer was produced. You were not charged; try again."));
      }
    });
  }
}
