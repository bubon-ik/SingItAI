// The published settlement fee is charged consistently, including while the
// facilitator's promotional free allowance applies. Amounts are USDC micro-units.
export const METERED_ROUTE = "/v1/chat/completions/metered";
export const BASE = "eip155:8453";
export const ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const PAY_TO = "0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e";
export const TERMS = Object.freeze({ version: 1, mode: "actual_usage", markupBps: 3000,
  settlementFeeAtomic: "1000", maxChargeAtomic: "3000" });

export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const SOLANA_ASSET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
export const SOLANA_PAY_TO = "4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu";
export const SOLANA_TERMS = Object.freeze({ ...TERMS, settlementFeeAtomic: "2000" });

// The x402 resource's branding, the same on every discoverable route (ResourceInfo's serviceName/iconUrl,
// @x402/core 2.28): Bazaar and other directories show these next to the listing.
export const SERVICE_NAME = "SingIt Ask";
export const ICON_URL = "https://singitai.app/assets/icons/icon-512.png";

// p90 of 38 recorded answer lengths was 0.65 of the max_tokens asked for: most answers stop well short of their
// budget, so pricing `exact`'s flat ceiling at the whole budget (fraction 1) overcharges the typical question.
// At this fraction, with the 30% markup and settlement fee, 2 of those 38 recorded cases (both Opus 5.5,
// 2000-char/1000-token questions) would have landed slightly negative, about -$0.0000 — an exact overrun that
// `allowOverrun` (below) absorbs instead of refusing the answer.
export const DEFAULT_EXACT_OUTPUT_FRACTION = 0.65;

// Below this, a request is never authorized: cheap enough that a sensible floor matters more than honesty.
// Unlike `terms.maxChargeAtomic`, which the gateway and every buyer pin as the standing maximum, this floor is
// free to move: it only ever pushes a ceiling up, never past that maximum.
export const MIN_CEILING_ATOMIC = "1500";

// `allowOverrun` is for `exact`: that scheme already charged the flat ceiling regardless of actual usage (see
// metered.mjs), so going over it costs this service the difference instead of refusing the answer. `upto` never
// sets it: a metered payment that would collect more than it is authorized to is refused instead, as before.
export function billUsage(usage, terms = TERMS, { allowOverrun = false } = {}) {
  const raw = usage?.buyer_cost_micro;
  if (!(typeof raw === "number" && Number.isSafeInteger(raw) && raw >= 0)
      && !(typeof raw === "string" && /^(0|[1-9][0-9]*)$/.test(raw))) {
    throw new Error("Provider did not report a valid actual cost; payment refused.");
  }
  const provider = BigInt(raw);
  const markup = (provider * BigInt(terms.markupBps) + 9999n) / 10000n;
  const total = provider + markup + BigInt(terms.settlementFeeAtomic);
  const cap = BigInt(terms.maxChargeAtomic);
  const billed = { ...terms, providerCostAtomic: provider.toString(), markupAtomic: markup.toString() };
  if (total <= cap) return { ...billed, totalAtomic: total.toString(), currency: "USDC" };
  if (!allowOverrun) throw new Error("Actual cost exceeds the authorized ceiling; payment refused.");
  return { ...billed, totalAtomic: cap.toString(), overrunAtomic: (total - cap).toString(), currency: "USDC" };
}

// Atomic USDC micro-units as a plain decimal string, for a human price (openapi.json, /v1/models, /openapi.json's
// x-payment-info): "1008" -> "0.001008".
export const usd = atomic => (Number(atomic) / 1e6).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");

// The ceiling one answer can cost: the most the model could bill for this question and answer size, plus the
// markup and the fee. It never drops below MIN_CEILING_ATOMIC; only models whose worst case is higher get a
// higher ceiling.
//
// Input is estimated from `charsPerToken`: by default about 4 characters per token (the way a chat template
// actually spends tokens), plus 4 tokens of overhead per message. That default charges a CJK, emoji or other
// high-entropy prompt for fewer tokens than a real tokenizer would spend on it, which is fine for `exact` (its
// flat ceiling already absorbs an overrun via `allowOverrun`) but not for a ceiling that is meant to be the
// highest this question can honestly cost: callers authorizing an `upto` ceiling, or a cap checked against actual
// usage after the fact, must pass `charsPerToken: 1`, the loosest bound no tokenizer exceeds, so that bound can
// never be undercut by a dense prompt. A higher authorization costs the payer nothing: `upto` only ever settles
// actual usage.
//
// The output is priced at the whole token budget the answer may ask for (its `max_tokens`, sent to Surplus
// unchanged: see askSurplus), since thinking shares that same budget instead of getting room beyond it.
//
// `share` is the worst-case fraction of list price this answer can be bought at, 1 when no better guarantee is
// known (metered.mjs's `ceilings`, catalogue.mjs's `priceGuard`): Surplus's own list prices are above what a
// seller actually charges, so a ceiling priced at list (share 1) holds even when a dearer seller wins; a smaller
// share holds only together with the X-Min-Discount header that makes it true.
export function ceilingAtomic(model, { chars, maxTokens, messageCount, charsPerToken = 4 }, terms, share = 1) {
  const inputTokens = Math.ceil(chars / charsPerToken) + 4 * messageCount;
  const worst = Math.ceil((inputTokens * model.promptUsdPerToken + maxTokens * model.completionUsdPerToken) * share * 1e6);
  const total = BigInt(worst) + (BigInt(worst) * BigInt(terms.markupBps) + 9999n) / 10000n + BigInt(terms.settlementFeeAtomic);
  const floor = BigInt(MIN_CEILING_ATOMIC);
  return (total > floor ? total : floor).toString();
}

// Web Ask: measure privately, then settle once from the delegated owner account.
export const QUOTED_ROUTE = "/v1/chat/completions/quoted/solana";
export const QUOTED_URL = `https://ask.singitai.app${QUOTED_ROUTE}`;
export const DIRECT_TERMS = TERMS;
