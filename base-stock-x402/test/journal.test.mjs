import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, appendFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Journal, newOrderId } from "../src/journal.mjs";

function journal() {
  const dir = mkdtempSync(join(tmpdir(), "journal-"));
  return new Journal({ path: join(dir, "orders.jsonl"), now: () => 1_800_000_000_000 });
}

test("a line is on disk before the next call returns", () => {
  const log = journal();
  log.append({ orderId: "a", step: "intent", payer: "0xabc" });

  const raw = readFileSync(log.path, "utf8");
  assert.match(raw, /"step":"intent"/);
  assert.match(raw, /"at":1800000000000/);
});

test("an order is unresolved until it reaches an ending", () => {
  const log = journal();
  log.append({ orderId: "a", step: "intent" });
  assert.equal(log.unresolved().length, 1);

  log.append({ orderId: "a", step: "settled", tx: "0x1" });
  assert.equal(log.unresolved().length, 1, "settled is the dangerous state, not an ending");

  log.append({ orderId: "a", step: "delivered", tx: "0x2" });
  assert.equal(log.unresolved().length, 0);
});

test("a refund closes an order just as delivery does", () => {
  const log = journal();
  log.append({ orderId: "a", step: "intent" });
  log.append({ orderId: "a", step: "settled" });
  log.append({ orderId: "a", step: "refunded", tx: "0x3" });

  assert.equal(log.unresolved().length, 0);
});

test("a failed refund stays unresolved so somebody sees it", () => {
  const log = journal();
  log.append({ orderId: "a", step: "settled" });
  log.append({ orderId: "a", step: "stranded", reason: "refund reverted" });

  // `stranded` is terminal for the state machine but is the one ending that
  // means a human owes somebody money, so it must be findable on its own.
  assert.equal(log.unresolved().length, 0);
  const stranded = log.read().filter((e) => e.step === "stranded");
  assert.equal(stranded.length, 1);
});

test("orders are tracked separately", () => {
  const log = journal();
  log.append({ orderId: "a", step: "settled" });
  log.append({ orderId: "b", step: "intent" });
  log.append({ orderId: "b", step: "delivered" });

  const open = log.unresolved();
  assert.equal(open.length, 1);
  assert.equal(open[0].orderId, "a");
});

test("a half-written last line does not hide the rest of the file", () => {
  const log = journal();
  log.append({ orderId: "a", step: "settled" });
  // What a crash mid-write actually leaves behind.
  appendFileSync(log.path, '{"orderId":"b","step":"int');

  const entries = log.read();
  assert.equal(entries.length, 1);
  assert.equal(log.unresolved()[0].orderId, "a");
});

test("the journal file is not world-readable", () => {
  const log = journal();
  log.append({ orderId: "a", step: "intent", payer: "0xabc" });

  const mode = statSync(log.path).mode & 0o777;
  assert.equal(mode & 0o077, 0, `mode was ${mode.toString(8)}`);
});

test("order ids are unique", () => {
  assert.notEqual(newOrderId(), newOrderId());
});
