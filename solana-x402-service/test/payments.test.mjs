import test from 'node:test';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { getBase58Decoder } from '@solana/kit';
import { Store } from '../src/store.mjs';
import { Payments } from '../src/payments.mjs';
import { directory, challenge, testWallet } from './helpers.mjs';

const signature = getBase58Decoder().decode(new Uint8Array(64).fill(7));
async function setup(t) {
  const dir = directory(); const store = new Store(dir);
  t.after(() => { store.close(); rmSync(dir, { recursive: true }); });
  const wallet = await testWallet();
  const calls = { build: 0, submit: 0 };
  const venice = {
    challenge: async () => challenge(),
    submit: async () => { calls.submit++; return { status: 200, headers: new Headers({ 'payment-response': Buffer.from(JSON.stringify({ success: true, transaction: signature })).toString('base64') }), data: {} }; },
    balance: async () => ({ balanceUsd: 5, canConsume: true }),
  };
  const chain = { balances: async () => ({ usdcAtomic: '10000000' }), build: async () => { calls.build++; return { payload: { test: true }, messageHash: 'a'.repeat(64) }; }, verify: async transaction => ({ state: 'confirmed', transaction }) };
  const payments = new Payments({ wallet, venice, chain, store });
  const quote = await payments.prepare();
  return { dir, store, wallet, calls, venice, chain, payments, quote };
}
test('quote does not sign or pay; missing approval is refused', async t => {
  const f = await setup(t);
  await assert.rejects(f.payments.pay(f.quote.quoteId), { code: 'APPROVAL_REQUIRED' });
  assert.deepEqual(f.calls, { build: 0, submit: 0 });
});
for (const [name, patch] of Object.entries({ amount: { amount: '6000000' }, payee: { payTo: '11111111111111111111111111111111' }, 'mint case': { asset: challenge().requirement.asset.toLowerCase() }, network: { network: 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1' }, 'fee payer': { extra: { feePayer: '11111111111111111111111111111111' } } })) {
  test(`changed ${name} is rejected before signing`, async t => {
    const f = await setup(t); f.venice.challenge = async () => challenge(patch);
    await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'TERMS_CHANGED' });
    assert.deepEqual(f.calls, { build: 0, submit: 0 });
  });
}
test('expired approval and wrong wallet cannot pay', async t => {
  const f = await setup(t);
  f.payments.now = () => Date.now() + 600000;
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'QUOTE_EXPIRED' });
  f.payments.now = Date.now; f.payments.wallet = await testWallet();
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'WRONG_WALLET' });
  assert.deepEqual(f.calls, { build: 0, submit: 0 });
});
test('insufficient funds and expiry during build do not submit', async t => {
  const f = await setup(t); f.chain.balances = async () => ({ usdcAtomic: '0' });
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'INSUFFICIENT_USDC' });
  assert.equal(f.calls.build, 0);
  f.chain.balances = async () => ({ usdcAtomic: '5000000' });
  f.chain.build = async () => { f.payments.now = () => Date.now() + 600000; return { payload: {}, messageHash: 'a'.repeat(64) }; };
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'QUOTE_EXPIRED' });
  assert.equal(f.calls.submit, 0);
});
test('confirmed payment cannot be submitted again for the same quote', async t => {
  const f = await setup(t);
  assert.equal((await f.payments.pay(f.quote.quoteId, f.quote.approvalHash)).state, 'confirmed');
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'ALREADY_ATTEMPTED' });
  assert.equal(f.calls.submit, 1);
});
test('a previously confirmed receipt is not downgraded by later RPC unavailability', async t => {
  const f = await setup(t);
  await f.payments.pay(f.quote.quoteId, f.quote.approvalHash);
  f.chain.verify = async () => { throw new Error('RPC unavailable'); };
  assert.equal((await f.payments.reconcile(f.quote.quoteId)).state, 'confirmed');
  assert.equal(f.calls.submit, 1);
});
test('lost response survives reopening the store and blocks another payment', async t => {
  const f = await setup(t); f.venice.submit = async () => { f.calls.submit++; throw new Error('timeout'); };
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'PAYMENT_UNCERTAIN' });
  const secondStore = new Store(f.dir); t.after(() => secondStore.close());
  assert.equal(secondStore.attempt(f.quote.quoteId).state, 'uncertain');
  const second = new Payments({ wallet: f.wallet, venice: f.venice, chain: f.chain, store: secondStore });
  const quote2 = await second.prepare();
  await assert.rejects(second.pay(quote2.quoteId, quote2.approvalHash), { code: 'UNRESOLVED_PAYMENT' });
  assert.equal(f.calls.submit, 1);
  assert.equal((await second.reconcile(f.quote.quoteId, signature)).state, 'confirmed');
  assert.equal(f.calls.submit, 1);
});
test('HTTP 200 without a receipt does not count as a confirmed payment', async t => {
  const f = await setup(t); f.venice.submit = async () => ({ status: 200, headers: new Headers(), data: { success: true } });
  await assert.rejects(f.payments.pay(f.quote.quoteId, f.quote.approvalHash), { code: 'PAYMENT_UNCERTAIN' });
  assert.equal(f.store.attempt(f.quote.quoteId).state, 'uncertain');
});
test('SQLite claim prevents concurrent payments with different quotes for the same wallet', async t => {
  const f = await setup(t); const q2 = await f.payments.prepare();
  const second = new Store(f.dir); t.after(() => second.close());
  f.store.claim(f.quote.quoteId, f.wallet.address, 'a'.repeat(64));
  assert.throws(() => second.claim(q2.quoteId, f.wallet.address, 'b'.repeat(64)), { code: 'UNRESOLVED_PAYMENT' });
  assert.equal(second.attempt(q2.quoteId), null);
});
test('reconciliation preserves uncertainty if chain proof does not match', async t => {
  const f = await setup(t); f.store.claim(f.quote.quoteId, f.wallet.address, 'a'.repeat(64));
  f.chain.verify = async () => { throw new Error('mismatched transaction'); };
  await assert.rejects(f.payments.reconcile(f.quote.quoteId, signature));
  assert.equal(f.store.attempt(f.quote.quoteId).state, 'sending');
  assert.equal(f.calls.submit, 0);
});
