/**
 * What a given amount of USDC buys, and the floor below which we will not sell.
 *
 * Two numbers come out of here and they do different jobs. `expectedOut` is
 * what the pool would give at the price we just read; it is an estimate and is
 * never promised to the buyer. `minOut` is the number that goes into the swap
 * calldata — below it the transaction reverts and the buyer is refunded rather
 * than filled at a price nobody agreed to.
 */

import { SEL, USDC } from "./chain.mjs";
import { ethCall, wordAt } from "./rpc.mjs";

// The same 1.5% floor Bankr's own entry path uses. It is not a slippage
// *target*: it is the point at which we would rather refund than fill.
export const DEFAULT_SLIPPAGE_BPS = 150;

const Q96 = 2n ** 96n;

/**
 * USD per share, from the pool's own sqrtPriceX96.
 *
 * USDC is token0 and the equity token1 in every one of these pools, so the raw
 * ratio is shares-per-USDC and has to be inverted. Getting that backwards
 * prices a $300 share at a third of a cent, which is why the pool ordering is
 * asserted in the chain test rather than assumed here.
 */
export function priceFromSqrt(sqrtPriceX96, market) {
  const sqrt = BigInt(sqrtPriceX96);
  if (sqrt <= 0n) {
    throw new Error("pool has no price");
  }
  const ratio = Number(sqrt) / Number(Q96);
  const sharesPerUsdc = ratio * ratio * 10 ** USDC.decimals / 10 ** market.decimals;
  if (!Number.isFinite(sharesPerUsdc) || sharesPerUsdc <= 0) {
    throw new Error("pool price is not usable");
  }
  return 1 / sharesPerUsdc;
}

/** Read the live pool price. One call, no caching: a stale price fills wrong. */
export async function readPoolPrice(market, options = {}) {
  const { data, message } = await ethCall(market.pool, SEL.slot0, options);
  if (!data || data === "0x") {
    throw new Error(`pool read failed: ${message ?? "empty response"}`);
  }
  const sqrtPriceX96 = BigInt(wordAt(data, 0));
  return { sqrtPriceX96, usdPerShare: priceFromSqrt(sqrtPriceX96, market) };
}

export function quoteBuy({ market, amountInAtomic, usdPerShare, slippageBps = DEFAULT_SLIPPAGE_BPS }) {
  const amountIn = BigInt(amountInAtomic);
  if (amountIn <= 0n) {
    throw new Error("amount must be positive");
  }
  const usd = Number(amountIn) / 10 ** USDC.decimals;
  // The pool takes its fee out of the input before the swap curve sees it.
  const afterFee = usd * (1 - market.feeBps / 10_000);
  const expectedShares = afterFee / usdPerShare;
  const expectedOutAtomic = BigInt(Math.floor(expectedShares * 10 ** market.decimals));
  const minOutAtomic = (expectedOutAtomic * BigInt(10_000 - slippageBps)) / 10_000n;
  if (minOutAtomic <= 0n) {
    throw new Error("amount is too small to buy any shares");
  }
  return {
    usdPerShare,
    expectedShares,
    expectedOutAtomic,
    minOutAtomic,
    slippageBps,
  };
}
