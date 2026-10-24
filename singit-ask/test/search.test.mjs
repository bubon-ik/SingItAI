// SingIt search: Exa's list price + 30% + the fee, Exa asked once with SingIt's key, nothing charged without an
// answer, nothing sold beyond what is listed.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BASE, SOLANA, createApp } from "../src/app.mjs";
import { exaPrice, searchPrice } from "../src/search.mjs";

const CONFIG = { payToSolana: "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
  payToBase: "0x1111111111111111111111111111111111111111", surplusKey: "inf_test", model: "deepseek-v4.1-flash" };
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64");
const decode = (header) => JSON.parse(Buffer.from(header, "base64").toString("utf8"));

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

// Exa with SingIt's key: answers, refuses or fails as the test says, and records what it was sent.
function fakeExa({ status = 200, cost = 0.007 } = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, key: init.headers["x-api-key"], body: JSON.parse(init.body) });
    if (status !== 200) return new Response('{"error":"no"}', { status });
    return new Response(JSON.stringify({ requestId: "r1", results: [{ title: "x402", url: "https://x402.org" }],
      costDollars: { total: cost } }), { status: 200, headers: { "content-type": "application/json" } });
  };
  return { calls, fetchImpl };
}

async function serve(t, { exa = fakeExa(), exaKey = "exa_test_key" } = {}) {
  const facilitator = fakeFacilitator();
  const logs = [];
  const app = createApp(CONFIG, { facilitatorClient: facilitator, catalogue: null, log: (line) => logs.push(line),
    upstream: async () => { throw new Error("no model is asked"); }, exaKey, exaFetch: exa.fetchImpl });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, exa, logs };
}

const post = (url, body, headers = {}) => fetch(url, { method: "POST",
  headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
const pay = (required, network) => ({ "PAYMENT-SIGNATURE": encode({ x402Version: 2, resource: required.resource,
  accepted: required.accepts.find((a) => a.network === network), payload: { signature: "0xsigned" },
  extensions: required.extensions }) });
const SEARCH = { query: "x402 facilitators", numResults: 5 };

test("a search is offered at Exa's list price + 30% + the network's fee, and listed for discovery", async (t) => {
  const { url, facilitator, exa } = await serve(t);
  const unpaid = await post(`${url}/v1/exa/search`, SEARCH);
  assert.equal(unpaid.status, 402);
  const required = decode(unpaid.headers.get("payment-required"));
  assert.deepEqual(required.accepts.map((a) => [a.scheme, a.network, a.amount, a.payTo]), [
    ["exact", BASE, "10100", CONFIG.payToBase], ["exact", SOLANA, "11100", CONFIG.payToSolana]]);
  assert.ok(required.extensions?.bazaar, "listed for discovery");
  assert.deepEqual(exa.calls, [], "Exa is not asked before the payer pays");
  assert.deepEqual(facilitator.calls.settle, []);
  const known = await (await fetch(`${url}/.well-known/x402`)).json();
  assert.deepEqual(known.resources.filter((r) => r.includes("/exa/")).map((r) => r.split("/v1/")[1]),
    ["exa/search", "exa/contents", "exa/answer"]);
});

test("a paid search asks Exa once with SingIt's key, then settles the payer", async (t) => {
  const { url, facilitator, exa } = await serve(t);
  const required = decode((await post(`${url}/v1/exa/search`, SEARCH)).headers.get("payment-required"));
  const paid = await post(`${url}/v1/exa/search`, SEARCH, pay(required, BASE));
  assert.equal(paid.status, 200);
  const body = await paid.json();
  assert.equal(body.results[0].url, "https://x402.org");
  assert.equal(body.costDollars, undefined, "Exa's own cost stays here");
  assert.deepEqual(body.billing, { exaAtomic: "7000", markupAtomic: "2100", feeAtomic: "1000", totalAtomic: "10100", currency: "USDC" });
  assert.deepEqual(exa.calls.map((c) => [c.url, c.key, c.body]), [["https://api.exa.ai/search", "exa_test_key", SEARCH]]);
  assert.deepEqual(facilitator.calls.settle, [[BASE, "10100"]]);
  const again = await post(`${url}/v1/exa/search`, SEARCH, pay(required, BASE));
  assert.equal(again.status, 409, "the same payment buys nothing twice");
  assert.equal(exa.calls.length, 1);
});

test("contents and answers are priced from Exa's list", async (t) => {
  const { url } = await serve(t);
  for (const [path, body, base] of [["/v1/exa/contents", { urls: ["https://a.test", "https://b.test"], text: true, highlights: true }, "6200"],
    ["/v1/exa/answer", { query: "What is x402?" }, "7500"],
    ["/v1/exa/search", { query: "x", numResults: 20, contents: { text: true } }, "49100"]]) {
    const required = decode((await post(`${url}${path}`, body)).headers.get("payment-required"));
    assert.equal(required.accepts.find((a) => a.network === BASE).amount, base, path);
  }
});

test("no answer from Exa, no charge to the payer", async (t) => {
  for (const status of [500, 402, 401]) {
    const { url, facilitator, logs } = await serve(t, { exa: fakeExa({ status }) });
    const required = decode((await post(`${url}/v1/exa/search`, SEARCH)).headers.get("payment-required"));
    const paid = await post(`${url}/v1/exa/search`, SEARCH, pay(required, SOLANA));
    assert.equal(paid.status, 502);
    assert.deepEqual(facilitator.calls.settle, []);
    assert.ok(logs.some((l) => l.includes(`HTTP ${status}`)));
  }
});

test("a cost Exa reports above its list price is logged", async (t) => {
  const { url, logs } = await serve(t, { exa: fakeExa({ cost: 0.009 }) });
  const required = decode((await post(`${url}/v1/exa/search`, SEARCH)).headers.get("payment-required"));
  await post(`${url}/v1/exa/search`, SEARCH, pay(required, BASE));
  assert.ok(logs.some((l) => /cost 9000 micro-USD, listed 7000/.test(l)));
});

test("what is not sold is refused before any payment is asked", async (t) => {
  const { url, exa } = await serve(t);
  for (const [path, body] of [["/v1/exa/search", { query: "" }], ["/v1/exa/search", { query: "x", type: "deep" }],
    ["/v1/exa/search", { query: "x", numResults: 100 }], ["/v1/exa/search", { query: "x", research: true }],
    ["/v1/exa/search", { query: "x", numResults: 25, contents: { text: true, highlights: true } }],
    ["/v1/exa/contents", { urls: [] }], ["/v1/exa/contents", { urls: Array(11).fill("https://a.test") }],
    ["/v1/exa/answer", { query: "x", model: "exa-research" }], ["/v1/exa/answer", { query: "x", stream: true }]]) {
    const refused = await post(`${url}${path}`, body);
    assert.equal(refused.status, 400, JSON.stringify(body));
    assert.equal(refused.headers.get("payment-required"), null);
  }
  assert.deepEqual(exa.calls, []);
});

test("x402scan's empty, unpaid probe gets the standing offer; a payment header does not excuse an invalid body", async (t) => {
  const { url, exa } = await serve(t);
  for (const route of ["/v1/exa/search", "/v1/exa/contents", "/v1/exa/answer"]) {
    let required;
    for (const init of [{ method: "POST" }, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }]) {
      const probe = await fetch(`${url}${route}`, init);
      assert.equal(probe.status, 402, route);
      required = decode(probe.headers.get("payment-required"));
      assert.deepEqual(required.accepts.map((a) => [a.scheme, a.network]), [["exact", BASE], ["exact", SOLANA]], route);
      assert.ok(required.extensions?.bazaar?.info, `${route} needs the input schema for x402scan to index it`);
      assert.ok(required.extensions?.bazaar?.schema, route);
    }
    const paidEmpty = await fetch(`${url}${route}`, { method: "POST", headers: pay(required, BASE) });
    assert.equal(paidEmpty.status, 400, `${route}: a payment header with no body is still refused, not waved through as a probe`);
  }
  assert.deepEqual(exa.calls, []);
});

test("prices and bounds", () => {
  assert.deepEqual(searchPrice("7000", SOLANA), { exaAtomic: "7000", markupAtomic: "2100", feeAtomic: "2000", totalAtomic: "11100" });
  assert.equal(searchPrice("1", BASE).markupAtomic, "1");
  assert.equal(exaPrice("/v1/exa/search", { query: "x", type: "instant" }), 4000);
  assert.throws(() => exaPrice("/v1/exa/search", null), /JSON/);
});

test("without an Exa key there is no search", async (t) => {
  const { url } = await serve(t, { exaKey: null });
  assert.equal((await post(`${url}/v1/exa/search`, SEARCH)).status, 404);
});
