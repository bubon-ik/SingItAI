import express from 'express';
import { createApp, readRequest } from '../src/app.mjs';
import { AUTO_CONFIG } from '../src/auto-router.mjs';
import { BASE, SOLANA } from '../src/pricing.mjs';
const input = (text = 'hi', extra = {}) => readRequest({model: 'singit/auto', messages: [{role:'user', content:text}], max_tokens:512, ...extra});
// Offline price fixtures from website/data/models-snapshot.json (2026-10-30), USD per million.
// Include the cheap eco models: their quotes exercise the minimum ceiling, not just markup.
const rates = {
  'deepseek-v4.1-flash': [.045, .5], 'claude-haiku-5.5': [.1, .5], 'gemini-3.8-flash': [.825, 4.125],
  'claude-sonnet-5.5': [2, 10], 'kimi-k3': [.192, 15],
  'claude-opus-5.5': [4, 20], 'gpt-5.5': [5, 30], 'gemini-3.1-pro-preview': [2, 12],
};
const models = Object.values(AUTO_CONFIG.tiers).flat().map(id => ({id,
  promptUsdPerToken: rates[id][0]*1e-6, completionUsdPerToken: rates[id][1]*1e-6}));
const catalogue = {resolve: async id => models.find(m=>m.id===id), canAnswer: async()=>true};
const CONFIG = {payToSolana:"9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",payToBase:"0x1111111111111111111111111111111111111111",model:"deepseek-v4.1-flash",surplusUrl:"https://surplus.test"};
const encode=v=>Buffer.from(JSON.stringify(v)).toString("base64");
const decode=v=>JSON.parse(Buffer.from(v,"base64").toString());
catalogue.list=async()=>models;
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

async function serve(t, { upstream, catalogue: selectedCatalogue = catalogue, middleware, config = CONFIG } = {}) {
  const facilitator = fakeFacilitator();
  const asked = [];
  const answer = upstream ?? (async (_config, messages, maxTokens, _fetchImpl, model) => {
    asked.push({ model, messages, maxTokens });
    return { content: "Try Caffè Propaganda.", finishReason: "stop", usage: { total_tokens: 42, buyer_cost_micro: 100 } };
  });
  const app = createApp(config, { facilitatorClient: facilitator, upstream: answer, log: () => {}, catalogue: selectedCatalogue });
  const server = middleware ? express().use(middleware, app) : app;
  const listener = await new Promise((resolve, reject) => { const l = server.listen(0, "127.0.0.1", error => error ? reject(error) : resolve(l)); l.on("error", reject); });
  t.after(() => listener.close());
  return { url: `http://127.0.0.1:${listener.address().port}`, facilitator, asked };
}

const post = (url, body, headers = {}) => fetch(url, { method: "POST",
  headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
const pay = (required, network, scheme = "upto") => ({ "PAYMENT-SIGNATURE": encode({ x402Version: 2, resource: required.resource,
  accepted: required.accepts.find((a) => a.network === network && a.scheme === scheme), payload: { signature: "0xsigned" },
  extensions: required.extensions }) });
export { catalogue, models, CONFIG, serve, post, pay, decode, input, fakeFacilitator, rates };
