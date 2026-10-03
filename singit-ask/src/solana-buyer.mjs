import { UptoSvmScheme } from '@x402/svm/upto/client';
import { address, isAddress, getBase58Encoder } from '@solana/kit';
import { findAssociatedTokenPda, TOKEN_PROGRAM_ADDRESS } from '@solana-program/token';
import { SOLANA, SOLANA_ASSET, SOLANA_PAY_TO, SOLANA_TERMS, billUsage } from './pricing.mjs';
// An x402 buyer for the public route's Solana offer: also the operator's live check of it.
export const ENDPOINT='https://ask.singitai.app/v1/chat/completions';
export const CHANNEL_PROGRAM='CHNLxYvVA28MJP9PrFuDXccuoGXAx7jBacfLEkahyGsX';
const decode=v=>JSON.parse(Buffer.from(v,'base64').toString());
export async function ata(owner) {
  return (await findAssociatedTokenPda({owner:address(owner),mint:address(SOLANA_ASSET),tokenProgram:TOKEN_PROGRAM_ADDRESS}))[0];
}
export function selectSolanaOffer(q) {
  const offers=q?.accepts?.filter(a=>a.scheme==='upto'&&a.network===SOLANA)??[];
  if(q?.x402Version!==2||q.resource?.url!==ENDPOINT||offers.length!==1)throw new Error('Unexpected Solana resource.');
  const a=offers[0],e=a.extra;
  if(a.asset!==SOLANA_ASSET||a.payTo!==SOLANA_PAY_TO||a.amount!==SOLANA_TERMS.maxChargeAtomic
    ||a.maxTimeoutSeconds!==300||e?.withdrawDelay!==300||e?.tokenProgram!==TOKEN_PROGRAM_ADDRESS
    ||(e.paymentFlow??'escrow')!=='escrow'||!isAddress(e.feePayer??'')||!isAddress(e.receiverAuthorizer??'')
    ||Object.entries(SOLANA_TERMS).some(([k,v])=>e.billing?.[k]!==v))throw new Error('Changed Solana billing terms.');
  return a;
}
export function validateSolanaBill(data) {
  const expected=billUsage(data?.usage,SOLANA_TERMS);
  if(Object.entries(expected).some(([k,v])=>data?.billing?.[k]!==v))throw new Error('Invalid actual-usage invoice.');
  return expected;
}
export async function requireReceiver(rpc) {
  const result=await rpc('getAccountInfo',[await ata(SOLANA_PAY_TO),{encoding:'jsonParsed',commitment:'confirmed'}]);
  const value=result?.value,info=value?.data?.parsed?.info;
  if(value?.owner!==TOKEN_PROGRAM_ADDRESS||info?.mint!==SOLANA_ASSET||info?.owner!==SOLANA_PAY_TO||info?.state!=='initialized') {
    const error=new Error('Merchant USDC account is not ready.');error.code='receiver_not_ready';throw error;
  }
}
export async function validateSolanaSettlement(tx,{payer,channelId,amount,signature}) {
  const cap=BigInt(SOLANA_TERMS.maxChargeAtomic),actual=BigInt(amount);
  if(!tx?.meta||tx.meta.err!==null||!tx.transaction?.signatures?.includes(signature))throw new Error('Settlement not confirmed.');
  const keys=tx.transaction.message.accountKeys.map(k=>typeof k==='string'?k:k.pubkey);
  const recipientAta=await ata(SOLANA_PAY_TO),payerAta=await ata(payer),escrowAta=await ata(channelId);
  const before=tx.meta.preTokenBalances??[],after=tx.meta.postTokenBalances??[];
  const balance=(list,key)=>{
    const b=list.find(b=>keys[Number(b.accountIndex)]===key);
    if(!b)return 0n;
    if(b.mint!==SOLANA_ASSET||b.uiTokenAmount.decimals!==6)throw new Error('Unexpected token.');
    return BigInt(b.uiTokenAmount.amount);
  };
  if(balance(after,recipientAta)-balance(before,recipientAta)!==actual
    ||balance(after,payerAta)-balance(before,payerAta)!==cap-actual
    ||balance(before,escrowAta)!==cap||balance(after,escrowAta)!==0n)throw new Error('Payout or refund differs from invoice.');
  const distributes=tx.transaction.message.instructions.filter(ix=>ix.programId===CHANNEL_PROGRAM
    &&ix.accounts?.includes(channelId)&&ix.accounts?.includes(escrowAta)&&ix.accounts?.includes(recipientAta)
    &&typeof ix.data==='string'&&getBase58Encoder().encode(ix.data)[0]===7);
  if(distributes.length!==1)throw new Error('No canonical channel distribution for this request.');
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export async function paySolana({signer,body,rpc,rpcUrl,fetchImpl=fetch,beforeSubmit,checkOnly=false}) {
  const send=headers=>fetchImpl(ENDPOINT,{method:'POST',redirect:'error',signal:AbortSignal.timeout(120000),
    headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
  const quote=await send({});
  if(quote.status!==402)throw new Error('No Solana payment quote.');
  const q=decode(quote.headers.get('payment-required')),accepted=selectSolanaOffer(q);
  await requireReceiver(rpc);
  if(checkOnly)return {ok:true,ready:true};
  if(!signer||[SOLANA_PAY_TO,accepted.extra.feePayer].includes(signer.address))throw new Error('Invalid payer.');
  const signed=await new UptoSvmScheme(signer,{rpcUrl}).createPaymentPayload(2,accepted);
  const p=signed.payload;
  if(p.from!==signer.address||p.maxAmount!==accepted.amount||p.deposit!==accepted.amount)throw new Error('Invalid ceiling.');
  if(!beforeSubmit)throw new Error('A durable checkpoint is required.');
  await beforeSubmit({payer:p.from,channelId:p.channelId,nonce:p.nonce,ceilingAtomic:p.maxAmount,
    expiresAt:p.expiresAt,feePayer:accepted.extra.feePayer,withdrawDelay:accepted.extra.withdrawDelay});
  const response=await send({'PAYMENT-SIGNATURE':Buffer.from(JSON.stringify({x402Version:2,resource:q.resource,accepted,payload:p})).toString('base64')});
  const header=response.headers.get('payment-response'),receipt=header?decode(header):null;
  const refunded=response.status!==200&&receipt?.amount==='0';
  if(response.status!==200&&!refunded)throw new Error('Payment or refund needs review. The request was not repeated.');
  const data=refunded?null:await response.json(),billing=refunded?{totalAtomic:'0'}:validateSolanaBill(data);
  if(receipt?.success!==true||receipt.network!==SOLANA||receipt.payer!==signer.address||receipt.amount!==billing.totalAtomic
    ||typeof receipt.transaction!=='string'||!/^([1-9A-HJ-NP-Za-km-z]){80,90}$/.test(receipt.transaction))throw new Error('Invalid Solana receipt.');
  let tx;
  for(let i=0;i<8;i++){
    tx=await rpc('getTransaction',[receipt.transaction,{encoding:'jsonParsed',commitment:'confirmed',maxSupportedTransactionVersion:0}]);
    if(tx)break;
    await sleep(1500);
  }
  await validateSolanaSettlement(tx,{payer:signer.address,channelId:p.channelId,amount:billing.totalAtomic,signature:receipt.transaction});
  return {ok:!refunded,refunded,body:data,amountAtomic:billing.totalAtomic,transaction:receipt.transaction,payer:signer.address,
    channelId:p.channelId,refundAtomic:(BigInt(p.maxAmount)-BigInt(billing.totalAtomic)).toString()};
}
