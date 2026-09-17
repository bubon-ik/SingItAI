import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, chmodSync } from 'node:fs';
import path from 'node:path';
import { ClientError } from './config.mjs';

export class Store {
  constructor(directory) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const file = path.join(directory, 'operations.sqlite3');
    this.db = new DatabaseSync(file);
    chmodSync(file, 0o600);
    this.db.exec(`PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, document TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS attempts (quote_id TEXT PRIMARY KEY REFERENCES quotes(id), payer TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('sending','uncertain','confirmed','failed')), message_hash TEXT NOT NULL, transaction_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_per_payer ON attempts(payer) WHERE state IN ('sending','uncertain');`);
  }
  close() { this.db.close(); }
  saveQuote(quote) { this.db.prepare('INSERT INTO quotes VALUES (?, ?)').run(quote.id, JSON.stringify(quote)); }
  quote(id) {
    const row = this.db.prepare('SELECT document FROM quotes WHERE id=?').get(id);
    if (!row) throw new ClientError('QUOTE_NOT_FOUND', 'Quote not found. Run quote first.');
    return JSON.parse(row.document);
  }
  attempt(id) { return this.db.prepare('SELECT * FROM attempts WHERE quote_id=?').get(id) || null; }
  unresolved(payer) { return this.db.prepare("SELECT quote_id FROM attempts WHERE payer=? AND state IN ('sending','uncertain')").get(payer) || null; }
  claim(id, payer, messageHash) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      if (this.attempt(id)) throw new ClientError('ALREADY_ATTEMPTED', 'This quote already has a payment attempt. Use reconcile; do not pay it again.');
      if (this.unresolved(payer)) throw new ClientError('UNRESOLVED_PAYMENT', 'Resolve the previous payment before starting another top-up.');
      const now = new Date().toISOString();
      this.db.prepare('INSERT INTO attempts VALUES (?, ?, ?, ?, NULL, ?, ?)').run(id, payer, 'sending', messageHash, now, now);
      this.db.exec('COMMIT');
    } catch (e) { this.db.exec('ROLLBACK'); throw e; }
  }
  update(id, state, transaction = null) {
    this.db.prepare('UPDATE attempts SET state=?, transaction_id=COALESCE(?, transaction_id), updated_at=? WHERE quote_id=?').run(state, transaction, new Date().toISOString(), id);
  }
}
