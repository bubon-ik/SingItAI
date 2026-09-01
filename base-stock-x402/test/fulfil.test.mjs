import assert from "node:assert/strict";
import test from "node:test";

import { fulfilOrder, deliveredAmount, swapCalldata } from "../src/fulfil.mjs";
import { ROUTER_EQUITY, USDC } from "../src/chain.mjs";
import { NVDA, PAYER, deps, requirements, transferLog } from "./fakes.mjs";

const AMOUNT_IN = "100000000"; // $100 of USDC, six decimals

function order(extra = {}) {
  const built = deps(extra);
  return {
    ...built,
    run: () =>
      fulfilOrder({
        payload: { authorization: {}, signature: "0x" },
        requirements: requirements(),
        market: NVDA,
        amountInAtomic: AMOUNT_IN,
        privateKey: "0xkey",
        now: () => 1_800_000_000,
        deps: built.deps,
      }),
  };
}

test("a delivered order reports what actually arrived, not what was quoted", async () => {
  const { run, wallet } = order({
    allowance: 10n ** 18n,
    receipts: [{ status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] }],
  });

  const result = await run();

  assert.equal(result.ok, true);
  assert.equal(result.deliveredAtomic, "45000000");
  assert.equal(result.payer, PAYER);
  assert.equal(result.symbol, "NVDAc");
  // The estimate is reported beside the fill rather than instead of it.
  assert.notEqual(result.expectedAtomic, result.deliveredAtomic);
  assert.equal(wallet.sent.length, 1, "one transaction: the swap");
});

test("the shares are sent to the payer, never to us", async () => {
  const { run, wallet } = order({
    allowance: 10n ** 18n,
    receipts: [{ status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] }],
  });

  await run();

  const swap = wallet.sent[0];
  assert.equal(swap.to, ROUTER_EQUITY);
  // Word 3 of the tuple is the recipient.
  const recipient = "0x" + swap.data.slice(10 + 3 * 64, 10 + 4 * 64).slice(24);
  assert.equal(recipient.toLowerCase(), PAYER.toLowerCase());
});

test("an invalid payment costs the buyer nothing and never reaches settlement", async () => {
  const { run, calls, wallet } = order({ valid: false, invalidReason: "the signature does not match" });

  const result = await run();

  assert.equal(result.ok, false);
  assert.equal(result.stage, "pre-settlement");
  assert.equal(result.charged, false);
  assert.equal(calls.settle, 0);
  assert.equal(wallet.sent.length, 0);
});

test("a failed settlement is still a free refusal", async () => {
  const { run, wallet } = order({ settleOk: false });

  const result = await run();

  assert.equal(result.ok, false);
  assert.equal(result.stage, "pre-settlement");
  assert.equal(result.charged, false);
  // Nothing was bought, so nothing needs refunding.
  assert.equal(wallet.sent.length, 0);
});

test("a reverted swap refunds the payer everything they paid", async () => {
  const { run, wallet } = order({
    allowance: 10n ** 18n,
    receipts: [
      { status: "reverted" },
      { status: "success" }, // the refund
    ],
  });

  const result = await run();

  assert.equal(result.ok, false);
  assert.equal(result.stage, "delivery");
  assert.equal(result.needsOperator, false);
  assert.equal(result.refunded, "101.000000 USDC");

  const refund = wallet.sent[1];
  assert.equal(refund.to, USDC.address);
  assert.match(refund.data, /^0xa9059cbb/, "an ERC-20 transfer");
});

test("a swap that delivers nothing to the payer is treated as a failure", async () => {
  const { run } = order({
    allowance: 10n ** 18n,
    receipts: [
      { status: "success", logs: [] }, // mined, but nothing reached the payer
      { status: "success" },
    ],
  });

  const result = await run();

  assert.equal(result.ok, false);
  assert.match(result.error, /delivered nothing/);
  assert.equal(result.refunded, "101.000000 USDC");
});

test("a refund that fails says a human has to look at it", async () => {
  const { run } = order({
    allowance: 10n ** 18n,
    receipts: [{ status: "reverted" }, { throws: "no gas" }],
  });

  const result = await run();

  assert.equal(result.needsOperator, true);
  assert.equal(result.refunded, null);
});

test("the router is approved for this order only, never for everything", async () => {
  const { run, wallet } = order({
    allowance: 0n,
    receipts: [
      { status: "success" }, // approve
      { status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] },
    ],
  });

  await run();

  const approve = wallet.sent[0];
  assert.equal(approve.to, USDC.address);
  const approved = BigInt("0x" + approve.data.slice(10 + 64, 10 + 128));
  assert.equal(approved, BigInt(AMOUNT_IN), "approved exactly the order, not MAX_UINT256");
});

test("an existing allowance is not re-approved", async () => {
  const { run, wallet } = order({
    allowance: 10n ** 18n,
    receipts: [{ status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] }],
  });

  await run();

  assert.equal(wallet.sent.length, 1);
});

test("only transfers of the right token to the right address are counted", () => {
  const other = "0x9999999999999999999999999999999999999999";
  const receipt = {
    logs: [
      transferLog(NVDA.token, other, 99_000_000n),
      transferLog(USDC.address, PAYER, 12_000_000n),
      transferLog(NVDA.token, PAYER, 45_000_000n),
    ],
  };

  assert.equal(deliveredAmount(receipt, NVDA, PAYER), 45_000_000n);
});

test("the swap tuple carries tickSpacing, not a Uniswap fee tier", () => {
  const data = swapCalldata({
    market: NVDA,
    recipient: PAYER,
    amountIn: 100_000_000n,
    minOut: 44_855_285n,
    deadline: 1_800_000_300n,
  });

  assert.match(data, /^0xa026383e/);
  assert.equal((data.length - 10) / 64, 8, "eight static words");
  const third = BigInt("0x" + data.slice(10 + 2 * 64, 10 + 3 * 64));
  assert.equal(third, 10n, "tickSpacing 10 — a 500 here would be the Uniswap fee");
});


// -- the journal ---------------------------------------------------------

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Journal } from "../src/journal.mjs";

function journalled(extra = {}, { settleSees = null } = {}) {
  const log = new Journal({ path: join(mkdtempSync(join(tmpdir(), "fulfil-")), "orders.jsonl") });
  const built = deps(extra);
  if (settleSees) {
    const original = built.deps.settle;
    built.deps.settle = async (...args) => {
      settleSees(log.read());
      return original(...args);
    };
  }
  return {
    log,
    ...built,
    run: () =>
      fulfilOrder({
        payload: { authorization: { nonce: "0xnonce" }, signature: "0x" },
        requirements: requirements(),
        market: NVDA,
        amountInAtomic: AMOUNT_IN,
        privateKey: "0xkey",
        now: () => 1_800_000_000,
        journal: log,
        deps: built.deps,
      }),
  };
}

test("the intent is on disk before the settlement is broadcast", async () => {
  let seen = null;
  const { run } = journalled(
    { allowance: 10n ** 18n, receipts: [{ status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] }] },
    { settleSees: (entries) => { seen = entries; } },
  );

  await run();

  // This is the whole reason the journal exists: a crash here must leave a
  // record that a payment was about to be taken.
  assert.equal(seen.length, 1);
  assert.equal(seen[0].step, "intent");
  assert.equal(seen[0].payer, PAYER);
  assert.equal(seen[0].nonce, "0xnonce", "payer + nonce is the recovery key");
});

test("a delivered order leaves nothing unresolved", async () => {
  const { run, log } = journalled({
    allowance: 10n ** 18n,
    receipts: [{ status: "success", logs: [transferLog(NVDA.token, PAYER, 45_000_000n)] }],
  });

  await run();

  assert.deepEqual(log.unresolved(), []);
  assert.deepEqual(log.read().map((e) => e.step), ["intent", "settled", "delivered"]);
});

test("a refunded order leaves nothing unresolved either", async () => {
  const { run, log } = journalled({
    allowance: 10n ** 18n,
    receipts: [{ status: "reverted" }, { status: "success" }],
  });

  await run();

  assert.deepEqual(log.unresolved(), []);
  assert.deepEqual(log.read().map((e) => e.step), ["intent", "settled", "refunded"]);
});

test("a failed refund is recorded as stranded, with the payer and the amount", async () => {
  const { run, log } = journalled({
    allowance: 10n ** 18n,
    receipts: [{ status: "reverted" }, { throws: "no gas" }],
  });

  await run();

  const last = log.read().at(-1);
  assert.equal(last.step, "stranded");
  assert.equal(last.payer, PAYER);
  assert.equal(last.amountAtomic, "101000000");
});

test("an unpaid probe writes nothing at all", async () => {
  const { run, log } = journalled({ valid: false, invalidReason: "no signature" });

  await run();

  // Every 402 challenge in the world would otherwise land in this file.
  assert.deepEqual(log.read(), []);
});

test("a settlement that never landed is closed, not left dangling", async () => {
  const { run, log } = journalled({ settleOk: false });

  await run();

  assert.deepEqual(log.read().map((e) => e.step), ["intent", "abandoned"]);
  assert.deepEqual(log.unresolved(), []);
});


// -- broadcast succeeded, confirmation did not ---------------------------

test("an unconfirmed settlement is never reported as 'you were not charged'", async () => {
  // What actually happened: the RPC we defaulted to serves eth_call and
  // eth_getLogs but rejects eth_getTransactionReceipt. Four settlements landed
  // on chain and all four were journalled as abandoned.
  const built = deps({ allowance: 10n ** 18n });
  built.deps.settle = async () => ({
    success: false,
    errorReason: "confirmation_unknown",
    message: "Invalid parameters were provided to the RPC method.",
    charged: null,
    transaction: "0xsent",
    payer: PAYER,
    value: "101000000",
  });
  const log = new Journal({ path: join(mkdtempSync(join(tmpdir(), "unconf-")), "orders.jsonl") });

  const result = await fulfilOrder({
    payload: { authorization: { nonce: "0xn" }, signature: "0x" },
    requirements: requirements(),
    market: NVDA,
    amountInAtomic: AMOUNT_IN,
    privateKey: "0xkey",
    journal: log,
    deps: built.deps,
  });

  assert.equal(result.ok, false);
  assert.equal(result.stage, "settlement-unknown");
  assert.equal(result.needsOperator, true);
  assert.equal(result.settlementTx, "0xsent");
  assert.notEqual(result.charged, false, "must not claim the buyer was spared");

  const last = log.read().at(-1);
  assert.equal(last.step, "stranded", "a human has to resolve this, not a retry");
  assert.equal(last.tx, "0xsent");
  assert.equal(last.amountAtomic, "101000000");
});

test("a send that never left is still a free refusal", async () => {
  const built = deps({ allowance: 10n ** 18n });
  built.deps.settle = async () => ({
    success: false,
    errorReason: "broadcast_failed",
    message: "connection refused",
    charged: false,
    transaction: null,
  });
  const log = new Journal({ path: join(mkdtempSync(join(tmpdir(), "nosend-")), "orders.jsonl") });

  const result = await fulfilOrder({
    payload: { authorization: { nonce: "0xn" }, signature: "0x" },
    requirements: requirements(),
    market: NVDA,
    amountInAtomic: AMOUNT_IN,
    privateKey: "0xkey",
    journal: log,
    deps: built.deps,
  });

  assert.equal(result.stage, "pre-settlement");
  assert.equal(result.charged, false);
  assert.equal(log.read().at(-1).step, "abandoned");
});
