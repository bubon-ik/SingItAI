// SingIt images: an image from any of Surplus's image models, sold over x402 at the model's list price + 30% + the
// network's settlement fee, and bought on Surplus with this service's own key, like the answers.
//
// The price is worked out from the request and the model's list price before the payer is asked (per image, per
// megapixel of the asked size, or, for models billed per token, a fixed allowance of tokens per image). The payment
// is verified, Surplus makes the image, and only then is the payment settled: no image, no charge. What Surplus
// actually charged is logged against the list price, so an allowance that runs short shows.
import crypto from "node:crypto";
import { x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { UnknownModel } from "./catalogue.mjs";
import { addPaymentRoute, isDiscoveryProbe } from "./metered.mjs";
import { ASSET, BASE, SOLANA, SOLANA_ASSET, TERMS, SOLANA_TERMS, SERVICE_NAME, ICON_URL } from "./pricing.mjs";

export const IMAGE_ROUTE = "/v1/images/generations";
export const IMAGE_MODELS_ROUTE = "/v1/images/models";
export const IMAGE_TERMS = Object.freeze({ markupBps: 3000, maxImages: 4, maxPromptChars: 4000, tokensPerImage: 2000,
  maxListAtomic: 2_000_000 });  // nothing above $2 a request at list price
const FEES = { [BASE]: TERMS.settlementFeeAtomic, [SOLANA]: SOLANA_TERMS.settlementFeeAtomic };
const SIZE = /^([1-9][0-9]{2,3})x([1-9][0-9]{2,3})$/;

export class ImageError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

// The request sent on to Surplus, within what is sold here.
export function readImageRequest(body, model) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new ImageError(400, "Send a JSON body.");
  if (typeof body.prompt !== "string" || !body.prompt.trim() || body.prompt.length > IMAGE_TERMS.maxPromptChars) {
    throw new ImageError(400, `Send a prompt of up to ${IMAGE_TERMS.maxPromptChars} characters.`);
  }
  const n = body.n ?? 1;
  if (!Number.isInteger(n) || n < 1 || n > IMAGE_TERMS.maxImages) throw new ImageError(400, `n must be 1 to ${IMAGE_TERMS.maxImages}.`);
  const request = { model: model.id, prompt: body.prompt, n };
  if (body.size !== undefined) {
    if (typeof body.size !== "string" || !SIZE.test(body.size) || (model.sizes && !model.sizes.includes(body.size))) {
      throw new ImageError(400, `size must be one of ${(model.sizes ?? ["WIDTHxHEIGHT"]).join(", ")}.`);
    }
    request.size = body.size;
  }
  for (const field of ["quality", "style"]) {
    if (body[field] !== undefined) {
      if (typeof body[field] !== "string" || body[field].length > 40) throw new ImageError(400, `${field} must be a short string.`);
      if (model.params.includes(field)) request[field] = body[field];
    }
  }
  if (body.response_format !== undefined) {
    if (!["b64_json", "url"].includes(body.response_format)) throw new ImageError(400, "response_format must be b64_json or url.");
    request.response_format = body.response_format;
  }
  return request;
}

// The model's list price for the request, in micro-USD.
export function imageListAtomic(model, request) {
  const n = request.n ?? 1;
  let each;
  if (model.metering === "per_image") each = model.imageUsd;
  else if (model.metering === "per_megapixel") {
    const [, w, h] = SIZE.exec(request.size ?? "1024x1024");
    each = model.imageUsd * (Number(w) * Number(h)) / 1e6;
  } else {
    each = (request.prompt.length / 3) * model.promptUsdPerToken + IMAGE_TERMS.tokensPerImage * model.completionUsdPerToken;
  }
  return Math.ceil(each * 1e6) * n;
}

// What the payer pays for a request listed at `listAtomic`, on `network`.
export function imagePrice(listAtomic, network) {
  const list = BigInt(listAtomic);
  const markup = (list * BigInt(IMAGE_TERMS.markupBps) + 9999n) / 10000n;
  return { listAtomic: list.toString(), markupAtomic: markup.toString(), feeAtomic: FEES[network],
           totalAtomic: (list + markup + BigInt(FEES[network])).toString() };
}

// <=300 characters with the "SingIt Images: " prefix (see app.mjs's DISCOVERY_DESCRIPTION for why).
const DESCRIPTION = "Images from 36 models (Nano Banana, GPT Image, Flux, Seedream, Recraft, Qwen and more; list at GET "
  + "/v1/images/models), no API key needed. Call it when an agent needs an image made from a text prompt. OpenAI "
  + "images request format.";

export function addImageRoutes(app, config, { facilitatorClient, catalogue, fetchImpl = fetch, log = console.error, open }) {
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new ExactEvmScheme())
    .register(SOLANA, new ExactSvmScheme());
  const priced = new WeakMap();   // parsed body -> { request, listAtomic }
  const used = new Map();         // payment signature digest -> when
  const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");

  // Free: every image model sold here, with what one 1024x1024 image is expected to cost the payer.
  app.get(IMAGE_MODELS_ROUTE, open, async (_req, res) => {
    const models = await catalogue.imageModels().catch(() => new Map());
    res.json({ object: "list", endpoint: IMAGE_ROUTE, fees: { markupPct: IMAGE_TERMS.markupBps / 100,
      perRequestUsd: { [BASE]: Number(TERMS.settlementFeeAtomic) / 1e6, [SOLANA]: Number(SOLANA_TERMS.settlementFeeAtomic) / 1e6 } },
      data: [...models.values()].map((m) => {
        const list = imageListAtomic(m, { prompt: "", n: 1, size: m.sizes?.includes("1024x1024") || !m.sizes ? "1024x1024" : m.sizes[0] });
        return { id: m.id, object: "model", name: m.name, owned_by: m.provider || "surplus", metering: m.metering,
          sizes: m.sizes, listUsdPerImage: list / 1e6, singitUsdPerImage: Number(imagePrice(list, BASE).totalAtomic) / 1e6 };
      }).sort((a, b) => a.singitUsdPerImage - b.singitUsdPerImage) });
  });

  const price = (network) => (ctx) => {
    const quote = priced.get(ctx.adapter.getBody());
    const amount = imagePrice(quote?.listAtomic ?? IMAGE_TERMS.maxListAtomic, network).totalAtomic;
    return network === BASE ? { asset: ASSET, amount, extra: { name: "USD Coin", version: "2" } } : { asset: SOLANA_ASSET, amount };
  };
  app.post(IMAGE_ROUTE, async (req, res, next) => {
    // x402scan's empty, unpaid probe gets the standing offer (the model-list ceiling), not a model-lookup 404;
    // paying it with no question is still refused once a signature arrives, in the handler below.
    if (isDiscoveryProbe(req)) return next();
    try {
      const model = await catalogue.resolveImage(req.body?.model);
      const request = readImageRequest(req.body, model);
      const listAtomic = imageListAtomic(model, request);
      if (listAtomic > IMAGE_TERMS.maxListAtomic) throw new ImageError(400, "This request costs more than is sold here. Ask for fewer images.");
      priced.set(req.body, { request, listAtomic });
      next();
    } catch (error) {
      const status = error instanceof UnknownModel ? 404 : error.status ?? 503;
      res.status(status).json({ error: { type: status === 503 ? "upstream_error" : "invalid_request_error",
        code: status === 404 ? "model_not_found" : undefined, message: status === 503 ? "Try again shortly." : error.message } });
    }
  });
  addPaymentRoute(app, { [`POST ${IMAGE_ROUTE}`]: {
    // exact on Base first: the simplest offer for a buyer that only inspects accepts[0].
    accepts: [
      { scheme: "exact", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 180, price: price(BASE) },
      { scheme: "exact", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 180, price: price(SOLANA) },
    ],
    mimeType: "application/json", description: `SingIt Images: ${DESCRIPTION}`, serviceName: SERVICE_NAME, iconUrl: ICON_URL,
    extensions: declareDiscoveryExtension({ bodyType: "json",
      input: { model: "venice-seedream-v5-lite", prompt: "a small green robot holding a coin, flat illustration", n: 1, size: "1024x1024" } }),
  } }, server);
  app.post(IMAGE_ROUTE, async (req, res) => {
    const header = req.get("payment-signature") ?? req.get("x-payment");
    const network = JSON.parse(Buffer.from(header, "base64").toString()).accepted?.network;
    const seen = digest(header);
    if (used.has(seen)) return res.status(409).json({ error: { type: "invalid_request_error", message: "This payment was already used." } });
    used.set(seen, Date.now());
    for (const [k, at] of used) if (Date.now() - at > 3_600_000) used.delete(k);
    const { request, listAtomic } = priced.get(req.body);
    const started = Date.now();
    let response;
    try {
      response = await fetchImpl(`${config.surplusUrl}${IMAGE_ROUTE}`, { method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}` },
        body: JSON.stringify(request), signal: AbortSignal.timeout(150_000) });
    } catch (error) {
      log(`singit-ask: image model unreachable (${error.name})`);
      return res.status(502).json({ error: { type: "upstream_error", message: "No image was made. You were not charged." } });
    }
    const data = await response.json().catch(() => null);
    const images = Array.isArray(data?.data) ? data.data.filter((d) => d && (d.b64_json || d.url)) : [];
    if (!response.ok || !images.length) {
      log(`singit-ask: image model ${request.model} answered HTTP ${response.status} with ${images.length} images`);
      return res.status(502).json({ error: { type: "upstream_error", message: "No image was made. You were not charged." } });
    }
    const cost = Number(response.headers.get("x-si-buyer-cost-micro"));
    if (Number.isFinite(cost) && cost > listAtomic) log(`singit-ask: ${request.model} cost ${cost} micro-USD, listed ${listAtomic}`);
    const billing = { ...imagePrice(listAtomic, network), currency: "USDC" };
    log(`singit-ask: ${images.length} image(s) from ${request.model} in ${Date.now() - started} ms for ${billing.totalAtomic} micro-USDC`);
    res.json({ created: data.created ?? Math.floor(started / 1000), data: images, model: request.model, billing });
  });
}
