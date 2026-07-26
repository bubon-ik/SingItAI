export class StagedCdpError extends Error {
  constructor(message, { stage = "", cause } = {}) {
    super(message, { cause });
    this.name = "StagedCdpError";
    this.stage = stage;
  }
}


export async function executeStagedSwap({
  getPrice,
  assertFloor,
  swap,
  minUsdc,
}) {
  if (minUsdc) {
    try {
      const price = await getPrice();
      assertFloor(price, minUsdc);
    } catch (cause) {
      throw new StagedCdpError("CDP pre-swap validation failed", {
        stage: "pre_swap",
        cause,
      });
    }
  }

  try {
    return await swap();
  } catch (cause) {
    throw new StagedCdpError("CDP swap result is ambiguous", { cause });
  }
}
