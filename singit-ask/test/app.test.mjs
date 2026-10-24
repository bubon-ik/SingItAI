// SingIt Ask against a fake CDP facilitator and a fake model: who is charged, when, and for what.

import { test } from "node:test";
import assert from "node:assert/strict";
import { request as httpRequest } from "node:http";
import { BASE, SOLANA, ROUTE, LIMITS, RequestError, UpstreamError, readRequest, askSurplus, createApp } from "../src/app.mjs";
import { FORMAT_ROUTES } from "../src/formats.mjs";
import { METERED_ROUTE, SOLANA_TERMS, TERMS, MIN_CEILING_ATOMIC, ceilingAtomic, billUsage } from "../src/pricing.mjs";
import { ModelCatalogue, UnknownModel, priceGuard } from "../src/catalogue.mjs";

const SOLANA_PAY_TO = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin";
const BASE_PAY_TO = "0x1111111111111111111111111111111111111111";
const PAYER = "0x2222222222222222222222222222222222222222";
const CONFIG = {
  payToSolana: SOLANA_PAY_TO, payToBase: BASE_PAY_TO, surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash",
};

function fakeFacilitator() {
  const calls = { verify: 0, settle: [] };
  return {
    calls,
    async getSupported() {
      return {
        kinds: [
          { x402Version: 2, scheme: "upto", network: BASE, extra: { facilitatorAddress: "0x" + "3".repeat(40) } },
          { x402Version: 2, scheme: "exact", network: BASE },
          { x402Version: 2, scheme: "exact", network: SOLANA, extra: { feePayer: "Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88" } },
          { x402Version: 2, scheme: "upto", network: SOLANA, extra: {
            feePayer: "Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88", receiverAuthorizer: "9dpHxn3XFZMZv59vE5MKxhfwGUCCgkcCUzYZLpdEm7ox" } },
        ],
        extensions: ["bazaar", "eip2612GasSponsoring"],
        signers: {},
      };
    },
    async verify() {
      calls.verify += 1;
      return { isValid: true, payer: PAYER };
    },
    async settle(_payload, requirements) {
      calls.settle.push(requirements.amount);
      return { success: true, transaction: "0x" + "a".repeat(64), network: requirements.network, payer: PAYER };
    },
  };
}

const PRICEY = { id: "pro-model", name: "Pro", provider: "X", contextLength: 1e6, promptUsdPerToken: 30e-6, completionUsdPerToken: 180e-6 };
const CHEAP = { id: "cheap-model", name: "Cheap", provider: "X", contextLength: 1e5, promptUsdPerToken: 0.1e-6, completionUsdPerToken: 0.3e-6 };
const IDLE = { id: "idle-model", name: "Idle", provider: "X", contextLength: 1, promptUsdPerToken: 1e-6, completionUsdPerToken: 1e-6 };
const fakeCatalogue = { async market() { return new Map([["pro-model", { trustedShare: 0.25 }]]); }, async resolve(id) { const m = [PRICEY, CHEAP, IDLE].find((x) => x.id === id); if (!m) throw new UnknownModel("That model is not available."); return m; },
  async list() { return [CHEAP, PRICEY]; }, async canAnswer(id) { return id !== "idle-model"; } };

async function serve(t, { upstream } = {}) {
  const facilitator = fakeFacilitator();
  const asked = [];
  const answer = upstream ?? (async (_config, messages, maxTokens) => {
    asked.push({ messages, maxTokens });
    return { content: "Try Caffè Propaganda.", finishReason: "stop", usage: { total_tokens: 42, buyer_cost_micro: 100 } };
  });
  const app = createApp(CONFIG, { facilitatorClient: facilitator, upstream: answer, log: () => {}, catalogue: fakeCatalogue });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, asked };
}

const QUESTION = { messages: [{ role: "user", content: "Coffee near the Colosseum?" }], max_tokens: 300 };

function post(url, body, headers = {}) {
  return fetch(`${url}${ROUTE}`, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  });
}

function decode(header) {
  return JSON.parse(Buffer.from(header, "base64").toString("utf8"));
}

// A Base payment the fake facilitator accepts, echoing the merchant's extensions as a real buyer does.
function baseSignature(required) {
  const accepted = required.accepts.find((a) => a.network === BASE && a.scheme === "upto");
  return Buffer.from(JSON.stringify({ x402Version: 2, resource: required.resource, accepted,
    payload: { signature: "0xsigned" }, extensions: required.extensions })).toString("base64");
}

// A buyer that only ever signs `exact`, like vet402: picks the exact offer instead of the upto one.
function exactSignature(required, network = BASE) {
  const accepted = required.accepts.find((a) => a.network === network && a.scheme === "exact");
  return Buffer.from(JSON.stringify({ x402Version: 2, resource: required.resource, accepted,
    payload: { signature: "0xsigned" }, extensions: required.extensions })).toString("base64");
}

async function offer(url) {
  const unpaid = await post(url, QUESTION);
  assert.equal(unpaid.status, 402);
  return decode(unpaid.headers.get("payment-required"));
}

test("an unpaid question is offered actual-usage payment on Solana and Base over CDP", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const required = await offer(url);
  const solana = required.accepts.find((a) => a.network === SOLANA && a.scheme === "upto");
  const base = required.accepts.find((a) => a.network === BASE && a.scheme === "upto");
  // `upto` first (what the gateway and most buyers sign), `exact` alongside it for buyers like vet402 that sign
  // only `exact`.
  assert.deepEqual(required.accepts.map((a) => a.scheme), ["upto", "upto", "exact", "exact"]);
  assert.equal(solana.amount, "3000");
  assert.equal(solana.payTo, SOLANA_PAY_TO);
  assert.deepEqual(solana.extra.billing, SOLANA_TERMS);
  assert.ok(solana.extra.feePayer, "Solana payers need CDP's fee payer");
  assert.equal(base.amount, "3000");
  assert.equal(base.payTo, BASE_PAY_TO);
  assert.equal(base.maxTimeoutSeconds, 120);
  assert.deepEqual(base.extra.billing, TERMS);
  assert.ok(required.extensions?.bazaar, "listed for discovery");
  assert.ok(required.extensions?.eip2612GasSponsoring, "Base payers need no gas");
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
});

test("the exact offers name the same ceiling as upto, flat and disclosed as such", async (t) => {
  const { url } = await serve(t);
  const required = await offer(url);
  const uptoBase = required.accepts.find((a) => a.network === BASE && a.scheme === "upto");
  const exactBase = required.accepts.find((a) => a.network === BASE && a.scheme === "exact");
  const exactSolana = required.accepts.find((a) => a.network === SOLANA && a.scheme === "exact");
  // Same ceiling as the upto offer for this question, never a different (lower or higher) one.
  assert.equal(exactBase.amount, uptoBase.amount);
  assert.equal(exactBase.payTo, BASE_PAY_TO);
  assert.equal(exactSolana.payTo, SOLANA_PAY_TO);
  assert.equal(exactBase.extra.billing.mode, "exact_ceiling");
  assert.equal(exactBase.extra.billing.maxChargeAtomic, exactBase.amount);
  assert.equal(exactSolana.extra.billing.mode, "exact_ceiling");
  assert.equal(exactSolana.extra.billing.maxChargeAtomic, exactSolana.amount);
});

test("an exact payment is settled at the full ceiling, not metered usage, and reports it as such", async (t) => {
  // The upstream answer costs far less than the ceiling (100 of 3000); exact still settles the whole ceiling.
  const { url, facilitator } = await serve(t);
  const required = await offer(url);
  const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": exactSignature(required) });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.billing.totalAtomic, "3000");
  assert.equal(body.billing.mode, "exact_ceiling");
  assert.deepEqual(facilitator.calls.settle, ["3000"]);
});

test("a failed upstream request paid with exact is not settled", async (t) => {
  const { url, facilitator } = await serve(t, { upstream: async () => { throw new UpstreamError("model answered HTTP 503"); } });
  const required = await offer(url);
  const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": exactSignature(required) });
  assert.equal(response.status, 502);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("the payment resource keeps the public HTTPS URL behind the local tunnel", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  // Native fetch can replace Host; send exactly the headers cloudflared forwards.
  const response = await new Promise((resolve, reject) => {
    const req = httpRequest(`${url}${ROUTE}`, { method: "POST", headers: {
      Host: "ask.singitai.app", "X-Forwarded-Proto": "https", "Content-Type": "application/json",
    } }, (res) => { res.resume(); res.on("end", () => resolve(res)); res.on("error", reject); });
    req.on("error", reject);
    req.end(JSON.stringify(QUESTION));
  });
  assert.equal(response.statusCode, 402);
  assert.equal(decode(response.headers["payment-required"]).resource.url,
               "https://ask.singitai.app/v1/chat/completions");
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
});

test("a paid question on Base gets the answer and is charged its actual cost once", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": baseSignature(await offer(url)) });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.choices[0].message.content, "Try Caffè Propaganda.");
  assert.equal(body.model, "singit-ask");
  assert.equal(body.billing.totalAtomic, "1130"); // 100 model + 30 markup + 1000 Base settlement fee
  assert.equal(asked[0].maxTokens, 300);
  assert.deepEqual(facilitator.calls.settle, ["1130"]);
  assert.equal(decode(response.headers.get("payment-response")).network, BASE);
});

for (const [name, upstream] of [
  ["a model that does not answer", async () => { throw new UpstreamError("model answered HTTP 503"); }],
  ["a model that reports no cost", async () => ({ content: "Hi", finishReason: "stop", usage: { total_tokens: 3 } })],
  ["an answer over the ceiling", async () => ({ content: "Hi", finishReason: "stop", usage: { buyer_cost_micro: 2000 } })],
]) {
  test(`${name} costs the payer nothing`, async (t) => {
    const { url, facilitator } = await serve(t, { upstream });
    const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": baseSignature(await offer(url)) });
    assert.equal(response.status, 502);
    assert.match((await response.json()).error.message, /not charged/);
    assert.deepEqual(facilitator.calls.settle, []);
  });
}

test("a malformed question is refused before any payment is asked for", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const response = await post(url, { messages: [{ role: "tool", content: "x" }] });
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("payment-required"), null);
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
});

test("an empty unpaid request gets the standing offer, so directories can register the route", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  for (const init of [{ method: "POST" }, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }]) {
    const response = await fetch(`${url}${ROUTE}`, init);
    assert.equal(response.status, 402);
    const required = decode(response.headers.get("payment-required"));
    assert.deepEqual(required.accepts.map((a) => a.scheme), ["upto", "upto", "exact", "exact"]);
    assert.deepEqual(required.accepts.map((a) => [a.network, a.amount]),
      [[SOLANA, "3000"], [BASE, "3000"], [SOLANA, "3000"], [BASE, "3000"]]);
    assert.ok(required.extensions?.bazaar?.info, "x402scan needs the input schema to index the route");
    assert.match(required.resource.description, /any model listed at GET \/v1\/models/);
    assert.doesNotMatch(required.resource.description, /at most 0\.003/);
  }
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
});

test("the listing tells agents what to send and what comes back, within Coinbase's limits", async (t) => {
  const { url } = await serve(t);
  const required = await offer(url);
  // Coinbase's facilitator rejects descriptions over 500 characters.
  assert.ok(required.resource.description.length <= 500, `description is ${required.resource.description.length} characters`);
  assert.match(required.resource.description, /Claude, GPT, Gemini/);
  const bazaar = required.extensions.bazaar;
  const body = bazaar.schema.properties.input.properties.body;
  assert.deepEqual(body.properties.messages.items.properties.role.enum, ["system", "user", "assistant"]);
  assert.equal(body.properties.max_tokens.maximum, LIMITS.maxTokens);
  // The declared example must satisfy the declared schema, or the Bazaar rejects the listing.
  const example = bazaar.info.input.body;
  assert.ok(example.messages.length >= body.properties.messages.minItems);
  for (const message of example.messages) {
    assert.ok(body.properties.messages.items.properties.role.enum.includes(message.role));
    assert.equal(typeof message.content, "string");
  }
  assert.ok(example.max_tokens <= body.properties.max_tokens.maximum && example.max_tokens >= body.properties.max_tokens.minimum);
  // What comes back is described too, with a realistic charge: 6 micro-USD of tokens, 2 markup, 1000 fee.
  assert.equal(bazaar.info.output.example.billing.totalAtomic, "1008");
  assert.deepEqual(billUsage(bazaar.info.output.example.usage), bazaar.info.output.example.billing);
  assert.ok(required.resource.tags.includes("claude"));
});

test("paying the offer with no question is refused and costs nothing", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const response = await fetch(`${url}${ROUTE}`, { method: "POST",
    headers: { "PAYMENT-SIGNATURE": baseSignature(await offer(url)) } });
  assert.equal(response.status, 400);
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("the web agent's metered route still refuses an empty request before any offer", async (t) => {
  const { url } = await serve(t);
  const response = await fetch(`${url}/v1/chat/completions/metered`, { method: "POST" });
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("payment-required"), null);
});

test("the OpenAPI document lists the gateway's free wallet report with its own server and no payment", async (t) => {
  const { url } = await serve(t);
  const doc = await (await fetch(`${url}/openapi.json`)).json();
  const item = doc.paths["/v1/wallet/{address}/reconciliation"];
  assert.deepEqual(item.servers, [{ url: "https://app.singitai.app" }]);
  assert.equal(item.get.operationId, "walletReconciliation");
  assert.deepEqual(item.get.security, []);
  assert.equal(item.get["x-payment-info"], undefined);
  assert.equal(item.get.responses[402], undefined);
  assert.ok(item.get.responses[200].content["application/json"].schema.properties.allowance);
  assert.deepEqual(item.get.parameters.find((p) => p.name === "days").schema.maximum, 90);
  const wellKnown = await (await fetch(`${url}/.well-known/x402`)).json();
  assert.ok(!JSON.stringify(wellKnown).includes("reconciliation"));
});

test("the OpenAPI document lists the paid route the way x402scan reads it", async (t) => {
  const { url, facilitator } = await serve(t);
  const response = await fetch(`${url}/openapi.json`);
  assert.equal(response.status, 200);
  const doc = await response.json();
  assert.equal(doc.openapi, "3.1.0");
  assert.ok(doc.info.title && doc.info.version);
  assert.equal(doc.servers[0].url, url);
  const op = doc.paths[ROUTE].post;
  assert.deepEqual(op["x-payment-info"].protocols, [{ x402: {} }]);
  const price = op["x-payment-info"].price;
  assert.equal(price.mode, "dynamic");
  assert.equal(price.currency, "USD");
  assert.equal(price.min, "0.001");
  // The dearest listed model (PRICEY) at the largest question and answer, on Solana's terms; `upto`'s loosest
  // input estimate (1 char/token), same as priceRange.
  const most = ceilingAtomic(PRICEY, { chars: LIMITS.chars, maxTokens: LIMITS.maxTokens, messageCount: LIMITS.messages, charsPerToken: 1 }, SOLANA_TERMS);
  assert.equal(Number(price.max), Number(most) / 1e6);
  assert.ok(op.responses[402]);
  assert.deepEqual(op.security, [{ x402: [] }]);
  assert.ok(doc.components.securitySchemes.x402);
  assert.deepEqual(op.requestBody.content["application/json"].schema.required, ["messages"]);
  assert.equal(doc.paths["/v1/models"].get["x-payment-info"], undefined, "the model list is free");
  assert.equal(doc.paths["/v1/chat/completions/metered"], undefined, "the web agent's private route is not listed");
  assert.equal(facilitator.calls.verify, 0);
});

test("/.well-known/x402 points to the paid route at its public URL", async (t) => {
  const { url } = await serve(t);
  const response = await new Promise((resolve, reject) => {
    const req = httpRequest(`${url}/.well-known/x402`, { headers: { Host: "ask.singitai.app", "X-Forwarded-Proto": "https" } },
      (res) => { let body = ""; res.on("data", (c) => { body += c; }); res.on("end", () => resolve({ status: res.statusCode, body })); res.on("error", reject); });
    req.on("error", reject);
    req.end();
  });
  assert.equal(response.status, 200);
  const doc = JSON.parse(response.body);
  assert.equal(doc.version, 1);
  assert.deepEqual(doc.resources, ["https://ask.singitai.app/v1/chat/completions",
                                   "https://ask.singitai.app/v1/messages", "https://ask.singitai.app/v1/responses",
                                   "https://ask.singitai.app/v1/images/generations", "https://ask.singitai.app/v1/audio/speech",
                                   "https://ask.singitai.app/v1/video/generations"]);
  assert.match(doc.instructions, /GET https:\/\/ask\.singitai\.app\/v1\/models/);
});

test("health and the model list are free, and the old test page is gone", async (t) => {
  const { url, facilitator } = await serve(t);
  const health = await (await fetch(`${url}/health`)).json();
  assert.equal(health.ok, true);
  assert.equal(health.facilitator, "coinbase-cdp");
  assert.deepEqual(health.pricing, { [SOLANA]: SOLANA_TERMS, [BASE]: TERMS });
  const models = await fetch(`${url}/v1/models`);
  assert.equal(models.status, 200);
  assert.equal((await models.json()).data[0].id, "singit-ask");
  assert.equal((await fetch(`${url}/test/`)).status, 404);
  assert.equal((await fetch(`${url}/v1/chat/completions/metered/solana`, { method: "POST" })).status, 404);
  assert.equal(facilitator.calls.verify, 0);
});

test("readRequest keeps the question within what one answer pays for", () => {
  assert.deepEqual(readRequest({ messages: [{ role: "user", content: "hi", name: "x" }] }),
                   { messages: [{ role: "user", content: "hi" }], maxTokens: LIMITS.defaultTokens, chars: 2, model: undefined, messageCount: 1 });
  assert.equal(readRequest({ ...QUESTION, max_tokens: 100_000 }).maxTokens, LIMITS.maxTokens);
  assert.equal(readRequest({ ...QUESTION, max_tokens: -5 }).maxTokens, LIMITS.defaultTokens);
  assert.throws(() => readRequest(null), RequestError);
  assert.throws(() => readRequest({ messages: [] }), RequestError);
  assert.throws(() => readRequest({ ...QUESTION, stream: true }), RequestError);
  assert.throws(() => readRequest({ messages: [{ role: "user", content: 5 }] }), RequestError);
  assert.throws(() => readRequest({ messages: [{ role: "user", content: "x".repeat(LIMITS.chars + 1) }] }), RequestError);
  assert.throws(() => readRequest({ messages: Array(LIMITS.messages + 1).fill({ role: "user", content: "x" }) }), RequestError);
});

test("askSurplus sends the key and model, and turns every failure into an UpstreamError", async () => {
  let sent;
  const ok = async (url, init) => {
    sent = { url, init };
    return new Response(JSON.stringify({ choices: [{ message: { content: "ciao" }, finish_reason: "stop" }],
                                         usage: { total_tokens: 7 } }), { status: 200 });
  };
  const answer = await askSurplus(CONFIG, QUESTION.messages, 300, ok);
  assert.equal(answer.content, "ciao");
  assert.equal(sent.url, "https://surplus.test/v1/chat/completions");
  assert.equal(sent.init.headers.Authorization, "Bearer inf_test");
  assert.equal(JSON.parse(sent.init.body).model, "deepseek-v4.1-flash");
  assert.equal(sent.init.headers["X-Min-Discount"], undefined, "no guard, no header");
  // Thinking off; max_tokens caps the answer and any thinking together, so neither gets room beyond it.
  assert.equal(JSON.parse(sent.init.body).reasoning_effort, "none");
  // The cheapest seller first (measured on 12 October: as fast, and cheaper).
  assert.deepEqual(JSON.parse(sent.init.body).si_route, { objective: "price" });
  assert.equal(JSON.parse(sent.init.body).max_tokens, 300);

  const guarded = await askSurplus(CONFIG, QUESTION.messages, 300, ok, CONFIG.model, { minDiscount: 59 });
  assert.equal(guarded.content, "ciao");
  assert.equal(sent.init.headers["X-Min-Discount"], "59");

  const failing = [
    async () => { throw new TypeError("fetch failed"); },
    async () => new Response("busy", { status: 503 }),
    async () => new Response("not json", { status: 200 }),
    async () => new Response(JSON.stringify({ choices: [{ message: { content: "  " } }] }), { status: 200 }),
  ];
  for (const fetchImpl of failing) {
    await assert.rejects(askSurplus(CONFIG, QUESTION.messages, 300, fetchImpl), UpstreamError);
  }
});

test("a seller that will not switch thinking off is asked once more with as little thinking as it allows", async () => {
  const efforts = [];
  const refusal = (message) => new Response(JSON.stringify({ error: { code: "request_rejected", message } }), { status: 400 });
  const fetchImpl = async (_url, init) => {
    efforts.push(JSON.parse(init.body).reasoning_effort);
    return efforts.length === 1 ? refusal("Reasoning is mandatory for this endpoint and cannot be disabled.")
      : new Response(JSON.stringify({ choices: [{ message: { content: "ciao" } }], usage: {} }), { status: 200 });
  };
  assert.equal((await askSurplus(CONFIG, QUESTION.messages, 300, fetchImpl)).content, "ciao");
  assert.deepEqual(efforts, ["none", "low"]);
  // Any other refusal, or a second one, is not retried.
  const other = [];
  await assert.rejects(askSurplus(CONFIG, QUESTION.messages, 300, async (_u, init) => {
    other.push(JSON.parse(init.body).reasoning_effort); return refusal("Too many messages."); }), UpstreamError);
  assert.deepEqual(other, ["none"]);
  const twice = [];
  await assert.rejects(askSurplus(CONFIG, QUESTION.messages, 300, async (_u, init) => {
    twice.push(JSON.parse(init.body).reasoning_effort); return refusal("Reasoning is mandatory."); }), UpstreamError);
  assert.deepEqual(twice, ["none", "low"]);
});

test("another model gets a ceiling worked out from its own prices; unresolvable default/standing models keep 3000", async (t) => {
  const { url, facilitator } = await serve(t);
  const offerFor = async (model) => {
    const unpaid = await post(url, { ...QUESTION, model });
    assert.equal(unpaid.status, 402);
    return decode(unpaid.headers.get("payment-required")).accepts;
  };
  // CONFIG.model ("deepseek-v4.1-flash") is not in the fake catalogue, so the default still falls back to null
  // info and the old standing ceiling.
  for (const model of [undefined, "deepseek-v4.1-flash", "singit-ask"]) {
    assert.deepEqual((await offerFor(model)).map((a) => a.amount), ["3000", "3000", "3000", "3000"], String(model));
  }
  const size = { chars: 26, maxTokens: 300, messageCount: 1 };
  const uptoSize = { ...size, charsPerToken: 1 };
  // Explicitly chosen (not the default), so a cheap model's own honest worst case is offered, floored at
  // MIN_CEILING_ATOMIC rather than forced up to the standing 3000.
  const cheap = await offerFor("cheap-model");
  assert.equal(cheap.find((a) => a.network === BASE && a.scheme === "upto").amount, ceilingAtomic(CHEAP, uptoSize, TERMS));
  assert.equal(cheap.find((a) => a.network === SOLANA && a.scheme === "upto").amount, ceilingAtomic(CHEAP, uptoSize, SOLANA_TERMS));
  assert.equal(cheap.find((a) => a.network === BASE && a.scheme === "upto").amount, MIN_CEILING_ATOMIC);
  const pro = await offerFor("pro-model");
  // `.find` on network alone picks the `upto` offer: it is listed before `exact` (see the ordering test below).
  assert.equal(pro.find((a) => a.network === BASE).amount, ceilingAtomic(PRICEY, uptoSize, TERMS));
  assert.equal(pro.find((a) => a.network === SOLANA).amount, ceilingAtomic(PRICEY, uptoSize, SOLANA_TERMS));
  assert.ok(BigInt(pro[0].amount) > 50_000n, "a dear model is authorized for what it can really cost");
  assert.equal(pro.find((a) => a.network === BASE).extra.billing.maxChargeAtomic, pro.find((a) => a.network === BASE).amount);
  assert.equal(facilitator.calls.verify, 0);
});

test("the default model, when it resolves, uses its own formula but is clamped to the standing ceiling", async (t) => {
  // A catalogue that happens to resolve the default model id: a cheap default gets an honest, lower ceiling; a
  // dear one never gets authorized above the standing 3000.
  const facilitator = fakeFacilitator();
  const cheapDefault = { payToSolana: SOLANA_PAY_TO, payToBase: BASE_PAY_TO, surplusKey: "inf_test",
    surplusUrl: "https://surplus.test", model: "cheap-model" };
  const dearDefault = { ...cheapDefault, model: "pro-model" };
  const app1 = createApp(cheapDefault, { facilitatorClient: facilitator, upstream: async () => ({ content: "ok", finishReason: "stop", usage: {} }),
    log: () => {}, catalogue: fakeCatalogue });
  const l1 = await new Promise((resolve) => { const l = app1.listen(0, "127.0.0.1", () => resolve(l)); }); t.after(() => l1.close());
  const size = { chars: 26, maxTokens: 300, messageCount: 1, charsPerToken: 1 };
  const unpaid1 = await post(`http://127.0.0.1:${l1.address().port}`, QUESTION);
  const required1 = decode(unpaid1.headers.get("payment-required"));
  assert.equal(required1.accepts.find((a) => a.network === BASE && a.scheme === "upto").amount, ceilingAtomic(CHEAP, size, TERMS));
  assert.ok(BigInt(required1.accepts.find((a) => a.network === BASE && a.scheme === "upto").amount) < 3000n, "honestly below the standing ceiling");

  const app2 = createApp(dearDefault, { facilitatorClient: facilitator, upstream: async () => ({ content: "ok", finishReason: "stop", usage: {} }),
    log: () => {}, catalogue: fakeCatalogue });
  const l2 = await new Promise((resolve) => { const l = app2.listen(0, "127.0.0.1", () => resolve(l)); }); t.after(() => l2.close());
  const unpaid2 = await post(`http://127.0.0.1:${l2.address().port}`, QUESTION);
  const required2 = decode(unpaid2.headers.get("payment-required"));
  assert.equal(required2.accepts.find((a) => a.network === BASE && a.scheme === "upto").amount, "3000", "clamped, not the far higher natural ceiling");
});

test("the paid call carries X-Min-Discount from the share guard fixed at quote time; none without a guard share", async (t) => {
  const guardedCatalogue = { ...fakeCatalogue, guardShare: (id) => (id === "pro-model" ? 0.4 : null) };
  const facilitator = fakeFacilitator();
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, headers: init.headers });
    return new Response(JSON.stringify({ choices: [{ message: { content: "ok" }, finish_reason: "stop" } ], usage: { buyer_cost_micro: 10 } }), { status: 200 });
  };
  const app = createApp(CONFIG, { facilitatorClient: facilitator, catalogue: guardedCatalogue, fetchImpl, upstream: askSurplus, log: () => {} });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  const url = `http://127.0.0.1:${listener.address().port}`;
  // pro-model: a 0.4 share, plus the default 1pp buffer, rounds to 0.41 -> X-Min-Discount 59.
  const requiredPro = decode((await post(url, { ...QUESTION, model: "pro-model" })).headers.get("payment-required"));
  const paidPro = await post(url, { ...QUESTION, model: "pro-model" }, { "PAYMENT-SIGNATURE": baseSignature(requiredPro) });
  assert.equal(paidPro.status, 200);
  assert.equal(calls.at(-1).headers["X-Min-Discount"], "59");
  // cheap-model: no guard share known (book not read) -> list price, no header at all.
  const requiredCheap = decode((await post(url, { ...QUESTION, model: "cheap-model" })).headers.get("payment-required"));
  const paidCheap = await post(url, { ...QUESTION, model: "cheap-model" }, { "PAYMENT-SIGNATURE": baseSignature(requiredCheap) });
  assert.equal(paidCheap.status, 200);
  assert.equal(calls.at(-1).headers["X-Min-Discount"], undefined);
});

test("every route offers all `upto` schemes before any `exact` scheme", async (t) => {
  const { url } = await serve(t);
  const probe = async (path, body) => {
    const response = await fetch(`${url}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    assert.equal(response.status, 402, path);
    return decode(response.headers.get("payment-required")).accepts.map((a) => a.scheme);
  };
  assert.deepEqual(await probe(ROUTE, {}), ["upto", "upto", "exact", "exact"], ROUTE);
  assert.deepEqual(await probe(METERED_ROUTE, { messages: [{ role: "user", content: "Hi" }] }), ["upto", "exact"], METERED_ROUTE);
  for (const route of Object.keys(FORMAT_ROUTES)) {
    assert.deepEqual(await probe(route, {}), ["upto", "upto", "exact", "exact"], route);
  }
});

test("a model that is not on the list is refused before any payment is asked for", async (t) => {
  const { url, asked } = await serve(t);
  const response = await post(url, { ...QUESTION, model: "no-such-model" });
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("payment-required"), null);
  assert.equal((await post(url, { ...QUESTION, model: 7 })).status, 400);
  assert.equal(asked.length, 0);
});

test("a paid question on another model is bought from that model and charged its actual cost under its own ceiling", async (t) => {
  const seen = [];
  // Comfortably under pro-model's ~71629 Base ceiling for this question.
  const { url, facilitator } = await serve(t, { upstream: async (_c, _m, _t, _f, model) => {
    seen.push(model);
    return { content: "ok", finishReason: "stop", usage: { buyer_cost_micro: 50_000 } };
  } });
  const unpaid = await post(url, { ...QUESTION, model: "pro-model" });
  const required = decode(unpaid.headers.get("payment-required"));
  const paid = await post(url, { ...QUESTION, model: "pro-model" }, { "PAYMENT-SIGNATURE": baseSignature(required) });
  assert.equal(paid.status, 200);
  assert.deepEqual(seen, ["pro-model"]);
  const body = await paid.json();
  assert.equal(body.billing.totalAtomic, String(50_000 + 15_000 + 1_000));
  assert.deepEqual(facilitator.calls.settle, [body.billing.totalAtomic]);
});

test("an answer dearer than the ceiling of its own model is still not charged", async (t) => {
  const { url, facilitator } = await serve(t, { upstream: async () => ({ content: "ok", usage: { buyer_cost_micro: 10_000_000 } }) });
  const required = decode((await post(url, { ...QUESTION, model: "pro-model" })).headers.get("payment-required"));
  const paid = await post(url, { ...QUESTION, model: "pro-model" }, { "PAYMENT-SIGNATURE": baseSignature(required) });
  assert.equal(paid.status, 502);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("the model list is free and carries Surplus's prices", async (t) => {
  const { url } = await serve(t);
  const list = await (await fetch(`${url}/v1/models`)).json();
  assert.equal(list.default, "deepseek-v4.1-flash");
  assert.deepEqual(list.data.map((m) => m.id), ["singit-ask", "cheap-model", "pro-model"]);
  assert.equal(list.data[2].usdPerMillionTokens.output, 180);
});

test("ModelCatalogue reads text models with prices, caches them, and survives a failed refresh", async () => {
  let calls = 0, fail = false, clock = 0;
  const entry = (id, modality = "text->text", params = ["max_tokens"]) => ({ id, name: id, provider: "P", context_length: 1000,
    architecture: { modality }, supported_parameters: params, pricing: { prompt: "0.000001", completion: "0.000002" } });
  const fetchImpl = async () => { calls++; if (fail) throw new Error("down");
    return new Response(JSON.stringify({ data: [entry("a"), entry("img", "text->image"), entry("nocap", "text->text", []),
      { ...entry("free"), pricing: { prompt: "0", completion: "0" } }, { ...entry("bad"), pricing: {} },
      { ...entry("sees"), architecture: { modality: "text+image+file->text", input_modalities: ["text", "image", "file"], output_modalities: ["text"] } },
      { ...entry("draws"), architecture: { modality: "text->image", input_modalities: ["text"], output_modalities: ["image"] } },
      { ...entry("listens"), architecture: { modality: "audio->text", input_modalities: ["audio"], output_modalities: ["text"] } }] })); };
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => clock });
  assert.deepEqual((await catalogue.list()).map((m) => m.id).sort(), ["a", "free", "sees"], "multimodal in, text out, is offered; image or audio-only is not");
  assert.equal((await catalogue.resolve("a")).completionUsdPerToken, 0.000002);
  await assert.rejects(catalogue.resolve("img"), UnknownModel);
  assert.equal(calls, 1);
  fail = true; clock = 7 * 3600_000;
  assert.equal((await catalogue.resolve("a")).id, "a", "a failed refresh keeps the last list");
});

test("ceilingAtomic never drops below MIN_CEILING_ATOMIC", () => {
  assert.equal(ceilingAtomic({ promptUsdPerToken: 0, completionUsdPerToken: 0 }, { chars: 10, maxTokens: 100, messageCount: 1 }, TERMS), MIN_CEILING_ATOMIC);
  assert.equal(billUsage({ buyer_cost_micro: 100 }, { ...TERMS, maxChargeAtomic: "50000" }).totalAtomic, "1130");
});

test("ceilingAtomic: input is ~4 chars/token plus 4 tokens of overhead per message, output is the whole max_tokens budget", () => {
  const model = { promptUsdPerToken: 4e-6, completionUsdPerToken: 20e-6 };
  // (ceil(64/4) + 4*1) * 4 + 500 * 20 = 20*4 + 10000 = 10080 micro of worst-case tokens; + 30% markup (3024,
  // rounded up) + the 1000 settlement fee = 14104.
  assert.equal(ceilingAtomic(model, { chars: 64, maxTokens: 500, messageCount: 1 }, TERMS), "14104");
  // More messages add more per-message overhead, all else equal.
  const withExtraMessage = ceilingAtomic(model, { chars: 64, maxTokens: 500, messageCount: 2 }, TERMS);
  assert.ok(BigInt(withExtraMessage) > 14104n);
});

test("ceilingAtomic: charsPerToken: 1 bounds a dense (CJK/emoji) prompt the same way `upto` authorizes it; the chars/4 default is what `exact` charges flat", () => {
  const model = { promptUsdPerToken: 4e-6, completionUsdPerToken: 20e-6 };
  const base = { chars: 4000, maxTokens: 100, messageCount: 1 };
  // upto: ceil(4000/1) + 4*1 = 4004 input tokens; (4004*4 + 100*20) = 18016 worst-case micro;
  // + 30% markup (ceil(5404.8) = 5405) + the 1000 settlement fee = 24421.
  assert.equal(ceilingAtomic(model, { ...base, charsPerToken: 1 }, TERMS), "24421");
  // exact (the default): ceil(4000/4) + 4*1 = 1004 input tokens; (1004*4 + 100*20) = 6016 worst-case micro;
  // + 30% markup (ceil(1804.8) = 1805) + the 1000 settlement fee = 8821. A CJK/emoji prompt tokenizes to more
  // than chars/4 estimates, but `exact` absorbs that overrun via `allowOverrun` instead of refusing the answer.
  assert.equal(ceilingAtomic(model, base, TERMS), "8821");
});

test("ceilingAtomic: share prices a smaller fraction of list, the worst case Surplus is bound to by X-Min-Discount", () => {
  const model = { promptUsdPerToken: 4e-6, completionUsdPerToken: 20e-6 };
  const atList = ceilingAtomic(model, { chars: 64, maxTokens: 500, messageCount: 1 }, TERMS, 1);
  const atHalf = ceilingAtomic(model, { chars: 64, maxTokens: 500, messageCount: 1 }, TERMS, 0.5);
  assert.equal(atList, "14104");
  // Half the list price: 10080 * 0.5 = 5040 worst-case micro, + markup (1512) + fee (1000) = 7552; below
  // MIN_CEILING_ATOMIC it would floor there instead, but 7552 is well above it.
  assert.equal(atHalf, "7552");
});

test("priceGuard: the ceiling share is the cheapest qualifying share rounded up a whole percent plus the buffer, capped at list price", () => {
  // 25% off list (share 0.75) plus a 1pp buffer -> share 0.76, so Surplus must beat a 24% discount.
  assert.deepEqual(priceGuard({ guardShare: () => 0.75 }, "m", 1), { share: 0.76, minDiscount: 24 });
  // A share that already rounds to a whole percent still gets the buffer added.
  assert.deepEqual(priceGuard({ guardShare: () => 0.6 }, "m", 2), { share: 0.62, minDiscount: 38 });
  // The buffer never pushes the share past list price (1): no header is sent, since there is nothing to bound.
  assert.deepEqual(priceGuard({ guardShare: () => 0.995 }, "m", 2), { share: 1, minDiscount: null });
  // No guard share known (book not read, or no guardShare at all): list price, no header.
  assert.deepEqual(priceGuard({ guardShare: () => null }, "m", 1), { share: 1, minDiscount: null });
  assert.deepEqual(priceGuard(null, "m", 1), { share: 1, minDiscount: null });
});

test("ModelCatalogue.guardShare: the cheapest qualifying offer's share of list, from the same order books as canAnswer", async () => {
  const offer = (o) => ({ trusted: true, healthy: true, available: true, provider_id: "openai", discount_pct: 50, ...o });
  const books = {
    cheapest: [offer({ discount_pct: 80 }), offer({ discount_pct: 20 })],  // 80% off (share 0.2) is the cheapest qualifying
    disqualified: [offer({ discount_pct: 90, trusted: false }), offer({ discount_pct: 10 })],  // the 90%-off offer does not qualify
    none: [offer({ discount_pct: 90, healthy: false })],
  };
  const fetchImpl = async (url) => {
    url = String(url);
    if (url.endsWith("/v1/models")) return new Response(JSON.stringify({ data: Object.keys(books).map((id) => ({
      id, name: id, architecture: { modality: "text->text" }, supported_parameters: ["max_tokens"], pricing: { prompt: "0", completion: "0" } })) }));
    const id = decodeURIComponent(url.split("/api/markets/")[1]);
    return new Response(JSON.stringify({ data: { offers: books[id] } }));
  };
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => 0 });
  assert.equal(catalogue.guardShare("cheapest"), null, "unread until the books are loaded");
  await catalogue.loadServed(0);
  assert.equal(catalogue.guardShare("cheapest"), 0.2);
  assert.equal(catalogue.guardShare("disqualified"), 0.9);
  assert.equal(catalogue.guardShare("none"), null);
});

test("the model list carries the price an answer is expected to cost, from trusted sellers until answers teach it", async (t) => {
  const { url } = await serve(t);
  const list = await (await fetch(`${url}/v1/models`)).json();
  const pro = list.data.find((m) => m.id === "pro-model");
  assert.deepEqual(pro.expectedUsdPerMillionTokens, { input: 30 * 0.25, output: 180 * 0.25, basis: "trusted" });
  assert.equal(list.data.find((m) => m.id === "cheap-model").expectedUsdPerMillionTokens, undefined);
  assert.equal(list.data.some((m) => "marketUsdPerMillionTokens" in m), false, "the unreachable best offer is never shown");
});

test("every answer that comes back teaches the price of its model", async (t) => {
  const { url } = await serve(t, { upstream: async () => ({ content: "ok", finishReason: "stop",
    usage: { prompt_tokens: 100, completion_tokens: 1000, buyer_cost_micro: 30_000 } }) });
  const required = decode((await post(url, { ...QUESTION, model: "pro-model" })).headers.get("payment-required"));
  assert.equal((await post(url, { ...QUESTION, model: "pro-model" }, { "PAYMENT-SIGNATURE": baseSignature(required) })).status, 200);
  await new Promise((r) => setTimeout(r, 20));
  const pro = (await (await fetch(`${url}/v1/models`)).json()).data.find((m) => m.id === "pro-model");
  // At list price those tokens cost 100*30 + 1000*180 = 183,000 micro-USD; the answer cost 30,000: 16.39%.
  assert.equal(pro.expectedUsdPerMillionTokens.basis, "observed");
  assert.equal(pro.expectedUsdPerMillionTokens.output, 180 * 0.1639);
});

test("ObservedPrices keeps the median share of list price, skips answers too small to measure, and persists", async () => {
  const fs = await import("node:fs"), os = await import("node:os"), path = await import("node:path");
  const { ObservedPrices } = await import("../src/observed.mjs");
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "obs-")), "prices.json");
  const list = { promptUsdPerToken: 1e-6, completionUsdPerToken: 1e-6 };  // 1 micro-USD a token
  const prices = new ObservedPrices({ file });
  assert.equal(prices.share("m"), null);
  prices.record("m", { prompt_tokens: 50, completion_tokens: 30, buyer_cost_micro: 1 }, list);  // 80 micro at list: too small
  assert.equal(prices.share("m"), null);
  for (const cost of [100, 300, 200]) prices.record("m", { prompt_tokens: 500, completion_tokens: 500, buyer_cost_micro: cost }, list);
  assert.equal(prices.share("m"), 0.2);
  assert.equal(new ObservedPrices({ file }).share("m"), 0.2, "kept across a restart");
  assert.equal((fs.statSync(file).mode & 0o777).toString(8), "600");
  assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(file, "utf8"))), ["m"], "only model ids and shares are stored");
});

test("ModelCatalogue.market reads the trusted sellers' share of list price, caches it, and keeps the last on failure", async () => {
  let calls = 0, fail = false, clock = 0;
  const fetchImpl = async (url) => { calls++; assert.ok(String(url).endsWith("/api/marketplace")); if (fail) throw new Error("down");
    return new Response(JSON.stringify({ data: { markets: [
      { model: "gpt-6-astra", best_input_per_1m: 1, best_output_per_1m: 1, best_trusted_offer_discount_pct: 75.5 },
      { model: "free", best_trusted_offer_discount_pct: 100 },
      { model: "none", best_trusted_offer_discount_pct: null }, { model: "odd", best_trusted_offer_discount_pct: 120 }] } })); };
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => clock });
  const prices = await catalogue.market();
  assert.deepEqual(prices.get("gpt-6-astra"), { trustedShare: 0.245 });
  assert.deepEqual(prices.get("free"), { trustedShare: 0 });
  assert.equal(prices.has("none") || prices.has("odd"), false);
  await catalogue.market();
  assert.equal(calls, 1);
  fail = true; clock = 11 * 60_000;
  assert.equal((await catalogue.market()).get("gpt-6-astra").trustedShare, 0.245, "a failed refresh keeps the last");
  assert.equal(calls, 2);
});

test("ModelCatalogue serves what it has at once, refreshes once in the background, and warm() reads both lists", async () => {
  let clock = 0, release, calls = { models: 0, market: 0 };
  const gate = new Promise((resolve) => { release = resolve; });
  const fetchImpl = async (url) => {
    if (String(url).includes("/api/markets/")) return new Response(JSON.stringify({ data: { offers: [] } }));
    const kind = String(url).endsWith("/api/marketplace") ? "market" : "models";
    calls[kind]++;
    if (calls[kind] > 1) await gate;  // every refresh after the first is slow
    return new Response(JSON.stringify(kind === "market"
      ? { data: { markets: [{ model: "a", best_trusted_offer_discount_pct: 0 }] } }
      : { data: [{ id: "a", name: "A", provider: "P", context_length: 1, architecture: { modality: "text->text" },
          supported_parameters: ["max_tokens"], pricing: { prompt: "0.000001", completion: "0.000002" } }] }));
  };
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => clock });
  await catalogue.warm(3_600_000);
  clearInterval(catalogue.timer);
  assert.deepEqual([calls.models, calls.market], [1, 1]);
  clock = 7 * 3600_000;
  const started = Date.now();
  assert.equal((await catalogue.list())[0].id, "a");           // the old list, without waiting for the slow refresh
  assert.equal((await catalogue.market()).get("a").trustedShare, 1);  // likewise the old prices
  await catalogue.list(); await catalogue.market();
  assert.ok(Date.now() - started < 1000);
  assert.deepEqual([calls.models, calls.market], [2, 2], "one refresh each, however many reads");
  release();
});

test("capabilities name what a model can do in the picker's category keys", async () => {
  const { capabilities } = await import("../src/catalogue.mjs");
  const entry = (id, features = []) => ({ id, supported_features: features });
  assert.deepEqual(capabilities(entry("claude-opus-5.5", ["vision", "reasoning", "tools"]), ["text", "image", "file"]), ["vision", "reasoning"]);
  assert.deepEqual(capabilities(entry("qwen3-coder"), ["text"]), ["code"]);
  assert.deepEqual(capabilities(entry("gpt-5.3-codex"), ["text"]), ["code"]);
  assert.deepEqual(capabilities(entry("kimi-k2.7-code:web"), ["text"]), ["code"]);
  assert.deepEqual(capabilities(entry("barcode-reader"), ["text"]), [], "a name merely containing 'code' is not a coder");
  assert.deepEqual(capabilities(entry("e2ee-glm-4.7", ["reasoning"]), ["text"]), ["reasoning", "private"]);
  assert.deepEqual(capabilities(entry("omni", ["audio"]), ["text", "video", "audio"]), ["video", "audio"]);
});

test("a model no trusted seller can answer now is not listed, and is refused before any payment", async (t) => {
  const { url, asked } = await serve(t);
  const ids = (await (await fetch(`${url}/v1/models`)).json()).data.map((m) => m.id);
  assert.ok(!ids.includes("idle-model"));
  const refused = await post(url, { ...QUESTION, model: "idle-model" });
  assert.equal(refused.status, 400);
  assert.equal(refused.headers.get("payment-required"), null);
  assert.match((await refused.json()).error.message, /No trusted seller/);
  assert.equal(asked.length, 0);
});

test("ModelCatalogue.canAnswer reads every order book: trusted, healthy, available and not blocked", async () => {
  const offer = (o) => ({ trusted: true, healthy: true, available: true, provider_id: "openai", ...o });
  const books = {
    good: [offer({})], untrusted: [offer({ trusted: false })], sick: [offer({ healthy: false })],
    full: [offer({ available: false })], blocked: [offer({ provider_id: "Mordiem" })], mixed: [offer({ trusted: false }), offer({})],
  };
  let unreadable = false;
  const fetchImpl = async (url) => {
    url = String(url);
    if (url.endsWith("/v1/models")) return new Response(JSON.stringify({ data: [...Object.keys(books), "broken"].map((id) => ({
      id, name: id, architecture: { modality: "text->text" }, supported_parameters: ["max_tokens"], pricing: { prompt: "0", completion: "0" } })) }));
    const id = decodeURIComponent(url.split("/api/markets/")[1]);
    if (id === "broken" || unreadable) return new Response("down", { status: 503 });
    return new Response(JSON.stringify({ data: { offers: books[id] } }));
  };
  let clock = 0;
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => clock, blocked: ["mordiem"] });
  assert.equal(await catalogue.canAnswer("good"), true, "offered until the books have been read");
  await catalogue.loadServed(0);
  const answer = async (id) => catalogue.canAnswer(id);
  assert.deepEqual(await Promise.all(Object.keys(books).map(answer)), [true, false, false, false, false, true]);
  assert.equal(await answer("broken"), true, "a book that cannot be read never hides a model");
  unreadable = true; clock = 11 * 60_000;
  await catalogue.loadServed(0);
  assert.equal(await answer("good"), true, "an unreadable refresh keeps the last answer");
  assert.equal(await answer("untrusted"), false);
});

test("ModelCatalogue throws away a pass of order books that would empty the picker", async () => {
  let sick = false, clock = 0;
  const ids = Array.from({ length: 8 }, (_, i) => `m${i}`);
  const fetchImpl = async (url) => {
    url = String(url);
    if (url.endsWith("/v1/models")) return new Response(JSON.stringify({ data: ids.map((id) => ({ id, name: id,
      architecture: { modality: "text->text" }, supported_parameters: ["max_tokens"], pricing: { prompt: "0", completion: "0" } })) }));
    // Under load Surplus has answered 200 with every offer unhealthy.
    return new Response(JSON.stringify({ data: { offers: [{ trusted: true, healthy: !sick, available: true, provider_id: "openai" }] } }));
  };
  const catalogue = new ModelCatalogue({ url: "https://x", fetchImpl, now: () => clock });
  sick = true;
  await catalogue.loadServed(0);
  assert.equal(catalogue.served, null, "a first pass offering under a quarter is not taken");
  assert.equal(await catalogue.canAnswer("m0"), true);
  sick = false;
  await catalogue.loadServed(0);
  assert.equal(catalogue.served.size, 8);
  sick = true; clock = 11 * 60_000;
  await catalogue.loadServed(0);
  assert.equal(catalogue.served.size, 8, "a pass offering under half of the last is thrown away");
});
