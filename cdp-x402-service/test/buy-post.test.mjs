import assert from "node:assert/strict";
import { test } from "node:test";

import { makePaymentRequirementsSelector } from "../src/payment-guard.mjs";

// Venice's top-up is a POST. These cover the guard that decides whether a
// payment may be signed at all — the part that protects the user's funds when
// the request is no longer a plain GET.

const VENICE = "0x2670b922ef37c7df47158725c0cc407b5382293f";
const USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";

function requirement(overrides = {}) {
  return {
    scheme: "exact",
    network: "eip155:8453",
    asset: USDC,
    amount: "5000000",
    payTo: VENICE,
    maxTimeoutSeconds: 300,
    ...overrides,
  };
}

test("a matching top-up requirement is selected", () => {
  const select = makePaymentRequirementsSelector(
    { maxAtomic: "5000000", expectedReceiver: VENICE, expectedAsset: USDC },
    { requireAll: true },
  );
  assert.ok(select(2, [requirement()])) ;
});

test("a different payout address is refused before signing", () => {
  const select = makePaymentRequirementsSelector(
    { maxAtomic: "5000000", expectedReceiver: VENICE, expectedAsset: USDC },
    { requireAll: true },
  );
  assert.throws(() =>
    select(2, [requirement({ payTo: "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef" })]),
  );
});

test("an amount above the cap is refused before signing", () => {
  const select = makePaymentRequirementsSelector(
    { maxAtomic: "5000000", expectedReceiver: VENICE, expectedAsset: USDC },
    { requireAll: true },
  );
  assert.throws(() => select(2, [requirement({ amount: "10000000" })])) ;
});

test("a different asset is refused before signing", () => {
  const select = makePaymentRequirementsSelector(
    { maxAtomic: "5000000", expectedReceiver: VENICE, expectedAsset: USDC },
    { requireAll: true },
  );
  assert.throws(() => select(2, [requirement({ asset: "0xbadc0ffee" })])) ;
});
