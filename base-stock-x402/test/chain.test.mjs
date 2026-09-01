/**
 * The constants, checked rather than trusted.
 *
 * Offline these assert the facts the code depends on internally. With
 * BASE_LIVE=1 they re-read the same facts from Base, so a token migration or a
 * repriced pool breaks the suite instead of breaking a payment.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { hashDomain } from "viem";

import {
  MARKETS,
  SEL,
  USDC,
  USDC_DOMAIN,
  USDC_DOMAIN_SEPARATOR,
  market,
  usdcAtomic,
} from "../src/chain.mjs";
import { addressWord, rpcBatch, wordAt } from "../src/rpc.mjs";

test("the pinned USDC domain reproduces the pinned separator", () => {
  // If these ever disagree, every signature this node accepts verifies as
  // somebody else's and settlement fails for reasons that look like the payer.
  const computed = hashDomain({
    domain: USDC_DOMAIN,
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
        { name: "verifyingContract", type: "address" },
      ],
    },
  });
  assert.equal(computed, USDC_DOMAIN_SEPARATOR);
});

test("every equity is eight decimals, and USDC is six", () => {
  assert.equal(USDC.decimals, 6);
  for (const entry of Object.values(MARKETS)) {
    assert.equal(entry.decimals, 8, `${entry.symbol} decimals`);
  }
});

test("tickers resolve case-insensitively and unknown ones do not", () => {
  assert.equal(market("nvda"), MARKETS.NVDA);
  assert.equal(market(" Meta "), MARKETS.META);
  assert.equal(market("TSLA"), null);
  assert.equal(market(""), null);
});

test("USD converts to atomic USDC without losing cents", () => {
  assert.equal(usdcAtomic("100"), "100000000");
  assert.equal(usdcAtomic("0.01"), "10000");
  assert.equal(usdcAtomic("249.99"), "249990000");
});

const live = process.env.BASE_LIVE === "1";

test("the equity tokens are what we think they are", { skip: !live }, async () => {
  const entries = Object.values(MARKETS);
  const results = await rpcBatch(
    entries.flatMap((entry) => [
      ["eth_call", [{ to: entry.token, data: SEL.decimals }, "latest"]],
      ["eth_call", [{ to: entry.token, data: SEL.symbol }, "latest"]],
    ]),
  );
  entries.forEach((entry, index) => {
    const decimals = Number(BigInt(results[index * 2].result));
    assert.equal(decimals, entry.decimals, `${entry.ticker} decimals on chain`);
    const symbol = decodeString(results[index * 2 + 1].result);
    assert.equal(symbol, entry.symbol, `${entry.ticker} symbol on chain`);
  });
});

test("USDC is token0 in every pool, and the spacing matches", { skip: !live }, async () => {
  const entries = Object.values(MARKETS);
  const results = await rpcBatch(
    entries.flatMap((entry) => [
      ["eth_call", [{ to: entry.pool, data: SEL.token0 }, "latest"]],
      ["eth_call", [{ to: entry.pool, data: SEL.token1 }, "latest"]],
      ["eth_call", [{ to: entry.pool, data: SEL.tickSpacingCall }, "latest"]],
    ]),
  );
  entries.forEach((entry, index) => {
    const token0 = "0x" + results[index * 3].result.slice(-40);
    const token1 = "0x" + results[index * 3 + 1].result.slice(-40);
    // The whole price calculation inverts on this. It is asserted, not assumed.
    assert.equal(token0.toLowerCase(), USDC.address.toLowerCase(), `${entry.ticker} token0`);
    assert.equal(token1.toLowerCase(), entry.token.toLowerCase(), `${entry.ticker} token1`);
    assert.equal(Number(BigInt(results[index * 3 + 2].result)), entry.tickSpacing);
  });
});

test("USDC still reports the domain separator we pinned", { skip: !live }, async () => {
  const [separator] = await rpcBatch([
    ["eth_call", [{ to: USDC.address, data: SEL.domainSeparator }, "latest"]],
  ]);
  assert.equal(separator.result, USDC_DOMAIN_SEPARATOR);
});

test("USDC still implements EIP-3009, so gasless payment works", { skip: !live }, async () => {
  const [state] = await rpcBatch([
    ["eth_call", [{
      to: USDC.address,
      data: SEL.authorizationState + addressWord(USDC.address) + "00".repeat(32),
    }, "latest"]],
  ]);
  assert.ok(state.result && state.result !== "0x", "authorizationState answered");
});

function decodeString(hex) {
  const bytes = Buffer.from(hex.slice(2), "hex");
  const length = Number(BigInt("0x" + bytes.subarray(32, 64).toString("hex")));
  return bytes.subarray(64, 64 + length).toString("utf8");
}
