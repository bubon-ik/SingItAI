import assert from "node:assert/strict";
import test from "node:test";
import { recoverTypedDataAddress } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { CDP_NETWORK, cdpClients, cdpSigner, resolveCdpAccount } from "../src/cdp.mjs";
import { USDC_DOMAIN } from "../src/chain.mjs";
import { TRANSFER_WITH_AUTHORIZATION_TYPES } from "../src/preflight.mjs";
import { signPayment } from "../src/client.mjs";
import { buildPaymentRequirements, decodePaymentHeader } from "../src/x402.mjs";

const ADDRESS = "0x3333333333333333333333333333333333333333";

function fakeAccount({ result = { transactionHash: "0xabc" }, address = ADDRESS } = {}) {
  const sent = [];
  return {
    sent,
    account: {
      address,
      async sendTransaction(options) {
        sent.push(options);
        if (result instanceof Error) throw result;
        return result;
      },
    },
  };
}

test("a transaction is sent on Base, in the shape CDP expects", async () => {
  const { account, sent } = fakeAccount();
  const { walletClient } = cdpClients(account)();

  const hash = await walletClient.sendTransaction({ to: "0xdead", data: "0xbeef" });

  assert.equal(hash, "0xabc");
  assert.equal(sent[0].network, CDP_NETWORK);
  assert.deepEqual(sent[0].transaction, { to: "0xdead", data: "0xbeef" });
});

test("both spellings of the hash are accepted", async () => {
  // The SDK has used transactionHash and hash; cdp-x402-service reads both.
  const { account } = fakeAccount({ result: { hash: "0xfeed" } });
  const { walletClient } = cdpClients(account)();

  assert.equal(await walletClient.sendTransaction({ to: "0x1", data: "0x2" }), "0xfeed");
});

test("a send that returns no hash fails loudly", async () => {
  const { account } = fakeAccount({ result: {} });
  const { walletClient } = cdpClients(account)();

  await assert.rejects(
    walletClient.sendTransaction({ to: "0x1", data: "0x2" }),
    /no transaction hash/,
  );
});

test("value is omitted rather than sent as undefined", async () => {
  const { account, sent } = fakeAccount();
  const { walletClient } = cdpClients(account)();

  await walletClient.sendTransaction({ to: "0x1", data: "0x2" });

  assert.equal("value" in sent[0].transaction, false);
});

test("an account with no address is refused before anything is sent", () => {
  assert.throws(() => cdpClients({}), /address is required/);
});

// -- resolving the named account ----------------------------------------

test("the named account is resolved and reported", async () => {
  const client = { evm: { getOrCreateAccount: async ({ name }) => ({ address: ADDRESS, name }) } };

  const account = await resolveCdpAccount({ client, name: "stocks-seller" });

  assert.equal(account.address, ADDRESS);
});

test("a name that resolves to an unexpected address stops the server", async () => {
  // A typo in the name would otherwise quietly spend from a different wallet —
  // and this repository already has one production account worth not touching.
  const client = { evm: { getOrCreateAccount: async () => ({ address: ADDRESS }) } };

  await assert.rejects(
    resolveCdpAccount({ client, name: "typo", expectedAddress: "0x" + "9".repeat(40) }),
    /not the expected/,
  );
});

test("no name is a configuration error, not a default", async () => {
  await assert.rejects(resolveCdpAccount({ client: {}, name: "" }), /name is required/);
});

// -- the buyer signing through CDP --------------------------------------

test("a key-less signer can pay, and the signature still recovers", async () => {
  // A viem account has the same shape a CDP account presents: an address and a
  // viem-flavoured signTypedData. If this recovers, CDP's will too.
  const local = privateKeyToAccount("0x" + "44".repeat(32));
  const signer = cdpSigner(local);

  const requirements = buildPaymentRequirements({
    resource: "https://x.test/paid/buy/NVDA?usd=1",
    priceAtomic: "1010000",
    description: "buy",
    payTo: "0x2222222222222222222222222222222222222222",
  });
  const { payload } = decodePaymentHeader(await signPayment(requirements, signer));

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

  assert.equal(recovered.toLowerCase(), local.address.toLowerCase());
});

test("something that is neither a key nor a signer is refused", async () => {
  await assert.rejects(
    signPayment({ maxAmountRequired: "1", network: "eip155:8453" }, { address: "0x1" }),
    /private key or a signer/,
  );
});

// -- settling without a key ---------------------------------------------

test("a wallet with no key to give can still settle", async () => {
  // The bug this covers: the swap was wired to the CDP wallet and settlement
  // was not, so a CDP deployment answered facilitator_not_configured to every
  // paid request while /health cheerfully reported payable: true.
  const { settlePayment } = await import("../src/x402.mjs");
  const sent = [];

  const result = await settlePayment(
    {
      authorization: {
        from: "0x1111111111111111111111111111111111111111",
        to: "0x2222222222222222222222222222222222222222",
        value: "1010000",
        validAfter: "0",
        validBefore: "1900000000",
        nonce: "0x" + "11".repeat(32),
      },
      signature: "0x" + "22".repeat(65),
    },
    { asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" },
    {
      privateKey: undefined,
      walletClient: {
        async sendTransaction(tx) {
          sent.push(tx);
          return "0xsettled";
        },
      },
      publicClient: {
        async waitForTransactionReceipt() {
          return { status: "success" };
        },
      },
    },
  );

  assert.equal(result.success, true);
  assert.equal(result.transaction, "0xsettled");
  assert.equal(sent.length, 1, "the authorization was broadcast");
  assert.match(sent[0].data, /^0xe3ee160e/, "transferWithAuthorization");
});

test("no key and no wallet is still a clear refusal", async () => {
  const { settlePayment } = await import("../src/x402.mjs");

  const result = await settlePayment({}, {}, { privateKey: undefined });

  assert.equal(result.success, false);
  assert.equal(result.errorReason, "facilitator_not_configured");
  assert.match(result.message, /BASE_CDP_ACCOUNT_NAME/, "names both ways to fix it");
});
