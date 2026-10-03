// Runs SingIt Ask on loopback, for the Cloudflare tunnel in front of it.
//
//   SINGIT_ASK_PAY_TO_SOLANA   where Solana payments go (its USDC account must exist)
//   SINGIT_ASK_PAY_TO_BASE     where Base payments go
//   SURPLUS_API_KEY            the inf_… buyer key the model is bought with
//   SINGIT_ASK_PRICE_SOLANA    per answer, default $0.003
//   SINGIT_ASK_PRICE_BASE      per answer, default $0.003
//   SINGIT_ASK_MODEL           upstream model, default deepseek-v4.1-flash
//   SINGIT_ASK_PORT            default 8140
//   PAYAI_API_KEY_ID, PAYAI_API_KEY_SECRET   optional: beyond PayAI's free settlements

import { HTTPFacilitatorClient } from "@x402/core/server";
import { facilitator } from "@payai/facilitator";
import { createApp } from "./app.mjs";
import { createRequire } from "node:module";

function required(name) {
  const value = String(process.env[name] || "").trim();
  if (!value) {
    console.error(`singit-ask: ${name} is required`);
    process.exit(1);
  }
  return value;
}

const config = {
  meteredSolana: process.env.SINGIT_ASK_METERED_SOLANA === "1",
  payToSolana: required("SINGIT_ASK_PAY_TO_SOLANA"),
  payToBase: required("SINGIT_ASK_PAY_TO_BASE"),
  surplusKey: required("SURPLUS_API_KEY"),
  surplusUrl: (process.env.SURPLUS_API_URL || "https://api.surplusintelligence.ai").replace(/\/$/, ""),
  model: process.env.SINGIT_ASK_MODEL || "deepseek-v4.1-flash",
  priceSolana: process.env.SINGIT_ASK_PRICE_SOLANA || "$0.003",
  priceBase: process.env.SINGIT_ASK_PRICE_BASE || "$0.003",
};
const port = Number(process.env.SINGIT_ASK_PORT || 8140);

const metered = process.env.SINGIT_ASK_METERED === "1";
let meteredFacilitatorClient;
if (metered) {
  required("CDP_API_KEY_ID"); required("CDP_API_KEY_SECRET");
  // Reuse the CDP component's installed authenticated SDK; no new dependency installation.
  const requireCdp = createRequire(new URL("../../cdp-x402-service/package.json", import.meta.url));
  meteredFacilitatorClient = new HTTPFacilitatorClient(requireCdp("@coinbase/x402").facilitator);
}
const app = createApp(config, { facilitatorClient: new HTTPFacilitatorClient(facilitator), meteredFacilitatorClient });
app.listen(port, "127.0.0.1", () => {
  console.error(`singit-ask: ${config.model} for ${config.priceSolana} on Solana, ${config.priceBase} on Base, `
                + `on http://127.0.0.1:${port}`);
});
