// SingIt Ask: one answer from a language model, paid over x402 in USDC on Solana or Base for what it
// actually cost: model tokens + 30% + the network's settlement fee, up to 0.003 USDC. Coinbase CDP settles.
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
import { addUsageRoute, baseOffer, solanaOffer } from "./metered.mjs";
import { addSolanaQuotedRoute } from "./solana-quoted.mjs";
import { DIRECT_TERMS, QUOTED_ROUTE, TERMS, METERED_ROUTE, SOLANA_TERMS } from "./pricing.mjs";

export const BASE = "eip155:8453";
export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const ROUTE = "/v1/chat/completions";

// What one paid answer may ask for: at these sizes the model's cost stays under the 0.003 USDC ceiling.
export const LIMITS = { messages: 40, chars: 24_000, maxTokens: 1_200, defaultTokens: 800, timeoutMs: 45_000 };
// Thinking is switched off, but a seller may not obey, and thinking shares the token budget with the
// answer: on 4 October one spent 931 of 1200 tokens thinking and the answer stopped mid-word. The answer keeps
// its whole budget; the most thinking can add stays far under the ceiling (about 0.0001 USDC).
export const THINKING_HEADROOM = 1_000;
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
  return { messages: clean, maxTokens };
}

export class UpstreamError extends Error {}

// One completion from Surplus Intelligence. Any failure is an UpstreamError: the payer is not charged.
export async function askSurplus(config, messages, maxTokens, fetchImpl = fetch) {
  let response;
  try {
    response = await fetchImpl(`${config.surplusUrl}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}` },
      // No thinking, from the fastest seller within twice the cheapest price. Measured on 4 October: the cheapest
      // seller ignored `thinking: disabled` and answered a travel question in 21-24 s; with these, 2.5-2.7 s, three
      // of three, at the same cost. Surplus documents both: docs/reference/reasoning, docs/marketplace/routing-controls.
      body: JSON.stringify({ model: config.model, messages, max_tokens: maxTokens + THINKING_HEADROOM, temperature: 0.4,
                             reasoning_effort: "none", si_route: { objective: "latency", price_tolerance_pct: 100 } }),
      signal: AbortSignal.timeout(LIMITS.timeoutMs),
    });
  } catch (error) {
    throw new UpstreamError(`model unreachable (${error.name})`);
  }
  if (!response.ok) throw new UpstreamError(`model answered HTTP ${response.status}`);
  const data = await response.json().catch(() => null);
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || !content.trim()) throw new UpstreamError("model returned no text");
  return { content, finishReason: data.choices[0].finish_reason ?? "stop", usage: data.usage ?? null };
}

const EXAMPLE = {
  input: { messages: [{ role: "user", content: "Where is good coffee near the Colosseum?" }], max_tokens: 400 },
  output: {
    object: "chat.completion",
    model: "singit-ask",
    choices: [{ index: 0, message: { role: "assistant", content: "Around the Colosseum, try…" }, finish_reason: "stop" }],
  },
};

export function createApp(config, { facilitatorClient, upstream = askSurplus, fetchImpl = fetch, log = console.error }) {
  const app = express();
  app.disable("x-powered-by");
  // cloudflared connects over loopback and supplies the public HTTPS scheme.
  app.set("trust proxy", "loopback");
  app.use(express.json({ limit: "128kb" })); // unreadable JSON is refused before any payment is asked

  const pricing = { [SOLANA]: SOLANA_TERMS, [BASE]: TERMS };
  app.get("/health", (_req, res) => {
    res.json({ ok: true, service: "singit-ask", model: config.model, facilitator: "coinbase-cdp", pricing,
               metered: TERMS, ...(config.directSolana ? { directSolana: { endpoint: QUOTED_ROUTE, ...DIRECT_TERMS } } : {}) });
  });
  app.get("/v1/models", (_req, res) => {
    res.json({ object: "list", data: [{ id: "singit-ask", object: "model", owned_by: "singit", upstream: config.model,
                                         endpoint: ROUTE, pricing }] });
  });

  const route = { facilitatorClient, config, readRequest, upstream, fetchImpl, log };
  // Public: any x402 agent pays on Solana or Base for actual usage. Listed for discovery.
  addUsageRoute(app, ROUTE, [solanaOffer(config), baseOffer(config)], {
    description: `SingIt Ask: one answer from ${config.model}, OpenAI-compatible. Pays actual token cost + 30% `
                 + "+ settlement fee (0.002 USDC on Solana, 0.001 on Base), at most 0.003 USDC; nothing without an answer.",
    serviceName: "SingIt Ask",
    tags: ["llm", "chat", "openai-compatible", "solana", "base", "singit"],
    extensions: {
      ...declareEip2612GasSponsoringExtension(),
      ...declareDiscoveryExtension({
        bodyType: "json",
        input: EXAMPLE.input,
        inputSchema: {
          properties: {
            messages: { type: "array", items: { type: "object" } },
            max_tokens: { type: "integer", maximum: LIMITS.maxTokens },
          },
          required: ["messages"],
        },
        output: { example: EXAMPLE.output },
      }),
    },
  }, route);
  // The web agent's Base payments, pinned to this address by the gateway's buyer.
  addUsageRoute(app, METERED_ROUTE, [baseOffer(config)], {
    description: "SingIt Ask: actual model cost + 30% markup + 0.001 USDC settlement fee. Authorize at most 0.003 USDC.",
    extensions: declareEip2612GasSponsoringExtension(),
  }, route);
  // The web agent's Solana payments: measured privately, then paid directly from the owner's delegated USDC.
  if (config.directSolana) addSolanaQuotedRoute(app, config, { facilitatorClient, readRequest, upstream, fetchImpl, log });

  return app;
}
