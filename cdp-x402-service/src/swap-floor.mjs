import { parseUnits } from "viem";

/**
 * Enforce the caller's absolute USDC floor before a swap executes.
 *
 * account.swap only honors slippageBps relative to its own execution price, so
 * without this check a swap that underfills versus the original gateway quote
 * would still settle and silently short the treasury. We compare the swap
 * price's guaranteed minimum output (minToAmount, atomic 6-decimal USDC)
 * against the required floor.
 *
 * @param {{liquidityAvailable?: boolean, minToAmount?: bigint|string}} price
 * @param {string} minUsdc Human USDC floor (e.g. "0.10"); falsy disables the check.
 */
export function assertSwapMeetsMinUsdc(price, minUsdc) {
  if (!minUsdc) return;
  const minToAmountFloor = parseUnits(String(minUsdc), 6);
  if (!price || !price.liquidityAvailable) {
    throw floorError(
      "no_liquidity",
      `No swap liquidity available to reach min ${minUsdc} USDC`,
    );
  }
  if (BigInt(price.minToAmount) < minToAmountFloor) {
    throw floorError(
      "rate_moved",
      `Swap would receive min ${price.minToAmount} atomic USDC, below required ${minToAmountFloor} (${minUsdc} USDC)`,
    );
  }
}

/**
 * Tag the failure with a closed-set reason code.
 *
 * The message itself never leaves this process, so the code is the only thing
 * the gateway can act on when it decides what to tell the buyer.
 */
function floorError(reason, message) {
  const error = new Error(message);
  error.reason = reason;
  return error;
}
