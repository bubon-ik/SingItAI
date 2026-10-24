// SingIt speech: the list price for the text + 30% + the fee, Surplus asked once with this service's key, the audio
// passed on as it came, nothing charged without audio.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BASE, SOLANA, createApp } from "../src/app.mjs";
import { UnknownModel, parseVoiceModels } from "../src/catalogue.mjs";
import { SPEECH_ROUTE, readSpeechRequest, speechListAtomic, speechPrice } from "../src/speech.mjs";

const CONFIG = { payToSolana: "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
  payToBase: "0x1111111111111111111111111111111111111111", surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash" };
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64");
const decode = (header) => JSON.parse(Buffer.from(header, "base64").toString("utf8"));
const VOICE = parseVoiceModels({ data: [
  { id: "venice-kokoro-tts", architecture: { modality: "text->audio" }, pricing: { image: "0.0035", media_unit: "1K characters" },
    voices: ["af_heart", "am_adam"] },
  { id: "venice-elevenlabs-tts-turbo-v2-5", architecture: { modality: "text->audio" }, pricing: { image: "0.0625", media_unit: "1K characters" } },
  { id: "venice-whisper-1", architecture: { modality: "audio->text" }, pricing: { image: "0.006", media_unit: "minute" } },
  { id: "deepseek-v4.1-flash", architecture: { modality: "text->text" }, pricing: { prompt: "0.0000001" } },
] });
const catalogue = { async voiceModels() { return VOICE; }, async imageModels() { return new Map(); },
  async resolveSpeech(id) { const m = VOICE.speech.get(id); if (!m) throw new UnknownModel("That speech model is not available."); return m; },
  async resolveImage() { throw new UnknownModel("no"); }, async list() { return []; }, async resolve() { throw new Error("no"); } };

function fakeFacilitator() {
  const calls = { settle: [] };
  return { calls,
    async getSupported() {
      return { kinds: [
        { x402Version: 2, scheme: "exact", network: BASE },
        { x402Version: 2, scheme: "exact", network: SOLANA, extra: { feePayer: "Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88" } },
        { x402Version: 2, scheme: "upto", network: BASE, extra: { facilitatorAddress: "0x" + "3".repeat(40) } },
        { x402Version: 2, scheme: "upto", network: SOLANA, extra: { feePayer: "Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88",
          receiverAuthorizer: "9dpHxn3XFZMZv59vE5MKxhfwGUCCgkcCUzYZLpdEm7ox" } },
      ], extensions: ["bazaar"], signers: {} };
    },
    async verify() { return { isValid: true, payer: "0x2222222222222222222222222222222222222222" }; },
    async settle(_payload, requirements) {
      calls.settle.push([requirements.network, requirements.amount]);
      return { success: true, transaction: "0x" + "a".repeat(64), network: requirements.network };
    } };
}

const AUDIO = Buffer.from("ID3\x04fake-mp3-bytes");
function fakeSurplus({ status = 200, type = "audio/mpeg", body = AUDIO, cost = "80" } = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, auth: init.headers.Authorization, body: JSON.parse(init.body) });
    return new Response(body, { status, headers: { "content-type": type, "x-si-buyer-cost-micro": cost } });
  };
  return { calls, fetchImpl };
}

async function serve(t, { surplus = fakeSurplus() } = {}) {
  const facilitator = fakeFacilitator();
  const logs = [];
  const app = createApp(CONFIG, { facilitatorClient: facilitator, catalogue, fetchImpl: surplus.fetchImpl,
    log: (line) => logs.push(line), upstream: async () => { throw new Error("no answer is asked"); } });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, surplus, logs };
}

const post = (url, body, headers = {}) => fetch(url, { method: "POST",
  headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
const pay = (required, network) => ({ "PAYMENT-SIGNATURE": encode({ x402Version: 2, resource: required.resource,
  accepted: required.accepts.find((a) => a.network === network), payload: { signature: "0xsigned" },
  extensions: required.extensions }) });
const TEXT = "x".repeat(2000);
const SPEECH = { model: "venice-kokoro-tts", input: TEXT, voice: "af_heart" };

test("speech is offered at the list price for its text + 30% + the network's fee", async (t) => {
  const { url, surplus } = await serve(t);
  const unpaid = await post(`${url}${SPEECH_ROUTE}`, SPEECH);
  assert.equal(unpaid.status, 402);
  const required = decode(unpaid.headers.get("payment-required"));
  // 2000 characters at $0.0035 per 1000 = 7000, + 2100 markup, + the fee. exact on Base leads (SI-61).
  assert.deepEqual(required.accepts.map((a) => [a.network, a.amount]), [[BASE, "10100"], [SOLANA, "11100"]]);
  assert.ok(required.extensions?.bazaar);
  assert.deepEqual(surplus.calls, []);
});

test("paid speech comes back as audio, made once with this service's key, then the payer is settled", async (t) => {
  const { url, surplus, facilitator } = await serve(t);
  const required = decode((await post(`${url}${SPEECH_ROUTE}`, SPEECH)).headers.get("payment-required"));
  const paid = await post(`${url}${SPEECH_ROUTE}`, SPEECH, pay(required, BASE));
  assert.equal(paid.status, 200);
  assert.equal(paid.headers.get("content-type"), "audio/mpeg");
  assert.equal(paid.headers.get("x-singit-charged-atomic"), "10100");
  assert.deepEqual(Buffer.from(await paid.arrayBuffer()), AUDIO);
  assert.deepEqual(surplus.calls.map((c) => [c.url, c.auth, c.body]), [["https://surplus.test/v1/audio/speech", "Bearer inf_test",
    { model: "venice-kokoro-tts", input: TEXT, voice: "af_heart" }]]);
  assert.deepEqual(facilitator.calls.settle, [[BASE, "10100"]]);
  assert.equal((await post(`${url}${SPEECH_ROUTE}`, SPEECH, pay(required, BASE))).status, 409);
});

test("no audio, no charge", async (t) => {
  for (const surplus of [fakeSurplus({ status: 500 }), fakeSurplus({ type: "application/json", body: "{}" }), fakeSurplus({ body: "" })]) {
    const { url, facilitator } = await serve(t, { surplus });
    const required = decode((await post(`${url}${SPEECH_ROUTE}`, SPEECH)).headers.get("payment-required"));
    assert.equal((await post(`${url}${SPEECH_ROUTE}`, SPEECH, pay(required, SOLANA))).status, 502);
    assert.deepEqual(facilitator.calls.settle, []);
  }
});

test("what is not sold is refused before any payment is asked", async (t) => {
  const { url, surplus } = await serve(t);
  for (const [body, status] of [[{ ...SPEECH, model: "venice-whisper-1" }, 404], [{ ...SPEECH, model: "tts-9" }, 404],
    [{ ...SPEECH, input: "" }, 400], [{ ...SPEECH, input: "x".repeat(4097) }, 400], [{ ...SPEECH, voice: "nobody" }, 400],
    [{ ...SPEECH, response_format: "midi" }, 400], [{ ...SPEECH, speed: 9 }, 400]]) {
    const refused = await post(`${url}${SPEECH_ROUTE}`, body);
    assert.equal(refused.status, status, JSON.stringify(body).slice(0, 80));
    assert.equal(refused.headers.get("payment-required"), null);
  }
  assert.deepEqual(surplus.calls, []);
});

test("x402scan's empty, unpaid probe gets the standing offer; a payment header does not excuse an invalid body", async (t) => {
  const { url, surplus } = await serve(t);
  for (const init of [{ method: "POST" }, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }]) {
    const probe = await fetch(`${url}${SPEECH_ROUTE}`, init);
    assert.equal(probe.status, 402);
    const required = decode(probe.headers.get("payment-required"));
    assert.deepEqual(required.accepts.map((a) => [a.scheme, a.network]), [["exact", BASE], ["exact", SOLANA]]);
    assert.ok(required.extensions?.bazaar?.info, "x402scan needs the input schema to index the route");
    assert.ok(required.extensions?.bazaar?.schema);
  }
  const required = decode((await post(`${url}${SPEECH_ROUTE}`, SPEECH)).headers.get("payment-required"));
  const paidEmpty = await fetch(`${url}${SPEECH_ROUTE}`, { method: "POST", headers: pay(required, BASE) });
  // Not a probe (it carries a payment attempt): refused with no body's model lookup failing first (404), same as
  // any other unknown-model request; never waved through, never charged.
  assert.equal(paidEmpty.status, 404, "a payment header with no body is still refused, not waved through as a probe");
  assert.deepEqual(surplus.calls, []);
});

test("prices, and the free list of voice models", async (t) => {
  const kokoro = VOICE.speech.get("venice-kokoro-tts");
  assert.equal(speechListAtomic(kokoro, { input: "hi" }), 7);
  assert.equal(speechListAtomic(kokoro, { input: "x".repeat(57) }), 200);
  assert.deepEqual(speechPrice(200, SOLANA), { listAtomic: "200", markupAtomic: "60", feeAtomic: "2000", totalAtomic: "2260" });
  assert.deepEqual(readSpeechRequest({ input: "hi", response_format: "wav", speed: 1.5 }, kokoro),
    { model: "venice-kokoro-tts", input: "hi", response_format: "wav", speed: 1.5 });
  const { url } = await serve(t);
  const list = await (await fetch(`${url}/v1/audio/models`)).json();
  assert.deepEqual(list.data.map((m) => [m.id, m.kind]), [["venice-kokoro-tts", "speech"],
    ["venice-elevenlabs-tts-turbo-v2-5", "speech"], ["venice-whisper-1", "transcription"]]);
  assert.equal(list.data[0].singitUsdPer1kChars, 0.00455);
});
