// Internal helper: keys only on stdin, never in argv, checkpoints or output.
import fs from 'node:fs';
import path from 'node:path';
import {createKeyPairSignerFromBytes,getBase58Encoder} from '@solana/kit';
import {paySolana,validateSolanaSettlement} from './solana-buyer.mjs';
let submitted=false;
try {
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  const rpcUrl=input.rpcUrl||'https://api.mainnet-beta.solana.com';
  const rpc=async(method,params)=>{
    const response=await fetch(rpcUrl,{method:'POST',signal:AbortSignal.timeout(15000),headers:{'Content-Type':'application/json'},
      body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});
    const data=await response.json();if(!response.ok||data.error)throw new Error('Solana RPC failed');return data.result;
  };
  if(input.verify){
    const tx=await rpc('getTransaction',[input.verify.signature,{encoding:'jsonParsed',commitment:'confirmed',maxSupportedTransactionVersion:0}]);
    await validateSolanaSettlement(tx,input.verify);
    console.log(JSON.stringify({ok:true,verified:true}));
  } else {
    let signer;
    if(!input.checkOnly){const bytes=getBase58Encoder().encode(input.privateKey);signer=await createKeyPairSignerFromBytes(bytes);bytes.fill(0);}
    delete input.privateKey;
    const result=await paySolana({signer,body:input.body,rpc,rpcUrl,checkOnly:input.checkOnly,beforeSubmit:async checkpoint=>{
      const fd=fs.openSync(input.checkpoint,'wx',0o600);
      try{fs.writeFileSync(fd,JSON.stringify(checkpoint));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
      const dir=fs.openSync(path.dirname(input.checkpoint),'r');try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}
      submitted=true;
    }});
    console.log(JSON.stringify({...result,submitted}));
  }
} catch(error){
  console.log(JSON.stringify({ok:false,submitted,error:error.code==='receiver_not_ready'?'receiver_not_ready':submitted?'payment_unresolved':'payment_not_submitted'}));
  process.exitCode=1;
}
