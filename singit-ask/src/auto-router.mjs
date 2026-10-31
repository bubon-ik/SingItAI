// Text-only lite policy: eco requires every short/plain limit; premium matches long input,
// reasoning with an answer budget >= 1000 (within the HTTP limit of 1200), or reasoning with code/medium input. Otherwise standard.
// Thresholds and phrases live in config. Selection prices the request's upto ceiling on Base;
// network fees are constant across candidates. Escalation is upward only; alternates stay in the chosen tier.
import { readFileSync } from "node:fs";
import { priceGuard, unservableError } from "./catalogue.mjs";
import { ceilings } from "./metered.mjs";
import { BASE, TERMS, minDiscountFor } from "./pricing.mjs";
export const AUTO_ID = "singit/auto";
export const AUTO_DESCRIPTION = "Picks eco/standard/premium by simple rules for text prompts. Optional min_tier sets the minimum tier. X-SingIt-Model and X-SingIt-Tier report the choice. Only POST /v1/chat/completions. If the chosen model's seller refuses before payment, the router picks the next model of the tier or the next tier up, never below min_tier; after payment it retries once with another model of the same tier only if it costs no more than the price quoted, otherwise the request fails with 503 and no charge; X-SingIt-Model and X-SingIt-Tier show what actually answered.";
export const AUTO_CONFIG = JSON.parse(readFileSync(new URL("./auto-router.config.json", import.meta.url), "utf8"));
export const TIERS = ["eco", "standard", "premium"];
export function validateAutoConfig(config) {
  for (const tier of TIERS) {
    if (!config.tiers[tier]?.length) throw new Error(`Empty auto tier: ${tier}`);
    for (const id of config.tiers[tier]) {
      if (!/^[a-z0-9][a-z0-9.-]*$/.test(id) || /image|video|free\//i.test(id)) throw new Error("Invalid auto model id");
    }
  }
}
validateAutoConfig(AUTO_CONFIG);
const reasoningPattern = new RegExp(`\\b(?:${AUTO_CONFIG.reasoningWords.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`, "i");
export function classifyAuto(input) {
  const text = input.messages.map(m => m.content).join("\n");
  const tokens = Math.ceil(input.chars / AUTO_CONFIG.charsPerToken);
  const reasoning = reasoningPattern.test(text), code = text.includes("```");
  const { eco, premium } = AUTO_CONFIG;
  let tier = "standard", reason = "default_standard";
  if (tokens >= premium.minInputTokens) { tier = "premium"; reason = "long_input"; }
  else if (reasoning && input.maxTokens >= premium.reasoningOutputTokens) { tier = "premium"; reason = "reasoning_large_output"; }
  else if (reasoning && (code || tokens >= premium.reasoningInputTokens)) {
    tier = "premium"; reason = code ? "reasoning_with_code" : "reasoning_with_input";
  } else if (tokens <= eco.maxInputTokens && input.maxTokens <= eco.maxOutputTokens && input.messageCount <= eco.maxMessages && !code && !reasoning) {
    tier = "eco"; reason = "short_plain";
  }
  if (TIERS.indexOf(input.minTier) > TIERS.indexOf(tier)) { tier = input.minTier; reason = "min_tier"; }
  return { tier, reason };
}
export async function chooseAuto(config, catalogue, input, ErrorClass, log = () => {}) {
  const classified = classifyAuto(input);
  for (const tier of TIERS.slice(TIERS.indexOf(classified.tier))) {
    const candidates = [];
    for (const id of AUTO_CONFIG.tiers[tier]) {
      const info = await catalogue?.resolve(id).catch(() => null);
      const skip = reason => log(`singit-ask: auto skip model=${id} reason=${reason}`);
      if (!info || catalogue.isUnservable?.(info.id) || (catalogue.canAnswer && !await catalogue.canAnswer(info.id))) { skip("unhealthy"); continue; }
      const candidate = { ...input, model: info.id, info, ...priceGuard(catalogue, info.id, config.priceBufferPp ?? 1),
        autoTier: tier, autoReason: classified.reason };
      // Same rule as the paid call: a quote whose ceiling already covers the list-price worst case sends no floor,
      // so the seller's discount cannot refuse it and nothing to pre-check.
      const floor = minDiscountFor(candidate, { ...TERMS, maxChargeAtomic: ceilings(info, candidate).upto[BASE] });
      if (floor != null) {
        if (catalogue.isPriceRejected?.(info.id)) { skip("price_rejected_recently"); continue; }
        const live = await catalogue.liveDiscount?.(info.id);
        if (live != null && 100 - live * 100 + 1e-9 < floor) { skip("price_below_floor"); continue; }
      }
      candidates.push(candidate);
    }
    candidates.sort((a, b) => {
      const x = BigInt(ceilings(a.info, a).upto[BASE]), y = BigInt(ceilings(b.info, b).upto[BASE]);
      return x < y ? -1 : x > y ? 1 : 0;
    });
    if (candidates.length) {
      const [best, ...autoAlternates] = candidates;
      log(`singit-ask: auto tier=${tier} model=${best.model} reason=${best.autoReason}`);
      return { ...best, autoAlternates };
    }
  }
  throw unservableError(catalogue, AUTO_ID, ErrorClass);
}
