import assert from "node:assert/strict";
import test from "node:test";

import { MARKETS } from "../src/chain.mjs";
import { DEFAULT_SLIPPAGE_BPS, priceFromSqrt, quoteBuy } from "../src/quote.mjs";
import { NVDA_SQRT } from "./fakes.mjs";

const NVDA = MARKETS.NVDA;

test("the pool price decodes to dollars a share, not shares a dollar", () => {
  const price = priceFromSqrt(NVDA_SQRT, NVDA);
  // Inverted, this would read about $0.0046 — a mistake that would sell a
  // whole share for half a cent.
  assert.ok(price > 100 && price < 400, `got ${price}`);
});

test("a hundred dollars buys roughly the right number of shares", () => {
  const price = priceFromSqrt(NVDA_SQRT, NVDA);
  const quote = quoteBuy({ market: NVDA, amountInAtomic: "100000000", usdPerShare: price });

  assert.ok(Math.abs(quote.expectedShares - 100 / price) < 0.001);
  assert.equal(quote.expectedOutAtomic > 0n, true);
});

test("the floor sits below the estimate by exactly the slippage allowance", () => {
  const quote = quoteBuy({ market: NVDA, amountInAtomic: "100000000", usdPerShare: 200 });
  const expected = (quote.expectedOutAtomic * BigInt(10_000 - DEFAULT_SLIPPAGE_BPS)) / 10_000n;

  assert.equal(quote.minOutAtomic, expected);
  assert.ok(quote.minOutAtomic < quote.expectedOutAtomic);
});

test("a tighter floor refuses more fills, not fewer", () => {
  const loose = quoteBuy({ market: NVDA, amountInAtomic: "100000000", usdPerShare: 200, slippageBps: 500 });
  const tight = quoteBuy({ market: NVDA, amountInAtomic: "100000000", usdPerShare: 200, slippageBps: 10 });

  assert.ok(tight.minOutAtomic > loose.minOutAtomic);
});

test("the pool fee comes out of the input before the shares are counted", () => {
  const quote = quoteBuy({ market: NVDA, amountInAtomic: "100000000", usdPerShare: 100 });

  // $100 at $100 a share is one share, less the pool's 0.05%.
  assert.ok(quote.expectedShares < 1 && quote.expectedShares > 0.999);
});

test("a dead pool is refused rather than priced", () => {
  assert.throws(() => priceFromSqrt(0n, NVDA), /no price/);
});

test("an order too small to buy any share is refused", () => {
  assert.throws(
    () => quoteBuy({ market: NVDA, amountInAtomic: "1", usdPerShare: 200 }),
    /too small/,
  );
});
