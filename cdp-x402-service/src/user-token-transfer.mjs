import { parseUnits } from "viem";

export function humanTokenAmountToAtomic(amount, decimals = 18) {
  return parseUnits(String(amount), Number(decimals));
}
