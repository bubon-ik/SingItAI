import assert from "node:assert/strict";
import test from "node:test";

import handler from "../x402/paid-risk-check/index.mjs";
import { analyzePaymentRisk } from "../x402/paid-risk-check/risk-check.mjs";

const SINGIT = "0xc2c1e0b7C401e6217193732272444D928646eba3";

test("analyzes a Base SINGIT payment requirement as a custom-token Permit2 payment", () => {
  const result = analyzePaymentRisk({
    url: "https://merchant.example/protected",
    paymentRequirements: {
      scheme: "upto",
      network: "eip155:8453",
      maxAmountRequired: "10000000000000000000",
      asset: SINGIT,
      payTo: "0x1111111111111111111111111111111111111111",
      resource: "https://merchant.example/protected",
      extra: {
        nonce: "risk-check-demo-1",
        assetTransferMethod: "permit2",
      },
    },
  });

  assert.equal(result.ok, true);
  assert.equal(result.product, "sign402-risk-check");
  assert.equal(result.riskLevel, "low");
  assert.equal(result.summary.network, "eip155:8453");
  assert.equal(result.summary.asset.symbol, "SINGIT");
  assert.equal(result.summary.asset.transferMethod, "permit2");
  assert.equal(result.summary.amount.display, "10 SINGIT");
  assert.equal(result.summary.receiver, "0x1111111111111111111111111111111111111111");
  assert.equal(
    result.checks.find((check) => check.name === "custom_token")?.status,
    "warning",
  );
  assert.equal(
    result.checks.find((check) => check.name === "replay_protection")?.status,
    "pass",
  );
});

test("marks missing receiver and replay protection as high risk", () => {
  const result = analyzePaymentRisk({
    paymentRequirements: {
      network: "base",
      asset: SINGIT,
      maxAmountRequired: "1000000000000000000",
    },
  });

  assert.equal(result.riskLevel, "high");
  assert.equal(
    result.checks.find((check) => check.name === "receiver_present")?.status,
    "fail",
  );
  assert.equal(
    result.checks.find((check) => check.name === "replay_protection")?.status,
    "fail",
  );
  assert.match(result.recommendation, /Do not approve/i);
});

test("handler rejects an empty request body", async () => {
  const response = await handler(
    new Request("https://x402.bankr.bot/demo/paid-risk-check", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    ok: false,
    error: "paymentRequirements is required",
  });
});
