// SingIt Ask: one answer from a language model, paid over x402 in USDC on Solana or Base for what it
// actually cost: model tokens + 30% + the network's settlement fee, up to a ceiling that follows the model's
// price (0.003 USDC for the default and cheap ones). Coinbase CDP settles.
//
// The payer is charged only when the answer came back: @x402/express holds the response, and a
// handler that answers 400 or above is never settled (checked in its source, 2.28). So a model
// that fails, times out or returns nothing costs the payer nothing.
//
// The model is bought upstream on Surplus Intelligence (OpenAI-compatible, cheapest seller wins),
// with this service's own key; the payer never sees or needs it.

import { parseBestDiscount } from "./reject-log.mjs";
import express from "express";
import { AUTO_ID, AUTO_DESCRIPTION, TIERS, chooseAuto } from "./auto-router.mjs";
import { createHash, timingSafeEqual } from "node:crypto";
import { errorCodesMiddleware, ERROR_SCHEMAS, errorRef, RATE_LIMIT_HEADERS } from "./errors.mjs";
import { HealthStats, FacilitatorCheck, healthBody, numbersBody, privateStatsBody } from "./health-stats.mjs";
import { declareEip2612GasSponsoringExtension } from "@x402/extensions";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { addUsageRoute, baseOffer, solanaOffer, exactBaseOffer, exactSolanaOffer, ceilings } from "./metered.mjs";
import { addSolanaQuotedRoute } from "./solana-quoted.mjs";
import { UnknownModel, ModelCatalogue, priceGuard, unservableError } from "./catalogue.mjs";
import { LIMITS } from "./limits.mjs";
import { ObservedPrices } from "./observed.mjs";
import { SpendLimitReader, spendLimitMiddleware } from "./spend-limit.mjs";
import { FREE_PREFIX, addFreeRoute, freeTerms } from "./free.mjs";
import { CREDIT_ROUTE, addCreditRoute } from "./credit.mjs";
import { SEARCH_ROUTES, addSearchRoutes, exaRequestBody, exaPriceRange } from "./search.mjs";
import { IMAGE_ROUTE, IMAGE_REQUEST_BODY, addImageRoutes } from "./images.mjs";
import { SPEECH_ROUTE, SPEECH_REQUEST_BODY, addSpeechRoutes } from "./speech.mjs";
import { addPriceRoutes } from "./prices.mjs";
import { VIDEO_ROUTE, addVideoRoutes } from "./video.mjs";
import { FORMAT_ROUTES, formatRequestBody, addFormatRoutes } from "./formats.mjs";
import { withRejectLog } from "./reject-log.mjs";
import { DIRECT_TERMS, QUOTED_ROUTE, TERMS, METERED_ROUTE, SOLANA_TERMS, ceilingAtomic, reasoningHeadroom, SERVICE_NAME, SERVICE_NAMES, ICON_URL, discoveryTags, usd, DEFAULT_EXACT_OUTPUT_FRACTION, isUptoPrimary, UPTO_PRIMARY } from "./pricing.mjs";
import { addModelRoutes, modelRoutePaths, modelOpenapiPaths } from "./model-routes.mjs";

export const BASE = "eip155:8453";
export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const ROUTE = "/v1/chat/completions";
// Linked from llms.txt: the x402 walkthrough, and SingIt's MCP server (paid with a SingIt API key, not x402).
const FIRST_PAID_CALL_DOC = "https://github.com/SingItAi/SingItAI/blob/main/docs/first-paid-call.md";
const MCP_URL = "https://app.singitai.app/mcp";

export { LIMITS };
const ROLES = new Set(["system", "user", "assistant"]);

export class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// The messages and size of an answer, or a RequestError: checked before the model is asked.
export function readRequest(body) {
  if (!body || typeof body !== "object") throw new RequestError(400, "Send a JSON body with messages.");
  if (body.stream) throw new RequestError(400, "Streaming is not offered; send stream: false.");
  const messages = body.messages;
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > LIMITS.messages) {
    throw new RequestError(400, `Send 1 to ${LIMITS.messages} messages.`);
  }
  let chars = 0;
  const clean = messages.map((message) => {
    if (!message || !ROLES.has(message.role) || typeof message.content !== "string") {
      throw new RequestError(400, "Each message needs a role (system, user or assistant) and text content.");
    }
    chars += message.content.length;
    return { role: message.role, content: message.content };
  });
  if (chars > LIMITS.chars) throw new RequestError(400, `Keep the messages under ${LIMITS.chars} characters in all.`);
  const asked = Number(body.max_tokens ?? LIMITS.defaultTokens);
  const maxTokens = Number.isInteger(asked) && asked > 0 ? Math.min(asked, LIMITS.maxTokens) : LIMITS.defaultTokens;
  const model = body.model === undefined || body.model === "singit-ask" ? undefined : body.model;
  if (model !== undefined && (typeof model !== "string" || !model.trim() || model.length > 120)) {
    throw new RequestError(400, "model must be the id of a model listed at GET /v1/models.");
  }
  if (Object.hasOwn(body, "min_tier") && (model?.trim() !== AUTO_ID || !TIERS.includes(body.min_tier))) {
    throw new RequestError(400, "min_tier must be eco, standard or premium and requires model singit/auto.");
  }
  return { ...(Object.hasOwn(body, "min_tier") ? { minTier: body.min_tier } : {}), messages: clean, maxTokens, chars, model: model?.trim(), messageCount: clean.length };
}

// The model an answer is bought from and what it may cost. No model, or the default one, is still looked up on
// Surplus's list when there is one, so its ceiling follows the same formula as any other model (metered.mjs
// clamps it to the standing ceiling); a catalogue that cannot resolve it, or no catalogue at all, keeps the old
// standing ceiling. Any other model must be on Surplus's list, and its ceiling follows its own prices.
export async function chooseModel(config, catalogue, input) {
  const bufferPp = config.priceBufferPp ?? 1;
  if (!input.model || input.model === config.model) {
    const info = catalogue ? await catalogue.resolve(config.model).catch(() => null) : null;
    if (catalogue?.isUnservable?.(config.model)) {
      throw unservableError(catalogue, config.model, RequestError);
    }
    return { ...input, model: config.model, info, isDefault: true,
      ...(info ? priceGuard(catalogue, info.id, bufferPp) : { share: 1, minDiscount: null }) };
  }
  if (!catalogue) throw new RequestError(400, "Choosing a model is not offered here.");
  try {
    const info = await catalogue.resolve(input.model);
    if (catalogue.canAnswer && !(await catalogue.canAnswer(info.id))) {
      if (catalogue.isUnservable?.(info.id)) throw unservableError(catalogue, info.id, RequestError);
      throw new RequestError(400, "No trusted seller offers that model right now. Pick another; nothing was paid.");
    }
    return { ...input, info, ...priceGuard(catalogue, info.id, bufferPp) };
  } catch (error) {
    if (error instanceof UnknownModel) throw Object.assign(new RequestError(400, error.message), { code: "model_not_found" });
    throw error;
  }
}

// `status` is the HTTP status Surplus answered (0 when it never did), `code` its error.code (never its message,
// which is not for matching and may say more than a payer should see), or ours: no_text, max_tokens_spent_reasoning,
// upstream_error, unreachable. The message is safe to log by construction.
export class UpstreamError extends Error {
  constructor(message, { status = 0, code = "upstream_error" } = {}) {
    super(message);
    this.name = "UpstreamError";
    this.status = status;
    this.code = code;
  }
}

// One completion from Surplus Intelligence. Any failure is an UpstreamError: the payer is not charged.
//
// Thinking is switched off and the cheapest seller is asked first. Measured on 12 October over four models: the
// cheapest seller answered as fast as the fastest one within twice its price (1.8-8 s against 2-14 s) and cost the
// same or less; the 21-24 s seen on 4 October did not recur. Some sellers refuse to switch thinking off ("Reasoning
// is mandatory for this endpoint"), and Surplus gives up after two of them; such a refusal costs nothing, so the
// question is asked once more with as little thinking as the seller allows. Surplus documents all three:
// docs/reference/reasoning, docs/marketplace/routing-controls.
//
// `params` is the model's supported_parameters from Surplus's list: Surplus forwards what it does not strip, so a
// parameter the model does not list (temperature on kimi-k3 and others) reaches the provider and is refused, and
// reasoning controls, when stripped, leave a thinking model spending the whole budget on thinking. Use the
// listed spelling: reasoning_effort takes precedence over nested reasoning.effort. Absent (a model
// not on the list), everything is sent as before.
//
// A long non-streamed request can come back as HTTP 200 with an error body, so the body is read for every status.
//
// REASONING_OFF (SI-110): extra body fields, per model, that switch thinking off where the listed spelling does
// not. deepseek-v4-pro lists `reasoning` only, and `reasoning: {effort: "none"}` (the Responses-wire spelling,
// docs/reference/reasoning) was ignored on 27 October: the answer still spent max_tokens thinking. Its cheapest
// trusted seller, the one `si_route: price` picks, is Alibaba DashScope (99% off list on 27 October), whose
// OpenAI-compatible API turns thinking off with `enable_thinking: false` (Model Studio, "DeepSeek" and "Deep
// thinking"); DeepSeek's own API does it with `thinking: {type: "disabled"}` (api-docs.deepseek.com, Thinking
// Mode); OpenRouter sellers read `reasoning.effort: "none"`, still sent. Surplus forwards fields it does not strip
// (a deny-list), so each reaches the seller that understands it. They are sent only with "none": the "low" retry
// asks for some thinking. Any 400 to them (an unknown field, or thinking it will not turn off) costs nothing, so the
// question is asked once more without them, and only that answer decides on the "low" fallback.
export const REASONING_OFF = Object.freeze({
  "deepseek-v4-pro": Object.freeze({ enable_thinking: false, thinking: Object.freeze({ type: "disabled" }) }),
});

// REASONING_HEADROOM (pricing.mjs, SI-112): a model that thinks inside max_tokens and will not stop gets that much
// more room upstream; its ceiling already prices it.
// REASONING_EFFORTS: what SINGIT_ASK_REASONING_EFFORT="claude-opus-5.5=low" (server.mjs, config.reasoningEffort) may
// ask a model for instead of the default "none" (a measurement knob: see pricing.mjs's setReasoningHeadroomOverrides).
export const REASONING_EFFORTS = Object.freeze(["none", "minimal", "low", "medium", "high"]);
export function parseReasoningEffort(text) {
  const out = {};
  for (const part of String(text ?? "").split(",").map((p) => p.trim()).filter(Boolean)) {
    const [id, effort, ...rest] = part.split("=").map((p) => p.trim());
    if (!/^[a-z0-9][a-z0-9._-]*$/i.test(id ?? "") || rest.length || !REASONING_EFFORTS.includes(effort))
      throw new Error(`SINGIT_ASK_REASONING_EFFORT: cannot read "${part}" (want model=${REASONING_EFFORTS.join("|")})`);
    out[id] = effort;
  }
  return out;
}

export async function askSurplus(config, messages, maxTokens, fetchImpl = fetch, model = config.model, { minDiscount, params, timeoutMs = LIMITS.timeoutMs, deadline = Date.now() + timeoutMs } = {}) {
  const sends = (name) => !params || params.includes(name);
  const nestedReasoning = !sends("reasoning_effort") && sends("reasoning");
  const off = Object.hasOwn(REASONING_OFF, model) ? REASONING_OFF[model] : null;
  // "none" unless this process was told to measure another effort for the model (config.reasoningEffort).
  const wanted = config.reasoningEffort && Object.hasOwn(config.reasoningEffort, model) ? config.reasoningEffort[model] : "none";
  const ask = (reasoning, extra = {}) => fetchImpl(`${config.surplusUrl}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}`,
               ...(minDiscount != null ? { "X-Min-Discount": String(minDiscount) } : {}) },
    body: JSON.stringify({ model, messages, max_tokens: maxTokens + reasoningHeadroom(model), ...(sends("temperature") ? { temperature: 0.4 } : {}),
                           ...(sends("reasoning_effort") ? { reasoning_effort: reasoning }
                             : nestedReasoning ? { reasoning: { effort: reasoning } } : {}),
                           ...extra, si_route: { objective: "price" } }),
    signal: AbortSignal.timeout(Math.max(1, Math.min(LIMITS.timeoutMs, timeoutMs, deadline - Date.now()))),
  });
  const refusedReasoning = async (response) => {
    if (response.status !== 400) return false;
    const refusal = await response.clone().json().catch(() => null);
    return refusal?.error?.code === "request_rejected" && /reasoning|thinking/i.test(refusal.error.message ?? "");
  };
  let response;
  try {
    response = await ask(wanted, wanted === "none" ? off ?? {} : {});
    if (off && wanted === "none" && response.status === 400) response = await ask("none");
    // Only a refusal of "none" falls back to "low": a model refusing the effort it was asked for is an error.
    if (wanted === "none" && (sends("reasoning_effort") || nestedReasoning || off) && await refusedReasoning(response)) response = await ask("low");
  } catch (error) {
    throw new UpstreamError(`model unreachable (${error.name})`, { code: "unreachable" });
  }
  const data = await response.json().catch(() => null);
  const choice = data?.choices?.[0];
  if (!response.ok || data?.error || !choice) {
    const code = typeof data?.error?.code === "string" ? data.error.code : "upstream_error";
    throw Object.assign(new UpstreamError(`model answered HTTP ${response.status} (${code})`, { status: response.status, code }), { bestDiscountPct: parseBestDiscount(data?.error?.message) });
  }
  const content = choice.message?.content;
  if (typeof content !== "string" || !content.trim()) {
    const code = choice.finish_reason === "length" ? "max_tokens_spent_reasoning" : "no_text";
    throw Object.assign(new UpstreamError(`model answered HTTP ${response.status} (${code})`, { status: response.status, code }), { bestDiscountPct: parseBestDiscount(data?.error?.message) });
  }
  // The cost is in the body, and in a header too (docs: x-si-buyer-cost-micro); billUsage reads the body's field.
  const header = response.headers?.get?.("x-si-buyer-cost-micro");
  const usage = data.usage?.buyer_cost_micro == null && header != null ? { ...data.usage, buyer_cost_micro: header } : data.usage ?? null;
  return { content, finishReason: choice.finish_reason ?? "stop", usage };
}

// The question's shape, for the 402 challenge (Bazaar) and for the OpenAPI document alike.
// Directories rank a listing by how completely it says what to send and what comes back (Coinbase's Bazaar:
// docs.cdp.coinbase.com/x402/seller/get-discovered), so every field an agent fills in is described.
const autoHeaders = Object.fromEntries(["X-SingIt-Model", "X-SingIt-Tier"].map(name => [name,
  { description: "Concrete model or tier selected for the quote, or actually used for a successful singit/auto answer.", schema: { type: "string" } }]));
const INPUT_SCHEMA = {
  properties: {
    messages: { type: "array", minItems: 1, maxItems: LIMITS.messages, description: "The conversation, oldest first.",
      items: { type: "object", required: ["role", "content"], properties: {
        role: { type: "string", enum: ["system", "user", "assistant"] },
        content: { type: "string", description: `Plain text; all messages together under ${LIMITS.chars} characters.` },
      } } },
    max_tokens: { type: "integer", minimum: 1, maximum: LIMITS.maxTokens, default: LIMITS.defaultTokens,
      description: "The longest answer, in tokens. The payment ceiling grows with it; the charge follows what was used." },
    model: { type: "string", examples: ["deepseek-v4.1-flash", "claude-opus-5.5", "gpt-5.5", "gemini-3.1-pro"],
      description: "Any model id from GET /v1/models; the ceiling follows its price. Omit for the default model." },
    stream: { type: "boolean", enum: [false], description: "Streaming is not offered." },
  },
  required: ["messages"],
};

const OUTPUT_SCHEMA = {
  type: "object", required: ["choices", "billing"],
  properties: {
    id: { type: "string" },
    object: { type: "string", const: "chat.completion" },
    created: { type: "integer", description: "Unix seconds" },
    model: { type: "string" },
    choices: { type: "array", items: { type: "object", properties: {
      index: { type: "integer" },
      message: { type: "object", properties: { role: { type: "string" }, content: { type: "string" } } },
      finish_reason: { type: "string", description: "stop, or length when max_tokens ended the answer" },
    } } },
    usage: { type: "object", description: "Token counts and the model's own cost in micro-USD (buyer_cost_micro)" },
    billing: { type: "object", description: "What was charged, in USDC micro-units", properties: {
      providerCostAtomic: { type: "string" }, markupAtomic: { type: "string" },
      totalAtomic: { type: "string", description: "The amount settled" }, currency: { type: "string", const: "USDC" },
    } },
  },
};

// Which scheme to sign for a model, when one is clearly better (pricing.mjs's isUptoPrimary): spread into a model's
// /pricing.json and /v1/models entries, nothing for the others. Advice only: every offer stays in the 402.
const schemeAdvice = (id) => (isUptoPrimary(id)
  ? { recommendedScheme: UPTO_PRIMARY.recommended, recommendedSchemeWhy: UPTO_PRIMARY.why } : {});

// The one question /pricing.json prices every model against: a single short message, and the max_tokens a
// request gets when it omits the field (LIMITS.defaultTokens), the same default readRequest applies.
const PRICING_REFERENCE = Object.freeze({ content: "What is the capital of France?", maxTokens: LIMITS.defaultTokens });

// What a real POST of PRICING_REFERENCE's exact size would be offered: readRequest's own shape, fed through the
// same chooseModel and ceilings() the 402 challenge calls (app.mjs's addUsageRoute, metered.mjs). `upto` is the
// ceiling it authorizes (it settles only actual usage once an answer comes back, which is normally less);
// `exact` is the flat ceiling it is charged, with no metering and no refund.
async function pricingFor(config, catalogue, modelId) {
  const chars = PRICING_REFERENCE.content.length;
  const input = { messages: [{ role: "user", content: PRICING_REFERENCE.content }], maxTokens: PRICING_REFERENCE.maxTokens,
    chars, messageCount: 1, model: modelId };
  const askInput = modelId === AUTO_ID ? await chooseAuto(config, catalogue, input, RequestError)
    : await chooseModel(config, catalogue, input);
  const exactOutputFraction = config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION;
  const { upto, exact } = ceilings(askInput.info, askInput, exactOutputFraction);
  const price = net => ({ priceAtomic: upto[net], usd: Number(usd(upto[net])) });
  const ceiling = net => ({ ceilingAtomic: exact[net], usd: Number(usd(exact[net])) });
  return {
    upto: { base: price(BASE), solana: price(SOLANA) },
    exact: { base: ceiling(BASE), solana: ceiling(SOLANA) },
  };
}

// The same expected-price-per-million-tokens shown at GET /v1/models (observed cost if this service has answered
// that model before, else the trusted market's cheapest qualifying share), null when neither is known.
function perMillionFor(m, observed, market) {
  const seen = observed.share(m.id), trusted = market.get(m.id)?.trustedShare;
  const share = seen ?? trusted ?? null;
  const listed = m.promptUsdPerToken > 0 || m.completionUsdPerToken > 0;
  const ours = share === null || !listed ? null : share * (1 + TERMS.markupBps / 10_000);
  return ours === null ? null : { input: m.promptUsdPerToken * 1e6 * ours, output: m.completionUsdPerToken * 1e6 * ours };
}

// The least and the most one answer can be charged, for discovery. The least is Base's settlement fee; the most is
// the dearest listed model's ceiling for the largest question and answer this service takes, on Solana's terms.
// `charsPerToken: 1` matches the `upto` authorization this max actually describes (see pricing.mjs's
// `ceilingAtomic`): the loosest input estimate, so a dense worst-case prompt is never quoted below what it costs.
export async function priceRange(catalogue) {
  const size = { chars: LIMITS.chars, maxTokens: LIMITS.maxTokens, messageCount: LIMITS.messages, charsPerToken: 1 };
  const listed = catalogue ? await catalogue.list().catch(() => []) : [];
  const most = listed.reduce((top, m) => {
    const c = BigInt(ceilingAtomic(m, size, SOLANA_TERMS));
    return c > top ? c : top;
  }, BigInt(SOLANA_TERMS.maxChargeAtomic));
  return { min: usd(TERMS.settlementFeeAtomic), max: usd(most) };
}

const AMOUNT = { type: "object", properties: { count: { type: "integer" }, atomic: { type: "string" }, usd: { type: "string" } } };
// The free wallet report, served by the gateway (sign402-gateway/README.md documents every field).
const RECONCILIATION_SCHEMA = {
  type: "object",
  required: ["address", "chain", "known", "window", "spent", "creditTopUps", "allowance", "generatedAt"],
  properties: {
    address: { type: "string" }, chain: { type: "string", enum: ["base", "solana"] }, known: { type: "boolean" },
    window: { type: "object", properties: { days: { type: "integer" }, since: { type: "integer" }, until: { type: "integer" } } },
    spent: { type: "object", properties: {
      totalAtomic: { type: "string" }, totalUsd: { type: "string" }, count: { type: "integer" },
      byCategory: { type: "object", additionalProperties: AMOUNT },
      byDay: { type: "array", items: { type: "object", properties: {
        date: { type: "string" }, atomic: { type: "string" }, usd: { type: "string" }, count: { type: "integer" } } } } } },
    creditTopUps: AMOUNT,
    allowance: { type: ["object", "null"], properties: {
      configured: { type: "boolean" }, state: { type: "string", enum: ["active", "paused", "expired", "none", "granted", "waiting_for_grant"] },
      dailyCapAtomic: { type: "string" }, perPurchaseCapAtomic: { type: "string" }, spentTodayAtomic: { type: "string" },
      remainingTodayAtomic: { type: "string" }, expiry: { type: "integer" }, limiter: { type: ["string", "null"] } } },
    generatedAt: { type: "integer" },
  },
};

const EXAMPLE = {
  input: { messages: [{ role: "user", content: "Where is good coffee near the Colosseum?" }], max_tokens: 400 },
  // A real answer's shape and charge (default model, Base): 6 micro-USD of tokens, 2 markup, 1000 settlement fee.
  output: {
    id: "singit-ask-1791932400000", object: "chat.completion", created: 1791932400, model: "singit-ask",
    choices: [{ index: 0, message: { role: "assistant",
      content: "Around the Colosseum, try Caffè Propaganda on Via Claudia or Bar Martini for an espresso with a view." },
      finish_reason: "stop" }],
    usage: { prompt_tokens: 14, completion_tokens: 31, total_tokens: 45, buyer_cost_micro: 6 },
    billing: { ...TERMS, providerCostAtomic: "6", markupAtomic: "2", totalAtomic: "1008", currency: "USDC" },
  },
};

// Error bodies, as the handlers send them: `{ error: { type, message } }`; /v1/messages uses Anthropic's own
// envelope. Only codes a route really answers with are listed in openapi.json (test/openapi-errors.test.mjs).
const ERROR_SCHEMA = errorRef("ErrorResponse");
const ANTHROPIC_ERROR_SCHEMA = errorRef("AnthropicErrorResponse");
const errorBody = (description, schema = ERROR_SCHEMA, example) => ({
  description, content: { "application/json": { schema, ...(example ? { example } : {}) } },
});
const errorExample = (type, message, code, schema) => ({
  ...(schema === ANTHROPIC_ERROR_SCHEMA ? { type: "error" } : {}),
  error: { type, message, code },
});
// Documentation fixtures only; the live builders and handlers remain unchanged.
const chatExampleOffers = [
  { scheme: "upto", network: BASE, payTo: "<pay-to address>", maxTimeoutSeconds: 120,
    asset: "<USDC contract on Base>", amount: "1500", extra: { name: "USD Coin", version: "2",
      billing: { version: 1, mode: "actual_usage", markupBps: 3000, settlementFeeAtomic: "1000", maxChargeAtomic: "1500" } } },
  { scheme: "exact", network: BASE, payTo: "<pay-to address>", maxTimeoutSeconds: 120,
    asset: "<USDC contract on Base>", amount: "1500", extra: { name: "USD Coin", version: "2",
      billing: { version: 1, mode: "exact_ceiling", markupBps: 3000, settlementFeeAtomic: "1000", maxChargeAtomic: "1500" } } },
];
const exactExampleOffers = timeout => [
  { scheme: "exact", network: BASE, amount: "<amount in USDC micro-units>", asset: "<USDC contract on Base>",
    payTo: "<pay-to address>", maxTimeoutSeconds: timeout, extra: { name: "USD Coin", version: "2" } },
  { scheme: "exact", network: SOLANA, amount: "<amount in USDC micro-units>", asset: "<USDC mint on Solana>",
    payTo: "<pay-to address>", maxTimeoutSeconds: timeout },
];
const PAYMENT_REQUIRED = (exactTimeout) => ({ description: "Payment Required: the x402 v2 challenge is in the PAYMENT-REQUIRED response header "
    + "(base64 JSON: x402Version, resource, accepts); the body contains matching x402Version and accepts only for a payment challenge. Sign one of the accepts and retry "
    + "with PAYMENT-SIGNATURE. If a payment was submitted but could not be settled, the 402 has no PAYMENT-REQUIRED header "
    + "and the body contains only error and code (payment_required).",
  ...(exactTimeout ? {} : { description: "Chat challenge example: one Base offer per scheme. The full accepts order is upto Base, upto Solana, exact Base, exact Solana. " +
    "The PAYMENT-REQUIRED header carries the base64 JSON challenge; settlement failures have only error and code in the body." }),
  headers: { "PAYMENT-REQUIRED": { description: "Base64-encoded JSON x402 payment requirements", schema: { type: "string" } } },
  content: { "application/json": { schema: errorRef("PaymentRequired402"), example: {
    x402Version: 2, accepts: exactTimeout ? exactExampleOffers(exactTimeout) : chatExampleOffers,
    error: "payment_required", code: "payment_required",
  } } } });
const BAD_REQUEST = "The request cannot be answered here (unreadable JSON, a field out of range, or a model that is not offered); nothing was charged";
const TOO_LARGE = "The JSON body is over 128 kB; nothing was charged";
const UNAVAILABLE = "The model list or a seller could not be reached; try again shortly. Nothing was charged";
const DUPLICATE = "This payment signature was already used; nothing was charged again";
// Shared by every paid POST: express.json runs first and refuses what it cannot read.
// A paid request that produced nothing billable is a 503 with Retry-After, never a 502: Cloudflare replaces an
// origin 502/504 with its own HTML page, so the JSON saying "not charged" would never reach the payer.
const RETRY_AFTER = { "Retry-After": { description: "Seconds to wait before trying again", schema: { type: "integer" } } };
export const MAX_TOKENS_TOO_SMALL = "max_tokens too small: a reasoning model spent it all thinking and wrote no answer; "
  + "raise max_tokens. Nothing was charged";
const paidErrors = ({ notFound, duplicate, noAnswer, unavailable, reasoning, schema, exactTimeout, exampleMessage = "Try again shortly.", exampleCode = "upstream_unavailable" } = {}) => ({
  400: errorBody(BAD_REQUEST, schema, errorExample("invalid_request_error", "Send valid JSON.", "invalid_json", schema)),
  402: PAYMENT_REQUIRED(exactTimeout),
  413: errorBody(TOO_LARGE, schema, errorExample("invalid_request_error", "The request body is too large.", "payload_too_large", schema)),
  ...(notFound ? { 404: errorBody(notFound, schema) } : {}),
  ...(duplicate ? { 409: errorBody(DUPLICATE, schema) } : {}),
  ...(reasoning ? { 422: errorBody(MAX_TOKENS_TOO_SMALL, schema, errorExample("invalid_request_error", MAX_TOKENS_TOO_SMALL, "max_tokens_too_small", schema)) } : {}),
  503: { ...errorBody(unavailable ?? `${UNAVAILABLE}. Also: ${noAnswer}`, schema,
    errorExample(schema === ANTHROPIC_ERROR_SCHEMA ? "api_error" : "upstream_error", exampleMessage, exampleCode, schema)), headers: RETRY_AFTER },
});

export function createApp(config, { facilitatorClient, upstream = askSurplus, fetchImpl = fetch, log = console.error,
                                    exaKey = config.exaKey ?? null, exaFetch = fetchImpl, priceBook = null,
                                    catalogue = config.surplusUrl ? new ModelCatalogue({
                                      url: config.surplusUrl, fetchImpl, blocked: config.blockedProviders ?? [],
                                      unservableTtlMs: config.unservableTtlMs }) : null,
                                    observed = new ObservedPrices({ file: config.observedFile }),
                                    spendLimits = priceBook?.publicClient ? new SpendLimitReader({ publicClient: priceBook.publicClient }) : null }) {
  // Every refusal Surplus answers a paid question with is logged once, with its error code and any price it names.
  fetchImpl = withRejectLog(fetchImpl, { surplusUrl: config.surplusUrl, log });
  const app = express();
  app.locals.paidRoutes = new Set([CREDIT_ROUTE]);
  app.locals.healthStats = new HealthStats({ file: config.statsFile ?? null,
    isPaidRoute: route => app.locals.paidRoutes.has(route),
    isPublicRoute: route => discoverableRoutes().includes(route), ownPayers: config.ownPayers ?? [], log });
  app.locals.facilitatorCheck = new FacilitatorCheck({ client: facilitatorClient });
  app.locals.catalogue = catalogue;  // server.mjs warms it
  app.disable("x-powered-by");
  // cloudflared connects over loopback and supplies the public HTTPS scheme.
  app.set("trust proxy", "loopback");
  app.use(errorCodesMiddleware);
  app.use((req, res, next) => {
    res.on("finish", () => {
      try {
        if (res.statusCode === 402) app.locals.healthStats.recordChallenge({ route: req.path,
          method: req.method, challenge: res.hasHeader("PAYMENT-REQUIRED") });
      } catch {}
    });
    next();
  });
  app.use((req, res, next) => {
    const header = "Access-Control-Expose-Headers";
    const writeHead = res.writeHead;
    res.writeHead = function (...args) {
      const values = [this.getHeader(header) ?? []];
      const index = typeof args[1] === "string" ? 2 : 1;
      const headers = args[index];
      if (headers) {
        const entries = Array.isArray(headers)
          ? Array.from({ length: headers.length / 2 }, (_, i) => [headers[i * 2], headers[i * 2 + 1]])
          : Object.entries(headers);
        const rest = entries.filter(([name, value]) => {
          if (name.toLowerCase() !== header.toLowerCase()) return true;
          values.push(value);
          return false;
        });
        args[index] = Array.isArray(headers) ? rest.flat() : Object.fromEntries(rest);
      }
      const seen = new Set();
      const merged = [...values, "X-SingIt-Model", "X-SingIt-Tier"]
        .flat().flatMap(value => String(value).split(",")).map(value => value.trim())
        .filter(value => {
          const key = value.toLowerCase();
          if (!value || seen.has(key)) return false;
          seen.add(key);
          return true;
        });
      this.setHeader(header, merged.join(", "));
      return writeHead.apply(this, args);
    };
    next();
  });
  if (config.statsToken && config.statsToken.length < 32) log("singit-ask: SINGIT_ASK_STATS_TOKEN must be at least 32 characters; internal stats disabled");
  if (config.statsToken?.length >= 32) {
    const digest = value => createHash("sha256").update(value).digest();
    const expected = digest(`Bearer ${config.statsToken}`);
    app.get("/internal/stats", (req, res) => {
      res.set("Cache-Control", "no-store");
      if (!timingSafeEqual(expected, digest(req.get("Authorization") || "")))
        return res.status(401).json({ error: { code: "unauthorized", message: "Unauthorized" } });
      res.json(privateStatsBody(app.locals.healthStats));
    });
  }
  app.use(express.json({ limit: "128kb" })); // unreadable JSON is refused before any payment is asked
  // ...with a JSON body like every other refusal, not Express's HTML page (documented in openapi.json).
  app.use((error, req, res, next) => {
    const tooLarge = error.type === "entity.too.large";
    if (!tooLarge && error.type !== "entity.parse.failed") return next(error);
    const code = tooLarge ? "payload_too_large" : "invalid_json";
    const message = tooLarge ? "The request body is too large." : "Send valid JSON.";
    res.status(tooLarge ? 413 : 400).json(req.path === "/v1/messages"
      ? { type: "error", error: { type: "invalid_request_error", message, code } }
      : { error: { type: "invalid_request_error", message, code } });
  });

  // The caller's own spend limit in the 402 (X-SingIt-Limiter) and a receipt on every settled answer: only headers
  // and the 402's body; no price, offer or settlement changes. Before every paid route below.
  app.use(spendLimitMiddleware({ reader: spendLimits, log }));

  const pricing = { [SOLANA]: SOLANA_TERMS, [BASE]: TERMS };
  const free = freeTerms(config);
  const freeOffer = free.models.length ? { models: free.models.map((m) => FREE_PREFIX + m), endpoint: ROUTE,
    perVisitorPerDay: free.perVisitorPerDay, maxTokens: free.maxTokens, maxChars: free.chars, budgetUsdPerDay: free.budgetUsdPerDay } : null;
  const freeLimits = { perVisitorPerDay: free.perVisitorPerDay, maxTokens: free.maxTokens, maxChars: free.chars };
  const freeListed = free.models.map((m) => ({ id: FREE_PREFIX + m, object: "model", owned_by: "singit", free: true,
    limits: freeLimits }));
  // The question's shape; where free answers are on, the model field says so (Bazaar and OpenAPI read it).
  const inputSchema = freeOffer ? { ...INPUT_SCHEMA, properties: { ...INPUT_SCHEMA.properties,
    model: { ...INPUT_SCHEMA.properties.model, description: `${INPUT_SCHEMA.properties.model.description} `
      + `Free to try, no payment: ${freeOffer.models.join(", ")}.` } } } : INPUT_SCHEMA;
  // What a payer pays on top of the model's own cost: the markup, and one settlement fee per answer.
  const fees = { markupPct: TERMS.markupBps / 100,
    perAnswerUsd: { [BASE]: Number(usd(TERMS.settlementFeeAtomic)), [SOLANA]: Number(usd(SOLANA_TERMS.settlementFeeAtomic)) } };
  const origin = req => `${req.protocol}://${req.get("host")}`;
  // Public, read-only documents: the website reads them straight from the browser.
  const open = (_req, res, next) => { res.set("Access-Control-Allow-Origin", "*"); next(); };
  // At most 500 characters: Coinbase's facilitator rejects longer descriptions.
  const DESCRIPTION = "Ask a language model without an API key: any model listed at GET /v1/models (Claude, GPT, Gemini, "
    + `Grok, DeepSeek, Qwen and more; default ${config.model}). OpenAI chat-completions request. Pays actual token `
    + "cost + 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana), up to a ceiling that follows the model's "
    + "price; nothing is charged without an answer. Exact payers instead pay a flat price for a typical-length answer.";
  // <=300 characters: Coinbase's Bazaar ranks listings by metadata completeness, and a description that is half
  // about billing reads as incomplete on what the endpoint does. Billing stays in DESCRIPTION above (openapi.json,
  // /.well-known/x402) for the humans/agents that read terms there. It opens with the category words a buyer
  // searches for and names the brand once, later, not as a prefix (SI-90: Bazaar's search is semantic).
  const DISCOVERY_DESCRIPTION = "LLM chat completions API, OpenAI-compatible: one endpoint for 100+ models (Claude, "
    + "GPT, Gemini, Grok, DeepSeek, Qwen), no API key or account, pay per call in USDC. Call it when an agent needs an "
    + "LLM answer, a second opinion, or a model it has no key for. SingIt Ask; models at GET /v1/models.";
  app.get("/health", open, (_req, res) => {
    res.set("Cache-Control", "no-store");
    res.json({ ...healthBody(app.locals.healthStats, app.locals.facilitatorCheck.get(), config.model, catalogue?.unservableSnapshot?.()),
               ok: true, service: "singit-ask", model: config.model, facilitator: "coinbase-cdp", pricing, free: freeOffer,
               metered: TERMS, ...(config.directSolana ? { directSolana: { endpoint: QUOTED_ROUTE, ...DIRECT_TERMS } } : {}) });
  });
  app.get("/numbers.json", open, (_req, res) => {
    res.set("Cache-Control", "public, max-age=60").json(numbersBody(app.locals.healthStats));
  });
  // Free: the default and every other model that can be asked for, with the list prices its ceiling is built from.
  app.get("/v1/models", open, async (_req, res) => {
    // Only models that can be answered now, including the default after a routing rejection.
    const listed = catalogue ? await catalogue.list().catch(() => []) : [];
    const answerable = await Promise.all(listed.map((m) => (catalogue.canAnswer ? catalogue.canAnswer(m.id) : true)));
    const models = listed.filter((m, i) => answerable[i] || (m.id === config.model && !catalogue.isUnservable?.(m.id)));
    const market = catalogue?.market ? await catalogue.market().catch(() => new Map()) : new Map();
    res.json({ object: "list", default: config.model, endpoint: ROUTE, pricing, fees, free: freeOffer,
      data: [...(catalogue.isUnservable?.(config.model) ? [] :
        [{ id: "singit-ask", object: "model", owned_by: "singit", upstream: config.model }]),
        // Virtual routes have no fixed token rate; priced-model consumers use property presence.
        { id: AUTO_ID, object: "model", owned_by: "singit", description: AUTO_DESCRIPTION },
        ...freeListed,
        ...models.map(m => {
          // The price an answer is expected to cost: what this service's own answers from the model cost, or,
          // before the first, the share of list price Surplus's trusted sellers ask. Never the best offer.
          const seen = observed.share(m.id), trusted = market.get(m.id)?.trustedShare;
          const share = seen ?? trusted ?? null;
          // What a payer is expected to pay per million tokens: that cost plus the markup (the per-answer fee is in
          // `fees`), and how much below the model's list price that is.
          // A model Surplus lists at no price has no saving to show.
          const listed = m.promptUsdPerToken > 0 || m.completionUsdPerToken > 0;
          const ours = share === null || !listed ? null : share * (1 + TERMS.markupBps / 10_000);
          return { id: m.id, object: "model", owned_by: m.provider || "surplus", name: m.name, contextLength: m.contextLength,
            tags: m.tags ?? [],
            ...schemeAdvice(m.id),
            usdPerMillionTokens: { input: m.promptUsdPerToken * 1e6, output: m.completionUsdPerToken * 1e6 },
            ...(share === null ? {} : { expectedUsdPerMillionTokens: { input: m.promptUsdPerToken * 1e6 * share,
              output: m.completionUsdPerToken * 1e6 * share, basis: seen === null ? "trusted" : "observed" } }),
            ...(ours === null ? {} : {
              singitUsdPerMillionTokens: { input: m.promptUsdPerToken * 1e6 * ours, output: m.completionUsdPerToken * 1e6 * ours },
              savingsVsListPct: Math.round((1 - ours) * 100) }) };
        })] });
  });

  // Free: the same per-model ceilings the 402 challenge offers, computed by the same chooseModel/ceilingAtomic
  // functions, for one reference question — so a buyer (or the website's pricing page) can compare SingIt's
  // price without sending a real request or a payment. 503 while the catalogue has never loaded, rather than a
  // list with invented or stale prices; a catalogue that loaded once and later failed to refresh keeps serving
  // its last list, same as /v1/models.
  app.get("/pricing.json", open, async (_req, res) => {
    if (catalogue && catalogue.models === null) {
      res.set("Retry-After", "5");
      return res.status(503).json({ error: { message: "The model catalogue has not loaded yet. Try again shortly.",
        type: "upstream_error", code: "upstream_unavailable" } });
    }
    const listed = catalogue ? await catalogue.list().catch(() => []) : [];
    const answerable = await Promise.all(listed.map((m) => (catalogue.canAnswer ? catalogue.canAnswer(m.id) : true)));
    const models = listed.filter((m, i) => answerable[i] || (m.id === config.model && !catalogue.isUnservable?.(m.id)));
    const market = catalogue?.market ? await catalogue.market().catch(() => new Map()) : new Map();
    const priced = [];
    for (const m of models) {
      let p;
      try { p = await pricingFor(config, catalogue, m.id); } catch { continue; } // not resolvable right now; omit it
      const perMillion = perMillionFor(m, observed, market);
      priced.push({ id: m.id, name: m.name, ...p, ...(perMillion ? { perMillion } : {}), ...schemeAdvice(m.id) });
    }
    let autoPrice = { upto: null, exact: null };
    try { autoPrice = await pricingFor(config, catalogue, AUTO_ID); } catch { /* no eligible healthy model */ }
    priced.push({ id: AUTO_ID, name: "SingIt Auto", description: AUTO_DESCRIPTION, ...autoPrice });
    const chars = PRICING_REFERENCE.content.length;
    res.json({
      generatedAt: new Date().toISOString(),
      currency: "USDC",
      referenceRequest: { messages: 1, chars, inputTokens: Math.ceil(chars / 4) + 4, maxOutputTokens: PRICING_REFERENCE.maxTokens },
      networks: {
        base: { chain: BASE, settlementFeeAtomic: TERMS.settlementFeeAtomic, settlementFeeUsd: Number(usd(TERMS.settlementFeeAtomic)) },
        solana: { chain: SOLANA, settlementFeeAtomic: SOLANA_TERMS.settlementFeeAtomic,
                  settlementFeeUsd: Number(usd(SOLANA_TERMS.settlementFeeAtomic)) },
      },
      note: "upto settles actual usage: the amount shown is the ceiling the reference request would be asked to "
        + "authorize, and a real answer normally settles for less once it comes back. exact pays the amount "
        + "shown, flat, with no metering and no refund: a price for a typical-length answer (about "
        + `${Math.round((config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION) * 100)}% of max_tokens), `
        + "and the whole answer is still returned. A model with recommendedScheme upto (Claude Opus) is best paid with "
        + "upto: its exact price carries room for thinking tokens, so it is usually dearer than the answer used.",
      models: priced,
    });
  });

  // Free: how directories (x402scan, agents) find the paid route. x402scan reads this first, then /.well-known/x402.
  app.get("/openapi.json", open, async (req, res) => {
    const price = await priceRange(catalogue);
    res.json({
      openapi: "3.1.0",
      info: { title: "SingIt Ask", version: "1.0.0",
              description: "Language-model answers paid per answer over x402 in USDC on Base or Solana. No accounts or API keys."
                + (freeOffer ? ` Free to try: model ${freeOffer.models.join(", ")}, ${freeOffer.perVisitorPerDay} answers a day, no payment.` : ""),
              "x-guidance": "POST JSON in OpenAI chat-completions format to the paths below. A first request without "
                + "payment returns 402 with the challenge in the PAYMENT-REQUIRED header; sign an x402 v2 \"exact\" or "
                + "\"upto\" USDC authorization on Base or Solana and retry the same request with a PAYMENT-SIGNATURE "
                + "header. List callable models at GET /v1/models and free (unpaid) prices at GET /v1/prices. Nothing "
                + "is charged unless an answer comes back.",
              contact: { name: "SingIt", email: "contact@singitai.app", url: "https://singitai.app" } },
      servers: [{ url: origin(req) }],
      paths: {
        [ROUTE]: { post: {
          operationId: "createChatCompletion", summary: "Generate an answer from a language model",
          description: DESCRIPTION + (freeOffer ? ` Models prefixed ${FREE_PREFIX} (${freeOffer.models.join(", ")}) answer `
            + `with no payment, within ${freeOffer.perVisitorPerDay} answers a day per network, ${freeOffer.maxTokens} tokens `
            + `and ${freeOffer.maxChars} characters each.` : ""),
          ...(freeOffer ? { "x-free-models": freeListed } : {}),
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", ...price }, protocols: [{ x402: {} }] },
          requestBody: { required: true, content: { "application/json": {
            schema: { type: "object", ...inputSchema, properties: { ...inputSchema.properties,
              min_tier: { type: "string", enum: TIERS, description: "Minimum tier; only valid with model singit/auto." },
              model: { ...inputSchema.properties.model, description: inputSchema.properties.model.description + " " + AUTO_DESCRIPTION } } }, example: EXAMPLE.input } } },
          responses: {
            200: { description: "The answer, with what it was charged", headers: autoHeaders,
                   content: { "application/json": { schema: OUTPUT_SCHEMA, example: EXAMPLE.output } } },
            ...Object.fromEntries(Object.entries(paidErrors({ noAnswer: "No billable answer came back; nothing was charged", reasoning: true }))
              .map(([status, response]) => [status, { ...response, headers: { ...response.headers, ...autoHeaders } }])),
            429: {
              description: "Free answers (model free/…) used up for today, or busy; nothing was charged",
              content: { "application/json": { schema: errorRef("RateLimited429"), examples: {
                daily: { summary: "Daily allowance exhausted",
                  description: "Headers: Retry-After: 3600; X-RateLimit-Limit: 20; X-RateLimit-Remaining: 0; X-RateLimit-Reset: 1793491200 (next UTC midnight).",
                  value: errorExample("rate_limit_error", "That is today's free answers from this network. Pay per answer instead: drop the free/ prefix (x402, USDC on Base or Solana), or come back tomorrow (UTC).", "free_tier_exhausted") },
                busy: { summary: "Free answer concurrency full",
                  description: "Headers: Retry-After: 5 only; X-RateLimit-* headers are omitted.",
                  value: errorExample("rate_limit_error", "Free answers are busy right now. Try again in a few seconds, or pay per answer: drop the free/ prefix.", "free_tier_busy") },
              } } },
              headers: { ...Object.fromEntries(Object.entries(RATE_LIMIT_HEADERS).map(([name, header]) => [name, {
                ...header, description: header.description + "; daily per-visitor allowance, omitted while busy",
                example: { "X-RateLimit-Limit": 20, "X-RateLimit-Remaining": 0, "X-RateLimit-Reset": 1793491200 }[name],
              }])), "Retry-After": { description: "Seconds until the next free answer", schema: { type: "string" },
                examples: { daily: { value: "3600" }, busy: { value: "5" } } } },
            },
          },
          security: [{ x402: [] }],
        } },
        ...(exaKey ? Object.fromEntries(Object.keys(SEARCH_ROUTES).map((route) => [route, { post: {
          operationId: { "/v1/exa/search": "exaSearch", "/v1/exa/contents": "exaContents", "/v1/exa/answer": "exaAnswer" }[route],
          summary: { "/v1/exa/search": "Search the web with Exa and return ranked results",
                     "/v1/exa/contents": "Fetch page contents from Exa for up to 10 URLs",
                     "/v1/exa/answer": "Generate a cited answer with Exa" }[route],
          description: "Exa's own request and answer, paid per request over x402: Exa's list price with no markup by default. "
            + "Nothing is charged without an answer.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", ...exaPriceRange() }, protocols: [{ x402: {} }] },
          requestBody: exaRequestBody(route),
          responses: { 200: { description: "Exa's answer, with billing" },
                       ...paidErrors({ exactTimeout: 120, exampleMessage: "Exa did not answer. You were not charged.", duplicate: true, unavailable: "Exa did not answer; nothing charged. Retry after Retry-After" }) },
          security: [{ x402: [] }],
        } }])) : {}),
        // Per-model routes (#66) answer the same errors as /v1/chat/completions minus the free tier's 429, plus 404
        // for a model the catalogue does not list; documented with the shared JSON error shapes (SI-63).
        ...(catalogue ? Object.fromEntries(Object.entries(await modelOpenapiPaths(catalogue, { inputSchema: INPUT_SCHEMA,
          outputSchema: OUTPUT_SCHEMA, exampleOutput: EXAMPLE.output, fallbackPrice: price })).map(([route, item]) => [route,
          { post: { ...item.post, responses: { 200: item.post.responses[200],
            ...paidErrors({ notFound: "This model is not available; nothing was charged",
                            noAnswer: "No billable answer came back; nothing was charged", reasoning: true }) } } }])) : {}),
        ...(catalogue ? Object.fromEntries(Object.keys(FORMAT_ROUTES).map((route) => [route, { post: {
          operationId: route === "/v1/messages" ? "createMessage" : "createResponse",
          summary: route === "/v1/messages" ? "Create a message in Anthropic's format, any listed model" : "Create a response in OpenAI's format, any listed model",
          description: "The request in that API's own format, without streaming. Pays actual token cost + 30% + a settlement "
            + "fee (0.001 USDC on Base, 0.002 on Solana), up to a ceiling that follows the model's price and the request's "
            + "size; nothing is charged without an answer. Models: GET /v1/models.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", ...price }, protocols: [{ x402: {} }] },
          requestBody: formatRequestBody(route),
          responses: { 200: { description: "The answer in that API's format, with billing" },
                       ...paidErrors({ notFound: "No such model; nothing was charged", noAnswer: "No billable answer; nothing charged", reasoning: true,
                                       schema: route === "/v1/messages" ? ANTHROPIC_ERROR_SCHEMA : ERROR_SCHEMA }) },
          security: [{ x402: [] }],
        } }])) : {}),
        ...(catalogue ? { [IMAGE_ROUTE]: { post: {
          operationId: "createImage", summary: "Generate images with any of 36 image models",
          description: "OpenAI images request (model, prompt, n, size). Paid per request over x402: the model's list price "
            + "+ 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana). Models and prices: GET /v1/images/models. "
            + "Nothing is charged without an image.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.014", max: "2.602" }, protocols: [{ x402: {} }] },
          requestBody: IMAGE_REQUEST_BODY(),
          responses: { 200: { description: "The images, with billing" },
                       ...paidErrors({ exactTimeout: 180, exampleMessage: "No image was made. You were not charged.", exampleCode: "no_billable_answer", notFound: "No such image model; nothing was charged", duplicate: true, noAnswer: "No image; nothing charged" }) },
          security: [{ x402: [] }],
        } }, "/v1/images/models": { get: { operationId: "listImageModels", summary: "List image models and their prices",
          responses: { 200: { description: "Model list" } }, security: [] } },
          [SPEECH_ROUTE]: { post: {
            operationId: "createSpeech", summary: "Generate speech with any of 11 voice models",
            description: "OpenAI speech request (model, input, voice, response_format, speed). Paid per request over x402: the "
              + "model's list price for the text + 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana). Models: "
              + "GET /v1/audio/models. Nothing is charged without audio.",
            "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.001", max: "0.336" }, protocols: [{ x402: {} }] },
            requestBody: SPEECH_REQUEST_BODY(),
            responses: { 200: { description: "The audio", content: { "audio/mpeg": {} } },
                         ...paidErrors({ exactTimeout: 120, exampleMessage: "No audio was made. You were not charged.", exampleCode: "no_billable_answer", notFound: "No such voice model; nothing was charged", duplicate: true, noAnswer: "No audio; nothing charged" }) },
            security: [{ x402: [] }],
          } },
          "/v1/audio/models": { get: { operationId: "listAudioModels", summary: "List voice models and their prices",
            responses: { 200: { description: "Model list" } }, security: [] } },
          ...(config.videoSold ? {
          [VIDEO_ROUTE]: { post: {
            operationId: "createVideo", summary: "Generate a video clip with any of 30+ text-to-video models",
            description: "Body: model, prompt, and optionally duration_seconds, resolution, aspect_ratio, audio, fallback. "
              + "Paid per clip over x402: the model's list price (minimum $0.20) + 30% + a settlement fee "
              + "(0.001 USDC on Base, 0.002 on Solana), charged "
              + "once the clip is ready. Answers once the clip is ready (up to about 85 seconds); if it is not, "
              + "or the job fails, you get a 503 and are not charged; the response carries the job id, token and file URLs. "
              + "Poll GET /v1/video/generations/{id} "
              + "with header X-Job-Token for free, then GET /v1/video/generations/{id}/content. Models: GET /v1/video/models. "
              + "Returns 503 no_healthy_sellers before any 402 when neither the requested model nor a compatible substitute is healthy.",
            "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.261", max: "1.302" }, protocols: [{ x402: {} }] },
            requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["model", "prompt"],
              properties: { model: { type: "string" }, prompt: { type: "string", maxLength: 2000 }, duration_seconds: { type: "integer" },
                            resolution: { type: "string" }, aspect_ratio: { type: "string" }, audio: { type: "boolean" },
                            fallback: { type: "boolean", default: true,
                              description: "Set false to refuse a substitute model when the chosen one has no healthy seller; "
                                + "the price never rises." } } } } } },
            responses: { 200: { description: "The finished clip, its file URLs, token and billing" },
                         ...paidErrors({ exactTimeout: 150, notFound: "No such video model; nothing was charged", duplicate: true,
                           noAnswer: "video_failed, video_timeout or no_healthy_sellers; nothing charged" }) },
            security: [{ x402: [] }],
          } },
          [`${VIDEO_ROUTE}/{id}`]: { get: { operationId: "getVideo", security: [],
            summary: "Get a video job's status and files (X-Job-Token header)",
            description: "No payment. Send the job token as header X-Job-Token, or as ?token=.",
            responses: { 200: { description: "Finished" }, 202: { description: "Still running" },
                         400: errorBody("No job id or no token"), 403: errorBody("Wrong token"), 404: errorBody("No such job"),
                         503: errorBody(`${UNAVAILABLE}, or Surplus answered something unreadable`) } } },
          "/v1/video/models": { get: { operationId: "listVideoModels", summary: "List video models and their prices",
            responses: { 200: { description: "Model list" } }, security: [] } } } : {}) } : {}),
        ...(priceBook ? {
          "/v1/prices": { get: { operationId: "listPrices", summary: "List the crypto, metal and currency symbols offered", security: [],
            responses: { 200: { description: "Crypto, metal and currency symbols, with sources" },
              429: { ...errorBody("Over 60 a minute", errorRef("RateLimited429")), headers: { ...RETRY_AFTER, ...RATE_LIMIT_HEADERS } } } } },
          "/v1/prices/crypto/{symbol}": { get: { operationId: "cryptoPrice", security: [],
            summary: "Get a cryptocurrency's USD price from Chainlink on Base",
            description: "Read from Chainlink on Base. No payment.",
            parameters: [{ name: "symbol", in: "path", required: true, schema: { type: "string", examples: ["BTC", "ETH", "SOL"] } }],
            responses: { 200: { description: "The price, its time and source" }, 404: errorBody("Not offered"), 429: { ...errorBody("Over 60 a minute", errorRef("RateLimited429")), headers: { ...RETRY_AFTER, ...RATE_LIMIT_HEADERS } } } } },
          "/v1/prices/metal/{symbol}": { get: { operationId: "metalPrice", security: [],
            summary: "Get the USD price of gold (XAU) or silver (XAG)",
            description: "Read from Chainlink on Base. No payment.",
            parameters: [{ name: "symbol", in: "path", required: true, schema: { type: "string", enum: ["XAU", "XAG"] } }],
            responses: { 200: { description: "The price, its time and source" }, 404: errorBody("Not offered"), 429: { ...errorBody("Over 60 a minute", errorRef("RateLimited429")), headers: { ...RETRY_AFTER, ...RATE_LIMIT_HEADERS } } } } },
          "/v1/prices/fx/{base}/{quote}": { get: { operationId: "fxRate", security: [],
            summary: "Get an exchange rate between two of 30 currencies",
            description: "From Chainlink on Base, or the ECB's daily rates. No payment.",
            parameters: [{ name: "base", in: "path", required: true, schema: { type: "string", examples: ["EUR"] } },
                         { name: "quote", in: "path", required: true, schema: { type: "string", examples: ["USD"] } }],
            responses: { 200: { description: "The rate, its date and source" }, 404: errorBody("Not offered"), 429: { ...errorBody("Over 60 a minute", errorRef("RateLimited429")), headers: { ...RETRY_AFTER, ...RATE_LIMIT_HEADERS } } } } },
        } : {}),
        // Free, and served by the sign402 gateway's web API, not by this service: hence its own server.
        "/v1/wallet/{address}/reconciliation": { servers: [{ url: config.walletReportOrigin || "https://app.singitai.app" }],
          get: {
            operationId: "walletReconciliation", security: [{ singitApiKey: [] }],
            summary: "Get a wallet's SingIt spend and allowance (owner only)",
            description: "No payment, but private: send a SingIt API key (Authorization: Bearer sk_singit_...) of the account "
              + "that signs in with this wallet; no key or a wrong key is 401, another account's key is 403. Settled spend "
              + "by category (chat_answer, data_tool, gift_card, other), by day, credit top-ups apart, and the owner's daily "
              + "and per-purchase limits with what is left today. Categories only: no purchase details. 30 requests a minute per IP.",
            parameters: [
              { name: "address", in: "path", required: true, schema: { type: "string" },
                description: "A Base address (0x + 40 hex) or a Solana address (base58)" },
              { name: "days", in: "query", required: false, description: "How many days back, 1 to 90.",
                schema: { type: "integer", minimum: 1, maximum: 90, default: 30 } }],
            responses: {
              200: { description: "The report; known:false with zeros for an address SingIt has never seen",
                     content: { "application/json": { schema: RECONCILIATION_SCHEMA } } },
              400: { description: "Not an address, or days out of range" },
              429: { description: "Too many requests; see Retry-After" } },
          } },
        "/v1/models": { get: {
          operationId: "listModels", summary: "List models that can be asked for now, with prices",
          responses: { 200: { description: "Model list" } }, security: [],
        } },
        "/health": { get: { operationId: "getHealth", security: [], summary: "Get service, model and payment-rail status",
          responses: { 200: { description: "Legacy service fields plus observed health",
            content: { "application/json": { example: {
                "ok": true,
                "status": "ok",
                "generatedAt": "2026-10-31T12:00:00.000Z",
                "windowHours": 24,
                "statsSince": "2026-10-31T12:00:00.000Z",
                "models": {
                  "deepseek-v4.1-flash": {
                    "status": "up",
                    "windowHours": 24,
                    "attempts": 20,
                    "successes": 20,
                    "failures": 0,
                    "errorRatePct": 0,
                    "lastSuccessAt": "2026-10-31T12:00:00.000Z",
                    "lastFailureAt": null,
                    "lastOutcome": "success",
                    "sellerFailures": 0,
                    "failureReasons": {},
                    "reason": null
                  },
                  "claude-opus-5.5": {
                    "status": "insufficient_data",
                    "windowHours": 24,
                    "attempts": 1,
                    "successes": 1,
                    "failures": 0,
                    "errorRatePct": 0,
                    "lastSuccessAt": "2026-10-31T12:00:00.000Z",
                    "lastFailureAt": null,
                    "lastOutcome": "success",
                    "sellerFailures": 0,
                    "failureReasons": {},
                    "reason": null
                  }
                },
                "models_summary": {
                  "up": 1,
                  "degraded": 0,
                  "down": 0,
                  "insufficient_data": 1,
                  "unavailable": 0
                },
                "payments": {
                  "facilitator": null,
                  "networks": {
                    "base": {
                      "status": "unknown",
                      "basis": "none",
                      "settled": 0,
                      "failed": 0,
                      "lastSettledAt": null,
                      "lastFailedAt": null
                    },
                    "solana": {
                      "status": "unknown",
                      "basis": "none",
                      "settled": 0,
                      "failed": 0,
                      "lastSettledAt": null,
                      "lastFailedAt": null
                    }
                  }
                }
            }, schema: { type: "object", properties: {
              ok: { type: "boolean", const: true }, status: { enum: ["ok", "degraded", "down"] },
              generatedAt: { type: "string", format: "date-time" }, windowHours: { type: "integer", const: 24 },
              statsSince: { type: "string", format: "date-time" },
              models_summary: { type: "object", required: ["up", "degraded", "down", "insufficient_data", "unavailable"],
                properties: Object.fromEntries(["up", "degraded", "down", "insufficient_data", "unavailable"]
                  .map(key => [key, { type: "integer", minimum: 0 }])) },
              models: { type: "object", additionalProperties: { type: "object",
                required: ["status", "windowHours", "attempts", "successes", "failures", "errorRatePct",
                  "lastSuccessAt", "lastFailureAt", "lastOutcome", "sellerFailures", "failureReasons", "reason"],
                properties: {
                status: { enum: ["up", "degraded", "down", "insufficient_data", "unavailable"] },
                windowHours: { type: "integer", const: 24 },
                ...Object.fromEntries(["attempts", "successes", "failures", "sellerFailures"]
                  .map(key => [key, { type: "integer", minimum: 0 }])),
                errorRatePct: { type: "number", minimum: 0, maximum: 100 },
                lastSuccessAt: { type: ["string", "null"], format: "date-time" },
                lastFailureAt: { type: ["string", "null"], format: "date-time" },
                lastOutcome: { enum: ["success", "failure", null] },
                reason: { enum: ["no_healthy_sellers", "minimum_discount_not_met", "other", null] },
                failureReasons: { type: "object", additionalProperties: false,
                  properties: Object.fromEntries(["no_healthy_sellers", "minimum_discount_not_met", "other"]
                    .map(key => [key, { type: "integer", minimum: 0 }])) },
                unavailableUntil: { type: "string", format: "date-time" },
                retryAfterSeconds: { type: "integer", minimum: 30 }
              } } },
              payments: { type: "object", properties: { facilitator: { type: ["object", "null"] },
                networks: { type: "object" } } }
            } } } } } } } },
        "/numbers.json": { get: { operationId: "getNumbers", security: [], summary: "Get measured usage numbers with definitions",
          responses: { 200: { description: "Calls, USDC volume and unique payer counts since counters began", content: { "application/json": { schema: { type: "object", required: ["own_payers_configured"], properties: { own_payers_configured: { type: "boolean" } } } } } } } } },
        "/pricing.json": { get: {
          operationId: "listPricing", summary: "Get per-model upto/exact prices for one reference question",
          description: "What GET/HEAD discovery and a real paid request would be offered for each model that can be "
            + "asked for now, computed by the same chooseModel/ceilingAtomic functions, for a documented reference "
            + "question. No payment, no model call. A model entry with recommendedScheme upto (Claude Opus) is best "
            + "paid with the upto offer; its exact price carries room for thinking tokens.",
          responses: { 200: { description: "Price by model" },
                       503: { ...errorBody("The model catalogue has never loaded yet; see Retry-After"), headers: RETRY_AFTER } }, security: [],
        } },
      },
      components: { schemas: ERROR_SCHEMAS, securitySchemes: { x402: { type: "apiKey", in: "header", name: "PAYMENT-SIGNATURE",
        description: "x402 v2 payment. Call without it, read the PAYMENT-REQUIRED header of the 402 response, sign the "
          + "`upto` or `exact` USDC authorization it offers on Base or Solana for the amount or ceiling it names, and "
          + "retry with the signature here." },
        singitApiKey: { type: "http", scheme: "bearer", description: "A SingIt API key (sk_singit_...) from "
          + "https://app.singitai.app/app/. Only the wallet spend report on app.singitai.app uses it." } } },
    });
  });
  app.get("/.well-known/x402", open, (req, res) => {
    res.json({ version: 1, resources: [`${origin(req)}${ROUTE}`,
        ...(catalogue ? [...Object.keys(FORMAT_ROUTES).map((route) => `${origin(req)}${route}`),
                         `${origin(req)}${IMAGE_ROUTE}`, `${origin(req)}${SPEECH_ROUTE}`,
                         ...(config.videoSold && config.videoDiscoverable ? [`${origin(req)}${VIDEO_ROUTE}`] : []),
                         ...modelRoutePaths().map((route) => `${origin(req)}${route}`)] : []),
        ...(exaKey ? Object.keys(SEARCH_ROUTES).map((route) => `${origin(req)}${route}`) : [])],
      instructions: `${DESCRIPTION} Models and prices: GET ${origin(req)}/v1/models.`
        + (freeOffer ? ` Free to try with no payment: model ${freeOffer.models.join(", ")}, ${freeOffer.perVisitorPerDay} answers a day.` : "") });
  });
  // Every discoverable paid route that is actually enabled: what DISCOVERABLE in test/discovery.test.mjs checks,
  // built from the same flags addFormatRoutes/addImageRoutes/addSpeechRoutes/addSearchRoutes are gated on, so the
  // list below and llms.txt can never list a route this instance did not add.
  const discoverableRoutes = () => [ROUTE, ...(catalogue ? [...Object.keys(FORMAT_ROUTES), IMAGE_ROUTE, SPEECH_ROUTE,
    ...(config.videoSold && config.videoDiscoverable ? [VIDEO_ROUTE] : []),
    ...modelRoutePaths()] : []),
    ...(exaKey ? Object.keys(SEARCH_ROUTES) : [])];
  // Free: a human or agent landing on the bare origin gets a small service index instead of Express's 404.
  // Express answers HEAD the same way automatically. Open CORS, like the other public documents.
  app.get("/", open, (req, res) => {
    const base = origin(req);
    res.json({ name: SERVICE_NAME, description: DISCOVERY_DESCRIPTION, icon: ICON_URL,
      links: { openapi: `${base}/openapi.json`, x402: `${base}/.well-known/x402`, llmsTxt: `${base}/llms.txt`,
        models: `${base}/v1/models`, docs: "https://singitai.app/developers/" } });
  });
  // Free: llmstxt.org format, generated from the same route list as discovery so it cannot drift from it.
  app.get("/llms.txt", open, (req, res) => {
    const base = origin(req);
    // What this instance sells, from the same flags its routes are added on: nothing listed that is not live here.
    const sold = ["language-model chat completions",
      ...(catalogue ? ["Anthropic's Messages and OpenAI's Responses formats", "images", "speech"] : []),
      ...(exaKey ? ["Exa web search, page contents and cited answers"] : [])];
    const exactShare = Math.round((config.exactOutputFraction ?? DEFAULT_EXACT_OUTPUT_FRACTION) * 100);
    const lines = [
      "# SingIt Ask", "", `> ${DISCOVERY_DESCRIPTION}`, "",
      "## What it is", "",
      `An x402 seller: ${sold.length > 1 ? `${sold.slice(0, -1).join(", ")} and ${sold.at(-1)}` : sold[0]}, `
        + "each paid per call. No accounts or API keys.", "",
      "## How to pay", "",
      "x402 v2. Call a paid route below with no payment and read the 402 response's PAYMENT-REQUIRED header for its "
        + "offers: `upto`, listed first (metered to actual cost, up to the ceiling it names), or `exact` (a flat price for "
        + `a typical-length answer, about ${exactShare}% of max_tokens), in USDC on Base or Solana. Sign one `
        + "and retry with the signature in the PAYMENT-SIGNATURE header. Live prices and models: "
        + `${base}/openapi.json and ${base}/v1/models. For Claude Opus, sign \`upto\`: its \`exact\` price carries room `
        + "for thinking tokens and is usually dearer than the answer used (recommendedScheme in "
        + `${base}/pricing.json). Step by step: ${FIRST_PAID_CALL_DOC}`, "",
      ...(freeOffer ? ["## Free to try", "",
        `Model ${freeOffer.models.map((m) => `"${m}"`).join(", ")} on POST ${base}${ROUTE} answers with no payment: `
          + `${freeOffer.perVisitorPerDay} answers a day per network (IPv4 /24, IPv6 /64), up to ${freeOffer.maxTokens} `
          + `tokens and ${freeOffer.maxChars} characters each, resetting at midnight UTC. `
          + (freeOffer.budgetUsdPerDay > 0 ? `Shared daily cost cap: $${freeOffer.budgetUsdPerDay}; free answers may run out for the day (free_budget_exhausted). ` : "")
          + `When they are used up, drop `
          + `the ${FREE_PREFIX} prefix to pay per answer, or come back tomorrow.`, ""] : []),
      "## Paid routes", "", ...discoverableRoutes().map((route) => `- POST ${base}${route}`), "",
      "## Free", "",
      `- GET ${base}/v1/models — models and their prices`,
      ...(catalogue ? [`- GET ${base}/v1/images/models — image models and their prices`,
        `- GET ${base}/v1/audio/models — voice models and their prices`] : []),
      ...(priceBook ? [`- GET ${base}/v1/prices — crypto, metal and fx prices, no payment`] : []),
      `- GET ${base}/health — service, model and payment-rail status`,
      `- GET ${base}/numbers.json — measured usage numbers with definitions`, "",
      "## Links", "",
      `- OpenAPI: ${base}/openapi.json`,
      `- x402 discovery: ${base}/.well-known/x402`,
      `- llms.txt: ${base}/llms.txt`,
      `- Models: ${base}/v1/models`,
      "- Docs: https://singitai.app/developers/",
      `- First paid x402 call: ${FIRST_PAID_CALL_DOC}`,
      `- MCP server (API key, not x402): ${MCP_URL}, see https://singitai.app/llms.txt`,
    ];
    res.set("Content-Type", "text/plain; charset=utf-8").send(lines.join("\n") + "\n");
  });

  const choose = input => input.model === AUTO_ID
    ? chooseAuto(config, catalogue, input, RequestError, log) : chooseModel(config, catalogue, input);
  // Every answer that came back teaches the price shown for its model. It never delays or fails the answer.
  const learning = async (config_, messages, maxTokens, fetchImpl_, model = config.model, options) => {
    let answer;
    try { answer = await upstream(config_, messages, maxTokens, fetchImpl_, model, options); }
    catch (error) {
      try { app.locals.healthStats.recordModel(model, { ok: false, code: error.code, status: error.status }); } catch {}
      throw error;
    }
    try { app.locals.healthStats.recordModel(model, { ok: true }); } catch {}
    catalogue?.resolve(model).then((list) => observed.record(model, answer.usage, list)).catch(() => {});
    return answer;
  };
  const route = { facilitatorClient, catalogue, config, readRequest, choose, upstream: learning, fetchImpl, log };
  // API balance top-ups for app.singitai.app, bought by the account's own agent. Not listed for discovery.
  addCreditRoute(app, config, { facilitatorClient });
  // Web search, page contents and cited answers from Exa, bought with SingIt's Exa key: only when there is one.
  if (exaKey) addSearchRoutes(app, config, { facilitatorClient, exaKey, fetchImpl: exaFetch, log });
  // Images from Surplus's image models, with this service's own Surplus key.
  if (catalogue) addImageRoutes(app, config, { facilitatorClient, catalogue, fetchImpl, log, open });
  // Speech from Surplus's voice models, the same way.
  if (catalogue) addSpeechRoutes(app, config, { facilitatorClient, catalogue, fetchImpl, log, open });
  // Anthropic's Messages and OpenAI's Responses, paid like the chat route.
  if (catalogue) addFormatRoutes(app, config, { facilitatorClient, catalogue, fetchImpl, log });
  // One x402 resource per popular model (SI-62), so a Bazaar search for a model name finds a dedicated listing
  // instead of only the generic chat route where the model is a body field a search engine cannot see.
  if (catalogue) addModelRoutes(app, config, { facilitatorClient, catalogue, readRequest, upstream: learning, fetchImpl, log,
    inputSchema: INPUT_SCHEMA, outputSchema: OUTPUT_SCHEMA, exampleOutput: EXAMPLE.output });
  // Video from Surplus's text-to-video models, as jobs.
  if (catalogue) addVideoRoutes(app, config, { facilitatorClient, catalogue, fetchImpl, log, open });
  // Free prices: crypto and metals from Chainlink on Base, currencies from Chainlink or the ECB.
  if (priceBook) addPriceRoutes(app, { book: priceBook, open });
  // Free answers first: a "free/" model is answered here; every other question goes on to be paid.
  addFreeRoute(app, ROUTE, { config, terms: free, readRequest, RequestError, upstream: learning, catalogue, fetchImpl, log });
  // Public: any x402 agent pays on Solana or Base for actual usage with `upto`, or the same ceiling flat with
  // `exact` for buyers that sign only that scheme. Listed for discovery. `upto` leads `accepts` (Base, then Solana;
  // SI-102): it is the honest price, metered to the answer's actual cost, and a buyer that only inspects the first
  // offer should see that one. `exact` (a flat price for a typical-length answer) follows on both networks.
  // Its discovery probes are quoted for the Bazaar example body below (metered.mjs's `exampleProbe`), the price
  // Bazaar records for this route, so vet402's empty POST is offered that same price (no price_mismatch).
  addUsageRoute(app, ROUTE, (quotes, probe) => [baseOffer(config, quotes, probe("upto", BASE)),
    solanaOffer(config, quotes, probe("upto", SOLANA)), exactBaseOffer(config, quotes, probe("exact", BASE)),
    exactSolanaOffer(config, quotes, probe("exact", SOLANA))], {
    description: DISCOVERY_DESCRIPTION,
    serviceName: SERVICE_NAMES[ROUTE],
    iconUrl: ICON_URL,
    tags: discoveryTags("llm", "chat-completions", "inference", "openai-compatible", "chat", "claude", "gpt", "gemini",
                        "deepseek", "singit"),
    extensions: {
      ...declareEip2612GasSponsoringExtension(),
      ...declareDiscoveryExtension({
        bodyType: "json",
        input: EXAMPLE.input,
        inputSchema,
        output: { example: EXAMPLE.output, schema: OUTPUT_SCHEMA },
      }),
    },
  }, { ...route, offerEmptyProbe: true, probeBody: EXAMPLE.input });
  // The web agent's Base payments, pinned to this address by the gateway's buyer (always `upto`: ask_metered.py
  // builds its own `upto` requirements and signs nothing else). `exact` is offered alongside it for buyers such
  // as vet402 that only sign `exact`; they pay the same ceiling flat instead of metered actual usage.
  addUsageRoute(app, METERED_ROUTE, quotes => [baseOffer(config, quotes), exactBaseOffer(config, quotes)], {
    description: "SingIt Ask: actual model cost + 30% markup + 0.001 USDC settlement fee. Authorize at most 0.003 USDC. "
      + "Exact payers are instead charged that ceiling flat, with no metering.",
    extensions: declareEip2612GasSponsoringExtension(),
  }, route);
  // The web agent's Solana payments: measured privately, then paid directly from the owner's delegated USDC.
  if (config.directSolana) addSolanaQuotedRoute(app, config, { facilitatorClient, catalogue, readRequest, choose,
    upstream: learning, fetchImpl, log });

  app.use((req, res) => res.status(404).json({ error: { type: "invalid_request_error", message: `Cannot ${req.method} ${req.path}`, code: "not_found" } }));
  return app;
}
