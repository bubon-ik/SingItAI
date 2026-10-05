// Pay for actual usage: the payer authorizes a ceiling with x402 `upto`, and CDP settles only what the
// answer cost, after it came back. A request that ends in 400 or above is never charged.
import { paymentMiddleware, x402ResourceServer, setSettlementOverrides } from "@x402/express";
import { UptoEvmScheme } from "@x402/evm/upto/server";
import { UptoSvmScheme } from "@x402/svm/upto/server";
import { BASE, ASSET, SOLANA, SOLANA_ASSET, TERMS, SOLANA_TERMS, billUsage } from "./pricing.mjs";

// Actual model cost + 30% + the network's settlement fee. Solana's upto escrows the ceiling, then
// settles the actual amount and refunds the rest: two transactions, hence its higher fee.
const TERMS_BY_NETWORK = { [BASE]: TERMS, [SOLANA]: SOLANA_TERMS };

export const baseOffer = config => ({
  // 120 s covers the model and settlement; after it, a lost answer's signature is provably unusable.
  scheme: "upto", network: BASE, payTo: config.payToBase, maxTimeoutSeconds: 120,
  price: { asset: ASSET, amount: TERMS.maxChargeAtomic, extra: { name: "USD Coin", version: "2", billing: TERMS } },
});
export const solanaOffer = config => ({
  scheme: "upto", network: SOLANA, payTo: config.payToSolana, maxTimeoutSeconds: 300,
  price: { asset: SOLANA_ASSET, amount: SOLANA_TERMS.maxChargeAtomic, extra: { billing: SOLANA_TERMS } },
});

// The terms of the network the payer chose. The middleware has already matched the payment to an offer.
function paidTerms(req) {
  const header = req.get("payment-signature") ?? req.get("x-payment");
  return TERMS_BY_NETWORK[JSON.parse(Buffer.from(header, "base64").toString()).accepted?.network];
}

export function addUsageRoute(app, route, offers, details, { facilitatorClient, config, readRequest, upstream, fetchImpl, log }) {
  const server = new x402ResourceServer(facilitatorClient)
    .register(BASE, new UptoEvmScheme())
    .register(SOLANA, new UptoSvmScheme({ withdrawDelay: 300 }));
  // An unreadable question is refused before any payment is asked for.
  app.post(route, (req, res, next) => {
    try { req.askInput = readRequest(req.body); next(); }
    catch (error) { res.status(error.status ?? 400).json({ error: { message: error.message, type: "invalid_request_error" } }); }
  });
  app.use(paymentMiddleware({ [`POST ${route}`]: { accepts: offers, mimeType: "application/json", ...details } }, server));
  app.post(route, async (req, res) => {
    const started = Date.now();
    try {
      const terms = paidTerms(req);
      if (!terms) throw new Error("payment on an unknown network");
      const answer = await upstream(config, req.askInput.messages, req.askInput.maxTokens, fetchImpl);
      const billing = billUsage(answer.usage, terms);
      setSettlementOverrides(res, { amount: billing.totalAtomic });
      log(`singit-ask: answered in ${Date.now() - started} ms for ${billing.totalAtomic} micro-USDC`);
      res.json({ id: `singit-ask-${started}`, object: "chat.completion", created: Math.floor(started / 1000),
        model: "singit-ask", choices: [{ index: 0, message: { role: "assistant", content: answer.content },
          finish_reason: answer.finishReason }], usage: answer.usage, billing });
    } catch (error) {
      // Not settled: on Base nothing moves, on Solana the escrowed ceiling is refunded in full.
      log(`singit-ask: no billable answer after ${Date.now() - started} ms (${error.name})`);
      res.status(502).json({ error: { message: "No billable answer was produced. You were not charged; try again.",
        type: "upstream_error" } });
    }
  });
}
