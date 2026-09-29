import test from 'node:test';
import assert from 'node:assert/strict';
import { getBase58Encoder } from '@solana/kit';
import { ResourcePayments, selectDataRequirement } from '../src/resource.mjs';
import { dispatch } from '../src/gateway.mjs';
import { Store } from '../src/store.mjs';
import { NETWORK, USDC, TOKEN_PROGRAM } from '../src/config.mjs';
import { testWallet, directory } from './helpers.mjs';

const URL_ = 'https://x402.ottoai.services/weather?location=Berlin';
const TX = '4VRuBGv35WgvsriGgXaD9CD272jhDvFiEA3DbnHuW3wAd8WJ8RJd3i7DoNQqgfB5kSrKavmjy3XDS9R96EVCZBtW';
const FEE_PAYER = 'BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP';

async function fixture({ delegated = 1_000_000n, paidStatus = 200, payTo = null } = {}) {
  const [agent, owner, seller] = [await testWallet(), await testWallet(), await testWallet()];
  const account = () => {
    const data = Buffer.alloc(165), enc = getBase58Encoder();
    Buffer.from(enc.encode(USDC)).copy(data, 0);
    Buffer.from(enc.encode(owner.address)).copy(data, 32);
    data.writeBigUInt64LE(5_000_000n, 64);
    data.writeUInt32LE(1, 72); Buffer.from(enc.encode(agent.address)).copy(data, 76); data.writeBigUInt64LE(delegated, 121);
    data[108] = 1;
    return data;
  };
  const built = [];
  const chain = {
    rpc: { getAccountInfo: () => ({ send: async () => ({ value: { owner: TOKEN_PROGRAM, data: [account().toString('base64'), 'base64'] } }) }) },
    buildDelegated: async (requirement, resource, wallet, from) => {
      built.push({ requirement, agent: wallet.address, from });
      return { messageHash: 'mh', payload: { x402Version: 2, accepted: requirement } };
    },
  };
  const offer = { x402Version: 2, resource: { url: URL_ }, accepts: [
    { scheme: 'exact', network: 'eip155:8453', asset: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', amount: '2000', payTo: '0x0E84' },
    { scheme: 'exact', network: NETWORK, asset: USDC, amount: '2000', payTo: payTo || seller.address, maxTimeoutSeconds: 60, extra: { feePayer: FEE_PAYER } },
  ] };
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, signed: init.headers['PAYMENT-SIGNATURE'] || null });
    if (!init.headers['PAYMENT-SIGNATURE']) {
      return new Response('{}', { status: 402, headers: { 'payment-required': Buffer.from(JSON.stringify(offer)).toString('base64') } });
    }
    return new Response('{"current":{"tempC":14}}', { status: paidStatus, headers: { 'payment-response': Buffer.from(JSON.stringify({ transaction: TX })).toString('base64') } });
  };
  const store = new Store(directory());
  const call = { callId: 'data-abcdefgh1234', url: URL_, method: 'GET', payTo: seller.address, maxAmount: '5000', owner: owner.address };
  return { agent, owner, seller, chain, fetcher, store, calls, built, call, payments: new ResourcePayments({ wallet: agent, chain, store, fetcher }) };
}

test('data is paid once from the owner’s account, the agent as delegate, to the bound seller', async () => {
  const f = await fixture();
  const paid = await f.payments.pay(f.call);
  assert.deepEqual(paid, { callId: f.call.callId, state: 'accepted', transaction: TX, amount: '2000', data: { current: { tempC: 14 } } });
  assert.deepEqual(f.built.map(b => [b.agent, b.from, b.requirement.network]), [[f.agent.address, f.owner.address, NETWORK]]);
  await assert.rejects(f.payments.pay(f.call), /already paid or attempted/);
  assert.equal(f.calls.filter(c => c.signed).length, 1);
  f.store.close();
});

test('another seller, a higher price, an unlisted host or no approval pays nothing', async () => {
  for (const [options, change, pattern] of [
    [{ payTo: '6XcSfqJHr9vNW2vbiRaMqUYVm7shDgLepca54wUTDPN5' }, {}, /somewhere unexpected/],
    [{}, { maxAmount: '1000' }, /asks more/],
    [{}, { url: 'https://evil.example/weather' }, /Only listed data sellers/],
    [{}, { url: 'http://x402.ottoai.services/weather' }, /Only listed data sellers/],
    [{ delegated: 0n }, {}, /does not cover/],
    [{}, { maxAmount: '60000' }, /Invalid maximum/],
  ]) {
    const f = await fixture(options);
    await assert.rejects(f.payments.pay({ ...f.call, ...change }), pattern);
    assert.ok(f.calls.every(c => !c.signed));
    assert.equal(f.store.invoiceAttempt(f.call.callId), null);
    f.store.close();
  }
});

test('an unclear answer is recorded and never retried', async () => {
  const f = await fixture({ paidStatus: 500 });
  await assert.rejects(f.payments.pay(f.call), /not repeated/);
  assert.equal(f.store.invoiceAttempt(f.call.callId).state, 'uncertain');
  await assert.rejects(f.payments.pay(f.call), /already paid or attempted/);
  f.store.close();
});

test('the bridge pays data only for its own agent', async () => {
  const f = await fixture();
  const context = { wallet: f.agent, chain: f.chain, store: f.store };
  const saved = globalThis.fetch;
  globalThis.fetch = f.fetcher;
  try {
    const paid = await dispatch({ operation: 'data-pay', payer: f.agent.address, ...f.call }, context);
    assert.equal(paid.state, 'accepted');
    await assert.rejects(dispatch({ operation: 'data-pay', payer: f.owner.address, ...f.call, callId: 'data-zzzzzzzz9999' }, context), /mismatch/);
  } finally { globalThis.fetch = saved; f.store.close(); }
});

test('only a sponsored Solana USDC option to the bound seller is taken', () => {
  const offer = { accepts: [{ scheme: 'exact', network: NETWORK, asset: USDC, amount: '1000', payTo: 'A', extra: { feePayer: 'me' } }] };
  assert.throws(() => selectDataRequirement(offer, { payTo: 'A', maxAmount: '5000', payer: 'me', owner: 'o' }), /sponsored/);
  assert.throws(() => selectDataRequirement({ accepts: [] }, { payTo: 'A', maxAmount: '5000', payer: 'me', owner: 'o' }), /no USDC on Solana/);
});
