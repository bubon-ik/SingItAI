import test from "node:test";
import assert from "node:assert/strict";

import { makePaymentRequirementsSelector } from "../src/payment-guard.mjs";

const RECEIVER = "0x1111111111111111111111111111111111111111";
const ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913";

function req(overrides = {}) {
  return {
    scheme: "exact",
    network: "eip155:8453",
    maxAmountRequired: "10000",
    payTo: RECEIVER,
    asset: ASSET,
    ...overrides,
  };
}

test("returns the requirement when it matches approved terms", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER,
    expectedAsset: ASSET,
  });
  assert.equal(select(2, [req()]).maxAmountRequired, "10000");
});

test("throws when the demanded amount exceeds the approved cap", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER,
    expectedAsset: ASSET,
  });
  assert.throws(() => select(2, [req({ maxAmountRequired: "10001" })]), /does not match approved/);
});

test("throws when the receiver differs from the approved receiver", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER,
    expectedAsset: ASSET,
  });
  assert.throws(() => select(2, [req({ payTo: "0x2222222222222222222222222222222222222222" })]), /does not match approved/);
});

test("throws when the asset differs from the approved asset", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER,
    expectedAsset: ASSET,
  });
  assert.throws(() => select(2, [req({ asset: "0x0000000000000000000000000000000000000000" })]), /does not match approved/);
});

test("receiver/asset comparison is case-insensitive", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER.toUpperCase().replace("0X", "0x"),
    expectedAsset: ASSET.toLowerCase(),
  });
  assert.doesNotThrow(() => select(2, [req()]));
});

test("picks the matching requirement among several", () => {
  const select = makePaymentRequirementsSelector({
    maxAtomic: "10000",
    expectedReceiver: RECEIVER,
    expectedAsset: ASSET,
  });
  const chosen = select(2, [
    req({ payTo: "0x2222222222222222222222222222222222222222" }),
    req({ maxAmountRequired: "9000" }),
  ]);
  assert.equal(chosen.maxAmountRequired, "9000");
});

test("without caps returns the first requirement (non-user funded path)", () => {
  const select = makePaymentRequirementsSelector({});
  assert.equal(select(2, [req({ maxAmountRequired: "5" }), req()]).maxAmountRequired, "5");
});
