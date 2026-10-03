import {SOLANA,SOLANA_ASSET,SOLANA_PAY_TO,billUsage} from './pricing.mjs';
import {DIRECT_TERMS,QUOTED_URL} from './pricing.mjs';
import {isAddress} from '@solana/kit';
import {TOKEN_PROGRAM_ADDRESS} from '@solana-program/token';
import {ata,requireReceiver} from './solana-buyer.mjs';
const decode=s=>JSON.parse(Buffer.from(s,'base64').toString());
export function validateDirectQuote(quote,{requestId,owner,agent}) {
  const r=quote?.accepts?.[0],bill=billUsage(quote?.usage,DIRECT_TERMS);
  if(quote?.x402Version!==2||quote.quoteId!==requestId||quote.owner!==owner||quote.agent!==agent||quote.accepts?.length!==1
    ||quote.resource?.url!==`${QUOTED_URL}/${requestId}`||r.scheme!=='exact'||r.network!==SOLANA||r.asset!==SOLANA_ASSET
    ||r.payTo!==SOLANA_PAY_TO||r.amount!==bill.totalAtomic||r.maxTimeoutSeconds!==300||r.extra?.memo!==`singit-ask:${requestId}`
    ||!isAddress(r.extra?.feePayer??'')||[owner,agent].includes(r.extra.feePayer)||!Number.isSafeInteger(quote.expiresAt)
    ||quote.expiresAt<Math.floor(Date.now()/1000)||quote.expiresAt>Math.floor(Date.now()/1000)+310
    ||Object.entries(bill).some(([k,v])=>quote.billing?.[k]!==v||r.extra?.billing?.[k]!==v))throw new Error('Changed direct payment terms');
  return r;
}
export async function validateDirectTransfer(tx,{signature,owner,agent,amount,memo,feePayer}) {
  if(!tx?.meta||tx.meta.err!==null||!tx.transaction?.signatures?.includes(signature))throw new Error('Unconfirmed payment');
  const keys=tx.transaction.message.accountKeys.map(k=>typeof k==='string'?k:k.pubkey);
  if(keys[0]!==feePayer||[owner,agent].includes(feePayer)
    ||!tx.transaction.message.accountKeys.some(k=>k.pubkey===agent&&k.signer===true))throw new Error('Unexpected gas payer');
  const source=await ata(owner),dest=await ata(SOLANA_PAY_TO);
  const bal=(list,address)=>{const b=(list??[]).find(b=>keys[b.accountIndex]===address);
    if(!b||b.mint!==SOLANA_ASSET||b.uiTokenAmount.decimals!==6)throw new Error('Missing USDC account');return BigInt(b.uiTokenAmount.amount);};
  if(bal(tx.meta.preTokenBalances,source)-bal(tx.meta.postTokenBalances,source)!==BigInt(amount)
    ||bal(tx.meta.postTokenBalances,dest)-bal(tx.meta.preTokenBalances,dest)!==BigInt(amount))throw new Error('Wrong USDC transfer');
  const ix=tx.transaction.message.instructions;
  const transfers=ix.filter(i=>i.programId===TOKEN_PROGRAM_ADDRESS&&i.parsed?.type==='transferChecked'&&i.parsed.info?.source===source&&i.parsed.info?.destination===dest
    &&i.parsed.info?.authority===agent&&i.parsed.info?.mint===SOLANA_ASSET&&i.parsed.info?.tokenAmount?.amount===String(amount));
  if(transfers.length!==1||!ix.some(i=>i.program==='spl-memo'&&i.parsed===memo))throw new Error('Wrong payment instructions');
}
export async function payDirect({body,owner,wallet,requestId,token,chain,rpc,beforeSubmit,fetchImpl=fetch}) {
  const send=(url,body,headers={})=>fetchImpl(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(90000),
    headers:{'Content-Type':'application/json','X-Singit-Ask-Token':token,...headers},body:JSON.stringify(body)});
  await requireReceiver(rpc);
  const prepared=await send(`${QUOTED_URL}/prepare`,{...body,requestId,owner,agent:wallet.address});
  if(!prepared.ok)throw new Error('Could not prepare answer');
  const {quote}=await prepared.json();
  const accepted=validateDirectQuote(quote,{requestId,owner,agent:wallet.address});
  const built=await chain.buildDelegated(accepted,quote.resource,wallet,owner);
  await beforeSubmit({requestId,owner,payer:wallet.address,amountAtomic:accepted.amount,feePayer:accepted.extra.feePayer,
    memo:accepted.extra.memo,expiresAt:quote.expiresAt});
  const response=await send(quote.resource.url,{}, {'PAYMENT-SIGNATURE':Buffer.from(JSON.stringify(built.payload)).toString('base64')});
  if(!response.ok)throw new Error('Submitted payment unresolved');
  const receipt=decode(response.headers.get('payment-response'));
  const answer=await response.json(),bill=billUsage(answer.usage,DIRECT_TERMS);
  if(receipt.success!==true||receipt.network!==SOLANA||receipt.payer!==wallet.address||receipt.amount!==accepted.amount
    ||!/^([1-9A-HJ-NP-Za-km-z]){80,90}$/.test(receipt.transaction??'')||bill.totalAtomic!==accepted.amount
    ||Object.entries(bill).some(([k,v])=>answer.billing?.[k]!==v||quote.billing?.[k]!==v))throw new Error('Wrong invoice');
  let tx;
  for(let i=0;i<10;i++){
    tx=await rpc('getTransaction',[receipt.transaction,{encoding:'jsonParsed',commitment:'confirmed',maxSupportedTransactionVersion:0}]);
    if(tx)break;await new Promise(r=>setTimeout(r,1000));
  }
  await validateDirectTransfer(tx,{signature:receipt.transaction,owner,agent:wallet.address,amount:accepted.amount,memo:accepted.extra.memo,feePayer:accepted.extra.feePayer});
  return {ok:true,payer:wallet.address,owner,amountAtomic:accepted.amount,transaction:receipt.transaction,body:answer};
}
