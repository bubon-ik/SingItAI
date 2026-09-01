/**
 * The record that survives the process.
 *
 * Between taking a buyer's USDC and handing them shares there is a window in
 * which this service owes somebody something. If it dies inside that window
 * with the only record in an HTTP response nobody received, the debt still
 * exists and nothing here knows about it.
 *
 * So every order writes an `intent` line to disk, flushed, *before* the
 * settlement is broadcast — and each later step appends its own line. The file
 * is append-only and one JSON object per line, because the failure this exists
 * for is a crash: a format that needs a clean close is a format that loses the
 * last record exactly when it matters.
 *
 * The chain remains the truth. This is the index that tells an operator where
 * to look.
 */

import { appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, writeSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

// A step that is not one of these ends the order with money unaccounted for,
// so `unresolved()` reports anything that stops earlier.
export const TERMINAL = new Set(["delivered", "refunded", "abandoned", "stranded"]);

export const DEFAULT_JOURNAL_PATH = process.env.BASE_ORDER_JOURNAL
  || resolve(process.env.HOME || ".", ".base-stock-x402", "orders.jsonl");

export function newOrderId() {
  return randomUUID();
}

export class Journal {
  constructor({ path = DEFAULT_JOURNAL_PATH, now = () => Date.now() } = {}) {
    this.path = path;
    this.now = now;
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    if (!existsSync(path)) {
      // Payer addresses and amounts. Not secret, but not world-readable either.
      closeSync(openSync(path, "a", 0o600));
    }
  }

  /**
   * Append one line and flush it.
   *
   * The fsync is the whole point: a buffered write that is lost in the crash
   * it was recording is worse than no journal, because it looks like one.
   */
  append(entry) {
    const line = JSON.stringify({ at: this.now(), ...entry });
    const fd = openSync(this.path, "a", 0o600);
    try {
      writeSync(fd, line + "\n");
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    return entry;
  }

  read() {
    if (!existsSync(this.path)) return [];
    return readFileSync(this.path, "utf8")
      .split("\n")
      .filter(Boolean)
      .flatMap((line) => {
        try {
          return [JSON.parse(line)];
        } catch {
          // A half-written last line is exactly what a crash leaves behind.
          // Skipping it is right; refusing to read the file because of it is not.
          return [];
        }
      });
  }

  /** Every order whose last recorded step is not an ending. */
  unresolved() {
    const last = new Map();
    for (const entry of this.read()) {
      if (!entry.orderId) continue;
      last.set(entry.orderId, entry);
    }
    return [...last.values()].filter((entry) => !TERMINAL.has(entry.step));
  }
}

/** A journal that writes nowhere, for tests and for `verify`-only deployments. */
export const nullJournal = {
  path: null,
  append: (entry) => entry,
  read: () => [],
  unresolved: () => [],
};
