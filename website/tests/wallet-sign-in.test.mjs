import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

// Run the real Wallet class against a fake EVM provider; no browser wallet involved.
const source = readFileSync(new URL("../app/wallet.js", import.meta.url), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return source.slice(from, to);
}
const code = section("const BASE = {", "// Installed extensions")
  + section("export class Wallet {", "// -- Reown AppKit").replace("export class", "class")
  + "\nlet kit = null; let kitNetworks = null;\nglobalThis.Wallet = Wallet;";

function wallet(chainId) {
  const calls = [];
  const provider = {
    async request({ method, params }) {
      calls.push(method);
      if (method === "eth_chainId") return chainId;
      if (method === "wallet_switchEthereumChain") { chainId = params[0].chainId; return null; }
      if (method === "personal_sign") return "0xsigned";
      throw new Error(`unexpected ${method}`);
    },
  };
  const context = vm.createContext({ TextEncoder });
  vm.runInContext(code, context);
  return { calls, wallet: new context.Wallet(provider, "Phantom", "0x7251") };
}

test("a sign-in on another network first moves the wallet to Base", async () => {
  // Phantom (and Reown's email/Google wallet) refuse a Base sign-in message while on Ethereum.
  const { calls, wallet: w } = wallet("0x1");
  assert.equal(await w.signMessage("Sign in to SingIt"), "0xsigned");
  assert.deepEqual(calls, ["eth_chainId", "wallet_switchEthereumChain", "personal_sign"]);
});

test("a wallet already on Base signs without switching", async () => {
  const { calls, wallet: w } = wallet("0x2105");
  await w.signMessage("Sign in to SingIt");
  assert.deepEqual(calls, ["eth_chainId", "personal_sign"]);
});
