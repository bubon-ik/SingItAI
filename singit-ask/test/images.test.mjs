// SingIt images: the list price + 30% + the fee, Surplus asked once with this service's key, nothing charged without
// an image, edits and adult models not sold.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BASE, SOLANA, createApp } from "../src/app.mjs";
import { parseImageModels } from "../src/catalogue.mjs";
import { IMAGE_ROUTE, imageListAtomic, imagePrice, readImageRequest } from "../src/images.mjs";

const CONFIG = { payToSolana: "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
  payToBase: "0x1111111111111111111111111111111111111111", surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash" };
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64");
const decode = (header) => JSON.parse(Buffer.from(header, "base64").toString("utf8"));
const entry = (id, metering, pricing, extra = {}) => ({ id, name: id, provider: "venice", metering, pricing,
  architecture: { modality: "text->image", input_modalities: ["text"], output_modalities: ["image"] },
  supported_parameters: ["prompt", "model", "n", "size", "quality", "style", "response_format"], ...extra });
const LIST = { data: [
  entry("venice-seedream-v5-lite", "per_image", { image: "0.01" }, { sizes: ["1024x1024", "1536x1024"] }),
  entry("venice-flux-2-pro", "per_megapixel", { image: "0.045" }),
  entry("venice-nano-banana-2", "per_token", { prompt: "0.0000005", completion: "0.000003" }),
  entry("venice-lustify-v7", "per_image", { image: "0.01" }),
  entry("gpt-image-2-edit", "per_image", { image: "0.36" }),
  { id: "deepseek-v4.1-flash", architecture: { modality: "text->text" }, pricing: { prompt: "0.0000001", completion: "0.0000003" } },
] };
const IMAGES = parseImageModels(LIST);
const catalogue = { async imageModels() { return IMAGES; },
  async resolveImage(id) { const m = IMAGES.get(id); if (!m) { const { UnknownModel } = await import("../src/catalogue.mjs"); throw new UnknownModel("That image model is not available."); } return m; },
  async list() { return []; }, async resolve() { throw new Error("no"); } };

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

function fakeSurplus({ status = 200, images = 1, cost = "3960" } = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, auth: init.headers.Authorization, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ created: 1, data: Array.from({ length: images }, () => ({ b64_json: "aW1hZ2U=" })) }),
      { status, headers: { "content-type": "application/json", "x-si-buyer-cost-micro": cost } });
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
const IMAGE = { model: "venice-seedream-v5-lite", prompt: "a green robot", n: 1, size: "1024x1024" };

test("edits, background removal and adult models are not sold; text models are not images", () => {
  assert.deepEqual([...IMAGES.keys()], ["venice-seedream-v5-lite", "venice-flux-2-pro", "venice-nano-banana-2"]);
});

test("an image is offered at its list price + 30% + the network's fee, and listed for discovery", async (t) => {
  const { url, surplus, facilitator } = await serve(t);
  const unpaid = await post(`${url}${IMAGE_ROUTE}`, IMAGE);
  assert.equal(unpaid.status, 402);
  const required = decode(unpaid.headers.get("payment-required"));
  assert.deepEqual(required.accepts.map((a) => [a.scheme, a.network, a.amount]), [
    ["exact", BASE, "14000"], ["exact", SOLANA, "15000"]]);
  assert.ok(required.extensions?.bazaar);
  assert.deepEqual(surplus.calls, []);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("a paid image is made once with this service's key, then the payer is settled", async (t) => {
  const { url, surplus, facilitator } = await serve(t);
  const required = decode((await post(`${url}${IMAGE_ROUTE}`, IMAGE)).headers.get("payment-required"));
  const paid = await post(`${url}${IMAGE_ROUTE}`, IMAGE, pay(required, BASE));
  assert.equal(paid.status, 200);
  const body = await paid.json();
  assert.deepEqual(body.data, [{ b64_json: "aW1hZ2U=" }]);
  assert.deepEqual(body.billing, { listAtomic: "10000", markupAtomic: "3000", feeAtomic: "1000", totalAtomic: "14000", currency: "USDC" });
  assert.deepEqual(surplus.calls.map((c) => [c.url, c.auth, c.body]), [["https://surplus.test/v1/images/generations",
    "Bearer inf_test", { model: "venice-seedream-v5-lite", prompt: "a green robot", n: 1, size: "1024x1024" }]]);
  assert.deepEqual(facilitator.calls.settle, [[BASE, "14000"]]);
  assert.equal((await post(`${url}${IMAGE_ROUTE}`, IMAGE, pay(required, BASE))).status, 409);
  assert.equal(surplus.calls.length, 1);
});

test("no image, no charge; a cost above list price is logged", async (t) => {
  for (const surplus of [fakeSurplus({ status: 500 }), fakeSurplus({ images: 0 })]) {
    const { url, facilitator } = await serve(t, { surplus });
    const required = decode((await post(`${url}${IMAGE_ROUTE}`, IMAGE)).headers.get("payment-required"));
    assert.equal((await post(`${url}${IMAGE_ROUTE}`, IMAGE, pay(required, SOLANA))).status, 502);
    assert.deepEqual(facilitator.calls.settle, []);
  }
  const { url, logs } = await serve(t, { surplus: fakeSurplus({ cost: "12000" }) });
  const required = decode((await post(`${url}${IMAGE_ROUTE}`, IMAGE)).headers.get("payment-required"));
  await post(`${url}${IMAGE_ROUTE}`, IMAGE, pay(required, BASE));
  assert.ok(logs.some((l) => /cost 12000 micro-USD, listed 10000/.test(l)));
});

test("what is not sold is refused before any payment is asked", async (t) => {
  const { url, surplus } = await serve(t);
  for (const [body, status] of [[{ ...IMAGE, model: "venice-lustify-v7" }, 404], [{ ...IMAGE, model: "gpt-image-2-edit" }, 404],
    [{ ...IMAGE, model: undefined }, 404], [{ ...IMAGE, prompt: "" }, 400], [{ ...IMAGE, n: 5 }, 400],
    [{ ...IMAGE, size: "4096x4096" }, 400], [{ ...IMAGE, response_format: "png" }, 400]]) {
    const refused = await post(`${url}${IMAGE_ROUTE}`, body);
    assert.equal(refused.status, status, JSON.stringify(body));
    assert.equal(refused.headers.get("payment-required"), null);
  }
  assert.deepEqual(surplus.calls, []);
});

test("x402scan's empty, unpaid probe gets the standing offer; a payment header does not excuse an invalid body", async (t) => {
  const { url, surplus } = await serve(t);
  for (const init of [{ method: "POST" }, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }]) {
    const probe = await fetch(`${url}${IMAGE_ROUTE}`, init);
    assert.equal(probe.status, 402);
    const required = decode(probe.headers.get("payment-required"));
    assert.deepEqual(required.accepts.map((a) => [a.scheme, a.network]), [["exact", BASE], ["exact", SOLANA]]);
    assert.ok(required.extensions?.bazaar?.info, "x402scan needs the input schema to index the route");
    assert.ok(required.extensions?.bazaar?.schema);
  }
  const required = decode((await post(`${url}${IMAGE_ROUTE}`, IMAGE)).headers.get("payment-required"));
  const paidEmpty = await fetch(`${url}${IMAGE_ROUTE}`, { method: "POST", headers: pay(required, BASE) });
  // Not a probe (it carries a payment attempt): refused with no body's model lookup failing first (404), same as
  // any other unknown-model request; never waved through, never charged.
  assert.equal(paidEmpty.status, 404, "a payment header with no body is still refused, not waved through as a probe");
  assert.deepEqual(surplus.calls, []);
});

test("prices: per image, per megapixel and per token", () => {
  const [seedream, flux, banana] = [...IMAGES.values()];
  assert.equal(imageListAtomic(seedream, { prompt: "x", n: 3 }), 30000);
  assert.equal(imageListAtomic(flux, { prompt: "x", n: 1, size: "1024x1024" }), 47186);
  assert.equal(imageListAtomic(banana, { prompt: "x".repeat(300), n: 1 }), 6050);
  assert.deepEqual(imagePrice(10000, SOLANA), { listAtomic: "10000", markupAtomic: "3000", feeAtomic: "2000", totalAtomic: "15000" });
  assert.deepEqual(readImageRequest({ prompt: "x", quality: "hd", style: "vivid" }, seedream),
    { model: "venice-seedream-v5-lite", prompt: "x", n: 1, quality: "hd", style: "vivid" });
});

test("the image model list is free, cheapest first", async (t) => {
  const { url } = await serve(t);
  const list = await (await fetch(`${url}/v1/images/models`)).json();
  assert.deepEqual(list.data.map((m) => [m.id, m.singitUsdPerImage]), [
    ["venice-nano-banana-2", 0.0088], ["venice-seedream-v5-lite", 0.014], ["venice-flux-2-pro", 0.062342]]);
});
