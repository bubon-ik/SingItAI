// Runs SingIt Ask on loopback, for the Cloudflare tunnel in front of it. Coinbase CDP settles every payment.
//
//   SINGIT_ASK_PAY_TO_SOLANA   where Solana payments go (its USDC account must exist)
//   SINGIT_ASK_PAY_TO_BASE     where Base payments go
//   SURPLUS_API_KEY            the inf_… buyer key the model is bought with
//   CDP_API_KEY_ID, CDP_API_KEY_SECRET   the CDP facilitator's credentials
//   SINGIT_ASK_DIRECT_SOLANA   1 turns on the web agent's private Solana route, with
//   SINGIT_ASK_QUOTE_TOKEN     its shared secret (32+ characters) and
//   SINGIT_ASK_QUOTE_DB        its private SQLite file
//   SINGIT_ASK_MODEL           upstream model, default deepseek-v4.1-flash
//   SINGIT_ASK_PORT            default 8140

import { createRequire } from "node:module";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { createApp } from "./app.mjs";

function required(name) {
  const value = String(process.env[name] || "").trim();
  if (!value) {
    console.error(`singit-ask: ${name} is required`);
    process.exit(1);
  }
  return value;
}

const config = {
  directSolana: process.env.SINGIT_ASK_DIRECT_SOLANA === "1",
  quoteToken: process.env.SINGIT_ASK_QUOTE_TOKEN,
  quoteDb: process.env.SINGIT_ASK_QUOTE_DB,
  payToSolana: required("SINGIT_ASK_PAY_TO_SOLANA"),
  payToBase: required("SINGIT_ASK_PAY_TO_BASE"),
  surplusKey: required("SURPLUS_API_KEY"),
  surplusUrl: (process.env.SURPLUS_API_URL || "https://api.surplusintelligence.ai").replace(/\/$/, ""),
  model: process.env.SINGIT_ASK_MODEL || "deepseek-v4.1-flash",
};
const port = Number(process.env.SINGIT_ASK_PORT || 8140);

required("CDP_API_KEY_ID"); required("CDP_API_KEY_SECRET");
// The CDP component's installed, authenticated SDK signs each facilitator call.
const requireCdp = createRequire(new URL("../../cdp-x402-service/package.json", import.meta.url));
const facilitatorClient = new HTTPFacilitatorClient(requireCdp("@coinbase/x402").facilitator);

const app = createApp(config, { facilitatorClient });
app.listen(port, "127.0.0.1", () => {
  console.error(`singit-ask: ${config.model}, actual usage over Coinbase CDP on Solana and Base, on http://127.0.0.1:${port}`);
});
