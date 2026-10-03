import { paymentMiddleware, x402ResourceServer, setSettlementOverrides } from '@x402/express';
import { UptoSvmScheme } from '@x402/svm/upto/server';
import { SOLANA, SOLANA_ASSET, SOLANA_ROUTE, SOLANA_TERMS, billUsage } from './pricing.mjs';

export function addSolanaMeteredRoute(app, config, {facilitatorClient, readRequest, upstream, fetchImpl, log}) {
  const server=new x402ResourceServer(facilitatorClient).register(SOLANA,new UptoSvmScheme({withdrawDelay:300}));
  app.post(SOLANA_ROUTE,(req,res,next)=>{
    try {req.askInput=readRequest(req.body);next();}
    catch(error){res.status(error.status??400).json({error:{message:error.message}});}
  });
  app.use(paymentMiddleware({[`POST ${SOLANA_ROUTE}`]:{
    accepts:[{scheme:'upto',network:SOLANA,payTo:config.payToSolana,maxTimeoutSeconds:300,
      price:{asset:SOLANA_ASSET,amount:SOLANA_TERMS.maxChargeAtomic,extra:{billing:SOLANA_TERMS}}}],
    description:'Actual model cost + 30% + 0.002 USDC settlement fee. Reserve up to 0.003 for this request; unused USDC is refunded.',
    mimeType:'application/json',
  }},server));
  app.post(SOLANA_ROUTE,async(req,res)=>{
    try {
      const answer=await upstream(config,req.askInput.messages,req.askInput.maxTokens,fetchImpl);
      const billing=billUsage(answer.usage,SOLANA_TERMS);
      setSettlementOverrides(res,{amount:billing.totalAtomic});
      res.json({id:`singit-ask-${Date.now()}`,object:'chat.completion',created:Math.floor(Date.now()/1000),model:'singit-ask',
        choices:[{index:0,message:{role:'assistant',content:answer.content},finish_reason:answer.finishReason}],usage:answer.usage,billing});
    } catch(error) {
      log(`singit-ask Solana metered: refused (${error.name})`);
      // UptoSvmScheme.settleOnCancel requests a zero-charge claim/refund. If the
      // facilitator is unreachable, the client's checkpoint survives for recovery.
      res.status(502).json({error:{message:'No billable answer. A zero-charge refund was requested; check settlement before retrying.'}});
    }
  });
}
