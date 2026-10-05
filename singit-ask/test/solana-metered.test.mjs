import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createKeyPairSignerFromPrivateKeyBytes} from '@solana/kit';
import {TOKEN_PROGRAM_ADDRESS} from '@solana-program/token';
import {createApp,ROUTE} from '../src/app.mjs';
import {BASE,SOLANA,SOLANA_TERMS,SOLANA_ASSET,SOLANA_PAY_TO,billUsage} from '../src/pricing.mjs';
import {ENDPOINT,CHANNEL_PROGRAM,ata,selectSolanaOffer,paySolana,validateSolanaSettlement} from '../src/solana-buyer.mjs';
const feePayer='Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88',authorizer='9dpHxn3XFZMZv59vE5MKxhfwGUCCgkcCUzYZLpdEm7ox';
const encode=v=>Buffer.from(JSON.stringify(v)).toString('base64');
const decode=v=>JSON.parse(Buffer.from(v,'base64').toString());
const sig='a'.repeat(88),signer=await createKeyPairSignerFromPrivateKeyBytes(new Uint8Array(32).fill(23));
const offer=()=>({x402Version:2,resource:{url:ENDPOINT},accepts:[{scheme:'upto',network:SOLANA,amount:'3000',asset:SOLANA_ASSET,
  payTo:SOLANA_PAY_TO,maxTimeoutSeconds:300,extra:{feePayer,receiverAuthorizer:authorizer,withdrawDelay:300,tokenProgram:TOKEN_PROGRAM_ADDRESS,
    billing:SOLANA_TERMS,recentBlockhash:'11111111111111111111111111111111',lastValidBlockHeight:'99999999',recentSlot:'123456'}}]});
const body={choices:[{message:{content:'Hello'}}],usage:{buyer_cost_micro:100},billing:billUsage({buyer_cost_micro:100},SOLANA_TERMS)};
const receiver=()=>({value:{owner:TOKEN_PROGRAM_ADDRESS,data:{parsed:{info:{mint:SOLANA_ASSET,owner:SOLANA_PAY_TO,state:'initialized'}}}}});
async function chainReceipt(channelId,refund=870,charge=2130){
  const keys=[await ata(SOLANA_PAY_TO),await ata(signer.address),await ata(channelId),channelId,CHANNEL_PROGRAM];
  const token=(i,n)=>({accountIndex:i,mint:SOLANA_ASSET,uiTokenAmount:{amount:String(n),decimals:6}});
  return {meta:{err:null,preTokenBalances:[token(0,0),token(1,0),token(2,3000)],postTokenBalances:[token(0,charge),token(1,refund)]},
    transaction:{signatures:[sig],message:{accountKeys:keys,instructions:[{programId:CHANNEL_PROGRAM,accounts:keys.slice(0,4),data:'8'}]}}};
}
test('Solana covers two settlements; Base fee remains unchanged',()=>{
  assert.equal(body.billing.totalAtomic,'2130');assert.equal(billUsage({buyer_cost_micro:100}).totalAtomic,'1130');
  assert.throws(()=>billUsage({buyer_cost_micro:770},SOLANA_TERMS));
});
test('Solana quote pins merchant, token, ceiling and escrow terms',()=>{
  assert.equal(selectSolanaOffer(offer()).amount,'3000');
  for(const [k,v] of [['network',BASE],['payTo',signer.address],['amount','3001'],['asset',signer.address]]){
    const q=offer();q.accepts[0][k]=v;assert.throws(()=>selectSolanaOffer(q));
  }
  for(const [k,v] of [['withdrawDelay',0],['paymentFlow','deferred'],['billing',{...SOLANA_TERMS,settlementFeeAtomic:'1000'}],['feePayer','bad']]){
    const q=offer();q.accepts[0].extra[k]=v;assert.throws(()=>selectSolanaOffer(q));
  }
});
test('missing receiver token account stops before signing or submission',async()=>{
  let calls=0,submitted=false;
  await assert.rejects(paySolana({signer,body:{},rpc:async()=>({value:null}),beforeSubmit:async()=>{submitted=true;},
    fetchImpl:async()=>{calls++;return new Response('',{status:402,headers:{'payment-required':encode(offer())}});}}),{code:'receiver_not_ready'});
  assert.equal(calls,1);assert.equal(submitted,false);
});
test('real SDK signs one request; receipt proves charge and full unused refund',async()=>{
  let calls=0,checkpoint;
  const result=await paySolana({signer,body:{messages:[{role:'user',content:'Hi'}]},rpcUrl:'https://unused.test',
    beforeSubmit:async cp=>{checkpoint=cp;},rpc:async method=>method==='getAccountInfo'?receiver():chainReceipt(checkpoint.channelId),
    fetchImpl:async(url,init)=>{
      assert.equal(url,ENDPOINT);calls++;
      if(calls===1)return new Response('',{status:402,headers:{'payment-required':encode(offer())}});
      assert.ok(checkpoint);const p=decode(init.headers['PAYMENT-SIGNATURE']).payload;
      assert.equal(p.deposit,'3000');assert.equal(p.maxAmount,'3000');assert.equal(p.channelId,checkpoint.channelId);
      return new Response(JSON.stringify(body),{status:200,headers:{'payment-response':encode({success:true,network:SOLANA,payer:signer.address,amount:'2130',transaction:sig})}});
    }});
  assert.equal(calls,2);assert.equal(result.amountAtomic,'2130');assert.equal(result.refundAtomic,'870');
  await assert.rejects(validateSolanaSettlement(await chainReceipt(checkpoint.channelId,869),{payer:signer.address,channelId:checkpoint.channelId,amount:'2130',signature:sig}));
});
test('confirmed model-error refund returns all funds and reports no charge',async()=>{
  let checkpoint,calls=0;
  const result=await paySolana({signer,body:{},rpcUrl:'https://unused.test',beforeSubmit:async cp=>{checkpoint=cp;},
    rpc:async method=>method==='getAccountInfo'?receiver():chainReceipt(checkpoint.channelId,3000,0),fetchImpl:async()=>{
      calls++;if(calls===1)return new Response('',{status:402,headers:{'payment-required':encode(offer())}});
      return new Response('{}',{status:502,headers:{'payment-response':encode({success:true,network:SOLANA,payer:signer.address,amount:'0',transaction:sig})}});
    }});
  assert.equal(result.ok,false);assert.equal(result.refunded,true);assert.equal(result.refundAtomic,'3000');assert.equal(calls,2);
});
test('lost paid response is not retried',async()=>{
  let calls=0,submitted=false;
  await assert.rejects(paySolana({signer,body:{},rpc:async()=>receiver(),rpcUrl:'https://unused.test',beforeSubmit:async()=>{submitted=true;},
    fetchImpl:async()=>{calls++;if(calls===1)return new Response('',{status:402,headers:{'payment-required':encode(offer())}});throw new Error('timeout');}}));
  assert.equal(calls,2);assert.equal(submitted,true);
});
for(const fail of [false,true])test(`public route: Solana deposits before the model and ${fail?'refunds on error':'settles actual usage'}`,async t=>{
  const settles=[];
  const facilitator={getSupported:async()=>({kinds:[{x402Version:2,scheme:'exact',network:BASE},{x402Version:2,scheme:'upto',network:BASE,extra:{facilitatorAddress:'0x'+'1'.repeat(40)}},
    {x402Version:2,scheme:'exact',network:SOLANA,extra:{feePayer}},{x402Version:2,scheme:'upto',network:SOLANA,extra:{feePayer,receiverAuthorizer:authorizer}}],extensions:[],signers:{}}),
    verify:async()=>({isValid:true,payer:signer.address}),settle:async(p,r)=>{settles.push({type:p.payload.type,amount:r.amount});return{success:true,network:SOLANA,payer:signer.address,transaction:sig,amount:r.amount};}};
  const app=createApp({payToBase:'0x'+'1'.repeat(40),payToSolana:SOLANA_PAY_TO},
    {facilitatorClient:facilitator,log:()=>{},upstream:async()=>{
      assert.deepEqual(settles,[{type:'deposit',amount:'3000'}]);if(fail)throw new Error('failed');return{content:'Hello',usage:{buyer_cost_micro:100}};}});
  const listener=await new Promise(resolve=>{const l=app.listen(0,'127.0.0.1',()=>resolve(l));});t.after(()=>listener.close());
  const send=h=>fetch(`http://127.0.0.1:${listener.address().port}${ROUTE}`,{method:'POST',headers:{'Content-Type':'application/json',...h},body:JSON.stringify({messages:[{role:'user',content:'Hi'}]})});
  const first=await send({});assert.equal(first.status,402);const q=decode(first.headers.get('payment-required'));
  const p={from:signer.address,channelId:authorizer,maxAmount:'3000',deposit:'3000',nonce:'1',openSlot:'123',openTransaction:'mock',expiresAt:Math.floor(Date.now()/1000)+300,validAfter:Math.floor(Date.now()/1000),authorizedSigner:authorizer};
  const accepted=q.accepts.find(a=>a.network===SOLANA);assert.equal(accepted.scheme,'upto');assert.deepEqual(accepted.extra.billing,SOLANA_TERMS);
  const response=await send({'PAYMENT-SIGNATURE':encode({x402Version:2,resource:q.resource,accepted,payload:p})});
  assert.equal(response.status,fail?502:200);assert.equal(decode(response.headers.get('payment-response')).amount,fail?'0':'2130');assert.deepEqual(settles,[{type:'deposit',amount:'3000'},{type:'claim',amount:fail?'0':'2130'}]);
});
