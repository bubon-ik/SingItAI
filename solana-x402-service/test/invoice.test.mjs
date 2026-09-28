import test from 'node:test';
import assert from 'node:assert/strict';
import { InvoicePayments, BITREFILL_PAY_URL, selectInvoiceRequirement } from '../src/invoice.mjs';
import { Store } from '../src/store.mjs';
import { NETWORK, USDC } from '../src/config.mjs';
import { testWallet, directory } from './helpers.mjs';

const INVOICE = 'c2b27180-610e-4132-af77-ad42fc0ac444';
const FEE_PAYER = 'BFK9TLC3edb13K6v4YyH3DwPb5DSUpkWvb7XnqCL9b4F';
const TX = '4VRuBGv35WgvsriGgXaD9CD272jhDvFiEA3DbnHuW3wAd8WJ8RJd3i7DoNQqgfB5kSrKavmjy3XDS9R96EVCZBtW';

function offer(amount = '9440000', extra = {}) {
  return { x402Version: 2, resource: { url: BITREFILL_PAY_URL }, accepts: [
    { scheme: 'exact', network: 'eip155:8453', asset: '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', amount, payTo: '0x480C' },
    { scheme: 'exact', network: NETWORK, asset: USDC, amount, payTo: 'BitrefiLLPayTo1111111111111111111111111111', maxTimeoutSeconds: 60, extra: { feePayer: FEE_PAYER, ...extra } },
  ] };
}

function bitrefill({ amount, paidStatus = 200 } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body), signed: init.headers['PAYMENT-SIGNATURE'] || null });
    if (!init.headers['PAYMENT-SIGNATURE']) {
      return new Response('{}', { status: 402, headers: { 'payment-required': Buffer.from(JSON.stringify(offer(amount))).toString('base64') } });
    }
    return new Response('{"accepted":true}', { status: paidStatus, headers: { 'payment-response': Buffer.from(JSON.stringify({ transaction: TX })).toString('base64') } });
  };
  return { calls, fetcher };
}

const chain = (held = '9440000') => ({
  balances: async () => ({ usdcAtomic: held }),
  build: async (requirement, resource, wallet) => ({ payload: { x402Version: 2, accepted: requirement, resource, payload: { transaction: 'signed-by-' + wallet.address } }, messageHash: 'mh' }),
});

test('an invoice is paid on Solana through Bitrefill’s x402 route, once', async () => {
  const wallet = await testWallet();
  const store = new Store(directory());
  const { calls, fetcher } = bitrefill();
  const paid = await new InvoicePayments({ wallet, chain: chain(), store, fetcher }).pay({ url: BITREFILL_PAY_URL, invoiceId: INVOICE, maxAmount: '9450000' });
  assert.deepEqual(paid, { invoiceId: INVOICE, state: 'accepted', transaction: TX, amount: '9440000' });
  assert.deepEqual(calls.map(c => c.body), [{ invoice_id: INVOICE }, { invoice_id: INVOICE }]);
  const sent = JSON.parse(Buffer.from(calls[1].signed, 'base64').toString());
  assert.equal(sent.accepted.network, NETWORK);  // the Solana option, not the Base one
  await assert.rejects(new InvoicePayments({ wallet, chain: chain(), store, fetcher }).pay({ url: BITREFILL_PAY_URL, invoiceId: INVOICE, maxAmount: '9450000' }), /already paid or attempted/);
  assert.equal(calls.length, 2);
  store.close();
});

test('a higher price, another merchant or too little held pays nothing', async () => {
  const wallet = await testWallet();
  const store = new Store(directory());
  for (const [args, held, pattern] of [
    [{ url: BITREFILL_PAY_URL, invoiceId: INVOICE, maxAmount: '9000000' }, '9440000', /asks more/],
    [{ url: 'https://evil.example/pay', invoiceId: INVOICE, maxAmount: '9450000' }, '9440000', /Only Bitrefill/],
    [{ url: BITREFILL_PAY_URL, invoiceId: INVOICE, maxAmount: '9450000' }, '1', /does not hold/],
  ]) {
    const { calls, fetcher } = bitrefill();
    await assert.rejects(new InvoicePayments({ wallet, chain: chain(held), store, fetcher }).pay(args), pattern);
    assert.ok(calls.every(c => !c.signed));
  }
  assert.equal(store.invoiceAttempt(INVOICE), null);
  store.close();
});

test('an unclear answer is recorded and never retried', async () => {
  const wallet = await testWallet();
  const store = new Store(directory());
  const { fetcher } = bitrefill({ paidStatus: 500 });
  await assert.rejects(new InvoicePayments({ wallet, chain: chain(), store, fetcher }).pay({ url: BITREFILL_PAY_URL, invoiceId: INVOICE, maxAmount: '9450000' }), /Do not pay again/);
  assert.equal(store.invoiceAttempt(INVOICE).state, 'uncertain');
  store.close();
});

test('only a sponsored Solana USDC option is taken', () => {
  assert.throws(() => selectInvoiceRequirement({ accepts: offer().accepts.slice(0, 1) }, '9450000', 'me'), /no single USDC payment on Solana/);
  assert.throws(() => selectInvoiceRequirement(offer('9440000', { feePayer: 'me' }), '9450000', 'me'), /sponsored/);
});
