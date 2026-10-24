// SI-62: one x402 resource per popular model, /v1/chat/<model>, each the same offers and billing as
// /v1/chat/completions (metered.mjs's addUsageRoute does that work; nothing here duplicates it), except the
// model is pinned by the path instead of the body.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BASE, SOLANA, ROUTE, createApp } from "../src/app.mjs";
import { UnknownModel } from "../src/catalogue.mjs";
import { MODEL_ROUTES, modelRoutePaths, routePath, discoveryDescription, operationId } from "../src/model-routes.mjs";

const CONFIG = { payToSolana: "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
  payToBase: "0x1111111111111111111111111111111111111111", surplusKey: "inf_test", surplusUrl: "https://surplus.test",
  model: "deepseek-v4.1-flash" };
const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64");
const decode = (header) => JSON.parse(Buffer.from(header, "base64").toString("utf8"));

// Three of the fourteen pinned ids, with prices far enough apart to tell a cheap and an expensive model's
// ceilings apart, plus one that cannot be answered right now. The other eleven pinned ids resolve to
// UnknownModel, same as a model Surplus does not list: that is also exercised below (the probe must still fall
// back cleanly, and a real question for one of them must be refused before payment).
const PRICEY = { id: "claude-opus-5.5", name: "Opus", provider: "anthropic", contextLength: 1e6, promptUsdPerToken: 30e-6, completionUsdPerToken: 180e-6 };
const CHEAP = { id: "gemini-3.8-flash", name: "Flash", provider: "google", contextLength: 1e6, promptUsdPerToken: 0.1e-6, completionUsdPerToken: 0.3e-6 };
const IDLE = { id: "glm-5.3", name: "GLM", provider: "zhipu", contextLength: 1e5, promptUsdPerToken: 1e-6, completionUsdPerToken: 1e-6 };
const KNOWN = [PRICEY, CHEAP, IDLE];
const catalogue = {
  async resolve(id) { const m = KNOWN.find((x) => x.id === id); if (!m) throw new UnknownModel("That model is not available."); return m; },
  async canAnswer(id) { return id !== IDLE.id; },
  async list() { return KNOWN; },
};

function fakeFacilitator() {
  const calls = { verify: 0, settle: [] };
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
    async verify() { calls.verify += 1; return { isValid: true, payer: "0x2222222222222222222222222222222222222222" }; },
    async settle(_payload, requirements) {
      calls.settle.push([requirements.network, requirements.amount]);
      return { success: true, transaction: "0x" + "a".repeat(64), network: requirements.network };
    } };
}

async function serve(t, { upstream } = {}) {
  const facilitator = fakeFacilitator();
  const asked = [];
  const answer = upstream ?? (async (_config, messages, maxTokens, _fetchImpl, model) => {
    asked.push({ model, messages, maxTokens });
    return { content: "Try Caffè Propaganda.", finishReason: "stop", usage: { total_tokens: 42, buyer_cost_micro: 100 } };
  });
  const app = createApp(CONFIG, { facilitatorClient: facilitator, upstream: answer, log: () => {}, catalogue });
  const listener = await new Promise((resolve) => { const l = app.listen(0, "127.0.0.1", () => resolve(l)); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, asked };
}

const post = (url, body, headers = {}) => fetch(url, { method: "POST",
  headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
const pay = (required, network, scheme = "upto") => ({ "PAYMENT-SIGNATURE": encode({ x402Version: 2, resource: required.resource,
  accepted: required.accepts.find((a) => a.network === network && a.scheme === scheme), payload: { signature: "0xsigned" },
  extensions: required.extensions }) });

test("the 14 ids are URL-safe and none is \"completions\"", () => {
  assert.equal(MODEL_ROUTES.length, 14);
  for (const model of MODEL_ROUTES) {
    assert.match(model.id, /^[a-z0-9.-]+$/, model.id);
    assert.notEqual(model.id, "completions");
    assert.equal(routePath(model.id), `/v1/chat/${model.id}`);
  }
  assert.equal(new Set(modelRoutePaths()).size, 14, "no two models share a route");
  assert.ok(!modelRoutePaths().includes(ROUTE), "a model route never collides with the generic chat route");
});

test("every model route's 402 (GET, HEAD, empty POST) leads with exact on Base", async (t) => {
  const { url, facilitator } = await serve(t);
  for (const model of MODEL_ROUTES) {
    const route = routePath(model.id);
    for (const method of ["GET", "HEAD", "POST"]) {
      const response = await fetch(`${url}${route}`, { method });
      assert.equal(response.status, 402, `${route} ${method}`);
      if (method !== "HEAD") {
        const required = decode(response.headers.get("payment-required"));
        assert.equal(required.accepts[0].scheme, "exact", route);
        assert.equal(required.accepts[0].network, BASE, route);
      }
    }
  }
  assert.equal(facilitator.calls.verify, 0);
  assert.deepEqual(facilitator.calls.settle, []);
});

test("every model route's description is short, billing-free, and the resource carries branding", async (t) => {
  const { url } = await serve(t);
  const BILLING_WORDS = /\b(fee|markup|ceiling|settlement)\b|%/i;
  for (const model of MODEL_ROUTES) {
    const route = routePath(model.id);
    const required = decode((await fetch(`${url}${route}`, { method: "GET" })).headers.get("payment-required"));
    const description = required.resource.description;
    assert.equal(description, discoveryDescription(model), route);
    assert.ok(description.length <= 300, `${route}: ${description.length} chars`);
    assert.doesNotMatch(description, BILLING_WORDS, route);
    assert.match(required.resource.iconUrl ?? "", /\.png$/, route);
    assert.equal(required.resource.serviceName, "SingIt Ask", route);
    assert.ok(required.resource.tags.includes(model.id), route);
  }
});

test("a body model that disagrees with the path is refused before any payment", async (t) => {
  const { url, facilitator } = await serve(t);
  const response = await post(`${url}${routePath(PRICEY.id)}`, { model: CHEAP.id, messages: [{ role: "user", content: "Hi" }] });
  assert.equal(response.status, 400);
  assert.equal(response.headers.get("payment-required"), null);
  assert.equal(facilitator.calls.verify, 0);
});

test("a body model that only differs in case is accepted", async (t) => {
  const { url } = await serve(t);
  const response = await post(`${url}${routePath(PRICEY.id)}`, { model: PRICEY.id.toUpperCase(), messages: [{ role: "user", content: "Hi" }] });
  assert.equal(response.status, 402);
});

test("an absent model reaches upstream pinned to the path's model", async (t) => {
  const { url, asked } = await serve(t);
  const required = decode((await post(`${url}${routePath(PRICEY.id)}`, { messages: [{ role: "user", content: "Hi" }] }))
    .headers.get("payment-required"));
  const paid = await post(`${url}${routePath(PRICEY.id)}`, { messages: [{ role: "user", content: "Hi" }] }, pay(required, BASE));
  assert.equal(paid.status, 200);
  assert.equal(asked.length, 1);
  assert.equal(asked[0].model, PRICEY.id);
});

test("a model Surplus does not list is refused before payment, same as an unknown model elsewhere", async (t) => {
  // "claude-sonnet-5.5" is a real pinned route, but outside the fake catalogue's KNOWN list: resolve() throws
  // UnknownModel for it, exactly as it would for a model genuinely absent from Surplus's list.
  const { url, facilitator } = await serve(t);
  const response = await post(`${url}${routePath("claude-sonnet-5.5")}`, { messages: [{ role: "user", content: "Hi" }] });
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("payment-required"), null);
  assert.equal(facilitator.calls.verify, 0);
});

test("a model with no trusted seller right now is refused before payment", async (t) => {
  const { url, facilitator } = await serve(t);
  const response = await post(`${url}${routePath(IDLE.id)}`, { messages: [{ role: "user", content: "Hi" }] });
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("payment-required"), null);
  assert.equal(facilitator.calls.verify, 0);
});

test("the probe price follows the model: an expensive model quotes more than a cheap one", async (t) => {
  const { url } = await serve(t);
  const priceyRequired = decode((await fetch(`${url}${routePath(PRICEY.id)}`, { method: "GET" })).headers.get("payment-required"));
  const cheapRequired = decode((await fetch(`${url}${routePath(CHEAP.id)}`, { method: "GET" })).headers.get("payment-required"));
  const amount = (required, network, scheme) => Number(required.accepts.find((a) => a.network === network && a.scheme === scheme).amount);
  assert.ok(amount(priceyRequired, BASE, "upto") > amount(cheapRequired, BASE, "upto"));
  assert.ok(amount(priceyRequired, SOLANA, "exact") > amount(cheapRequired, SOLANA, "exact"));
  // GET and an empty POST must agree: both fall through the same quotes-miss path to the same model lookup.
  const postedRequired = decode((await post(`${url}${routePath(PRICEY.id)}`, {})).headers.get("payment-required"));
  assert.equal(amount(postedRequired, BASE, "upto"), amount(priceyRequired, BASE, "upto"));
});

test("a model unknown to the catalogue still answers its probe, falling back like the main route", async (t) => {
  const { url } = await serve(t);
  const required = decode((await fetch(`${url}${routePath("gpt-5.5")}`, { method: "GET" })).headers.get("payment-required"));
  assert.ok(required.accepts.length > 0);
  assert.equal(required.accepts.find((a) => a.network === BASE && a.scheme === "upto").amount, "3000");
});

test("GET /openapi.json and /.well-known/x402 list all 14 model routes", async (t) => {
  const { url } = await serve(t);
  const doc = await (await fetch(`${url}/openapi.json`)).json();
  for (const model of MODEL_ROUTES) {
    const op = doc.paths[routePath(model.id)]?.post;
    assert.ok(op, routePath(model.id));
    assert.equal(op.operationId, operationId(model), model.id);
    assert.equal(op.summary, `${model.name} chat completion`, model.id);
    assert.ok(op["x-payment-info"].price.max, model.id);
  }
  // PRICEY is resolvable: its own ceiling, not the generic fallback, is quoted.
  const opusMax = Number(doc.paths[routePath(PRICEY.id)].post["x-payment-info"].price.max);
  const genericMax = Number(doc.paths[ROUTE].post["x-payment-info"].price.max);
  assert.ok(opusMax <= genericMax);

  const known = await (await fetch(`${url}/.well-known/x402`)).json();
  for (const route of modelRoutePaths()) assert.ok(known.resources.includes(`${url}${route}`), route);
});

test("GET / and /llms.txt list all 14 model routes", async (t) => {
  const { url } = await serve(t);
  const index = await (await fetch(`${url}/`)).json();
  assert.equal(index.name, "SingIt Ask");
  const llmsTxt = await (await fetch(`${url}/llms.txt`)).text();
  for (const route of modelRoutePaths()) assert.ok(llmsTxt.includes(`POST ${url}${route}`), route);
});

test("/v1/chat/completions keeps taking model in the body, unaffected by the per-model routes", async (t) => {
  const { url, asked } = await serve(t);
  const required = decode((await post(`${url}${ROUTE}`, { model: CHEAP.id, messages: [{ role: "user", content: "Hi" }] }))
    .headers.get("payment-required"));
  const paid = await post(`${url}${ROUTE}`, { model: CHEAP.id, messages: [{ role: "user", content: "Hi" }] }, pay(required, BASE));
  assert.equal(paid.status, 200);
  assert.equal(asked[0].model, CHEAP.id);
});
