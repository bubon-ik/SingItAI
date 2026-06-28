import assert from "node:assert/strict";
import test from "node:test";

import handler from "../x402/buy-bitrefill/index.mjs";

test("buy-bitrefill requires quoteId and fulfillmentToken", async () => {
  const response = await handler(
    new Request("https://bankr.example/buy-bitrefill", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ quoteId: "quote_1" }),
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    ok: false,
    error: "quoteId and fulfillmentToken are required",
  });
});

test("buy-bitrefill prepares settlement without fulfilling Bitrefill and sets x402 settle amount", async () => {
  const originalFetch = globalThis.fetch;
  process.env.SIGN402_GATEWAY_INTERNAL_URL = "https://gateway.example";
  process.env.SIGN402_BANKR_FULFILLMENT_SECRET = "secret_123";
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return Response.json({
      ok: true,
      quoteId: "quote_1",
      orderId: "order_1",
      status: "delivered",
      pricingMode: "bankr_real_rate",
      settleAmountAtomic: "2625000000000000000000",
    });
  };

  try {
    const response = await handler(
      new Request("https://bankr.example/buy-bitrefill", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          quoteId: "quote_1",
          fulfillmentToken: "fulfill_1",
        }),
      }),
    );

    assert.equal(response.status, 200);
    assert.equal(response.headers.get("X-402-Settle-Amount"), "2625000000000000000000");
    assert.deepEqual(await response.json(), {
      ok: true,
      quoteId: "quote_1",
      orderId: "order_1",
      status: "delivered",
      pricingMode: "bankr_real_rate",
      settleAmountAtomic: "2625000000000000000000",
    });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://gateway.example/internal/prepare-bitrefill-settlement");
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers.authorization, "Bearer secret_123");
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      quoteId: "quote_1",
      fulfillmentToken: "fulfill_1",
    });
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.SIGN402_GATEWAY_INTERNAL_URL;
    delete process.env.SIGN402_BANKR_FULFILLMENT_SECRET;
  }
});
