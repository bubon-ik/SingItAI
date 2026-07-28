import test from "node:test";
import assert from "node:assert/strict";

import { assertSwapMeetsMinUsdc } from "../src/swap-floor.mjs";


test("missing liquidity is reported as no_liquidity", () => {
  assert.throws(
    () => assertSwapMeetsMinUsdc({ liquidityAvailable: false }, "10"),
    (error) => error.reason === "no_liquidity",
  );
});


test("an output below the floor is reported as rate_moved", () => {
  assert.throws(
    () =>
      assertSwapMeetsMinUsdc(
        { liquidityAvailable: true, minToAmount: 9_000_000n },
        "10",
      ),
    (error) => error.reason === "rate_moved",
  );
});


test("an output at the floor passes", () => {
  assert.doesNotThrow(() =>
    assertSwapMeetsMinUsdc(
      { liquidityAvailable: true, minToAmount: 10_000_000n },
      "10",
    ),
  );
});
