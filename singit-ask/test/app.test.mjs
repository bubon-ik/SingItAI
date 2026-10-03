// SingIt Ask against a fake facilitator and a fake model: who is charged, when, and for what.

import { test } from "node:test";
import assert from "node:assert/strict";
import { request as httpRequest } from "node:http";
import { BASE, SOLANA, ROUTE, LIMITS, RequestError, UpstreamError, readRequest, askSurplus, createApp } from "../src/app.mjs";

const SOLANA_PAY_TO = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin";
const BASE_PAY_TO = "0x1111111111111111111111111111111111111111";
const CONFIG = {
  payToSolana: SOLANA_PAY_TO, payToBase: BASE_PAY_TO, surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash", priceSolana: "$0.003", priceBase: "$0.003",
};

function fakeFacilitator() {
  const calls = { verify: 0, settle: 0 };
  return {
    calls,
    async getSupported() {
      return {
        kinds: [
          { x402Version: 2, scheme: "exact", network: BASE },
          { x402Version: 2, scheme: "exact", network: SOLANA, extra: { feePayer: "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4" } },
        ],
        extensions: ["bazaar"],
        signers: {},
      };
    },
    async verify(payload) {
      calls.verify += 1;
      return { isValid: true, payer: payload.payload?.from ?? "payer" };
    },
    async settle(_payload, requirements) {
      calls.settle += 1;
      return { success: true, transaction: "tx-1", network: requirements.network, payer: "payer" };
    },
  };
}

async function serve(t, { upstream } = {}) {
  const facilitator = fakeFacilitator();
  const asked = [];
  const answer = upstream ?? (async (_config, messages, maxTokens) => {
    asked.push({ messages, maxTokens });
    return { content: "Try Caffè Propaganda.", finishReason: "stop", usage: { total_tokens: 42 } };
  });
  const app = createApp(CONFIG, { facilitatorClient: facilitator, upstream: answer, log: () => {} });
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

// A payment that the fake facilitator accepts, for whichever of the offered requirements is given.
function signature(required, accepted) {
  const payload = {
    x402Version: 2,
    resource: required.resource,
    accepted,
    payload: { from: "payer", signature: "0xsigned" },
    extensions: required.extensions,
  };
  return Buffer.from(JSON.stringify(payload)).toString("base64");
}

async function offer(url) {
  const unpaid = await post(url, QUESTION);
  assert.equal(unpaid.status, 402);
  return decode(unpaid.headers.get("payment-required"));
}

test("an unpaid question is asked to pay on Solana or Base, and the model is not asked", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const required = await offer(url);
  const solana = required.accepts.find((a) => a.network === SOLANA);
  const base = required.accepts.find((a) => a.network === BASE);
  assert.equal(solana.scheme, "exact");
  assert.equal(solana.amount, "3000");
  assert.equal(solana.payTo, SOLANA_PAY_TO);
  assert.ok(solana.extra?.feePayer, "Solana payers need the facilitator's fee payer");
  assert.equal(base.amount, "3000");
  assert.equal(base.payTo, BASE_PAY_TO);
  assert.ok(required.extensions?.bazaar, "listed for discovery");
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.settle, 0);
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
  assert.equal(facilitator.calls.settle, 0);
});

for (const network of [SOLANA, BASE]) {
  test(`a paid question on ${network} gets the answer and is charged once`, async (t) => {
    const { url, facilitator, asked } = await serve(t);
    const required = await offer(url);
    const accepted = required.accepts.find((a) => a.network === network);
    const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": signature(required, accepted) });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.choices[0].message.content, "Try Caffè Propaganda.");
    assert.equal(body.model, "singit-ask");
    assert.equal(asked.length, 1);
    assert.equal(asked[0].maxTokens, 300);
    assert.equal(facilitator.calls.settle, 1);
    assert.equal(decode(response.headers.get("payment-response")).network, network);
  });
}

test("a model that does not answer costs the payer nothing", async (t) => {
  const { url, facilitator } = await serve(t, { upstream: async () => { throw new UpstreamError("model answered HTTP 503"); } });
  const required = await offer(url);
  const response = await post(url, QUESTION, { "PAYMENT-SIGNATURE": signature(required, required.accepts[0]) });
  assert.equal(response.status, 502);
  assert.match((await response.json()).error.message, /not charged/);
  assert.equal(facilitator.calls.settle, 0);
});

test("a malformed question is refused without a charge", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const required = await offer(url);
  const response = await post(url, { messages: [{ role: "tool", content: "x" }] },
                              { "PAYMENT-SIGNATURE": signature(required, required.accepts[0]) });
  assert.equal(response.status, 400);
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.settle, 0);
});

test("health and the model list are free", async (t) => {
  const { url, facilitator } = await serve(t);
  const health = await fetch(`${url}/health`);
  assert.equal(health.status, 200);
  assert.equal((await health.json()).ok, true);
  const models = await fetch(`${url}/v1/models`);
  assert.equal(models.status, 200);
  assert.equal((await models.json()).data[0].id, "singit-ask");
  assert.equal(facilitator.calls.verify, 0);
});

test("the MetaMask test page is free and cannot expose local configuration", async (t) => {
  const { url, facilitator, asked } = await serve(t);
  const page = await fetch(`${url}/test/`);
  assert.equal(page.status, 200);
  assert.equal(page.headers.get("cache-control"), "no-store");
  assert.match(await page.text(), /MetaMask/);
  assert.equal((await fetch(`${url}/test/payment.mjs`)).status, 200);
  assert.equal((await fetch(`${url}/test/.env`)).status, 404);
  assert.equal((await fetch(`${url}/test/src/server.mjs`)).status, 404);
  assert.equal(asked.length, 0);
  assert.equal(facilitator.calls.verify, 0);
});

test("readRequest keeps the question within what one answer pays for", () => {
  assert.deepEqual(readRequest({ messages: [{ role: "user", content: "hi", name: "x" }] }),
                   { messages: [{ role: "user", content: "hi" }], maxTokens: LIMITS.defaultTokens });
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
