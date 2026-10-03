// Private preparation, then one delegated x402 exact payment for measured usage.
// The answer is withheld until settlement. No agent funding or native-gas wallet.
import {createHash, timingSafeEqual} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {isAddress} from '@solana/kit';
import {ExactSvmScheme} from '@x402/svm/exact/server';
import {SOLANA, SOLANA_ASSET, SOLANA_PAY_TO, DIRECT_TERMS, QUOTED_ROUTE, QUOTED_URL, billUsage} from './pricing.mjs';

const encode=v=>Buffer.from(JSON.stringify(v)).toString('base64');
const digest=v=>createHash('sha256').update(JSON.stringify(v)).digest('hex');

export class QuoteStore {
  constructor(file) {
    if(file!==':memory:') {fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});}
    this.db=new DatabaseSync(file);
    if(file!==':memory:')fs.chmodSync(file,0o600);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000;
      CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, owner TEXT NOT NULL, agent TEXT NOT NULL,
      hash TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
      offer TEXT, answer TEXT, receipt TEXT, signature TEXT UNIQUE);
      CREATE INDEX IF NOT EXISTS quote_account ON quotes(owner,agent,status);`);
  }
  get(id){return this.db.prepare('SELECT * FROM quotes WHERE id=?').get(id);}
  create(id,owner,agent,hash,now){
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare("UPDATE quotes SET status='expired',answer=NULL WHERE status IN ('preparing','quoted') AND expires<?").run(now);
      // The gateway sends one request per account at a time, so an older unpaid quote is abandoned.
      // Unresolved settlements stay as records; the gateway proves them on chain before asking again.
      if(this.db.prepare("SELECT 1 FROM quotes WHERE owner=? AND agent=? AND status='settling' AND expires>=?").get(owner,agent,now))throw new Error('pending');
      this.db.prepare("UPDATE quotes SET status='expired',answer=NULL WHERE owner=? AND agent=? AND status IN ('preparing','quoted')").run(owner,agent);
      this.db.prepare("INSERT INTO quotes(id,owner,agent,hash,status,created,expires) VALUES(?,?,?,?,'preparing',?,?)").run(id,owner,agent,hash,now,now+300);
      this.db.exec('COMMIT');
    }catch(e){this.db.exec('ROLLBACK');throw e;}
  }
  ready(id,offer,answer,now){
    const result=this.db.prepare("UPDATE quotes SET status='quoted',offer=?,answer=?,expires=? WHERE id=? AND status='preparing'").run(JSON.stringify(offer),JSON.stringify(answer),now+300,id);
    if(result.changes!==1)throw new Error("Quote expired during preparation");
  }
  claim(id,now){return this.db.prepare("UPDATE quotes SET status='settling' WHERE id=? AND status='quoted' AND expires>=?").run(id,now).changes===1;}
  fail(id,status='failed'){this.db.prepare('UPDATE quotes SET status=? WHERE id=?').run(status,id);}
  paid(id,receipt){this.db.prepare("UPDATE quotes SET status='paid',receipt=?,signature=? WHERE id=? AND status='settling'").run(JSON.stringify(receipt),receipt.transaction,id);}
  close(){this.db.close();}
}
export function addSolanaQuotedRoute(app,config,{facilitatorClient,readRequest,upstream,fetchImpl,store,now=()=>Math.floor(Date.now()/1000)}) {
  if(typeof config.quoteToken!=='string'||config.quoteToken.length<32)throw new Error('Private quote token required');
  store??=new QuoteStore(config.quoteDb);
  const authorized=req=>{
    const a=Buffer.from(req.get('x-singit-ask-token')??''),b=Buffer.from(config.quoteToken);
    return a.length===b.length&&timingSafeEqual(a,b);
  };
  app.use(QUOTED_ROUTE,(req,res,next)=>{
    res.set('Cache-Control','no-store');
    if(!authorized(req))return res.status(401).json({error:'unauthorized'});
    next();
  });
  app.post(`${QUOTED_ROUTE}/prepare`,async(req,res)=>{
    const {requestId,owner,agent}=req.body??{};
    if(!/^[a-f0-9]{32}$/.test(requestId??'')||!isAddress(owner??'')||!isAddress(agent??'')||owner===agent||[owner,agent].includes(SOLANA_PAY_TO))
      return res.status(400).json({error:'invalid_identity'});
    let input;
    try{input=readRequest(req.body);}catch{return res.status(400).json({error:'invalid_request'});}
    const hash=digest({owner,agent,input});
    let old=store.get(requestId);
    if(old){
      if(old.hash!==hash)return res.status(409).json({error:'request_changed'});
      if(old.status==='quoted'&&old.expires>=now())return res.json({quote:JSON.parse(old.offer)});
      return res.status(409).json({error:'already_prepared'});
    }
    try{store.create(requestId,owner,agent,hash,now());}catch{return res.status(409).json({error:'previous_quote_pending'});}
    try {
      const supported=await facilitatorClient.getSupported();
      const kind=supported.kinds.find(x=>x.x402Version===2&&x.scheme==='exact'&&x.network===SOLANA);
      if(!isAddress(kind?.extra?.feePayer??'')||[owner,agent].includes(kind.extra.feePayer))throw new Error('no_sponsor');
      const answer=await upstream(config,input.messages,input.maxTokens,fetchImpl);
      const billing=billUsage(answer.usage,DIRECT_TERMS);
      const requirement=await new ExactSvmScheme().enhancePaymentRequirements({scheme:'exact',network:SOLANA,
        asset:SOLANA_ASSET,amount:billing.totalAtomic,payTo:SOLANA_PAY_TO,maxTimeoutSeconds:300,
        extra:{billing,memo:`singit-ask:${requestId}`}},kind,[]);
      const quote={x402Version:2,resource:{url:`${QUOTED_URL}/${requestId}`,mimeType:'application/json'},accepts:[requirement],
        quoteId:requestId,owner,agent,usage:answer.usage,billing,expiresAt:now()+300};
      const result={id:`singit-ask-${requestId}`,object:'chat.completion',model:'singit-ask',created:now(),
        choices:[{index:0,message:{role:'assistant',content:answer.content},finish_reason:answer.finishReason??'stop'}],usage:answer.usage,billing};
      store.ready(requestId,quote,result,now());
      return res.json({quote});
    }catch{store.fail(requestId);return res.status(502).json({error:'no_billable_answer',message:'No billable answer was prepared. Nothing was paid.'});}
  });
  app.post(`${QUOTED_ROUTE}/:id`,async(req,res)=>{
    const row=store.get(req.params.id);
    if(!row)return res.status(404).json({error:'not_found'});
    if(row.status==='paid')return res.status(409).json({error:'already_paid'});
    if(row.status==='settling'||row.status==='unresolved')return res.status(409).json({error:'already_submitted'});
    if(row.status!=='quoted'||row.expires<now())return res.status(409).json({error:'quote_unavailable'});
    const quote=JSON.parse(row.offer),required=quote.accepts[0];
    const header=req.get('payment-signature');
    if(!header)return res.status(402).set('PAYMENT-REQUIRED',encode(quote)).json({error:'payment_required'});
    let payload;
    try{payload=JSON.parse(Buffer.from(header,'base64').toString());}catch{return res.status(400).json({error:'invalid_payment'});}
    if(payload.x402Version!==2||digest(payload.accepted)!==digest(required)||payload.resource?.url!==quote.resource.url)
      return res.status(400).json({error:'terms_changed'});
    // Claim durably before any facilitator call; restart/concurrent requests never resettle.
    if(!store.claim(row.id,now()))return res.status(409).json({error:'already_submitted'});
    // Verification never submits anything: whatever it answers, this quote was not settled.
    let verified;
    try{verified=await facilitatorClient.verify(payload,required);}
    catch{store.fail(row.id);return res.status(502).json({error:'verification_unavailable'});}
    if(!verified.isValid||verified.payer!==row.agent){store.fail(row.id);return res.status(402).json({error:'verification_refused'});}
    try {
      const receipt=await facilitatorClient.settle(payload,required);
      if(receipt.success!==true||receipt.network!==SOLANA||receipt.payer!==row.agent||!/^([1-9A-HJ-NP-Za-km-z]){80,90}$/.test(receipt.transaction??''))throw new Error('unresolved');
      store.paid(row.id,{...receipt,amount:required.amount});
      return res.set('PAYMENT-RESPONSE',encode({...receipt,amount:required.amount})).json(JSON.parse(row.answer));
    }catch{store.fail(row.id,'unresolved');return res.status(503).json({error:'payment_unresolved'});}
  });
  return store;
}
