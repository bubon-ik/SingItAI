import test from "node:test";
import assert from "node:assert/strict";

import { executeStagedSwap } from "../src/staged-swap.mjs";


test("floor failure is classified pre_swap and never calls swap", async () => {
  let swapCalls = 0;

  await assert.rejects(
    executeStagedSwap({
      minUsdc: "23.9976",
      sleep: async () => {},
      getPrice: async () => ({
        liquidityAvailable: true,
        minToAmount: 23795602n,
      }),
      assertFloor: () => {
        throw new Error("below floor");
      },
      swap: async () => {
        swapCalls += 1;
      },
    }),
    (error) => error.stage === "pre_swap",
  );

  assert.equal(swapCalls, 0);
});


test("swap call failure has no safe pre_swap stage", async () => {
  await assert.rejects(
    executeStagedSwap({
      minUsdc: "1",
      getPrice: async () => ({
        liquidityAvailable: true,
        minToAmount: 1000000n,
      }),
      assertFloor: () => {},
      swap: async () => {
        throw new Error("ambiguous");
      },
    }),
    (error) => error.stage === "",
  );
});


test("successful staged swap returns the swap result", async () => {
  const result = await executeStagedSwap({
    minUsdc: "",
    getPrice: async () => {
      throw new Error("price must not be requested without a floor");
    },
    assertFloor: () => {},
    swap: async () => ({ transactionHash: "0xSWAP" }),
  });

  assert.deepEqual(result, { transactionHash: "0xSWAP" });
});
