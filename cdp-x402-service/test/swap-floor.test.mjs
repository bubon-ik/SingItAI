import test from "node:test";
import assert from "node:assert/strict";
import { parseUnits } from "viem";

import { assertSwapMeetsMinUsdc } from "../src/swap-floor.mjs";

test("passes when the guaranteed minimum meets the floor", () => {
  const price = { liquidityAvailable: true, minToAmount: parseUnits("0.12", 6) };
  assert.doesNotThrow(() => assertSwapMeetsMinUsdc(price, "0.10"));
});

test("throws when the guaranteed minimum is below the floor", () => {
  const price = { liquidityAvailable: true, minToAmount: parseUnits("0.05", 6) };
  assert.throws(() => assertSwapMeetsMinUsdc(price, "0.10"), /below required/);
});

test("throws when no liquidity is available", () => {
  const price = { liquidityAvailable: false };
  assert.throws(() => assertSwapMeetsMinUsdc(price, "0.10"), /liquidity/);
});

test("is a no-op when no minUsdc floor is requested", () => {
  assert.doesNotThrow(() => assertSwapMeetsMinUsdc(undefined, ""));
});

test("accepts string minToAmount values", () => {
  const price = { liquidityAvailable: true, minToAmount: "120000" };
  assert.doesNotThrow(() => assertSwapMeetsMinUsdc(price, "0.10"));
  const low = { liquidityAvailable: true, minToAmount: "50000" };
  assert.throws(() => assertSwapMeetsMinUsdc(low, "0.10"), /below required/);
});
