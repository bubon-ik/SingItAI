import { paymentMiddleware, x402ResourceServer, setSettlementOverrides } from "@x402/express";
import { UptoEvmScheme } from "@x402/evm/upto/server";
import { declareEip2612GasSponsoringExtension } from "@x402/extensions";
import { BASE, ASSET, METERED_ROUTE, TERMS, billUsage } from "./pricing.mjs";

export function addMeteredRoute(app, config, { facilitatorClient, readRequest, upstream, fetchImpl, log }) {
  const server = new x402ResourceServer(facilitatorClient).register(BASE, new UptoEvmScheme());
  app.post(METERED_ROUTE, (req, res, next) => {
    try { req.askInput = readRequest(req.body); next(); }
    catch (error) { res.status(error.status ?? 400).json({ error: { message: error.message } }); }
  });
  app.use(paymentMiddleware({
    [`POST ${METERED_ROUTE}`]: {
      // 120 s covers the model and settlement; after it, a lost answer's signature is provably unusable.
      accepts: [{ scheme: "upto", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120,
        price: { asset: ASSET, amount: TERMS.maxChargeAtomic,
          extra: { name: "USD Coin", version: "2", billing: TERMS } } }],
      description: "SingIt Ask: actual model cost + 30% markup + 0.001 USDC settlement fee. Authorize at most 0.003 USDC.",
      mimeType: "application/json", extensions: declareEip2612GasSponsoringExtension(),
    },
  }, server));
  app.post(METERED_ROUTE, async (req, res) => {
    try {
      const answer = await upstream(config, req.askInput.messages, req.askInput.maxTokens, fetchImpl);
      const billing = billUsage(answer.usage);
      setSettlementOverrides(res, { amount: billing.totalAtomic });
      res.json({ id: `singit-ask-${Date.now()}`, object: "chat.completion", created: Math.floor(Date.now() / 1000),
        model: "singit-ask", choices: [{ index: 0, message: { role: "assistant", content: answer.content },
          finish_reason: answer.finishReason }], usage: answer.usage, billing });
    } catch (error) {
      log(`singit-ask metered: refused (${error.name})`);
      res.status(502).json({ error: { message: "No billable answer was produced. No settlement was requested." } });
    }
  });
}
