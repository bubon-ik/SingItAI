import test from 'node:test';
import assert from 'node:assert/strict';
import { rmSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { getBase58Decoder } from '@solana/kit';
import { ExaClient, ExaPayments, EXA_URL, searchBody, selectExaRequirement, publicResults } from '../src/exa.mjs';
import { Store, NETWORK, USDC } from '../src/index.mjs';
import { dispatch } from '../src/gateway.mjs';
import { directory, testWallet, challenge, jsonResponse } from './helpers.mjs';

async function fixture(t) {
  const wallet = await testWallet(), dir = directory(), store = new Store(dir);
  t.after(() => { store.close(); rmSync(dir, { recursive: true }); });
  const terms = challenge({ amount: '7000' }); terms.resource.url = EXA_URL;
  const transaction = getBase58Decoder().decode(new Uint8Array(64).fill(3));
  let calls = 0, now = Date.now();
  const chain = {
    balances: async () => ({ usdcAtomic: '1000000' }),
    build: async () => ({ messageHash: 'signed-message-hash', payload: { fixture: 'signed-payload-secret' } }),
    verify: async (tx, hash) => { assert.equal(hash, 'signed-message-hash'); return { state: 'confirmed', transaction: tx }; },
  };
  const exa = new ExaClient({ fetcher: async (url, init) => {
    assert.equal(url, EXA_URL);
    assert.equal(init.headers.authorization, undefined);
    assert.equal(init.headers['X-Sign-In-With-X'], undefined);
    assert.equal(init.redirect, 'error');
    if (!init.headers['PAYMENT-SIGNATURE']) return jsonResponse({ x402Version: 2, accepts: [terms.requirement] }, 402,
      { 'payment-required': Buffer.from(JSON.stringify({ x402Version: 2, accepts: [terms.requirement], resource: terms.resource })).toString('base64') });
    calls++;
    assert.ok(store.unresolved(wallet.address)); // journal precedes submission
    return jsonResponse({ results: [{ url: 'https://example.com', text: 'excerpt-secret', title: 'Source' }] }, 200,
      { 'payment-response': Buffer.from(JSON.stringify({ success: true, network: NETWORK, transaction })).toString('base64') });
  } });
  const context = { wallet, store, chain, exa };
  const payments = new ExaPayments({ ...context, now: () => now });
  const authorization = { policyHash: 'a'.repeat(64), payer: wallet.address, recipient: terms.requirement.payTo, network: NETWORK, asset: USDC, endpoint: EXA_URL, maxPerCallAtomic: 20000, expiresAt: Math.floor(now/1000)+600 };
  const quote = await payments.prepare('query-private');
  const input = { ...quote, query: 'query-private', authorization };
  return { ...context, dir, terms, payments, input, transaction, calls: () => calls, advance: () => { now += 61000; } };
}

test('Exa bridge: fresh quote, bound authorization, chain proof, no stored query/results/payload', async t => {
  const f = await fixture(t);
  assert.equal(f.calls(), 0);
  const result = await dispatch({ ...f.input, operation: 'exa-search' }, f);
  assert.equal(result.state, 'confirmed');
  assert.equal(result.results.length, 1);
  await assert.rejects(f.payments.pay(f.input), { code: 'ALREADY_ATTEMPTED' });
  await f.payments.reconcile(f.input.quoteId);
  assert.equal(f.calls(), 1);
  const bytes = readFileSync(path.join(f.dir, 'operations.sqlite3'));
  for (const secret of ['query-private', 'excerpt-secret', 'signed-payload-secret']) assert.equal(bytes.includes(Buffer.from(secret)), false);
});

test('request/query/hash/policy binding rejects substitutions before payment', async t => {
  const f = await fixture(t);
  for (const override of [{ query: 'changed' }, { approvalHash: 'f'.repeat(64) }, { authorization: null },
    ...Object.entries({ payer: 'other', recipient: 'other', network: 'base', asset: 'other', endpoint: 'https://evil.test', expiresAt: 1, maxPerCallAtomic: 6999, policyHash: '' }).map(([k,v]) => ({ authorization: { ...f.input.authorization, [k]: v } }))]) {
    await assert.rejects(f.payments.pay({ ...f.input, ...override }));
  }
  assert.equal(f.calls(), 0);
  assert.equal(f.store.attempt(f.input.quoteId), null);
});

test('expiry and insufficient funds fail without a signed submission', async t => {
  const f = await fixture(t);
  f.chain.balances = async () => ({ usdcAtomic: '0' });
  await assert.rejects(f.payments.pay(f.input), { code: 'INSUFFICIENT_USDC' });
  f.advance();
  await assert.rejects(f.payments.pay(f.input), { code: 'QUOTE_EXPIRED' });
  assert.equal(f.calls(), 0);
});

test('network loss leaves a durable hold, reconciliation never resubmits', async t => {
  const f = await fixture(t);
  f.exa.request = async () => { throw new Error('response lost'); };
  await assert.rejects(f.payments.pay(f.input), { code: 'EXA_PAYMENT_UNCERTAIN' });
  assert.equal(f.store.attempt(f.input.quoteId).state, 'uncertain');
  await assert.rejects(f.payments.prepare('again'), { code: 'EXA_PAYMENT_PENDING' });
  const restarted = new ExaPayments(f);
  await assert.rejects(restarted.pay(f.input), { code: 'ALREADY_ATTEMPTED' });
  assert.equal((await restarted.reconcile(f.input.quoteId, f.transaction)).state, 'confirmed');
});

test('unrelated chain proof or malformed receipt cannot mark search delivered', async t => {
  const f = await fixture(t);
  f.chain.verify = async () => { throw new Error('wrong signed message'); };
  await assert.rejects(f.payments.pay(f.input));
  assert.equal(f.store.attempt(f.input.quoteId).state, 'uncertain');
});

test('paid HTTP error with valid receipt records the charge without results', async t => {
  const f = await fixture(t), original = f.exa.request.bind(f.exa);
  f.exa.request = async (...args) => ({ ...await original(...args), status: 500 });
  const result = await f.payments.pay(f.input);
  assert.equal(result.state, 'confirmed');
  assert.equal(result.delivered, false);
  assert.equal(f.calls(), 1);
});

test('cross-wallet quote recovery and bridge access are rejected', async t => {
  const f = await fixture(t), other = await testWallet();
  await assert.rejects(dispatch({ operation: 'exa-status', payer: other.address, quoteId: f.input.quoteId }, f), { code: 'WRONG_WALLET' });
  const stolen = new ExaPayments({ ...f, wallet: other });
  await assert.rejects(stolen.pay(f.input), { code: 'WRONG_WALLET' });
  await assert.rejects(stolen.reconcile(f.input.quoteId, f.transaction), { code: 'WRONG_WALLET' });
});

test('challenge strictly validates network/mint/price/sponsor/resource', () => {
  const requirement = challenge({ amount: '7000' }).requirement;
  for (const override of [{ amount: '0' }, { amount: '20001' }, { amount: 7000 }, { network: 'solana:devnet' }, { asset: 'other' }, { payTo: 'bad' }, { extra: {} }, { maxTimeoutSeconds: 301 }]) {
    assert.throws(() => selectExaRequirement({ x402Version: 2, accepts: [{ ...requirement, ...override }] }));
  }
  assert.throws(() => selectExaRequirement({ x402Version: 2, accepts: [requirement], resource: { url: 'https://evil.test' } }));
  assert.throws(() => selectExaRequirement({ x402Version: 2, accepts: [requirement, requirement] }));
});

test('only bounded public http(s) source links are returned', () => {
  assert.deepEqual(publicResults({ results: [{ url: 'javascript:alert(1)' }, { url: 'https://user:pass@evil.test' }, { url: 'https://safe.test', title: 'x'.repeat(500), text: 'y'.repeat(2000) }] }),
    [{ url: 'https://safe.test/', title: 'x'.repeat(200), text: 'y'.repeat(1200) }]);
  assert.throws(() => searchBody(''));
  assert.throws(() => searchBody('x'.repeat(2001)));
});
