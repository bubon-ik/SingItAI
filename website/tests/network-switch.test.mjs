import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// Run the real switch markup and its button handler from main.js; no AppKit, no wallet.
const source = readFileSync(new URL("../app/main.js", import.meta.url), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return source.slice(from, to);
}
const choice = section("function networkChoice() {", "function renderHero() {");
const handler = section('"sign-in-network": async (el) => {', "\n  },\n").replace('"sign-in-network": ', "") + "\n  }";

function setup(wallet, switchImpl = async () => {}) {
  const calls = [], toasts = [];
  const context = vm.createContext({
    state: { wallet },
    switchAppKitNetwork: async (chain) => { calls.push(chain); return switchImpl(chain); },
    toast: (message, error) => toasts.push([message, error]),
    explain: (error) => error.message,
  });
  vm.runInContext(choice + "\nglobalThis.press = " + handler + ";", context);
  return { context, calls, toasts };
}

test("a wallet connected through AppKit can pick Base or Solana before signing in", () => {
  const onBase = setup({ name: "WalletConnect", chain: "base", address: "0x3937" }).context.networkChoice();
  assert.match(onBase, /data-chain="base"\s+class="on" aria-pressed="true">Base/);
  assert.match(onBase, /data-chain="solana"\s+class="" aria-pressed="false">Solana/);
  const onSolana = setup({ name: "WalletConnect", chain: "solana", address: "3dTW" }).context.networkChoice();
  assert.match(onSolana, /data-chain="solana"\s+class="on" aria-pressed="true">Solana/);
});

test("a wallet extension, or no wallet, gets no switch", () => {
  assert.equal(setup({ name: "Phantom", chain: "solana", address: "3dTW" }).context.networkChoice(), "");
  assert.equal(setup(null).context.networkChoice(), "");
});

test("pressing the other network switches AppKit; the current one does nothing", async () => {
  const { context, calls } = setup({ name: "WalletConnect", chain: "base", address: "0x3937" });
  await context.press({ dataset: { chain: "base" } });
  await context.press({ dataset: { chain: "solana" } });
  assert.deepEqual(calls, ["solana"]);
});

test("a switch the wallet refuses is shown, not swallowed", async () => {
  const { context, toasts } = setup({ name: "WalletConnect", chain: "base", address: "0x3937" },
    async () => { throw new Error("This wallet has no Solana account."); });
  await context.press({ dataset: { chain: "solana" } });
  assert.deepEqual(toasts, [["This wallet has no Solana account.", true]]);
});
