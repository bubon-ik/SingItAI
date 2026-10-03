import { UptoEvmScheme } from "@x402/evm/upto/client";
import { ASSET, BASE, PAY_TO, METERED_ROUTE, TERMS, billUsage } from "./pricing.mjs";

export const ENDPOINT = `https://ask.singitai.app${METERED_ROUTE}`;
const PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
const TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
const same = (a, b) => typeof a === "string" && a.toLowerCase() === b.toLowerCase();
export const decode = value => JSON.parse(Buffer.from(value, "base64").toString());

export function selectOffer(challenge) {
  if (challenge?.x402Version !== 2 || challenge.resource?.url !== ENDPOINT) throw new Error("Unexpected payment resource.");
  const offers = challenge.accepts?.filter(a => a.scheme === "upto" && a.network === BASE) ?? [];
  if (offers.length !== 1) throw new Error("A single Base upto offer is required.");
  const a = offers[0];
  // CDP supplies a rotating settlement signer. The trusted HTTPS merchant
  // advertises it; the SDK witness still pins recipient, token and ceiling.
  if (!same(a.asset, ASSET) || !same(a.payTo, PAY_TO) || a.amount !== TERMS.maxChargeAtomic
      || !/^0x[0-9a-fA-F]{40}$/.test(a.extra?.facilitatorAddress ?? "")
      || /^0x0{40}$/i.test(a.extra.facilitatorAddress) || a.extra?.assetTransferMethod !== "permit2" || a.extra?.name !== "USD Coin" || a.extra?.version !== "2"
      || !Number.isInteger(a.maxTimeoutSeconds) || a.maxTimeoutSeconds < 1 || a.maxTimeoutSeconds > 300
      || Object.entries(TERMS).some(([key, value]) => a.extra?.billing?.[key] !== value)) {
    throw new Error("The merchant, ceiling or billing terms changed. Nothing was signed.");
  }
  if (!challenge.extensions?.eip2612GasSponsoring) throw new Error("Gasless Permit2 support is required.");
  return a;
}

export function validateBill(body) {
  const expected = billUsage(body?.usage);
  if (Object.entries(expected).some(([key, value]) => body?.billing?.[key] !== value)) {
    throw new Error("The returned invoice does not match actual usage and the agreed fees.");
  }
  return expected;
}

export function validateTransfer(receipt, payer, amount) {
  if (receipt.status !== "success") throw new Error("Settlement reverted or is pending.");
  const logs = receipt.logs.filter(l => same(l.address, ASSET) && l.topics[0] === TRANSFER
    && same(`0x${l.topics[1]?.slice(-40)}`, payer) && same(`0x${l.topics[2]?.slice(-40)}`, PAY_TO));
  if (logs.length !== 1 || BigInt(logs[0].data) !== BigInt(amount)) throw new Error("The on-chain payment differs from the invoice.");
  return logs[0].logIndex;
}

export async function payMetered({ signer, chain, body, fetchImpl = fetch, beforeSubmit, onProgress = () => {} }) {
  const encodedBody = JSON.stringify(body);
  const send = headers => fetchImpl(ENDPOINT, { method: "POST", redirect: "error",
    signal: AbortSignal.timeout(90000), headers: { "Content-Type": "application/json", ...headers }, body: encodedBody });
  onProgress("quote");
  const quote = await send({});
  if (quote.status !== 402 || !quote.headers.get("payment-required")) throw new Error("The endpoint did not provide a payment quote.");
  const required = decode(quote.headers.get("payment-required"));
  const accepted = selectOffer(required);
  // The SDK signs the Permit2 witness and, if needed, a bounded EIP-2612 approval.
  // Expose no sendTransaction/writeContract method: the agent never pays native gas here.
  onProgress("signing");
  const signed = await new UptoEvmScheme(signer).createPaymentPayload(2, accepted, { extensions: required.extensions });
  const permit = signed.extensions?.eip2612GasSponsoring?.info;
  if (permit && (!same(permit.spender, PERMIT2) || !same(permit.asset, ASSET)
      || !same(permit.from, signer.address) || permit.amount !== TERMS.maxChargeAtomic)) {
    throw new Error("Refusing an unbounded token approval.");
  }
  // Scheme clients return only the buyer's extension fields. Preserve the
  // merchant's declaration (notably description/version/schema) when adding
  // the signed permit, as the full x402 client does. Dropping description
  // causes extension_echo_mismatch before the facilitator is even contacted.
  const extensions = signed.extensions && Object.fromEntries(Object.entries(signed.extensions).map(([key, value]) => {
    const declared = required.extensions?.[key] ?? {};
    return [key, { ...declared, ...value, info: { ...declared.info, ...value.info } }];
  }));
  const payload = { x402Version: 2, accepted, resource: required.resource, payload: signed.payload,
    ...(extensions ? { extensions } : {}) };
  const auth = signed.payload.permit2Authorization;
  // Durable non-secret checkpoint is mandatory before the only paid HTTP submission.
  if (!beforeSubmit) throw new Error("A durable payment checkpoint is required.");
  await beforeSubmit({ payer: signer.address, nonce: auth.nonce, deadline: auth.deadline,
    ceilingAtomic: accepted.amount });
  onProgress("submission");
  const response = await send({ "PAYMENT-SIGNATURE": Buffer.from(JSON.stringify(payload)).toString("base64") });
  onProgress("response", response.status);
  if (response.status !== 200) throw new Error(`Paid request returned HTTP ${response.status}; it was not repeated.`);
  const result = await response.json();
  const billing = validateBill(result);
  const receiptHeader = response.headers.get("payment-response");
  const receipt = receiptHeader ? decode(receiptHeader) : null;
  if (!receipt?.success || receipt.network !== BASE || !same(receipt.payer, signer.address)
      || !/^0x[0-9a-fA-F]{64}$/.test(receipt.transaction ?? "")) throw new Error("No valid settlement receipt was returned.");
  onProgress("settlement");
  const onchain = await chain.waitForTransactionReceipt({ hash: receipt.transaction, timeout: 30000 });
  const logIndex = validateTransfer(onchain, signer.address, billing.totalAtomic);
  return { ok: true, status: 200, body: result, amountAtomic: billing.totalAtomic,
    transaction: receipt.transaction, logIndex, payer: signer.address };
}
