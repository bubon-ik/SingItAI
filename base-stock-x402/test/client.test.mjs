import assert from "node:assert/strict";
import test from "node:test";
import { recoverTypedDataAddress } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { USDC, USDC_DOMAIN } from "../src/chain.mjs";
import { TRANSFER_WITH_AUTHORIZATION_TYPES } from "../src/preflight.mjs";
import { fetchWithPayment, readRequirements, signPayment } from "../src/client.mjs";
import { buildPaymentRequirements, decodePaymentHeader } from "../src/x402.mjs";

const KEY = "0x" + "22".repeat(32);
const ACCOUNT = privateKeyToAccount(KEY);
const PAY_TO = "0x2222222222222222222222222222222222222222";

function requirements(overrides = {}) {
  return {
    ...buildPaymentRequirements({
      resource: "https://x.test/paid/buy/NVDA?usd=100",
      priceAtomic: "101000000",
      description: "buy",
      payTo: PAY_TO,
    }),
    ...overrides,
  };
}

test("what the buyer signs is what the seller verifies", async () => {
  // The one failure that looks like the payer's fault and is not: a signature
  // made under a different domain recovers to a different address entirely.
  const header = await signPayment(requirements(), KEY);
  const { payload } = decodePaymentHeader(header);

  const recovered = await recoverTypedDataAddress({
    domain: USDC_DOMAIN,
    types: TRANSFER_WITH_AUTHORIZATION_TYPES,
    primaryType: "TransferWithAuthorization",
    message: {
      from: payload.authorization.from,
      to: payload.authorization.to,
      value: BigInt(payload.authorization.value),
      validAfter: BigInt(payload.authorization.validAfter),
      validBefore: BigInt(payload.authorization.validBefore),
      nonce: payload.authorization.nonce,
    },
    signature: payload.signature,
  });

  assert.equal(recovered.toLowerCase(), ACCOUNT.address.toLowerCase());
});

test("the authorization pays the seller, for the price asked, with an expiry", async () => {
  const header = await signPayment(requirements(), KEY, { now: 1_800_000_000 });
  const { payload } = decodePaymentHeader(header);

  assert.equal(payload.authorization.to, PAY_TO);
  assert.equal(payload.authorization.value, "101000000");
  assert.equal(payload.authorization.validBefore, String(1_800_000_000 + 300));
  assert.match(payload.authorization.nonce, /^0x[0-9a-f]{64}$/);
});

test("two payments never reuse a nonce", async () => {
  const a = decodePaymentHeader(await signPayment(requirements(), KEY));
  const b = decodePaymentHeader(await signPayment(requirements(), KEY));

  assert.notEqual(a.payload.authorization.nonce, b.payload.authorization.nonce);
});

// -- reading the challenge in either form --------------------------------

function response({ body, header, status = 402 }) {
  const headers = new Map(header ? [["payment-required", header]] : []);
  const res = {
    status,
    headers: { get: (k) => headers.get(k.toLowerCase()) ?? null },
    clone: () => res,
    json: async () => {
      if (body === undefined) throw new Error("no body");
      return body;
    },
  };
  return res;
}

test("terms are read from the body when the seller sends a body", async () => {
  const req = requirements();
  const got = await readRequirements(response({ body: { accepts: [req] } }));
  assert.equal(got.payTo, PAY_TO);
});

test("terms are read from the header when the body has none", async () => {
  // Exactly the shape Massive answers with.
  const req = requirements();
  const encoded = Buffer.from(JSON.stringify({ accepts: [req] }), "utf8").toString("base64");
  const got = await readRequirements(
    response({ body: { message: "See the PAYMENT-REQUIRED header.", status: "PAYMENT_REQUIRED" }, header: encoded }),
  );

  assert.equal(got.payTo, PAY_TO, "a header-only seller must still be payable");
});

test("a 402 with neither form is refused rather than guessed at", async () => {
  assert.equal(await readRequirements(response({ body: { error: "pay me" } })), null);
});

// -- the buyer-side ceiling ---------------------------------------------

test("a price above the buyer's ceiling is refused before anything is signed", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({
    status: 402,
    headers: { get: () => null },
    clone() { return this; },
    json: async () => ({ accepts: [requirements({ maxAmountRequired: "900000000" })] }),
  });
  try {
    await assert.rejects(
      fetchWithPayment("https://x.test/paid/buy/NVDA?usd=100", {}, {
        privateKey: KEY,
        maxAmountAtomic: "101000000",
      }),
      /exceeds the allowed maximum/,
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("a non-402 response is passed straight back", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ status: 200, headers: { get: () => null } });
  try {
    const res = await fetchWithPayment("https://x.test/quote/NVDA", {}, { privateKey: KEY });
    assert.equal(res.status, 200);
  } finally {
    globalThis.fetch = original;
  }
});

test("the payment names USDC on Base, not some other asset", async () => {
  const header = await signPayment(requirements(), KEY);
  const payload = decodePaymentHeader(header);

  assert.equal(payload.network, "eip155:8453");
  assert.equal(payload.scheme, "exact");
  assert.equal(requirements().asset, USDC.address);
});
