/**
 * Build an x402 paymentRequirementsSelector that refuses to pay anything
 * outside the terms the user actually approved.
 *
 * The x402 client calls the selector with the 402 challenge's requirements
 * BEFORE constructing/signing a payment, so throwing here prevents any funds
 * from moving. Without this, wrapFetchWithPayment pays whatever amount/receiver
 * the resource server demands on its own re-fetch — so a price/receiver change
 * (or a malicious server) between approval and execution would drain the wallet.
 *
 * @param {{maxAtomic?: string|number|bigint, expectedReceiver?: string, expectedAsset?: string}} caps
 * @returns {(x402Version: number, paymentRequirements: object[]) => object}
 */
export function makePaymentRequirementsSelector(caps = {}) {
  const { maxAtomic, expectedReceiver, expectedAsset } = caps;
  const maxCap =
    maxAtomic !== undefined && maxAtomic !== null && String(maxAtomic) !== ""
      ? BigInt(maxAtomic)
      : null;
  const wantReceiver = expectedReceiver ? String(expectedReceiver).toLowerCase() : null;
  const wantAsset = expectedAsset ? String(expectedAsset).toLowerCase() : null;

  return (_x402Version, paymentRequirements) => {
    const list = Array.isArray(paymentRequirements) ? paymentRequirements : [];
    for (const requirement of list) {
      if (wantReceiver && String(requirement.payTo).toLowerCase() !== wantReceiver) continue;
      if (wantAsset && String(requirement.asset).toLowerCase() !== wantAsset) continue;
      if (maxCap !== null && BigInt(requirement.maxAmountRequired) > maxCap) continue;
      return requirement;
    }
    throw new Error(
      `x402 payment requirement does not match approved terms ` +
        `(max ${maxAtomic ?? "∞"} atomic to ${expectedReceiver ?? "any"} asset ${expectedAsset ?? "any"})`,
    );
  };
}
