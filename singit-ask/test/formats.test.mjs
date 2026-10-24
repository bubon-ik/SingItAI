// Anthropic's Messages and OpenAI's Responses over x402: the ceiling follows the model and the request, the charge is
// what the answer cost + 30% + the fee, nothing without an answer, no seller shown.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BASE, SOLANA, createApp } from "../src/app.mjs";
import { UnknownModel } from "../src/catalogue.mjs";
import { FORMAT_LIMITS, readFormatRequest, scrub, surplusModel } from "../src/formats.mjs";

const CONFIG = { payToSolana: "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
  payToBase: "0x1111111111111111111111111111111111111111", surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash" };
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64");
const decode = (header) => JSON.parse(Buffer.from(header, "base64").toString("utf8"));
const SONNET = { id: "claude-sonnet-5.5", name: "Sonnet", provider: "anthropic", contextLength: 1e6, promptUsdPerToken: 3e-6, completionUsdPerToken: 15e-6 };
const GPT = { id: "gpt-5.5", name: "GPT", provider: "openai", contextLength: 1e6, promptUsdPerToken: 1.25e-6, completionUsdPerToken: 10e-6 };
const catalogue = { async resolve(id) { const m = [SONNET, GPT].find((x) => x.id === id); if (!m) throw new UnknownModel("That model is not available."); return m; },
  async canAnswer() { return true; }, async list() { return [SONNET, GPT]; }, async imageModels() { return new Map(); },
  async resolveImage() { throw new UnknownModel("no"); } };

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
      ], extensions: ["bazaar", "eip2612GasSponsoring"], signers: {} };
    },
    async verify() { return { isValid: true, payer: "0x2222222222222222222222222222222222222222" }; },
    async settle(_payload, requirements) {
      calls.settle.push([requirements.network, requirements.amount]);
      return { success: true, transaction: "0x" + "a".repeat(64), network: requirements.network };
    } };
}

function fakeSurplus({ status = 200, cost = 2000, inHeader = false } = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    const usage = { input_tokens: 12, output_tokens: 30, ...(inHeader ? {} : { buyer_cost_micro: cost, is_byok: true }) };
    const body = url.endsWith("/messages")
      ? { id: "msg_1", type: "message", role: "assistant", provider: "venice", content: [{ type: "text", text: "x402 is a payment protocol." }], usage }
      : { id: "resp_1", object: "response", output: [{ type: "message", content: [{ type: "output_text", text: "x402 pays per request." }] }], usage };
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json",
      ...(inHeader ? { "x-si-buyer-cost-micro": String(cost) } : {}) } });
  };
  return { calls, fetchImpl };
}

async function serve(t, { surplus = fakeSurplus() } = {}) {
  const facilitator = fakeFacilitator();
  const app = createApp(CONFIG, { facilitatorClient: facilitator, catalogue, fetchImpl: surplus.fetchImpl, log: () => {},
    upstream: async () => { throw new Error("the chat route is not asked"); } });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, surplus };
}

const post = (url, body, headers = {}) => fetch(url, { method: "POST",
  headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
const pay = (required, network, scheme = "upto") => ({ "PAYMENT-SIGNATURE": encode({ x402Version: 2, resource: required.resource,
  accepted: required.accepts.find((a) => a.network === network && a.scheme === scheme), payload: { signature: "0xsigned" },
  extensions: required.extensions }) });
const MESSAGE = { model: "claude-sonnet-5-5-20260101", max_tokens: 300, system: "Be brief.",
  messages: [{ role: "user", content: "What is x402?" }] };
const RESPONSE = { model: "gpt-5.5", input: "What is x402?" };

test("Anthropic's Messages: offered with a ceiling, charged what it cost + 30% + the fee, no seller shown", async (t) => {
  const { url, surplus, facilitator } = await serve(t);
  const unpaid = await post(`${url}/v1/messages`, MESSAGE);
  assert.equal(unpaid.status, 402);
  const required = decode(unpaid.headers.get("payment-required"));
  // SI-61: `exact` on Base leads; `upto` is what most buyers sign, `exact` on Solana alongside it at the same
  // ceiling for buyers that sign only `exact`.
  assert.deepEqual(required.accepts.map((a) => a.scheme), ["exact", "upto", "upto", "exact"]);
  assert.ok(required.extensions?.bazaar);
  const paid = await post(`${url}/v1/messages`, MESSAGE, pay(required, BASE));
  assert.equal(paid.status, 200);
  const body = await paid.json();
  assert.equal(body.content[0].text, "x402 is a payment protocol.");
  assert.equal(body.provider, undefined);
  assert.deepEqual(body.usage, { input_tokens: 12, output_tokens: 30 });
  assert.equal(body.billing.totalAtomic, "3600");  // 2000 + 600 markup + 1000 fee
  assert.deepEqual(facilitator.calls.settle, [[BASE, "3600"]]);
  const sent = surplus.calls[0];
  assert.equal(sent.url, "https://surplus.test/anthropic/v1/messages");
  assert.deepEqual([sent.headers.Authorization, sent.headers["anthropic-version"], sent.body.model],
    ["Bearer inf_test", "2023-06-01", "claude-sonnet-5.5"]);
});

test("OpenAI's Responses: a length is set when none is asked, and the cost may come in the header", async (t) => {
  const { url, surplus, facilitator } = await serve(t, { surplus: fakeSurplus({ inHeader: true, cost: 500 }) });
  const required = decode((await post(`${url}/v1/responses`, RESPONSE)).headers.get("payment-required"));
  const paid = await post(`${url}/v1/responses`, RESPONSE, pay(required, SOLANA));
  assert.equal(paid.status, 200);
  assert.equal((await paid.json()).billing.totalAtomic, "2650");  // 500 + 150 + 2000 Solana fee
  // Solana's upto escrows the ceiling, then settles what was used and refunds the rest.
  assert.deepEqual(facilitator.calls.settle.at(-1), [SOLANA, "2650"]);
  assert.ok(Number(facilitator.calls.settle[0][1]) >= 2650);
  assert.equal(surplus.calls[0].url, "https://surplus.test/v1/responses");
  assert.equal(surplus.calls[0].body.max_output_tokens, FORMAT_LIMITS.defaultTokens);
});

test("Anthropic's Messages: an exact payment settles the full ceiling, not metered usage", async (t) => {
  // Surplus reports 2000 micro-USD of usage, well under the ceiling; exact still settles the whole ceiling.
  const { url, facilitator } = await serve(t);
  const required = decode((await post(`${url}/v1/messages`, MESSAGE)).headers.get("payment-required"));
  const ceiling = required.accepts.find((a) => a.network === BASE && a.scheme === "exact").amount;
  const paid = await post(`${url}/v1/messages`, MESSAGE, pay(required, BASE, "exact"));
  assert.equal(paid.status, 200);
  const body = await paid.json();
  assert.equal(body.billing.totalAtomic, ceiling);
  assert.equal(body.billing.mode, "exact_ceiling");
  assert.deepEqual(facilitator.calls.settle, [[BASE, ceiling]]);
});

test("Anthropic's Messages: a failed upstream request paid with exact is not settled", async (t) => {
  const { url, facilitator } = await serve(t, { surplus: fakeSurplus({ status: 500 }) });
  const required = decode((await post(`${url}/v1/messages`, MESSAGE)).headers.get("payment-required"));
  const paid = await post(`${url}/v1/messages`, MESSAGE, pay(required, BASE, "exact"));
  assert.equal(paid.status, 502);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("no billable answer, no charge", async (t) => {
  for (const surplus of [fakeSurplus({ status: 500 }), fakeSurplus({ cost: "unknown" })]) {
    const { url, facilitator } = await serve(t, { surplus });
    const required = decode((await post(`${url}/v1/messages`, MESSAGE)).headers.get("payment-required"));
    const paid = await post(`${url}/v1/messages`, MESSAGE, pay(required, BASE));
    assert.equal(paid.status, 502);
    assert.equal((await paid.json()).type, "error", "Anthropic's error shape");
    assert.deepEqual(facilitator.calls.settle, []);
  }
});

test("what is not sold is refused before any payment is asked", async (t) => {
  const { url, surplus } = await serve(t);
  for (const [path, body, status] of [["/v1/messages", { ...MESSAGE, stream: true }, 400],
    ["/v1/messages", { ...MESSAGE, max_tokens: undefined }, 400], ["/v1/messages", { ...MESSAGE, model: "nope" }, 404],
    ["/v1/messages", { ...MESSAGE, messages: [] }, 400], ["/v1/responses", { model: "gpt-5.5" }, 400],
    ["/v1/responses", { ...RESPONSE, input: "x".repeat(FORMAT_LIMITS.chars + 1) }, 400]]) {
    const refused = await post(`${url}${path}`, body);
    assert.equal(refused.status, status, `${path} ${JSON.stringify(body).slice(0, 60)}`);
    assert.equal(refused.headers.get("payment-required"), null);
  }
  assert.deepEqual(surplus.calls, []);
});

test("x402scan's empty, unpaid probe gets the standing offer; a payment header does not excuse an invalid body", async (t) => {
  const { url, surplus } = await serve(t);
  for (const route of ["/v1/messages", "/v1/responses"]) {
    let required;
    for (const init of [{ method: "POST" }, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }]) {
      const probe = await fetch(`${url}${route}`, init);
      assert.equal(probe.status, 402, route);
      required = decode(probe.headers.get("payment-required"));
      // Same offer order as a real question (SI-61): Base exact first, then Solana upto, Base upto, Solana exact.
      assert.deepEqual(required.accepts.map((a) => a.scheme), ["exact", "upto", "upto", "exact"], route);
      assert.ok(required.extensions?.bazaar?.info, `${route} needs the input schema for x402scan to index it`);
      assert.ok(required.extensions?.bazaar?.schema, route);
    }
    const paidEmpty = await fetch(`${url}${route}`, { method: "POST", headers: pay(required, BASE) });
    assert.equal(paidEmpty.status, 400, `${route}: a payment header with no body is still refused, not waved through as a probe`);
  }
  assert.deepEqual(surplus.calls, []);
});

test("names, sizes and scrubbing", () => {
  assert.equal(surplusModel("claude-opus-5-5"), "claude-opus-5.5");
  assert.equal(surplusModel("claude-haiku-4-5-20251001"), "claude-haiku-4.5");
  assert.equal(surplusModel("claude-fable-5-1"), "claude-fable-5.1");
  assert.equal(surplusModel("gpt-5.5"), "gpt-5.5");
  const read = readFormatRequest("/v1/messages", { ...MESSAGE, tools: [{ name: "Bash" }] });
  assert.equal(read.maxTokens, 300);
  assert.ok(read.chars > 20);
  assert.deepEqual(scrub({ provider: "x", usage: { input_tokens: 1, cost: 2, buyer_cost_micro: 3 } }), { usage: { input_tokens: 1 } });
  assert.deepEqual(scrub({ cost: { usd: 0, diem: 1e-5 }, usage: { input_tokens: 1 } }), { usage: { input_tokens: 1 } });
});
