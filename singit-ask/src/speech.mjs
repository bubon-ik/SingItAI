// SingIt speech: text read aloud by any of Surplus's voice models, sold over x402 at the model's list price for the
// text's length + 30% + the network's settlement fee, and made on Surplus with this service's own key.
//
// The price follows from the text before the payer is asked (the models are priced per 1000 characters); the
// payment is verified, Surplus makes the audio, and only then is the payment settled: no audio, no charge. The audio
// comes back as it is (mp3 by default), with what was charged in the X-SingIt-* headers.
import crypto from "node:crypto";
import { x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { UnknownModel } from "./catalogue.mjs";
import { addPaymentRoute, isDiscoveryProbe } from "./metered.mjs";
import { ASSET, BASE, SOLANA, SOLANA_ASSET, TERMS, SOLANA_TERMS, SERVICE_NAME, ICON_URL } from "./pricing.mjs";

export const SPEECH_ROUTE = "/v1/audio/speech";
export const AUDIO_MODELS_ROUTE = "/v1/audio/models";
export const SPEECH_TERMS = Object.freeze({ markupBps: 3000, maxChars: 4096 });
const FORMATS = new Set(["mp3", "opus", "aac", "flac", "wav", "pcm"]);
const FEES = { [BASE]: TERMS.settlementFeeAtomic, [SOLANA]: SOLANA_TERMS.settlementFeeAtomic };

export class SpeechError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function readSpeechRequest(body, model) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new SpeechError(400, "Send a JSON body.");
  if (typeof body.input !== "string" || !body.input.trim() || body.input.length > SPEECH_TERMS.maxChars) {
    throw new SpeechError(400, `Send input text of up to ${SPEECH_TERMS.maxChars} characters.`);
  }
  const request = { model: model.id, input: body.input };
  if (body.voice !== undefined) {
    if (typeof body.voice !== "string" || body.voice.length > 60 || (model.voices && !model.voices.includes(body.voice))) {
      throw new SpeechError(400, model.voices ? `voice must be one of ${model.voices.slice(0, 12).join(", ")}…` : "voice must be a short string.");
    }
    request.voice = body.voice;
  }
  if (body.response_format !== undefined) {
    if (!FORMATS.has(body.response_format)) throw new SpeechError(400, `response_format must be one of ${[...FORMATS].join(", ")}.`);
    request.response_format = body.response_format;
  }
  if (body.speed !== undefined) {
    if (typeof body.speed !== "number" || body.speed < 0.25 || body.speed > 4) throw new SpeechError(400, "speed must be 0.25 to 4.");
    request.speed = body.speed;
  }
  return request;
}

// The model's list price for this text, in micro-USD (at least 1).
export const speechListAtomic = (model, request) => Math.max(1, Math.ceil(model.usdPerUnit * request.input.length / 1000 * 1e6));

export function speechPrice(listAtomic, network) {
  const list = BigInt(listAtomic);
  const markup = (list * BigInt(SPEECH_TERMS.markupBps) + 9999n) / 10000n;
  return { listAtomic: list.toString(), markupAtomic: markup.toString(), feeAtomic: FEES[network],
           totalAtomic: (list + markup + BigInt(FEES[network])).toString() };
}

// <=300 characters with the "SingIt Speech: " prefix (see app.mjs's DISCOVERY_DESCRIPTION for why).
const DESCRIPTION = "Text read aloud by 11 voice models (Kokoro, ElevenLabs, Gemini, xAI, MiniMax and more; list at GET "
  + "/v1/audio/models), no API key needed. Call it when an agent needs spoken audio made from text. OpenAI speech "
  + "request format, mp3 by default.";

export function addSpeechRoutes(app, config, { facilitatorClient, catalogue, fetchImpl = fetch, log = console.error, open }) {
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new ExactEvmScheme())
    .register(SOLANA, new ExactSvmScheme());
  const priced = new WeakMap();
  const used = new Map();
  const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");

  // Free: every voice model, speech per 1000 characters and transcription per minute (transcription: API keys only).
  app.get(AUDIO_MODELS_ROUTE, open, async (_req, res) => {
    const voice = await catalogue.voiceModels().catch(() => ({ speech: new Map(), transcription: new Map() }));
    const speech = [...voice.speech.values()].map((m) => ({ id: m.id, object: "model", kind: "speech", name: m.name,
      owned_by: m.provider || "surplus", voices: m.voices, listUsdPer1kChars: m.usdPerUnit,
      singitUsdPer1kChars: Number((m.usdPerUnit * (1 + SPEECH_TERMS.markupBps / 10000)).toFixed(6)) }));
    const transcription = [...voice.transcription.values()].map((m) => ({ id: m.id, object: "model", kind: "transcription",
      name: m.name, owned_by: m.provider || "surplus", listUsdPerMinute: m.usdPerUnit,
      singitUsdPerMinute: Number((m.usdPerUnit * (1 + SPEECH_TERMS.markupBps / 10000)).toFixed(6)) }));
    res.json({ object: "list", endpoint: SPEECH_ROUTE, fees: { markupPct: SPEECH_TERMS.markupBps / 100,
      perRequestUsd: { [BASE]: Number(TERMS.settlementFeeAtomic) / 1e6, [SOLANA]: Number(SOLANA_TERMS.settlementFeeAtomic) / 1e6 } },
      data: [...speech.sort((a, b) => a.listUsdPer1kChars - b.listUsdPer1kChars),
             ...transcription.sort((a, b) => a.listUsdPerMinute - b.listUsdPerMinute)] });
  });

  const price = (network) => (ctx) => {
    const quote = priced.get(ctx.adapter.getBody());
    const amount = speechPrice(quote?.listAtomic ?? 1_000_000, network).totalAtomic;
    return network === BASE ? { asset: ASSET, amount, extra: { name: "USD Coin", version: "2" } } : { asset: SOLANA_ASSET, amount };
  };
  app.post(SPEECH_ROUTE, async (req, res, next) => {
    // x402scan's empty, unpaid probe gets the standing offer, not a model-lookup 404.
    if (isDiscoveryProbe(req)) return next();
    try {
      const model = await catalogue.resolveSpeech(req.body?.model);
      const request = readSpeechRequest(req.body, model);
      priced.set(req.body, { request, listAtomic: speechListAtomic(model, request) });
      next();
    } catch (error) {
      const status = error instanceof UnknownModel ? 404 : error.status ?? 503;
      res.status(status).json({ error: { type: status === 503 ? "upstream_error" : "invalid_request_error",
        code: status === 404 ? "model_not_found" : undefined, message: status === 503 ? "Try again shortly." : error.message } });
    }
  });
  addPaymentRoute(app, { [`POST ${SPEECH_ROUTE}`]: {
    // exact on Base first: the simplest offer for a buyer that only inspects accepts[0].
    accepts: [
      { scheme: "exact", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120, price: price(BASE) },
      { scheme: "exact", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 120, price: price(SOLANA) },
    ],
    mimeType: "audio/mpeg", description: `SingIt Speech: ${DESCRIPTION}`, serviceName: SERVICE_NAME, iconUrl: ICON_URL,
    extensions: declareDiscoveryExtension({ bodyType: "json",
      input: { model: "venice-kokoro-tts", input: "Hello from SingIt.", voice: "af_heart", response_format: "mp3" } }),
  } }, server);
  app.post(SPEECH_ROUTE, async (req, res) => {
    const header = req.get("payment-signature") ?? req.get("x-payment");
    const network = JSON.parse(Buffer.from(header, "base64").toString()).accepted?.network;
    const seen = digest(header);
    if (used.has(seen)) return res.status(409).json({ error: { type: "invalid_request_error", message: "This payment was already used." } });
    used.set(seen, Date.now());
    for (const [k, at] of used) if (Date.now() - at > 3_600_000) used.delete(k);
    const { request, listAtomic } = priced.get(req.body);
    const started = Date.now();
    let response, audio;
    try {
      response = await fetchImpl(`${config.surplusUrl}${SPEECH_ROUTE}`, { method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.surplusKey}` },
        body: JSON.stringify(request), signal: AbortSignal.timeout(120_000) });
      audio = Buffer.from(await response.arrayBuffer());
    } catch (error) {
      log(`singit-ask: voice model unreachable (${error.name})`);
      return res.status(502).json({ error: { type: "upstream_error", message: "No audio was made. You were not charged." } });
    }
    const type = response.headers.get("content-type") ?? "";
    if (!response.ok || !audio.length || !/^audio\//.test(type)) {
      log(`singit-ask: voice model ${request.model} answered HTTP ${response.status} (${type}, ${audio.length} bytes)`);
      return res.status(502).json({ error: { type: "upstream_error", message: "No audio was made. You were not charged." } });
    }
    const cost = Number(response.headers.get("x-si-buyer-cost-micro"));
    if (Number.isFinite(cost) && cost > listAtomic) log(`singit-ask: ${request.model} cost ${cost} micro-USD, listed ${listAtomic}`);
    const billing = speechPrice(listAtomic, network);
    log(`singit-ask: ${request.input.length} characters read by ${request.model} in ${Date.now() - started} ms for ${billing.totalAtomic} micro-USDC`);
    res.set({ "Content-Type": type, "X-SingIt-Charged-Atomic": billing.totalAtomic, "X-SingIt-List-Atomic": billing.listAtomic,
              "Cache-Control": "no-store" });
    res.send(audio);
  });
}
