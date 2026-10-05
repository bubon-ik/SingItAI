import fs from 'node:fs';
import path from 'node:path';
import {getBase58Encoder} from '@solana/kit';
import {SolanaChain} from '../../solana-x402-service/src/chain.mjs';
import {walletFromBytes} from '../../solana-x402-service/src/wallet.mjs';
import {payDirect,validateDirectTransfer,reconcileDirect,PaymentRefused} from './solana-direct-buyer.mjs';
let submitted=false;
try {
 const input=JSON.parse(fs.readFileSync(0,'utf8'));
 const rpcUrl=input.rpcUrl||'https://api.mainnet-beta.solana.com';
 const rpc=async(method,params)=>{const r=await fetch(rpcUrl,{method:'POST',signal:AbortSignal.timeout(20000),headers:{'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});const d=await r.json();if(!r.ok||d.error)throw new Error('RPC failed');return d.result;};
 if(input.reconcile){
  console.log(JSON.stringify({ok:true,...await reconcileDirect(input.reconcile,rpc)}));
 }else if(input.verify){
  const tx=await rpc('getTransaction',[input.verify.signature,{encoding:'jsonParsed',commitment:'confirmed',maxSupportedTransactionVersion:0}]);
  await validateDirectTransfer(tx,input.verify);console.log(JSON.stringify({ok:true,verified:true}));
 }else{
  const bytes=getBase58Encoder().encode(input.privateKey);delete input.privateKey;
  const wallet=await walletFromBytes(bytes);bytes.fill(0);
  const result=await payDirect({...input,wallet,rpc,chain:new SolanaChain(rpcUrl),beforeSubmit:async cp=>{
   const fd=fs.openSync(input.checkpoint,'wx',0o600);try{fs.writeFileSync(fd,JSON.stringify(cp));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
   const dir=fs.openSync(path.dirname(input.checkpoint),'r');try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}submitted=true;
  }});
  console.log(JSON.stringify(result));
 }
}catch(error){
 // settled:false only on the merchant's own refusal before settlement; anything else stays unresolved.
 const refused=submitted&&error instanceof PaymentRefused;
 console.log(JSON.stringify({ok:false,submitted,...(refused?{settled:false}:{}),
  error:refused?'payment_refused':submitted?'payment_unresolved':'payment_not_submitted'}));process.exitCode=1;
}
