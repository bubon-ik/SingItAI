import { test } from 'node:test';
import assert from 'node:assert/strict';
import { catalogue, models, CONFIG, serve, post, pay, decode, input, fakeFacilitator, rates } from './auto-router-fixtures.mjs';
import { createApp, ROUTE, readRequest, RequestError } from '../src/app.mjs';
import { AUTO_CONFIG, classifyAuto, chooseAuto, validateAutoConfig } from '../src/auto-router.mjs';
import { ceilings } from '../src/metered.mjs';
import { BASE, SOLANA, METERED_ROUTE, TERMS, SOLANA_TERMS, worstModelCostMicro, billUsage, exactReserveTokens, DEFAULT_EXACT_OUTPUT_FRACTION } from '../src/pricing.mjs';
test('classifier boundaries, whole phrases and min_tier', () => {
  for (const [text, extra, tier] of [
    ['a'.repeat(1600),{},'eco'], ['a'.repeat(1601),{},'standard'],
    ['hi',{max_tokens:513},'standard'], ['a'.repeat(23996),{},'standard'],
    ['a'.repeat(23997),{},'premium'], ['DEBUG ```x```',{},'premium'],
    ['debug',{max_tokens:1000},'premium'], ['debug',{max_tokens:999},'standard'],
    ['debugger',{},'eco'], ['step by step',{},'standard'],
    ['debug '+'a'.repeat(5994),{},'premium'], ['debug '+'a'.repeat(5990),{},'standard'],
    ['hi',{min_tier:'standard'},'standard'], ['hi',{min_tier:'premium'},'premium'],
    ['hi',{max_tokens:undefined},'standard'],
    ['hi',{messages:Array.from({length:4},()=>({role:'user',content:'hi'}))},'standard'],
  ]) assert.equal(classifyAuto(input(text,extra)).tier,tier);
  assert.equal(classifyAuto(input('debug', {max_tokens:1000})).reason, 'reasoning_large_output');
  for (const extra of [{min_tier:null},{min_tier:'bad'},{model:'other',min_tier:'eco'}]) assert.throws(()=>input('hi',extra),e=>e.status===400);
});
test('config, healthy selection, escalation, tie order and determinism', async()=>{
  validateAutoConfig(AUTO_CONFIG);
  assert.throws(()=>validateAutoConfig({...AUTO_CONFIG,tiers:{...AUTO_CONFIG.tiers,eco:['free/test']}}));
  const a = await chooseAuto({},catalogue,input(),RequestError);
  assert.equal(a.model,AUTO_CONFIG.tiers.eco[0]);
  assert.deepEqual(await chooseAuto({},catalogue,input(),RequestError),a);
  const unhealthy = {...catalogue,canAnswer:async id=>!AUTO_CONFIG.tiers.eco.includes(id)};
  assert.equal((await chooseAuto({},unhealthy,input(),RequestError)).autoTier,'standard');
  assert.equal((await chooseAuto({},{...catalogue,isUnservable:id=>!AUTO_CONFIG.tiers.premium.includes(id)},input(),RequestError)).autoTier,'premium');
  await assert.rejects(chooseAuto({},{...catalogue,canAnswer:async()=>false},input(),RequestError),e=>e.status===503 && !!e.retryAfter);
});
test('every tier retains margin and floor on both priced budgets',()=>{
  assert.equal(models.length, 8);
  assert.deepEqual(models.map(m=>m.id).sort(), Object.keys(rates).sort());
  for(const model of models) for(const chars of [1,1600,24000]) for(const maxTokens of [32,512,1200]) {
    const request={chars,maxTokens,messageCount:2}, quotes=ceilings(model,request);
    for(const [net,terms] of [[BASE,TERMS],[SOLANA,SOLANA_TERMS]]) for(const scheme of ['upto','exact']) {
      const cost=BigInt(worstModelCostMicro(model,{...request,charsPerToken:scheme==='upto'?1:4,
        maxTokens:scheme==='upto'?maxTokens:Math.ceil(maxTokens*DEFAULT_EXACT_OUTPUT_FRACTION),
        reserveTokens:scheme==='upto'?null:exactReserveTokens(model.id,maxTokens)}));
      const minimum=cost+(cost*3000n+9999n)/10000n+BigInt(terms.settlementFeeAtomic);
      assert.ok(BigInt(quotes[scheme][net])>=minimum); assert.ok(BigInt(quotes[scheme][net])>=1500n);
      assert.ok(BigInt(billUsage({buyer_cost_micro:cost.toString()},{...terms,maxChargeAtomic:quotes[scheme][net]}).totalAtomic)>=minimum);
    }
  }
});

const exposed = response => (response.headers.get('access-control-expose-headers') ?? '').split(',').map(s => s.trim().toLowerCase());
const assertExposed = (response, origin = null) => {
  const names = exposed(response);
  for (const name of ['x-singit-model', 'x-singit-tier']) assert.ok(names.includes(name));
  assert.equal(names.length, new Set(names).size);
  assert.equal(response.headers.get('access-control-allow-origin'), origin);
};




for (const path of [ROUTE, METERED_ROUTE]) test(`auto headers, paid model and price, validation: ${path}`, async t=>{
 const {url,asked,facilitator}=await serve(t);
 const body={model:'singit/auto',messages:[{role:'user',content:'Hi'}],max_tokens:512};
 const r=await post(url+path,body); assert.equal(r.status,402); assertExposed(r);
 const required=decode(r.headers.get('payment-required'));
 const concrete=await chooseAuto(CONFIG,catalogue,readRequest(body),RequestError);
 assert.equal(r.headers.get('x-singit-model'),concrete.model);
 const expected=ceilings(concrete.info,concrete);
 for(const offer of required.accepts) assert.equal(offer.amount,expected[offer.scheme][offer.network]);
 const paid=await post(url+path,body,pay(required,BASE));
 assert.equal(paid.status,200); assertExposed(paid); assert.equal(r.headers.get('x-singit-tier'),'eco');
 assert.equal(paid.headers.get('x-singit-model'),r.headers.get('x-singit-model'));
 assert.equal(paid.headers.get('x-singit-tier'),'eco'); assert.equal(asked[0].model,r.headers.get('x-singit-model'));
 for(const extra of [{min_tier:null},{min_tier:'bad'},{model:CONFIG.model,min_tier:'eco'}]) {
  const bad=await post(url+path,{...body,...extra}); assert.equal(bad.status,400); assertExposed(bad);
  assert.equal((await bad.json()).error.code,'invalid_request'); assert.equal(bad.headers.get('payment-required'),null);
 }
 for(const path of ['/v1/messages','/v1/responses']) {
  const bad=await post(url+path,{...body,input:"hi"}); assert.ok(bad.status>=400); assert.equal(bad.headers.get('payment-required'),null); assert.equal(bad.headers.get('x-singit-model'),null);
 }
 const raised=await post(url+path,{...body,min_tier:'premium'});
 assert.equal(raised.status,402); assert.equal(raised.headers.get('x-singit-tier'),'premium');
 for(const [max_tokens,tier] of [[999,'standard'],[1000,'premium']]) {
  const boundary=await post(url+path,{...body,messages:[{role:'user',content:'debug'}],max_tokens});
  assert.equal(boundary.status,402); assert.equal(boundary.headers.get('x-singit-tier'),tier);
 }
 assert.equal(facilitator.calls.verify,1); assert.equal(facilitator.calls.settle.length,1);
});
// With unchanged health, identical unpaid and paid bodies choose the same model and ceilings.
// Health changes between challenge and retry can change the model and price. The existing
// middleware refuses a signed amount that no longer matches before upstream or settlement.
// Equal ceilings may still match, in which case the newly chosen healthy model answers.

test('chosen headers survive unpaid and paid retries on both chat routes, including upstream errors', async t=>{
 for (const path of [ROUTE, METERED_ROUTE]) for (const code of [undefined, 'max_tokens_spent_reasoning']) {
  const {url,facilitator}=await serve(t,{upstream:async()=>{throw Object.assign(new Error('offline'),{code});}});
  const body={model:'singit/auto',messages:[{role:'user',content:'debug'}],max_tokens:1000};
  const challenge=await post(url+path,body);
  assert.equal(challenge.status,402);
  const response=await post(url+path,body,pay(decode(challenge.headers.get('payment-required')),BASE));
  assert.equal(response.status,code?422:503);
  assertExposed(response);
  for(const header of ['x-singit-model','x-singit-tier']) {
   assert.ok(challenge.headers.get(header)); assert.equal(response.headers.get(header),challenge.headers.get(header));
  }
  assert.equal(response.headers.get('x-singit-tier'),'premium');
  assert.equal(facilitator.calls.settle.length,0);
 }
});
test('HTTP health escalation never goes down and exhaustion refuses payment',async t=>{
 for(const healthy of ['standard','premium',null]) {
  const {url,facilitator}=await serve(t,{catalogue:{...catalogue,canAnswer:async id=>healthy!==null && AUTO_CONFIG.tiers[healthy].includes(id)}});
  for(const min_tier of ['eco','standard','premium']) {
   const response=await post(url+ROUTE,{model:'singit/auto',min_tier,messages:[{role:'user',content:'hi'}],max_tokens:512});
   if(healthy && ['eco','standard','premium'].indexOf(healthy)>=['eco','standard','premium'].indexOf(min_tier)) {
    assert.equal(response.status,402); assert.equal(response.headers.get('x-singit-tier'),healthy);
   } else {
    assert.equal(response.status,503); assert.equal((await response.json()).error.code,'no_healthy_sellers');
    assert.ok(Number(response.headers.get('retry-after'))>0); assert.equal(response.headers.get('payment-required'),null);
   }
  }
  assert.equal(facilitator.calls.verify,0); assert.equal(facilitator.calls.settle.length,0);
 }
});

test('exposed headers merge only at writeHead without patching setters', () => {
 const app = createApp(CONFIG, { facilitatorClient: fakeFacilitator(), catalogue, log() {} });
 for (const form of ['none', 'object', 'array', 'status-message']) {
  const headers = new Map();
  let written;
  const res = {
   getHeader(name) { return headers.get(name.toLowerCase()); },
   setHeader(name, value) { headers.set(name.toLowerCase(), value); },
   writeHead(...args) { written = args; return this; },
  };
  const setter = res.setHeader;
  app.router.stack[2].handle({}, res, () => {});
  assert.equal(res.setHeader, setter);
  assert.equal(headers.size, 0);
  res.setHeader('Access-Control-Expose-Headers', [' X-Test, x-test, ', 'x-singit-model']);
  const value = ['X-Other, X-Test', 'x-singit-tier, X-OTHER'];
  const explicit = form === 'array'
   ? ['X-Keep', 'yes', 'ACCESS-CONTROL-EXPOSE-HEADERS', value]
   : { 'X-Keep': 'yes', 'access-control-expose-headers': value };
  const args = form === 'none' ? [200] : form === 'status-message' ? [200, 'OK', explicit] : [200, explicit];
  assert.equal(res.writeHead(...args), res);
  assert.equal(res.getHeader('Access-Control-Expose-Headers'), form === 'none'
   ? 'X-Test, x-singit-model, X-SingIt-Tier'
   : 'X-Test, x-singit-model, X-Other, x-singit-tier');
  assert.deepEqual(written, form === 'none' ? [200] : form === 'array'
   ? [200, ['X-Keep', 'yes']] : form === 'status-message'
   ? [200, 'OK', { 'X-Keep': 'yes' }] : [200, { 'X-Keep': 'yes' }]);
  assert.equal(headers.size, 1);
 }
});

test('exposed headers on models merge current entries and explicit writeHead headers', async t => {
 for (const form of ['object', 'array', 'status-message']) {
  const { url } = await serve(t, { middleware: (req, res, next) => {
   res.set('Access-Control-Expose-Headers', 'PAYMENT-REQUIRED, payment-required');
   res.json = function (body) {
    this.set('access-control-expose-headers', 'PAYMENT-RESPONSE, x-singit-model');
    this.append('Access-Control-Expose-Headers', ['X-SingIt-Receipt', 'payment-response']);
    const value = 'x-singit-tier, PAYMENT-REQUIRED, X-Test';
    if (form === 'array') this.writeHead(200, ['Access-Control-Expose-Headers', value]);
    else if (form === 'status-message') this.writeHead(200, 'OK', { 'access-control-expose-headers': value });
    else this.writeHead(200, { 'Access-Control-Expose-Headers': value });
    this.end(JSON.stringify(body));
    return this;
   };
   next();
  } });
  const response = await fetch(url + '/v1/models');
  assert.equal(response.status, 200);
  assertExposed(response, '*');
  assert.deepEqual(exposed(response), ['payment-response', 'x-singit-model', 'x-singit-receipt',
   'x-singit-tier', 'payment-required', 'x-test']);
 }
});

test('models expose the chosen-model header names without payment', async t => {
 const { url } = await serve(t);
 const response = await fetch(url + '/v1/models');
 assert.equal(response.status, 200);
 assertExposed(response, '*');
 const missing = await fetch(url + '/missing');
 assert.equal(missing.status, 404);
 assertExposed(missing);
});
