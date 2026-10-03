// SingIt Ask: one answer from a language model, paid per answer over x402 in USDC on Solana or Base.
//
// The payer is charged only when the answer came back: @x402/express holds the response, and a
// handler that answers 400 or above is never settled (checked in its source, 2.28). So a model
// that fails, times out or returns nothing costs the payer nothing.
//
// The model is bought upstream on Surplus Intelligence (OpenAI-compatible, cheapest seller wins),
// with this service's own key; the payer never sees or needs it.

import express from "express";
import { fileURLToPath } from "node:url";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { bazaarResourceServerExtension, declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { addMeteredRoute } from "./metered.mjs";
import { addSolanaMeteredRoute } from "./solana-metered.mjs";
import { addSolanaQuotedRoute } from "./solana-quoted.mjs";
import { DIRECT_TERMS, QUOTED_ROUTE, TERMS, METERED_ROUTE, SOLANA_TERMS, SOLANA_ROUTE } from "./pricing.mjs";

export const BASE = "eip155:8453";
export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const ROUTE = "/v1/chat/completions";

// What one paid answer may ask for: the fixed price covers the model at these sizes with room to spare.
export const LIMITS = { messages: 40, chars: 24_000, maxTokens: 1_200, defaultTokens: 800, timeoutMs: 45_000 };
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
      body: JSON.stringify({ model: config.model, messages, max_tokens: maxTokens, temperature: 0.4 }),
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

export function createApp(config, { facilitatorClient, meteredFacilitatorClient, upstream = askSurplus, fetchImpl = fetch, log = console.error }) {
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new ExactEvmScheme())
    .register(SOLANA, new ExactSvmScheme())
    .registerExtension(bazaarResourceServerExtension);

  const app = express();
  app.disable("x-powered-by");
  // cloudflared connects over loopback and supplies the public HTTPS scheme.
  app.set("trust proxy", "loopback");
  app.use(express.json({ limit: "128kb" })); // unreadable JSON is refused before any payment is asked
  app.use("/test", express.static(fileURLToPath(new URL("../public/", import.meta.url)), {
    dotfiles: "deny",
    setHeaders(res) {
      res.setHeader("Cache-Control", "no-store");
      res.setHeader("X-Content-Type-Options", "nosniff");
      res.setHeader("Referrer-Policy", "no-referrer");
    },
  }));

  app.get("/health", (_req, res) => {
    res.json({ ok: true, service: "singit-ask", model: config.model,
               ...(config.directSolana ? { directSolana: {endpoint: QUOTED_ROUTE, ...DIRECT_TERMS} } : {}),
               price: { [SOLANA]: config.priceSolana, [BASE]: config.priceBase },
               ...(meteredFacilitatorClient ? { metered: TERMS } : {}),
               ...(meteredFacilitatorClient && config.meteredSolana ? { meteredSolana: SOLANA_TERMS } : {}) });
  });
  app.get("/v1/models", (_req, res) => {
    res.json({ object: "list", data: [{ id: "singit-ask", object: "model", owned_by: "singit",
                                         upstream: config.model, ...(config.directSolana ? {directSolana: {endpoint: QUOTED_ROUTE, ...DIRECT_TERMS}} : {}), ...(meteredFacilitatorClient && config.meteredSolana ? { meteredSolana: { endpoint: SOLANA_ROUTE, network: SOLANA, ...SOLANA_TERMS } } : {}), ...(meteredFacilitatorClient ? { metered: { endpoint: METERED_ROUTE, network: BASE, ...TERMS } } : {}), price_per_answer: { solana: config.priceSolana, base: config.priceBase } }] });
  });

  if (meteredFacilitatorClient) addMeteredRoute(app, config, {
    facilitatorClient: meteredFacilitatorClient, readRequest, upstream, fetchImpl, log,
  });

  if (meteredFacilitatorClient && config.meteredSolana) addSolanaMeteredRoute(app, config, {
    facilitatorClient: meteredFacilitatorClient, readRequest, upstream, fetchImpl, log,
  });

  if (config.directSolana) {
    if (!meteredFacilitatorClient) throw new Error("CDP facilitator is required for direct Solana");
    addSolanaQuotedRoute(app, config, {facilitatorClient: meteredFacilitatorClient, readRequest, upstream, fetchImpl});
  }

  app.use(paymentMiddleware({
    [`POST ${ROUTE}`]: {
      accepts: [
        { scheme: "exact", price: config.priceSolana, network: SOLANA, payTo: config.payToSolana },
        { scheme: "exact", price: config.priceBase, network: BASE, payTo: config.payToBase },
      ],
      description: `SingIt Ask: one answer from ${config.model}, OpenAI-compatible. Paid per answer; charged only when the answer came back.`,
      mimeType: "application/json",
      serviceName: "SingIt Ask",
      tags: ["llm", "chat", "openai-compatible", "solana", "base", "singit"],
      extensions: {
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
    },
  }, server));

  app.post(ROUTE, async (req, res) => {
    const started = Date.now();
    let request;
    try {
      request = readRequest(req.body);
    } catch (error) {
      return res.status(error.status ?? 400).json({ error: { message: error.message, type: "invalid_request_error" } });
    }
    try {
      const answer = await upstream(config, request.messages, request.maxTokens, fetchImpl);
      log(`singit-ask: answered in ${Date.now() - started} ms (${answer.usage?.total_tokens ?? "?"} tokens)`);
      return res.json({
        id: `singit-ask-${started}`,
        object: "chat.completion",
        created: Math.floor(started / 1000),
        model: "singit-ask",
        choices: [{ index: 0, message: { role: "assistant", content: answer.content }, finish_reason: answer.finishReason }],
        usage: answer.usage,
      });
    } catch (error) {
      // 502: the middleware does not settle, so the payer keeps their money.
      log(`singit-ask: no answer after ${Date.now() - started} ms: ${error.message}`);
      return res.status(502).json({ error: { message: "The model did not answer. You were not charged; try again.",
                                             type: "upstream_error" } });
    }
  });

  return app;
}
