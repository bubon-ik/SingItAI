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

import express from "express";
import { declareEip2612GasSponsoringExtension } from "@x402/extensions";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { addUsageRoute, baseOffer, solanaOffer, exactBaseOffer, exactSolanaOffer } from "./metered.mjs";
import { addSolanaQuotedRoute } from "./solana-quoted.mjs";
import { UnknownModel, ModelCatalogue, priceGuard } from "./catalogue.mjs";
import { LIMITS } from "./limits.mjs";
import { ObservedPrices } from "./observed.mjs";
import { FREE_PREFIX, addFreeRoute, freeTerms } from "./free.mjs";
import { addCreditRoute } from "./credit.mjs";
import { SEARCH_ROUTES, addSearchRoutes } from "./search.mjs";
import { IMAGE_ROUTE, addImageRoutes } from "./images.mjs";
import { SPEECH_ROUTE, addSpeechRoutes } from "./speech.mjs";
import { addPriceRoutes } from "./prices.mjs";
import { VIDEO_ROUTE, addVideoRoutes } from "./video.mjs";
import { FORMAT_ROUTES, addFormatRoutes } from "./formats.mjs";
import { DIRECT_TERMS, QUOTED_ROUTE, TERMS, METERED_ROUTE, SOLANA_TERMS, ceilingAtomic } from "./pricing.mjs";

export const BASE = "eip155:8453";
export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const ROUTE = "/v1/chat/completions";

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
  return { messages: clean, maxTokens, chars, model: model?.trim(), messageCount: clean.length };
}

// The model an answer is bought from and what it may cost. No model, or the default one, is still looked up on
// Surplus's list when there is one, so its ceiling follows the same formula as any other model (metered.mjs
// clamps it to the standing ceiling); a catalogue that cannot resolve it, or no catalogue at all, keeps the old
// standing ceiling. Any other model must be on Surplus's list, and its ceiling follows its own prices.
export async function chooseModel(config, catalogue, input) {
  const bufferPp = config.priceBufferPp ?? 1;
  if (!input.model || input.model === config.model) {
    const info = catalogue ? await catalogue.resolve(config.model).catch(() => null) : null;
    return { ...input, model: config.model, info, isDefault: true,
      ...(info ? priceGuard(catalogue, info.id, bufferPp) : { share: 1, minDiscount: null }) };
  }
  if (!catalogue) throw new RequestError(400, "Choosing a model is not offered here.");
  try {
    const info = await catalogue.resolve(input.model);
    if (catalogue.canAnswer && !(await catalogue.canAnswer(info.id))) {
      throw new RequestError(400, "No trusted seller offers that model right now. Pick another; nothing was paid.");
    }
    return { ...input, info, ...priceGuard(catalogue, info.id, bufferPp) };
  } catch (error) {
    if (error instanceof UnknownModel) throw new RequestError(400, error.message);
    throw error;
  }
}

export class UpstreamError extends Error {}

// One completion from Surplus Intelligence. Any failure is an UpstreamError: the payer is not charged.
//
// Thinking is switched off and the cheapest seller is asked first. Measured on 12 October over four models: the
// cheapest seller answered as fast as the fastest one within twice its price (1.8-8 s against 2-14 s) and cost the
// same or less; the 21-24 s seen on 4 October did not recur. Some sellers refuse to switch thinking off ("Reasoning
// is mandatory for this endpoint"), and Surplus gives up after two of them; such a refusal costs nothing, so the
// question is asked once more with as little thinking as the seller allows. Surplus documents all three:
// docs/reference/reasoning, docs/marketplace/routing-controls.
export async function askSurplus(config, messages, maxTokens, fetchImpl = fetch, model = config.model, { minDiscount } = {}) {
  const ask = (reasoning) => fetchImpl(`${config.surplusUrl}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}`,
               ...(minDiscount != null ? { "X-Min-Discount": String(minDiscount) } : {}) },
    body: JSON.stringify({ model, messages, max_tokens: maxTokens, temperature: 0.4,
                           reasoning_effort: reasoning, si_route: { objective: "price" } }),
    signal: AbortSignal.timeout(LIMITS.timeoutMs),
  });
  let response;
  try {
    response = await ask("none");
    if (response.status === 400) {
      const refusal = await response.clone().json().catch(() => null);
      if (refusal?.error?.code === "request_rejected" && /reasoning/i.test(refusal.error.message ?? "")) response = await ask("low");
    }
  } catch (error) {
    throw new UpstreamError(`model unreachable (${error.name})`);
  }
  if (!response.ok) throw new UpstreamError(`model answered HTTP ${response.status}`);
  const data = await response.json().catch(() => null);
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || !content.trim()) throw new UpstreamError("model returned no text");
  return { content, finishReason: data.choices[0].finish_reason ?? "stop", usage: data.usage ?? null };
}

// The question's shape, for the 402 challenge (Bazaar) and for the OpenAPI document alike.
// Directories rank a listing by how completely it says what to send and what comes back (Coinbase's Bazaar:
// docs.cdp.coinbase.com/x402/seller/get-discovered), so every field an agent fills in is described.
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

const usd = atomic => (Number(atomic) / 1e6).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");

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

export function createApp(config, { facilitatorClient, upstream = askSurplus, fetchImpl = fetch, log = console.error,
                                    exaKey = config.exaKey ?? null, exaFetch = fetchImpl, priceBook = null,
                                    catalogue = config.surplusUrl ? new ModelCatalogue({ url: config.surplusUrl, fetchImpl, blocked: config.blockedProviders ?? [] }) : null,
                                    observed = new ObservedPrices({ file: config.observedFile }) }) {
  const app = express();
  app.locals.catalogue = catalogue;  // server.mjs warms it
  app.disable("x-powered-by");
  // cloudflared connects over loopback and supplies the public HTTPS scheme.
  app.set("trust proxy", "loopback");
  app.use(express.json({ limit: "128kb" })); // unreadable JSON is refused before any payment is asked

  const pricing = { [SOLANA]: SOLANA_TERMS, [BASE]: TERMS };
  const free = freeTerms(config);
  const freeOffer = free.models.length ? { models: free.models.map((m) => FREE_PREFIX + m), endpoint: ROUTE,
    perVisitorPerDay: free.perVisitorPerDay, maxTokens: free.maxTokens, maxChars: free.chars } : null;
  // What a payer pays on top of the model's own cost: the markup, and one settlement fee per answer.
  const fees = { markupPct: TERMS.markupBps / 100,
    perAnswerUsd: { [BASE]: Number(usd(TERMS.settlementFeeAtomic)), [SOLANA]: Number(usd(SOLANA_TERMS.settlementFeeAtomic)) } };
  const origin = req => `${req.protocol}://${req.get("host")}`;
  // Public, read-only documents: the website reads them straight from the browser.
  const open = (_req, res, next) => { res.set("Access-Control-Allow-Origin", "*"); next(); };
  // At most 500 characters with the "SingIt Ask: " prefix: Coinbase's facilitator rejects longer descriptions.
  const DESCRIPTION = "Ask a language model without an API key: any model listed at GET /v1/models (Claude, GPT, Gemini, "
    + `Grok, DeepSeek, Qwen and more; default ${config.model}). OpenAI chat-completions request. Pays actual token `
    + "cost + 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana), up to a ceiling that follows the model's "
    + "price; nothing is charged without an answer. Exact payers are instead charged that ceiling flat, with no metering.";
  app.get("/health", (_req, res) => {
    res.json({ ok: true, service: "singit-ask", model: config.model, facilitator: "coinbase-cdp", pricing, free: freeOffer,
               metered: TERMS, ...(config.directSolana ? { directSolana: { endpoint: QUOTED_ROUTE, ...DIRECT_TERMS } } : {}) });
  });
  // Free: the default and every other model that can be asked for, with the list prices its ceiling is built from.
  app.get("/v1/models", open, async (_req, res) => {
    // Only the models that can be answered now (the default always is).
    const listed = catalogue ? await catalogue.list().catch(() => []) : [];
    const answerable = await Promise.all(listed.map((m) => (catalogue.canAnswer ? catalogue.canAnswer(m.id) : true)));
    const models = listed.filter((m, i) => answerable[i] || m.id === config.model);
    const market = catalogue?.market ? await catalogue.market().catch(() => new Map()) : new Map();
    res.json({ object: "list", default: config.model, endpoint: ROUTE, pricing, fees, free: freeOffer,
      data: [{ id: "singit-ask", object: "model", owned_by: "singit", upstream: config.model },
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
            usdPerMillionTokens: { input: m.promptUsdPerToken * 1e6, output: m.completionUsdPerToken * 1e6 },
            ...(share === null ? {} : { expectedUsdPerMillionTokens: { input: m.promptUsdPerToken * 1e6 * share,
              output: m.completionUsdPerToken * 1e6 * share, basis: seen === null ? "trusted" : "observed" } }),
            ...(ours === null ? {} : {
              singitUsdPerMillionTokens: { input: m.promptUsdPerToken * 1e6 * ours, output: m.completionUsdPerToken * 1e6 * ours },
              savingsVsListPct: Math.round((1 - ours) * 100) }) };
        })] });
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
              contact: { name: "SingIt", email: "Exkalibur1919@proton.me", url: "https://singitai.app" } },
      servers: [{ url: origin(req) }],
      paths: {
        [ROUTE]: { post: {
          operationId: "createChatCompletion", summary: "One answer from a language model", description: DESCRIPTION,
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", ...price }, protocols: [{ x402: {} }] },
          requestBody: { required: true, content: { "application/json": {
            schema: { type: "object", ...INPUT_SCHEMA }, example: EXAMPLE.input } } },
          responses: {
            200: { description: "The answer, with what it was charged",
                   content: { "application/json": { schema: OUTPUT_SCHEMA, example: EXAMPLE.output } } },
            429: { description: "Free answers (model free/…) used up for today, or busy; see Retry-After" },
            400: { description: "The question cannot be answered here; nothing was charged" },
            402: { description: "Payment Required: x402 challenge in the PAYMENT-REQUIRED header" },
            502: { description: "No billable answer came back; nothing was charged" },
          },
          security: [{ x402: [] }],
        } },
        ...(exaKey ? Object.fromEntries(Object.keys(SEARCH_ROUTES).map((route) => [route, { post: {
          operationId: { "/v1/exa/search": "exaSearch", "/v1/exa/contents": "exaContents", "/v1/exa/answer": "exaAnswer" }[route],
          summary: { "/v1/exa/search": "Web search from Exa", "/v1/exa/contents": "Page contents from Exa, by URL",
                     "/v1/exa/answer": "An answer with cited sources, from Exa" }[route],
          description: "Exa's own request and answer, paid per request over x402: Exa's list price + 30% + a settlement fee "
            + "(0.001 USDC on Base, 0.002 on Solana). Nothing is charged without an answer.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.0023", max: "0.067" }, protocols: [{ x402: {} }] },
          requestBody: { required: true, content: { "application/json": { schema: { type: "object" } } } },
          responses: { 200: { description: "Exa's answer, with billing" }, 400: { description: "Not offered; nothing charged" },
                       402: { description: "Payment Required" }, 502: { description: "No answer; nothing charged" },
                       503: { description: "Paused or Exa unavailable; nothing charged" } },
          security: [{ x402: [] }],
        } }])) : {}),
        ...(catalogue ? Object.fromEntries(Object.keys(FORMAT_ROUTES).map((route) => [route, { post: {
          operationId: route === "/v1/messages" ? "createMessage" : "createResponse",
          summary: route === "/v1/messages" ? "Anthropic's Messages API, any listed model" : "OpenAI's Responses API, any listed model",
          description: "The request in that API's own format, without streaming. Pays actual token cost + 30% + a settlement "
            + "fee (0.001 USDC on Base, 0.002 on Solana), up to a ceiling that follows the model's price and the request's "
            + "size; nothing is charged without an answer. Models: GET /v1/models.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", ...price }, protocols: [{ x402: {} }] },
          requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["model"] } } } },
          responses: { 200: { description: "The answer in that API's format, with billing" },
                       400: { description: "Not offered; nothing charged" }, 402: { description: "Payment Required" },
                       404: { description: "No such model" }, 502: { description: "No billable answer; nothing charged" } },
          security: [{ x402: [] }],
        } }])) : {}),
        ...(catalogue ? { [IMAGE_ROUTE]: { post: {
          operationId: "createImage", summary: "Images from any of 36 image models",
          description: "OpenAI images request (model, prompt, n, size). Paid per request over x402: the model's list price "
            + "+ 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana). Models and prices: GET /v1/images/models. "
            + "Nothing is charged without an image.",
          "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.014", max: "2.602" }, protocols: [{ x402: {} }] },
          requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["model", "prompt"],
            properties: { model: { type: "string" }, prompt: { type: "string" }, n: { type: "integer", minimum: 1, maximum: 4 },
                          size: { type: "string", examples: ["1024x1024"] }, response_format: { type: "string", enum: ["b64_json", "url"] } } } } } },
          responses: { 200: { description: "The images, with billing" }, 400: { description: "Not offered; nothing charged" },
                       402: { description: "Payment Required" }, 404: { description: "No such image model" },
                       502: { description: "No image; nothing charged" } },
          security: [{ x402: [] }],
        } }, "/v1/images/models": { get: { operationId: "listImageModels", summary: "Image models and their prices",
          responses: { 200: { description: "Model list" } }, security: [] } },
          [SPEECH_ROUTE]: { post: {
            operationId: "createSpeech", summary: "Text read aloud by any of 11 voice models",
            description: "OpenAI speech request (model, input, voice, response_format, speed). Paid per request over x402: the "
              + "model's list price for the text + 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana). Models: "
              + "GET /v1/audio/models. Nothing is charged without audio.",
            "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.001", max: "0.336" }, protocols: [{ x402: {} }] },
            requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["model", "input"],
              properties: { model: { type: "string" }, input: { type: "string", maxLength: 4096 }, voice: { type: "string" },
                            response_format: { type: "string", enum: ["mp3", "opus", "aac", "flac", "wav", "pcm"] },
                            speed: { type: "number" } } } } } },
            responses: { 200: { description: "The audio", content: { "audio/mpeg": {} } }, 400: { description: "Not offered; nothing charged" },
                         402: { description: "Payment Required" }, 404: { description: "No such voice model" },
                         502: { description: "No audio; nothing charged" } },
            security: [{ x402: [] }],
          } },
          "/v1/audio/models": { get: { operationId: "listAudioModels", summary: "Voice models and their prices",
            responses: { 200: { description: "Model list" } }, security: [] } },
          [VIDEO_ROUTE]: { post: {
            operationId: "createVideo", summary: "A video clip from any of 36 text-to-video models, as a job",
            description: "Body: model, prompt, and optionally duration_seconds, resolution, aspect_ratio, audio. Paid per clip "
              + "over x402: the model's list price + 30% + a settlement fee (0.001 USDC on Base, 0.002 on Solana), charged "
              + "once the job is accepted. Answers with the job id and job_token; poll GET /v1/video/generations/{id} "
              + "with header X-Job-Token for free, then GET /v1/video/generations/{id}/content. Models: GET /v1/video/models.",
            "x-payment-info": { price: { mode: "dynamic", currency: "USD", min: "0.105", max: "1.302" }, protocols: [{ x402: {} }] },
            requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["model", "prompt"],
              properties: { model: { type: "string" }, prompt: { type: "string", maxLength: 2000 }, duration_seconds: { type: "integer" },
                            resolution: { type: "string" }, aspect_ratio: { type: "string" }, audio: { type: "boolean" } } } } } },
            responses: { 200: { description: "The accepted job, its token and billing" }, 400: { description: "Not offered; nothing charged" },
                         402: { description: "Payment Required" }, 404: { description: "No such video model" },
                         502: { description: "Not started; nothing charged" } },
            security: [{ x402: [] }],
          } },
          [`${VIDEO_ROUTE}/{id}`]: { get: { operationId: "getVideo", security: [],
            summary: "Free: a video job's status and files (header X-Job-Token, or ?token=)",
            responses: { 200: { description: "Finished" }, 202: { description: "Still running" }, 403: { description: "Wrong token" } } } },
          "/v1/video/models": { get: { operationId: "listVideoModels", summary: "Video models and their prices",
            responses: { 200: { description: "Model list" } }, security: [] } } } : {}),
        ...(priceBook ? {
          "/v1/prices": { get: { operationId: "listPrices", summary: "Free prices: what can be asked for", security: [],
            responses: { 200: { description: "Crypto, metal and currency symbols, with sources" } } } },
          "/v1/prices/crypto/{symbol}": { get: { operationId: "cryptoPrice", security: [],
            summary: "Free: a cryptocurrency's USD price, from Chainlink on Base",
            parameters: [{ name: "symbol", in: "path", required: true, schema: { type: "string", examples: ["BTC", "ETH", "SOL"] } }],
            responses: { 200: { description: "The price, its time and source" }, 404: { description: "Not offered" },
                         429: { description: "Over 60 a minute" } } } },
          "/v1/prices/metal/{symbol}": { get: { operationId: "metalPrice", security: [],
            summary: "Free: gold (XAU) or silver (XAG) in USD, from Chainlink on Base",
            parameters: [{ name: "symbol", in: "path", required: true, schema: { type: "string", enum: ["XAU", "XAG"] } }],
            responses: { 200: { description: "The price, its time and source" }, 404: { description: "Not offered" } } } },
          "/v1/prices/fx/{base}/{quote}": { get: { operationId: "fxRate", security: [],
            summary: "Free: an exchange rate between 30 currencies (Chainlink on Base, or the ECB's daily rates)",
            parameters: [{ name: "base", in: "path", required: true, schema: { type: "string", examples: ["EUR"] } },
                         { name: "quote", in: "path", required: true, schema: { type: "string", examples: ["USD"] } }],
            responses: { 200: { description: "The rate, its date and source" }, 404: { description: "Not offered" } } } },
        } : {}),
        "/v1/models": { get: {
          operationId: "listModels", summary: "Models that can be asked for now, with their prices",
          responses: { 200: { description: "Model list" } }, security: [],
        } },
      },
      components: { securitySchemes: { x402: { type: "apiKey", in: "header", name: "PAYMENT-SIGNATURE",
        description: "x402 v2 payment. Call without it, read the PAYMENT-REQUIRED header of the 402 response, sign the "
          + "`upto` or `exact` USDC authorization it offers on Base or Solana for the amount or ceiling it names, and "
          + "retry with the signature here." } } },
    });
  });
  app.get("/.well-known/x402", open, (req, res) => {
    res.json({ version: 1, resources: [`${origin(req)}${ROUTE}`,
        ...(catalogue ? [...Object.keys(FORMAT_ROUTES).map((route) => `${origin(req)}${route}`),
                         `${origin(req)}${IMAGE_ROUTE}`, `${origin(req)}${SPEECH_ROUTE}`, `${origin(req)}${VIDEO_ROUTE}`] : []),
        ...(exaKey ? Object.keys(SEARCH_ROUTES).map((route) => `${origin(req)}${route}`) : [])],
      instructions: `${DESCRIPTION} Models and prices: GET ${origin(req)}/v1/models.`
        + (freeOffer ? ` Free to try with no payment: model ${freeOffer.models.join(", ")}, ${freeOffer.perVisitorPerDay} answers a day.` : "") });
  });

  const choose = input => chooseModel(config, catalogue, input);
  // Every answer that came back teaches the price shown for its model. It never delays or fails the answer.
  const learning = async (config_, messages, maxTokens, fetchImpl_, model = config.model, options) => {
    const answer = await upstream(config_, messages, maxTokens, fetchImpl_, model, options);
    catalogue?.resolve(model).then((list) => observed.record(model, answer.usage, list)).catch(() => {});
    return answer;
  };
  const route = { facilitatorClient, config, readRequest, choose, upstream: learning, fetchImpl, log };
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
  // Video from Surplus's text-to-video models, as jobs.
  if (catalogue) addVideoRoutes(app, config, { facilitatorClient, catalogue, fetchImpl, log, open });
  // Free prices: crypto and metals from Chainlink on Base, currencies from Chainlink or the ECB.
  if (priceBook) addPriceRoutes(app, { book: priceBook, open });
  // Free answers first: a "free/" model is answered here; every other question goes on to be paid.
  addFreeRoute(app, ROUTE, { config, terms: free, readRequest, RequestError, upstream: learning, fetchImpl, log });
  // Public: any x402 agent pays on Solana or Base for actual usage with `upto`, or the same ceiling flat with
  // `exact` for buyers that sign only that scheme. Listed for discovery.
  addUsageRoute(app, ROUTE, quotes => [solanaOffer(config, quotes), baseOffer(config, quotes),
    exactSolanaOffer(config, quotes), exactBaseOffer(config, quotes)], {
    description: `SingIt Ask: ${DESCRIPTION}`,
    serviceName: "SingIt Ask",
    tags: ["llm", "chat", "openai-compatible", "inference", "claude", "gpt", "gemini", "deepseek", "pay-per-use",
           "solana", "base", "singit"],
    extensions: {
      ...declareEip2612GasSponsoringExtension(),
      ...declareDiscoveryExtension({
        bodyType: "json",
        input: EXAMPLE.input,
        inputSchema: INPUT_SCHEMA,
        output: { example: EXAMPLE.output, schema: OUTPUT_SCHEMA },
      }),
    },
  }, { ...route, offerEmptyProbe: true });
  // The web agent's Base payments, pinned to this address by the gateway's buyer (always `upto`: ask_metered.py
  // builds its own `upto` requirements and signs nothing else). `exact` is offered alongside it for buyers such
  // as vet402 that only sign `exact`; they pay the same ceiling flat instead of metered actual usage.
  addUsageRoute(app, METERED_ROUTE, quotes => [baseOffer(config, quotes), exactBaseOffer(config, quotes)], {
    description: "SingIt Ask: actual model cost + 30% markup + 0.001 USDC settlement fee. Authorize at most 0.003 USDC. "
      + "Exact payers are instead charged that ceiling flat, with no metering.",
    extensions: declareEip2612GasSponsoringExtension(),
  }, route);
  // The web agent's Solana payments: measured privately, then paid directly from the owner's delegated USDC.
  if (config.directSolana) addSolanaQuotedRoute(app, config, { facilitatorClient, readRequest, choose, upstream: learning, fetchImpl, log });

  return app;
}
