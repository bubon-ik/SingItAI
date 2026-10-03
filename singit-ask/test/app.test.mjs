// SingIt Ask against a fake CDP facilitator and a fake model: who is charged, when, and for what.

import { test } from "node:test";
import assert from "node:assert/strict";
import { request as httpRequest } from "node:http";
import { BASE, SOLANA, ROUTE, LIMITS, RequestError, UpstreamError, readRequest, askSurplus, createApp } from "../src/app.mjs";
import { SOLANA_TERMS, TERMS } from "../src/pricing.mjs";

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

async function serve(t, { upstream } = {}) {
  const facilitator = fakeFacilitator();
  const asked = [];
  const answer = upstream ?? (async (_config, messages, maxTokens) => {
    asked.push({ messages, maxTokens });
    return { content: "Try Caffè Propaganda.", finishReason: "stop", usage: { total_tokens: 42, buyer_cost_micro: 100 } };
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

// A Base payment the fake facilitator accepts, echoing the merchant's extensions as a real buyer does.
function baseSignature(required) {
  const accepted = required.accepts.find((a) => a.network === BASE);
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
  const solana = required.accepts.find((a) => a.network === SOLANA);
  const base = required.accepts.find((a) => a.network === BASE);
  assert.deepEqual(required.accepts.map((a) => a.scheme), ["upto", "upto"]);
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
