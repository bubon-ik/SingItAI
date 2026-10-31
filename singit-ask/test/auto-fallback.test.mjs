import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CONFIG, models, catalogue, serve, post, pay, decode, input } from './auto-router-fixtures.mjs';
import { chooseAuto, AUTO_CONFIG } from '../src/auto-router.mjs';
import { ModelCatalogue } from '../src/catalogue.mjs';
import { ceilings } from '../src/metered.mjs';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { answerLogFor } from '../src/answer-log.mjs';
import { readRequest, RequestError, UpstreamError, ROUTE } from '../src/app.mjs';
import { BASE, SOLANA, TERMS, SOLANA_TERMS, billUsage, worstModelCostMicro, exactReserveTokens, DEFAULT_EXACT_OUTPUT_FRACTION } from '../src/pricing.mjs';

test('price rejection and fresh floor skip; escalation and exhaustion are deterministic', async () => {
  for (const rejection of [
    { guardShare: () => .25, isPriceRejected: id => id === AUTO_CONFIG.tiers.standard[0] },
    { guardShare: () => .25, liveDiscount: async id => id === AUTO_CONFIG.tiers.standard[0] ? .38 : .25 },
  ]) {
    const cat = { ...catalogue, ...rejection };
    const a = await chooseAuto({}, cat, input('hi', { min_tier: 'standard' }), RequestError);
    assert.equal(a.model, AUTO_CONFIG.tiers.standard[1]);
    assert.deepEqual(await chooseAuto({}, cat, input('hi', { min_tier: 'standard' }), RequestError), a);
  }
  const cat = { ...catalogue, guardShare: () => .25, isPriceRejected: id => AUTO_CONFIG.tiers.standard.includes(id) };
  assert.equal((await chooseAuto({}, cat, input('hi', { min_tier: 'standard' }), RequestError)).autoTier, 'premium');
  await assert.rejects(chooseAuto({}, { ...cat, guardShare: () => .25, isPriceRejected: () => true }, input('hi', { min_tier: 'standard' }), RequestError), e => e.status === 503 && e.retryAfter > 0);
});

test('catalogue refusal TTL, bounded memory, fresh cache and concurrent dedupe fail open', async () => {
  let now = 0, calls = 0;
  const cat = new ModelCatalogue({ url: 'https://offline.test', now: () => now, fetchImpl: async () => {
    calls++;
    return { ok: true, json: async () => ({ offers: [{ trusted: true, healthy: true, available: true, discount_pct: 62 }] }) };
  }});
  cat.markPriceRejected('x', { bestDiscountPct: 62 });
  assert.equal(cat.isPriceRejected('x'), true);
  now = 120000; assert.equal(cat.isPriceRejected('x'), false);
  for (let i = 0; i < 1001; i++) cat.markPriceRejected(String(i));
  assert.equal(cat.priceRejected.size, 1000); assert.equal(cat.isPriceRejected('0'), false);
  assert.deepEqual(await Promise.all([cat.liveDiscount('x'), cat.liveDiscount('x')]), [.38, .38]);
  assert.equal(calls, 1); await cat.liveDiscount('x'); assert.equal(calls, 1);
  now += 30000; await cat.liveDiscount('x'); assert.equal(calls, 2);
  cat.fetchImpl = async () => { throw new Error('offline'); };
  assert.equal(await cat.liveDiscount('unreadable'), null);
  assert.equal((await chooseAuto({}, { ...catalogue, guardShare: () => .25,
    liveDiscount: id => cat.liveDiscount(id) }, input(), RequestError)).model, AUTO_CONFIG.tiers.eco[0]);
  for (let i = 0; i < 1001; i++) await cat.liveDiscount(`book-${i}`);
  assert.equal(cat.liveBooks.size, 1000);
  assert.equal(cat.loadingLiveBooks.size, 0);
});

for (const scheme of ['upto', 'exact']) for (const failure of ['success', 'both', 'unreachable', 'reasoning', 'notext', 'expensive']) {
  test(`paid fallback ${scheme}: ${failure}`, async t => {
    let calls = 0;
    const directory = await mkdtemp(join(tmpdir(), 'singit-fallback-'));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const config = { ...CONFIG, answerLogFile: join(directory, 'answers.jsonl') };
    // Equal cheap ceilings allow a retry; an inflated second model must be excluded.
    const cat = failure === 'expensive' ? { ...catalogue, resolve: async id => {
      const info = await catalogue.resolve(id);
      return id === AUTO_CONFIG.tiers.eco[0] ? info : { ...info, completionUsdPerToken: 1 };
    }} : catalogue;
    const { url, facilitator } = await serve(t, { config, catalogue: cat, upstream: async (_c, _m, _n, _f, model) => {
      calls++;
      if (calls === 1 || failure === 'both') throw new UpstreamError('refused', {
        status: failure === 'unreachable' ? 0 : ['notext', 'reasoning'].includes(failure) ? 200 : 404,
        code: failure === 'unreachable' ? 'unreachable' : failure === 'reasoning' ? 'max_tokens_spent_reasoning' : failure === 'notext' ? 'no_text' : 'minimum_discount_not_met',
      });
      return { content: model, finishReason: 'stop', usage: { buyer_cost_micro: 100 } };
    }});
    const body = { model: 'singit/auto', messages: [{ role: 'user', content: 'hi' }], max_tokens: 32 };
    const challenge = await post(url + ROUTE, body);
    assert.equal(challenge.status, 402);
    const required = decode(challenge.headers.get('payment-required'));
    const response = await post(url + ROUTE, body, pay(required, BASE, scheme));
    assert.equal(response.status, failure === 'success' ? 200 : failure === 'reasoning' ? 422 : 503);
    assert.equal(calls, failure === 'success' || failure === 'both' ? 2 : 1);
    assert.equal(facilitator.calls.settle.length, failure === 'success' ? 1 : 0);
    if (failure === 'success') {
      assert.equal(response.headers.get('x-singit-model'), AUTO_CONFIG.tiers.eco[1]);
      assert.equal(response.headers.get('x-singit-tier'), 'eco');
      const result = await response.json();
      assert.equal(result.choices[0].message.content, AUTO_CONFIG.tiers.eco[1]);
      const amount = BigInt(result.billing.totalAtomic);
      if (scheme === 'upto') assert.equal(amount, 100n + 30n + 1000n);  // actual provider cost + markup + settlement fee
      await answerLogFor(config).flush();
      const entries = (await readFile(config.answerLogFile, 'utf8')).trim().split('\n').map(JSON.parse);
      assert.equal(entries.length, 1);
      assert.equal(entries[0].model, AUTO_CONFIG.tiers.eco[1]);
      assert.equal(entries[0].upstreamMicro, 100);
      assert.ok(amount <= BigInt(required.accepts.find(a => a.network === BASE && a.scheme === scheme).amount));
    }
  });
}


test('HTTP prepayment rejection skips and escalates, and exhaustion never verifies or settles', async t => {
  for (const mode of ['recent', 'fresh', 'tier', 'all']) {
    const cat = { ...catalogue, guardShare: () => .25,
      isPriceRejected: id => mode === 'all' || mode === 'tier' && AUTO_CONFIG.tiers.standard.includes(id)
        || mode === 'recent' && id === AUTO_CONFIG.tiers.standard[0],
      liveDiscount: async id => mode === 'fresh' && id === AUTO_CONFIG.tiers.standard[0] ? .38 : .25 };
    const { url, facilitator } = await serve(t, { catalogue: cat });
    const body = { model: 'singit/auto', min_tier: 'standard', messages: [{ role: 'user', content: 'hi' }], max_tokens: 512 };
    const response = await post(url + ROUTE, body);
    assert.equal(response.status, mode === 'all' ? 503 : 402);
    if (mode === 'all') {
      assert.equal((await response.json()).error.code, 'no_healthy_sellers');
      assert.ok(Number(response.headers.get('retry-after')) > 0);
      assert.equal(response.headers.get('payment-required'), null);
    } else {
      assert.equal(response.headers.get('x-singit-tier'), mode === 'tier' ? 'premium' : 'standard');
      if (mode !== 'tier') assert.equal(response.headers.get('x-singit-model'), 'kimi-k3');
      const chosen = await chooseAuto(CONFIG, cat, readRequest(body), RequestError);
      for (const offer of decode(response.headers.get('payment-required')).accepts)
        assert.equal(offer.amount, ceilings(chosen.info, chosen)[offer.scheme][offer.network]);
    }
    assert.equal(facilitator.calls.verify, 0);
    assert.equal(facilitator.calls.settle.length, 0);
  }

  // A quote at the minimum charge sends no floor, so a recent refusal or a low book does not exclude the model.
  const cheap = { ...catalogue, guardShare: () => .25, isPriceRejected: () => true, liveDiscount: async () => .38 };
  assert.equal((await chooseAuto({}, cheap, input(), RequestError)).model, AUTO_CONFIG.tiers.eco[0]);
});

test('every tier model retains margin on direct and cheaper alternate priced usage, both schemes and networks', () => {
  for (const model of models) for (const network of [BASE, SOLANA]) for (const bucket of ['exact', 'upto']) {
    const terms = network === BASE ? TERMS : SOLANA_TERMS;
    const request = { chars: 1600, messageCount: 2, maxTokens: 512 };
    const quote = ceilings(model, request)[bucket][network];
    const cost = BigInt(worstModelCostMicro(model, { ...request, charsPerToken: bucket === 'upto' ? 1 : 4,
      maxTokens: bucket === 'upto' ? request.maxTokens : Math.ceil(request.maxTokens * DEFAULT_EXACT_OUTPUT_FRACTION),
      reserveTokens: bucket === 'upto' ? null : exactReserveTokens(model.id, request.maxTokens) }));
    for (const paid of [BigInt(quote), BigInt(quote) + 1000n]) {
      const billing = billUsage({ buyer_cost_micro: cost.toString() }, { ...terms, maxChargeAtomic: paid.toString() });
      const charged = bucket === 'exact' ? paid : BigInt(billing.totalAtomic);
      assert.ok(charged >= cost + (cost * 3000n + 9999n) / 10000n + BigInt(terms.settlementFeeAtomic));
      if (bucket === 'exact') assert.ok(charged >= 1500n);
      assert.ok(charged <= paid);
    }
  }
});


test('a cheaper exact-ceiling alternate settles the original flat price once', async t => {
  const cat = { ...catalogue, resolve: async id => ({ id,
    promptUsdPerToken: id === 'kimi-k3' ? .0000053 : 0,
    completionUsdPerToken: id === 'claude-sonnet-5.5' ? .00001 : 0 }) };
  let calls = 0;
  const { url, facilitator } = await serve(t, { catalogue: cat, upstream: async (_c, _m, _n, _f, model, options) => {
    assert.ok(options.timeoutMs <= 95000);
    if (++calls === 1) throw new UpstreamError('refused', { status: 404, code: 'minimum_discount_not_met' });
    assert.equal(model, 'kimi-k3');
    return { content: 'answer', finishReason: 'stop', usage: { buyer_cost_micro: 1000 } };
  }});
  const body = { model: 'singit/auto', min_tier: 'standard', messages: [{ role: 'user', content: 'a'.repeat(1000) }], max_tokens: 512 };
  const chosen = await chooseAuto(CONFIG, cat, readRequest(body), RequestError);
  assert.equal(chosen.model, 'claude-sonnet-5.5');
  assert.ok(BigInt(ceilings(chosen.autoAlternates[0].info, chosen.autoAlternates[0]).exact[BASE]) < BigInt(ceilings(chosen.info, chosen).exact[BASE]));
  const challenge = await post(url + ROUTE, body);
  const required = decode(challenge.headers.get('payment-required'));
  const paid = required.accepts.find(a => a.network === BASE && a.scheme === 'exact').amount;
  const response = await post(url + ROUTE, body, pay(required, BASE, 'exact'));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-singit-model'), 'kimi-k3');
  assert.equal((await response.json()).billing.totalAtomic, paid);
  assert.equal(calls, 2);
  assert.deepEqual(facilitator.calls.settle, [[BASE, paid]]);
});


for (const retry of [false, true]) test(`auto exact settles the flat paid price once even when actual cost overruns, retry=${retry}`, async t => {
  let calls = 0;
  const { url, facilitator } = await serve(t, { upstream: async () => {
    if (++calls === 1 && retry) throw new UpstreamError('refused', { status: 404, code: 'minimum_discount_not_met' });
    return { content: 'answer', finishReason: 'stop', usage: { buyer_cost_micro: 1000000 } };
  }});
  const body = { model: 'singit/auto', messages: [{ role: 'user', content: 'hi' }], max_tokens: 32 };
  const challenge = await post(url + ROUTE, body);
  const response = await post(url + ROUTE, body, pay(decode(challenge.headers.get('payment-required')), BASE, 'exact'));
  assert.equal(response.status, 200);
  assert.equal(calls, retry ? 2 : 1);
  assert.equal(facilitator.calls.settle.length, 1);
});
