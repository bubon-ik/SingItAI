import test from 'node:test';
import assert from 'node:assert/strict';
import { VeniceClient, selectRequirement } from '../src/venice.mjs';
import { challenge, jsonResponse, testWallet } from './helpers.mjs';

test('selects Solana USDC from a mixed-network challenge', () => {
  const r = challenge().requirement;
  assert.deepEqual(selectRequirement({ x402Version: 2, accepts: [{ ...r, network: 'eip155:8453' }, r] }), r);
});
for (const [name, patch] of Object.entries({ 'wrong network': { network: 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1' }, 'different mint case': { asset: challenge().requirement.asset.toLowerCase() }, 'zero amount': { amount: '0' }, 'negative amount': { amount: '-1' }, 'over prototype limit': { amount: '50000001' }, 'missing fee payer': { extra: {} }, 'invalid payee': { payTo: 'invalid' } })) {
  test(`rejects ${name}`, () => assert.throws(() => selectRequirement({ x402Version: 2, accepts: [challenge(patch).requirement] })));
}
test('rejects ambiguous options and changed payment resource', () => {
  const r = challenge().requirement;
  assert.throws(() => selectRequirement({ x402Version: 2, accepts: [r, r] }));
  assert.throws(() => selectRequirement({ x402Version: 2, accepts: [r], resource: { url: 'https://other.example/top-up' } }));
});
test('requires canonical string atomic amounts', () => {
  for (const amount of [5000000, '05000000', '5e6', '5.000000']) assert.throws(() => selectRequirement({ x402Version: 2, accepts: [challenge({ amount }).requirement] }), { code: 'INVALID_AMOUNT' });
});
test('accepts a 402 challenge without triggering a signed request', async () => {
  const calls = [];
  const wallet = await testWallet();
  const body = { x402Version: 2, accepts: [challenge().requirement] };
  const venice = new VeniceClient({ wallet, fetcher: async (url, init) => { calls.push({ url, init }); return jsonResponse(body, 402, { 'payment-required': Buffer.from(JSON.stringify(body)).toString('base64') }); } });
  assert.deepEqual(await venice.challenge(), challenge());
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.headers['X-402-Payment'], undefined);
  assert.equal(calls[0].init.redirect, 'error');
});
test('refuses inconsistent header and body requirements', async () => {
  const body = { x402Version: 2, accepts: [challenge().requirement] };
  const header = { x402Version: 2, accepts: [challenge({ amount: '6000000' }).requirement] };
  const v = new VeniceClient({ fetcher: async () => jsonResponse(body, 402, { 'payment-required': Buffer.from(JSON.stringify(header)).toString('base64') }) });
  await assert.rejects(v.challenge(), { code: 'INCONSISTENT_CHALLENGE' });
});
test('chat uses existing credit and never calls top-up when the balance is empty', async () => {
  const calls = [];
  const v = new VeniceClient({ wallet: await testWallet(), fetcher: async url => { calls.push(url); return jsonResponse({ data: { canConsume: false, balanceUsd: 0 } }); } });
  await assert.rejects(v.chat({ model: 'model', message: 'hello' }), { code: 'TOP_UP_REQUIRED' });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].includes('/x402/balance/'));
});
test('chat signs requests, limits output and returns text without automatic payments', async () => {
  const calls = [];
  const v = new VeniceClient({ wallet: await testWallet(), fetcher: async (url, init) => {
    calls.push({ url, init });
    return url.includes('/balance/') ? jsonResponse({ data: { canConsume: true, balanceUsd: 5 } }) : jsonResponse({ model: 'model', choices: [{ message: { content: 'Hello' } }], usage: { total_tokens: 4 } });
  } });
  assert.equal((await v.chat({ model: 'model', message: 'hello', maxTokens: 64 })).text, 'Hello');
  assert.equal(JSON.parse(calls[1].init.body).max_tokens, 64);
  assert.ok(calls.every(c => c.init.headers['X-Sign-In-With-X']));
  assert.ok(calls.every(c => !c.url.includes('top-up')));
});
