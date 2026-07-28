import test from "node:test";
import assert from "node:assert/strict";

import {
  executeStagedSwap,
  StagedCdpError,
  stagedErrorPayload,
} from "../src/staged-swap.mjs";


test("the reported payload carries the reason and never the provider text", () => {
  const payload = stagedErrorPayload(
    new StagedCdpError("CDP pre-swap validation failed", {
      stage: "pre_swap",
      reason: "rate_moved",
      cause: new Error("0xDEADBEEF pool reverted for taker 0xabc"),
    }),
  );

  assert.deepEqual(payload, {
    ok: false,
    error: "CDP wallet service failed",
    stage: "pre_swap",
    reason: "rate_moved",
  });
});


test("an ambiguous swap failure reports no stage and no reason", () => {
  const payload = stagedErrorPayload(
    new StagedCdpError("CDP swap result is ambiguous", {
      cause: new Error("boom"),
    }),
  );

  assert.equal(payload.stage, "");
  assert.equal(payload.reason, "");
});


function floorError(reason) {
  const error = new Error(`floor: ${reason}`);
  error.reason = reason;
  return error;
}


test("a transient floor miss is retried and the swap still runs", async () => {
  let priceCalls = 0;
  const slept = [];

  const result = await executeStagedSwap({
    minUsdc: "47.6013",
    attempts: 3,
    retryDelayMs: 1500,
    sleep: async (ms) => slept.push(ms),
    getPrice: async () => {
      priceCalls += 1;
      return { liquidityAvailable: true, minToAmount: 47_645_453n };
    },
    assertFloor: () => {
      // The rate dips under the floor once, then recovers.
      if (priceCalls === 1) throw floorError("rate_moved");
    },
    swap: async () => ({ transactionHash: "0xSWAP" }),
  });

  assert.deepEqual(result, { transactionHash: "0xSWAP" });
  assert.equal(priceCalls, 2);
  assert.deepEqual(slept, [1500]);
});


test("exhausted retries fail pre_swap and carry the last reason", async () => {
  let swapCalls = 0;

  await assert.rejects(
    executeStagedSwap({
      minUsdc: "47.6013",
      attempts: 3,
      retryDelayMs: 0,
      sleep: async () => {},
      getPrice: async () => ({ liquidityAvailable: true, minToAmount: 1n }),
      assertFloor: () => {
        throw floorError("rate_moved");
      },
      swap: async () => {
        swapCalls += 1;
      },
    }),
    (error) => error.stage === "pre_swap" && error.reason === "rate_moved",
  );

  assert.equal(swapCalls, 0);
});


test("a failing price request is reported as price_unavailable", async () => {
  await assert.rejects(
    executeStagedSwap({
      minUsdc: "47.6013",
      attempts: 2,
      retryDelayMs: 0,
      sleep: async () => {},
      getPrice: async () => {
        throw new Error("CDP getSwapPrice exploded");
      },
      assertFloor: () => {},
      swap: async () => {},
    }),
    (error) =>
      error.stage === "pre_swap" && error.reason === "price_unavailable",
  );
});


test("the swap itself is never retried", async () => {
  let swapCalls = 0;

  await assert.rejects(
    executeStagedSwap({
      minUsdc: "1",
      attempts: 3,
      retryDelayMs: 0,
      sleep: async () => {},
      getPrice: async () => ({ liquidityAvailable: true, minToAmount: 1000000n }),
      assertFloor: () => {},
      swap: async () => {
        swapCalls += 1;
        throw new Error("ambiguous");
      },
    }),
    (error) => error.stage === "",
  );

  assert.equal(swapCalls, 1);
});
