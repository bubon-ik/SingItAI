import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/main.js", import.meta.url), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `${start} .. ${end}`);
  return source.slice(from, to);
}

const presets = section("const PRESETS = [", "const esc =");
const helpers = section("const esc =", "function amount(");
const views = section("function renderSetup(heading) {", "function renderStale(");
const tracked = [];
const context = vm.createContext({ state: { preset: 0, allowance: null }, track: (name, props) => tracked.push([name, props]) });
vm.runInContext(`${presets}\n${helpers}\n${views}`, context);

test("an unconfigured Solana account does not get the Base contract guarantee", () => {
  context.state.allowance = { chain: "solana", configured: false, supported: true };
  const html = context.renderAllowance();
  assert.match(html, /Solana enforces the total you approve/);
  assert.match(html, /SingIt enforces the daily and per-purchase limits/);
  assert.match(html, /deploys no contract/);
  assert.match(html, /Save my limits/);
  assert.doesNotMatch(html, /even if our server/);
  assert.doesNotMatch(html, /contract on Base/);
  assert.doesNotMatch(html, /Creating your limiter on Base/);
});

test("Solana with the lane off still says limits are coming", () => {
  context.state.allowance = { chain: "solana", configured: false, supported: false, text: "Soon." };
  const html = context.renderAllowance();
  assert.match(html, /Limits on Solana are/);
  assert.match(html, /Soon\./);
  assert.doesNotMatch(html, /contract on Base/);
});

test("an unconfigured Base account still describes the on-chain limiter", () => {
  context.state.allowance = { chain: "base", configured: false };
  const html = context.renderAllowance();
  assert.match(html, /even if our server or the agent is compromised/);
  assert.match(html, /A small contract on Base/);
  assert.match(html, /Create my limiter/);
});

test("saving Solana limits does not say a Base contract is being created", () => {
  const fn = section("async function createLimiter() {", "function hasGas() {");
  assert.match(fn, /state\.session\?\.chain === "solana"/);
  assert.match(fn, /Saving your limits\./);
  assert.match(fn, /Limits saved\. Now approve them in your wallet\./);
});
