import { test } from "node:test";
import assert from "node:assert/strict";
import { recoverTypedDataAddress } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { ENDPOINT, ASSET, PAY_TO, encode, decode, typedPayment, validateOffer, runTest } from "../public/payment.mjs";

// Public deterministic test key, used offline only. No RPC or live payment calls.
const account = privateKeyToAccount("0x" + "11".repeat(32));
const accepted = { scheme: "exact", network: "eip155:8453", amount: "3000", asset: ASSET,
  payTo: PAY_TO, maxTimeoutSeconds: 300, extra: { name: "USD Coin", version: "2" } };
const offer = { x402Version: 2, resource: { url: ENDPOINT }, accepts: [accepted] };

function setup({ offerOverride = offer, signatureError, paidError, switchAccount = false, chain = "0x2105" } = {}) {
  const calls = { requests: [], signatures: 0, submitted: 0, accounts: 0 };
  const provider = { async request({ method, params }) {
    if (method === "eth_accounts") { calls.accounts++; return [switchAccount && calls.accounts >= 3 ? PAY_TO : account.address]; }
    if (method === "eth_chainId") return chain;
    assert.equal(method, "eth_signTypedData_v4");
    calls.signatures++;
    if (signatureError) throw signatureError;
    assert.equal(params[0], account.address);
    return account.signTypedData(JSON.parse(params[1]));
  } };
  const fetchImpl = async (url, init) => {
    calls.requests.push({ url, init });
    assert.equal(url, ENDPOINT);
    assert.equal(init.redirect, "error");
    if (!init.headers["PAYMENT-SIGNATURE"]) return new Response("{}", { status: 402, headers: { "payment-required": encode(offerOverride) } });
    if (paidError) throw paidError;
    const receipt = { success: true, network: "eip155:8453", payer: account.address, transaction: "0x" + "aa".repeat(32) };
    return new Response(JSON.stringify({ choices: [{ message: { content: "OK" } }] }),
      { headers: { "payment-response": encode(receipt) } });
  };
  return { calls, args: { provider, payer: account.address, fetchImpl, onSubmitting() { calls.submitted++; } } };
}

test("browser test signs the same EIP-3009 type and domain as the official SDK", async () => {
  let sdkTyped;
  await new ExactEvmScheme({ address: account.address, signTypedData: async value => { sdkTyped = value; return "0x"; } })
    .createPaymentPayload(2, accepted);
  const typed = typedPayment(account.address, accepted, 1_800_000_000, "0x" + "12".repeat(32));
  assert.deepEqual(typed.domain, sdkTyped.domain);
  assert.deepEqual(typed.types.TransferWithAuthorization, sdkTyped.types.TransferWithAuthorization);
  assert.equal(typed.primaryType, sdkTyped.primaryType);
  assert.equal(typed.message.value, "3000");
  assert.equal(typed.message.validBefore, "1800000300");
});

test("one explicit signature submits once and returns a valid receipt and answer", async () => {
  const { calls, args } = setup();
  const result = await runTest(args);
  assert.equal(result.answer, "OK");
  assert.equal(calls.requests.length, 2);
  assert.equal(calls.signatures, 1);
  assert.equal(calls.submitted, 1);
  const payload = decode(calls.requests[1].init.headers["PAYMENT-SIGNATURE"]);
  const authorization = payload.payload.authorization;
  assert.deepEqual(payload.accepted, accepted);
  assert.equal(authorization.value, "3000");
  assert.equal(authorization.to, PAY_TO);
  assert.equal(calls.requests[0].init.body, calls.requests[1].init.body);
  const typed = typedPayment(account.address, accepted, 0, authorization.nonce);
  typed.message = authorization;
  assert.equal(await recoverTypedDataAddress({ ...typed, signature: payload.payload.signature }), account.address);
});

test("changed payment terms are refused before signing", async () => {
  for (const patch of [{ amount: "3001" }, { payTo: account.address }, { asset: account.address },
    { network: "eip155:1" }, { scheme: "upto" }, { maxTimeoutSeconds: 301 },
    { extra: { name: "USD Coin", version: "2", assetTransferMethod: "permit2" } }]) {
    const { calls, args } = setup({ offerOverride: { ...offer, accepts: [{ ...accepted, ...patch }] } });
    await assert.rejects(runTest(args));
    assert.equal(calls.signatures, 0);
    assert.equal(calls.submitted, 0);
  }
  assert.throws(() => validateOffer({ ...offer, resource: { url: "https://other.test" } }));
  assert.throws(() => validateOffer({ ...offer, accepts: [accepted, accepted] }));
});

test("account or chain changes never submit a payment", async () => {
  for (const options of [{ switchAccount: true }, { chain: "0x1" }]) {
    const { calls, args } = setup(options);
    await assert.rejects(runTest(args));
    assert.equal(calls.submitted, 0);
    assert.ok(calls.requests.length <= 1);
  }
});

test("cancelled signatures and failed paid requests are never retried", async () => {
  const rejected = setup({ signatureError: Object.assign(new Error("User rejected"), { code: 4001 }) });
  await assert.rejects(runTest(rejected.args));
  assert.equal(rejected.calls.submitted, 0);
  assert.equal(rejected.calls.requests.length, 1);
  const lost = setup({ paidError: new TypeError("Connection lost") });
  await assert.rejects(runTest(lost.args));
  assert.equal(lost.calls.requests.length, 2);
  assert.equal(lost.calls.submitted, 1);
});
