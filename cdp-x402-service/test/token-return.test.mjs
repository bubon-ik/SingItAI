import test from "node:test";
import assert from "node:assert/strict";

import { decodeFunctionData, erc20Abi } from "viem";

import { returnErc20 } from "../src/token-return.mjs";


const TOKEN = "0xc2c1e0b7C401e6217193732272444D928646eba3";
const RECIPIENT = "0x2A52e5eA26013bdCDCfEf8b71d700A6cDc918423";
const HASH = `0x${"1".repeat(64)}`;


test("token return sends the exact atomic amount and waits for success", async () => {
  const sent = [];
  const waited = [];
  const account = {
    address: "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
    sendTransaction: async (request) => {
      sent.push(request);
      return { transactionHash: HASH };
    },
  };
  const publicClient = {
    waitForTransactionReceipt: async (request) => {
      waited.push(request);
      return { status: "success" };
    },
  };

  const result = await returnErc20({
    account,
    publicClient,
    token: TOKEN,
    to: RECIPIENT,
    amountAtomic: "17220296305476533495957212",
    network: "base",
    idempotencyKey: "bitrefill-return:quote_1",
  });

  assert.equal(sent.length, 1);
  assert.equal(sent[0].network, "base");
  assert.equal(sent[0].transaction.to, TOKEN);
  assert.equal(sent[0].idempotencyKey, "bitrefill-return:quote_1");
  const decoded = decodeFunctionData({
    abi: erc20Abi,
    data: sent[0].transaction.data,
  });
  assert.equal(decoded.functionName, "transfer");
  assert.equal(decoded.args[0], RECIPIENT);
  assert.equal(decoded.args[1], 17220296305476533495957212n);
  assert.deepEqual(waited, [{ hash: HASH }]);
  assert.equal(result.ok, true);
  assert.equal(result.transactionHash, HASH);
  assert.equal(result.amountAtomic, "17220296305476533495957212");
});


test("an attribution suffix is appended without changing the transfer", async () => {
  const sent = [];
  const account = {
    address: "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
    sendTransaction: async (request) => {
      sent.push(request);
      return { transactionHash: HASH };
    },
  };
  const publicClient = {
    waitForTransactionReceipt: async () => ({ status: "success" }),
  };
  const suffix = `0x${"8021".repeat(4)}`;

  await returnErc20({
    account,
    publicClient,
    token: TOKEN,
    to: RECIPIENT,
    amountAtomic: "1",
    network: "base",
    idempotencyKey: "bitrefill-return:quote_1",
    dataSuffix: suffix,
  });

  const data = sent[0].transaction.data;
  assert.ok(data.endsWith(suffix.slice(2)));
  const decoded = decodeFunctionData({ abi: erc20Abi, data });
  assert.equal(decoded.functionName, "transfer");
  assert.equal(decoded.args[0], RECIPIENT);
  assert.equal(decoded.args[1], 1n);
});


test("a malformed attribution suffix is dropped, not sent", async () => {
  const sent = [];
  const account = {
    address: "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
    sendTransaction: async (request) => {
      sent.push(request);
      return { transactionHash: HASH };
    },
  };
  const publicClient = {
    waitForTransactionReceipt: async () => ({ status: "success" }),
  };

  for (const bad of ["not-hex", "0x123", "", null, 42]) {
    sent.length = 0;
    await returnErc20({
      account,
      publicClient,
      token: TOKEN,
      to: RECIPIENT,
      amountAtomic: "1",
      network: "base",
      idempotencyKey: "bitrefill-return:quote_1",
      dataSuffix: bad,
    });
    assert.equal(sent[0].transaction.data.length, 138);
  }
});


test("token return never reports success for a reverted receipt", async () => {
  const account = {
    address: "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
    sendTransaction: async () => ({ transactionHash: HASH }),
  };
  const publicClient = {
    waitForTransactionReceipt: async () => ({ status: "reverted" }),
  };

  await assert.rejects(
    returnErc20({
      account,
      publicClient,
      token: TOKEN,
      to: RECIPIENT,
      amountAtomic: "1",
      network: "base",
      idempotencyKey: "bitrefill-return:quote_1",
    }),
    /reverted/,
  );
});


test("token return rejects unsafe inputs before broadcasting", async () => {
  let sendCalls = 0;
  const account = {
    address: "0x84C0f9cd76b351e4dc90B0dD70Fa85b8aCC2b9dd",
    sendTransaction: async () => {
      sendCalls += 1;
      return { transactionHash: HASH };
    },
  };
  const publicClient = {
    waitForTransactionReceipt: async () => ({ status: "success" }),
  };
  const invalidCases = [
    { token: "not-an-address" },
    { to: "not-an-address" },
    { amountAtomic: "0" },
    { amountAtomic: "-1" },
    { amountAtomic: "1.5" },
    { idempotencyKey: "" },
    { idempotencyKey: "x".repeat(129) },
  ];

  for (const overrides of invalidCases) {
    await assert.rejects(
      returnErc20({
        account,
        publicClient,
        token: TOKEN,
        to: RECIPIENT,
        amountAtomic: "1",
        network: "base",
        idempotencyKey: "bitrefill-return:quote_1",
        ...overrides,
      }),
    );
  }

  assert.equal(sendCalls, 0);
});
