import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import express from 'express';
import {createKeyPairSignerFromPrivateKeyBytes} from '@solana/kit';
import {TOKEN_PROGRAM_ADDRESS} from '@solana-program/token';
import {addSolanaQuotedRoute,QuoteStore} from '../src/solana-quoted.mjs';
import {QUOTED_ROUTE,QUOTED_URL,SOLANA,SOLANA_ASSET,SOLANA_PAY_TO} from '../src/pricing.mjs';
import {readRequest} from '../src/app.mjs';
import {payDirect,validateDirectQuote,validateDirectTransfer,reconcileDirect,PaymentRefused} from '../src/solana-direct-buyer.mjs';
import {ata} from '../src/solana-buyer.mjs';
import {SolanaChain} from '../../solana-x402-service/src/chain.mjs';
const signer=await createKeyPairSignerFromPrivateKeyBytes(new Uint8Array(32).fill(21));
const owner=(await createKeyPairSignerFromPrivateKeyBytes(new Uint8Array(32).fill(22))).address;
const agent=signer.address,feePayer=(await createKeyPairSignerFromPrivateKeyBytes(new Uint8Array(32).fill(23))).address;
const requestId='1'.repeat(32),token='test-secret-'.repeat(4),signature='a'.repeat(88);
const question={messages:[{role:'user',content:'Hi'}],requestId,owner,agent};
const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64');
const decode=x=>JSON.parse(Buffer.from(x,'base64').toString());
async function serve(t,opts={}) {
  const counts={model:0,verify:0,settle:0};
  const facilitator={getSupported:async()=>({kinds:[{x402Version:2,scheme:'exact',network:SOLANA,extra:{feePayer}}]}),
    verify:async()=>{counts.verify++;if(opts.failVerify)throw new Error('facilitator offline');return {isValid:true,payer:agent};},
    settle:async()=>{counts.settle++;if(opts.failSettlement)throw new Error('lost receipt');return {success:true,network:SOLANA,payer:agent,transaction:signature};}};
  const app=express();app.use(express.json());
  const store=new QuoteStore(opts.db??':memory:');t.after(()=>store.close());
  addSolanaQuotedRoute(app,{quoteToken:token},{store,facilitatorClient:facilitator,readRequest,
    upstream:async()=>{counts.model++;if(opts.failModel)throw new Error('model offline');return {content:'Hello',usage:{buyer_cost_micro:opts.cost??100,total_tokens:42}};}});
  const listener=await new Promise((resolve,reject)=>{const l=app.listen(0,'127.0.0.1',()=>resolve(l));l.on('error',reject);});
  t.after(()=>listener.close());
  const url=`http://127.0.0.1:${listener.address().port}`;
  const send=(suffix,body={},headers={})=>fetch(url+QUOTED_ROUTE+suffix,{method:'POST',headers:{'Content-Type':'application/json','X-Singit-Ask-Token':token,...headers},body:JSON.stringify(body)});
  return {send,counts,store,url};
}
async function prepare(s){const r=await s.send('/prepare',question);assert.equal(r.status,200);return (await r.json()).quote;}
const payment=q=>({'PAYMENT-SIGNATURE':encode({x402Version:2,accepted:q.accepts[0],resource:q.resource,payload:{transaction:'fixture'}})});
async function receiptTx(memo=`singit-ask:${requestId}`) {
 const source=await ata(owner),dest=await ata(SOLANA_PAY_TO);
 const keys=[{pubkey:feePayer,signer:true},{pubkey:agent,signer:true},{pubkey:source,signer:false},{pubkey:dest,signer:false}];
 const b=(index,amount)=>({accountIndex:index,mint:SOLANA_ASSET,uiTokenAmount:{amount:String(amount),decimals:6}});
 return {meta:{err:null,preTokenBalances:[b(2,5000),b(3,0)],postTokenBalances:[b(2,3870),b(3,1130)]},
 transaction:{signatures:[signature],message:{accountKeys:keys,instructions:[
 {programId:TOKEN_PROGRAM_ADDRESS,parsed:{type:'transferChecked',info:{source,destination:dest,authority:agent,mint:SOLANA_ASSET,tokenAmount:{amount:'1130'}}}},
 {program:'spl-memo',parsed:memo}]}}};
}
test('private preparation needs authentication and withholds answer until payment',async t=>{
 const s=await serve(t);
 assert.equal((await s.send('/prepare',question,{'X-Singit-Ask-Token':''})).status,401);assert.equal(s.counts.model,0);
 const q=await prepare(s);assert.equal(q.accepts[0].amount,'1130');assert.equal(q.accepts[0].scheme,'exact');assert.equal(q.choices,undefined);
 const unpaid=await s.send('/'+requestId);assert.equal(unpaid.status,402);assert.equal(decode(unpaid.headers.get('payment-required')).accepts[0].amount,'1130');
 assert.equal(s.counts.settle,0);assert.equal((await s.send('/prepare',question)).status,200);assert.equal(s.counts.model,1);
 assert.equal((await s.send('/prepare',{...question,messages:[{role:'user',content:'Changed'}]})).status,409);
});
test('model failure sends no payment',async t=>{
 const s=await serve(t,{failModel:true});assert.equal((await s.send('/prepare',question)).status,502);
 assert.deepEqual(s.counts,{model:1,verify:0,settle:0});assert.equal(s.store.get(requestId).status,'failed');
});
test('single durable claim prevents concurrent settlement and response replay',async t=>{
 const s=await serve(t),q=await prepare(s);
 const responses=await Promise.all([s.send('/'+requestId,{},payment(q)),s.send('/'+requestId,{},payment(q))]);
 assert.deepEqual(responses.map(r=>r.status).sort(),[200,409]);assert.equal(s.counts.settle,1);
 const good=responses.find(r=>r.status===200);assert.equal((await good.json()).choices[0].message.content,'Hello');
 assert.equal(s.store.get(requestId).status,'paid');assert.equal((await s.send('/'+requestId,{},payment(q))).status,409);
});
test('settlement uncertainty survives a reopened DB and is never settled again',async t=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'ask-quotes-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const db=path.join(dir,'quotes.db'),s=await serve(t,{db,failSettlement:true}),q=await prepare(s);
 assert.equal((await s.send('/'+requestId,{},payment(q))).status,503);
 const reopen=new QuoteStore(db);assert.equal(reopen.get(requestId).status,'unresolved');reopen.close();
 assert.equal((await s.send('/'+requestId,{},payment(q))).status,409);assert.equal(s.counts.settle,1);
 // The gateway proves the outcome on chain before it asks again; the record stays for review.
 assert.equal((await s.send('/prepare',{...question,requestId:'2'.repeat(32)})).status,200);
 assert.equal(s.store.get(requestId).status,'unresolved');
});
test('a facilitator that cannot verify leaves the quote unpaid and the account free',async t=>{
 const s=await serve(t,{failVerify:true}),q=await prepare(s);
 const r=await s.send('/'+requestId,{},payment(q));
 assert.equal(r.status,502);assert.equal((await r.json()).error,'verification_unavailable');
 assert.equal(s.counts.settle,0);assert.equal(s.store.get(requestId).status,'failed');
 assert.equal((await s.send('/prepare',{...question,requestId:'2'.repeat(32)})).status,200);
});
test('a new request replaces an abandoned unpaid quote, which can no longer be paid',async t=>{
 const s=await serve(t),q=await prepare(s);
 assert.equal((await s.send('/prepare',{...question,requestId:'2'.repeat(32)})).status,200);
 const old=await s.send('/'+requestId,{},payment(q));
 assert.equal(old.status,409);assert.equal((await old.json()).error,'quote_unavailable');assert.equal(s.counts.verify,0);
});
test('altered recipient, price, owner, memo or fee payer is refused',async t=>{
 const s=await serve(t),q=await prepare(s);validateDirectQuote(q,{requestId,owner,agent});
 for(const change of [x=>x.accepts[0].amount='3000',x=>x.accepts[0].payTo=owner,x=>x.owner=agent,
 x=>x.accepts[0].extra.memo='other',x=>x.accepts[0].extra.feePayer=agent,x=>delete x.expiresAt]){
  const copy=structuredClone(q);change(copy);assert.throws(()=>validateDirectQuote(copy,{requestId,owner,agent}));
 }
 const altered=structuredClone(q);altered.accepts[0].amount='1';
 assert.equal((await s.send('/'+requestId,{},payment(altered))).status,400);assert.equal(s.counts.verify,0);
});
test('chain validation rejects wrong debit, recipient, signer, memo and native fee payer',async()=>{
 const expected={signature,owner,agent,amount:'1130',memo:`singit-ask:${requestId}`,feePayer};
 await validateDirectTransfer(await receiptTx(),expected);
 for(const change of [x=>x.meta.postTokenBalances[0].uiTokenAmount.amount='3869',x=>x.meta.postTokenBalances[1].uiTokenAmount.amount='1129',
 x=>x.transaction.message.accountKeys[1].signer=false,x=>x.transaction.message.accountKeys[0].pubkey=owner,
 x=>x.transaction.message.instructions[1].parsed='different',x=>x.transaction.message.instructions[0].programId=owner]){
 const tx=await receiptTx();change(tx);await assert.rejects(validateDirectTransfer(tx,expected));
 }
});
test('real delegated builder and merchant pay once with zero agent SOL and no funding leg',async t=>{
 const s=await serve(t);let cp,posts=0;
 const chain=new SolanaChain('https://unused.invalid');chain.rpc={getLatestBlockhash:()=>({send:async()=>({value:{blockhash:'11111111111111111111111111111111',lastValidBlockHeight:999999n}})})};
 const result=await payDirect({owner,wallet:{address:agent,signer},requestId,token,body:question,chain,beforeSubmit:async v=>{cp=v;},
 rpc:async method=>{if(method==='getAccountInfo')return {value:{owner:TOKEN_PROGRAM_ADDRESS,data:{parsed:{info:{mint:SOLANA_ASSET,owner:SOLANA_PAY_TO,state:'initialized'}}}}};return receiptTx();},
 fetchImpl:async(url,init)=>{posts++;if(posts===2){assert.ok(cp);assert.equal(cp.amountAtomic,'1130');const payload=decode(init.headers['PAYMENT-SIGNATURE']);assert.ok(payload.payload.transaction);}
 return fetch(url.replace('https://ask.singitai.app',s.url),init);}});
 assert.equal(result.amountAtomic,'1130');assert.equal(posts,2);assert.equal(cp.lastValidBlockHeight,'999999');assert.equal(s.counts.settle,1);assert.equal(result.owner,owner);
});
test('lost paid response is submitted only once and leaves checkpoint',async t=>{
 const s=await serve(t);let cp,calls=0;
 await assert.rejects(payDirect({owner,wallet:{address:agent},requestId,token,body:question,
 chain:{buildDelegated:async()=>({payload:{}})},beforeSubmit:async v=>{cp=v;},
 rpc:async()=>({value:{owner:TOKEN_PROGRAM_ADDRESS,data:{parsed:{info:{mint:SOLANA_ASSET,owner:SOLANA_PAY_TO,state:'initialized'}}}}}),
 fetchImpl:async(url,init)=>{calls++;if(calls===2)throw new Error('lost');return fetch(url.replace('https://ask.singitai.app',s.url),init);}}));
 assert.ok(cp);assert.equal(calls,2);
});

test('over-ceiling model usage is never offered or settled',async t=>{
 const s=await serve(t,{cost:2000});assert.equal((await s.send('/prepare',question)).status,502);
 assert.equal(s.counts.verify,0);assert.equal(s.counts.settle,0);
});
test('expired unpaid quotes cannot be claimed; a settlement in flight holds the account',()=>{
 const store=new QuoteStore(':memory:');
 try {
  store.create(requestId,owner,agent,'hash',1000);store.ready(requestId,{}, {},1000);
  assert.equal(store.claim(requestId,1301),false);
  store.create('2'.repeat(32),owner,agent,'hash2',1301);store.ready('2'.repeat(32),{}, {},1301);
  assert.equal(store.claim('2'.repeat(32),1302),true);
  assert.throws(()=>store.create('3'.repeat(32),owner,agent,'hash3',1400));
  store.create('3'.repeat(32),owner,agent,'hash3',99999);
  assert.equal(store.get('2'.repeat(32)).status,'settling');
 } finally {store.close();}
});
async function paidThrough(s,reply){
 return payDirect({owner,wallet:{address:agent},requestId,token,body:question,beforeSubmit:async()=>{},
  chain:{buildDelegated:async()=>({payload:{},lastValidBlockHeight:'500'})},
  rpc:async()=>({value:{owner:TOKEN_PROGRAM_ADDRESS,data:{parsed:{info:{mint:SOLANA_ASSET,owner:SOLANA_PAY_TO,state:'initialized'}}}}}),
  fetchImpl:async(url,init)=>url.endsWith('/prepare')?fetch(url.replace('https://ask.singitai.app',s.url),init):reply()});
}
test('only the merchant\'s own refusals before settlement count as unpaid',async t=>{
 const s=await serve(t);
 for(const [status,error] of [[402,'verification_refused'],[502,'verification_unavailable'],[409,'quote_unavailable'],[400,'terms_changed']]){
  await assert.rejects(paidThrough(s,async()=>Response.json({error},{status})),PaymentRefused);
 }
 // A second submission of a quote already being settled is not a refusal.
 const q=await prepare(s);s.store.claim(requestId,Math.floor(Date.now()/1000));
 const again=await s.send('/'+requestId,{},payment(q));assert.equal(again.status,409);assert.equal((await again.json()).error,'already_submitted');
 for(const reply of [async()=>Response.json({error:'payment_unresolved'},{status:503}),async()=>Response.json({error:'already_submitted'},{status:409}),
  async()=>new Response('{}',{status:402}),async()=>new Response('bad gateway',{status:502}),async()=>{throw new Error('lost');}]){
  await assert.rejects(paidThrough(s,reply),e=>!(e instanceof PaymentRefused));
 }
});
test('the chain settles up a lost payment: paid, unpaid once its blockhash is dead, or pending',async()=>{
 const memo=`singit-ask:${requestId}`,cp={owner,agent,amount:'1130',memo,feePayer,lastValidBlockHeight:'500'};
 const asked=[];
 const chain=(height,listed)=>async(method,params)=>{asked.push([method,params]);
  return method==='getEpochInfo'?{blockHeight:height,absoluteSlot:height+70}:method==='getSignaturesForAddress'?listed:receiptTx();};
 assert.deepEqual(await reconcileDirect(cp,chain(600,[{signature,err:null,memo:`[45] ${memo}`}])),{state:'paid',transaction:signature});
 assert.deepEqual(await reconcileDirect(cp,chain(600,[{signature,err:null,memo:'[5] other'},{signature,err:{},memo}])),{state:'unpaid'});
 assert.deepEqual(await reconcileDirect(cp,chain(500,[])),{state:'pending'});
 await assert.rejects(reconcileDirect({...cp,lastValidBlockHeight:undefined},chain(600,[])));
 await assert.rejects(reconcileDirect({...cp,amount:'3000'},chain(600,[{signature,err:null,memo}])));
 const listing=asked.find(([m])=>m==='getSignaturesForAddress')[1];
 assert.equal(listing[0],await ata(owner));assert.equal(listing[1].minContextSlot,670);assert.equal(listing[1].commitment,'finalized');
});
