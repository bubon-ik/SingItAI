import test from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dispatch } from '../src/gateway.mjs';
import { Store } from '../src/store.mjs';
import { directory, testWallet, challenge } from './helpers.mjs';

test('gateway rejects another wallet before any provider call', async () => {
  const wallet = await testWallet();
  await assert.rejects(dispatch({ payer: 'someone-else', operation: 'balance' }, { wallet }), { code: 'WRONG_WALLET' });
});

test('gateway quotes without paying and refuses a quote belonging to another payer', async t => {
  const wallet = await testWallet(), other = await testWallet();
  const dir = directory(), store = new Store(dir);
  t.after(() => { store.close(); rmSync(dir, { recursive: true }); });
  const venice = { challenge: async () => challenge(), submit: () => { throw new Error('must not pay'); } };
  const context = { wallet, store, venice };
  const quote = await dispatch({ payer: wallet.address, operation: 'quote' }, context);
  const status = await dispatch({ payer: wallet.address, operation: 'status', quoteId: quote.quoteId }, context);
  assert.equal(status.attempted, false);
  assert.equal(status.quote.payer, wallet.address);
  await assert.rejects(dispatch({ payer: other.address, operation: 'status', quoteId: quote.quoteId }, { ...context, wallet: other }), { code: 'WRONG_WALLET' });
});

test('gateway paid operation still requires the exact quote hash', async t => {
  const wallet = await testWallet(), dir = directory(), store = new Store(dir);
  t.after(() => { store.close(); rmSync(dir, { recursive: true }); });
  const context = { wallet, store, venice: { challenge: async () => challenge() } };
  const quote = await dispatch({ payer: wallet.address, operation: 'quote' }, context);
  await assert.rejects(dispatch({ payer: wallet.address, operation: 'pay', quoteId: quote.quoteId, approvalHash: 'b'.repeat(64) }, context), { code: 'APPROVAL_REQUIRED' });
  assert.equal(store.attempt(quote.quoteId), null);
});

test('gateway CLI redacts malformed secret input and never falls back to environment wallet', () => {
  const result = spawnSync(process.execPath, ['src/gateway.mjs'], {
    cwd: new URL('../', import.meta.url), input: JSON.stringify({ privateKey: 'PRIVATE SECRET MARKER', operation: 'balance' }),
    encoding: 'utf8', env: { ...process.env, NODE_NO_WARNINGS: '1', SOLANA_PRIVATE_KEY: 'ANOTHER SECRET MARKER' },
  });
  assert.equal(result.status, 1);
  assert.equal(JSON.parse(result.stdout).ok, false);
  assert.doesNotMatch(result.stdout + result.stderr, /SECRET MARKER/);
});
