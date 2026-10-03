// The published settlement fee is charged consistently, including while the
// facilitator's promotional free allowance applies. Amounts are USDC micro-units.
export const METERED_ROUTE = "/v1/chat/completions/metered";
export const BASE = "eip155:8453";
export const ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const PAY_TO = "0xC23d1Dc0f5fCe1abfFB051e06cB93f0329968B4e";
export const TERMS = Object.freeze({ version: 1, mode: "actual_usage", markupBps: 3000,
  settlementFeeAtomic: "1000", maxChargeAtomic: "3000" });

export const SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
export const SOLANA_ROUTE = `${METERED_ROUTE}/solana`;
export const SOLANA_ASSET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
export const SOLANA_PAY_TO = "4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu";
export const SOLANA_TERMS = Object.freeze({ ...TERMS, settlementFeeAtomic: "2000" });

export function billUsage(usage, terms = TERMS) {
  const raw = usage?.buyer_cost_micro;
  if (!(typeof raw === "number" && Number.isSafeInteger(raw) && raw >= 0)
      && !(typeof raw === "string" && /^(0|[1-9][0-9]*)$/.test(raw))) {
    throw new Error("Provider did not report a valid actual cost; payment refused.");
  }
  const provider = BigInt(raw);
  const markup = (provider * BigInt(terms.markupBps) + 9999n) / 10000n;
  const total = provider + markup + BigInt(terms.settlementFeeAtomic);
  if (total > BigInt(terms.maxChargeAtomic)) throw new Error("Actual cost exceeds the authorized ceiling; payment refused.");
  return { ...terms, providerCostAtomic: provider.toString(), markupAtomic: markup.toString(),
    totalAtomic: total.toString(), currency: "USDC" };
}
