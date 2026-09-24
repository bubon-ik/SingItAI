"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { Observable, of } = require("rxjs");
const { waitForDeviceAction, purchaseSigningAction } = require("./device-action.cjs");

const readableApproval = () => ({
  signingMethod: "personal_sign",
  domain: { name: "SingIt Spending Approval", version: "2", chainId: 8453 },
  displayText: "SingIt purchase v2\nBuy: Crypto News\nPay: 0.001 USDC on Base",
});

test("purchase signs exact readable text, never a hash or typed data", () => {
  const approval = readableApproval();
  const action = {};
  const calls = [];
  assert.equal(purchaseSigningAction({
    signMessage: (...args) => { calls.push(args); return action; },
    signTypedData: () => assert.fail("typed data must never be requested"),
  }, "44'/60'/0'/0/0", approval), action);
  assert.deepEqual(calls, [["44'/60'/0'/0/0", approval.displayText]]);
});

test("old format, wrong chain, missing or non-displayable text cannot reach the device", () => {
  const good = readableApproval();
  for (const approval of [
    null, { ...good, signingMethod: "eth_signTypedData_v4" },
    { ...good, domain: { ...good.domain, version: "1" } },
    { ...good, domain: { ...good.domain, chainId: 1 } },
    { ...good, displayText: undefined }, { ...good, displayText: "0xabcdef" },
    { ...good, displayText: good.displayText + "\u202e" },
    { ...good, displayText: good.displayText + "a".repeat(2000) },
  ]) {
    assert.throws(() => purchaseSigningAction({
      signMessage: () => assert.fail("invalid input must not reach the device"),
    }, "path", approval), /readable v2/);
  }
});

test("hash-only fallback cancels and cannot return a later signature", async () => {
  let cancelled = 0;
  let unsubscribed = false;
  const lines = [];
  const action = {
    observable: new Observable((subscriber) => {
      subscriber.next({ status: "not-started" });
      subscriber.next({
        status: "pending",
        intermediateValue: { step: "signer.eth.steps.signTypedDataLegacy" },
      });
      subscriber.next({ status: "completed", output: { signature: "must-not-escape" } });
      subscriber.complete();
      return () => { unsubscribed = true; };
    }),
    cancel: () => { cancelled++; },
  };
  await assert.rejects(waitForDeviceAction(action, { log: (s) => lines.push(s) }), /hash-only/);
  assert.equal(cancelled, 1);
  assert.equal(unsubscribed, true);
  assert.match(lines.join("\n"), /signTypedDataLegacy/);
  assert.doesNotMatch(lines.join("\n"), /must-not-escape/);
});

test("normal typed-data action waits for completion and logs no payload", async () => {
  const lines = [];
  const result = { status: "completed", output: { signature: "private-result" } };
  const done = await waitForDeviceAction({
    observable: of(
      { status: "not-started" },
      { status: "pending", intermediateValue: { step: "signer.eth.steps.provideContext" } },
      { status: "pending", intermediateValue: { step: "signer.eth.steps.signTypedData" } },
      result
    ),
    cancel: () => assert.fail("successful action must not be cancelled"),
  }, { log: (s) => lines.push(s) });
  assert.equal(done, result);
  assert.match(lines.join("\n"), /provideContext/);
  assert.doesNotMatch(lines.join("\n"), /private-result/);
});

test("transport errors cancel without hiding the original error", async () => {
  await assert.rejects(waitForDeviceAction({
    observable: new Observable((subscriber) => subscriber.error(new Error("USB disconnected"))),
    cancel: () => { throw new Error("cancel failed"); },
  }), /USB disconnected/);
});

test("address reads and terminal device refusal remain supported", async () => {
  const address = { status: "completed", output: { address: "0x123" } };
  assert.equal(await waitForDeviceAction({ observable: of(address) }, { signing: false }), address);
  for (const status of ["error", "stopped"]) {
    const result = { status };
    assert.equal(await waitForDeviceAction({ observable: of(result) }), result);
  }
});
