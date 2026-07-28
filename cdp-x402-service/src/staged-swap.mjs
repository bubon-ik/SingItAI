export class StagedCdpError extends Error {
  constructor(message, { stage = "", reason = "", cause } = {}) {
    super(message, { cause });
    this.name = "StagedCdpError";
    this.stage = stage;
    this.reason = reason;
  }
}

const PRE_SWAP_REASONS = new Set([
  "rate_moved",
  "no_liquidity",
  "price_unavailable",
]);

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));


export async function executeStagedSwap({
  getPrice,
  assertFloor,
  swap,
  minUsdc,
  attempts = 3,
  retryDelayMs = 1500,
  sleep = defaultSleep,
}) {
  if (minUsdc) {
    // The priced amount clears the floor by a thin margin, so a single adverse
    // tick between pricing and settlement would strand an otherwise good order.
    // Re-check a few times before giving up; only a floor miss that persists is
    // a real rate move.
    const total = Math.max(1, Number(attempts) || 1);
    let lastCause;
    for (let attempt = 1; attempt <= total; attempt += 1) {
      try {
        const price = await getPrice();
        assertFloor(price, minUsdc);
        lastCause = undefined;
        break;
      } catch (cause) {
        lastCause = cause;
        if (attempt < total) await sleep(retryDelayMs);
      }
    }
    if (lastCause) {
      throw new StagedCdpError("CDP pre-swap validation failed", {
        stage: "pre_swap",
        reason: preSwapReason(lastCause),
        cause: lastCause,
      });
    }
  }

  try {
    // Never retried: a swap that failed ambiguously may still have settled.
    return await swap();
  } catch (cause) {
    throw new StagedCdpError("CDP swap result is ambiguous", { cause });
  }
}

/**
 * Build the only thing this process tells the gateway about a staged failure.
 *
 * The cause carries provider text (pool addresses, taker addresses, raw revert
 * strings) and must never cross the boundary; the reason code is the whole
 * signal.
 */
export function stagedErrorPayload(error) {
  const stage = error && error.stage === "pre_swap" ? "pre_swap" : "";
  const reason =
    stage === "pre_swap" && PRE_SWAP_REASONS.has(error && error.reason)
      ? error.reason
      : "";
  return {
    ok: false,
    error: "CDP wallet service failed",
    stage,
    reason,
  };
}

function preSwapReason(cause) {
  const reason = cause && cause.reason;
  // Anything the floor check did not classify is a failure to obtain a price.
  return PRE_SWAP_REASONS.has(reason) ? reason : "price_unavailable";
}
