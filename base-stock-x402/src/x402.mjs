/**
 * The seller half of x402 on Base, settled in USDC.
 *
 * Same shape as the Robinhood Chain node, with one rule deliberately inverted.
 * There, work runs before settlement so a failing handler never leaves a buyer
 * charged for nothing. Here the "work" is spending our own money on shares, so
 * settlement has to come first — and the cost of that inversion is a refund
 * path, which `fulfil.mjs` owns.
 */

import { createPublicClient, createWalletClient, encodeFunctionData, http } from "viem";
import { base } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";
import { BASE_CAIP2, BASE_RPC_URL, USDC, USDC_DOMAIN } from "./chain.mjs";
import { preflightPayment } from "./preflight.mjs";

export const X402_VERSION = 2;

const TRANSFER_WITH_AUTHORIZATION_ABI = [
  {
    type: "function",
    name: "transferWithAuthorization",
    stateMutability: "nonpayable",
    inputs: [
      { name: "from", type: "address" },
      { name: "to", type: "address" },
      { name: "value", type: "uint256" },
      { name: "validAfter", type: "uint256" },
      { name: "validBefore", type: "uint256" },
      { name: "nonce", type: "bytes32" },
      { name: "v", type: "uint8" },
      { name: "r", type: "bytes32" },
      { name: "s", type: "bytes32" },
    ],
    outputs: [],
  },
];

/**
 * Build the 402 body a client needs in order to pay.
 *
 * `extra` publishes the EIP-712 domain even though USDC exposes version() and
 * a guessable name. A buyer that has to guess the domain is a buyer whose
 * signature might verify as somebody else's, and the guess costs us nothing to
 * remove.
 */
export function buildPaymentRequirements({ resource, priceAtomic, description, payTo, maxTimeoutSeconds = 300 }) {
  return {
    scheme: "exact",
    network: BASE_CAIP2,
    maxAmountRequired: String(priceAtomic),
    resource,
    description,
    mimeType: "application/json",
    payTo,
    maxTimeoutSeconds,
    asset: USDC.address,
    extra: {
      name: USDC_DOMAIN.name,
      version: USDC_DOMAIN.version,
      decimals: USDC.decimals,
      symbol: USDC.symbol,
    },
  };
}

export function challengeBody(requirements, error = "X-PAYMENT header is required", extensions = null) {
  const body = { x402Version: X402_VERSION, error, accepts: [requirements] };
  return extensions ? { ...body, extensions } : body;
}

/**
 * Answer a 402 in both forms x402 v2 allows.
 *
 * The terms go in the JSON body *and*, base64-encoded, in the
 * `payment-required` header. Sellers in the wild pick one or the other —
 * Massive publishes only the header, this repository's Robinhood node only the
 * body — and a buyer that reads the form you did not send sees an endpoint
 * with no payable leg. Sending both costs a header and removes that failure
 * for every client.
 */
export function sendChallenge(response, requirements, error, extensions = null) {
  const body = challengeBody(requirements, error, extensions);
  response.set("payment-required", Buffer.from(JSON.stringify(body), "utf8").toString("base64"));
  return response.status(402).json(body);
}

/** Decode the base64 X-PAYMENT header into the payment payload. */
export function decodePaymentHeader(header) {
  if (!header || typeof header !== "string") return null;
  try {
    return JSON.parse(Buffer.from(header, "base64").toString("utf8"));
  } catch {
    return null;
  }
}

/** Encode a payment payload back into an X-PAYMENT header (used by clients and tests). */
export function encodePaymentHeader(payload) {
  return Buffer.from(JSON.stringify(payload), "utf8").toString("base64");
}

export function readAuthorization(payload) {
  return {
    authorization: payload?.payload?.authorization ?? payload?.authorization ?? null,
    signature: payload?.payload?.signature ?? payload?.signature ?? null,
  };
}

/** Verify a payment payload against requirements without broadcasting anything. */
export async function verifyPayment(payload, requirements, options = {}) {
  const { authorization, signature } = readAuthorization(payload);
  const report = await preflightPayment({
    authorization,
    signature,
    payTo: requirements.payTo,
    maxAmountRequired: requirements.maxAmountRequired,
    asset: requirements.asset,
    options,
  });
  return {
    isValid: report.willSettle,
    invalidReason: report.willSettle ? null : report.reasons.join("; "),
    payer: report.payer ?? null,
    report,
  };
}

export function facilitatorAccount(privateKey = process.env.BASE_FACILITATOR_KEY) {
  return privateKey ? privateKeyToAccount(privateKey) : null;
}

export function clients(privateKey = process.env.BASE_FACILITATOR_KEY, { rpcUrl = BASE_RPC_URL } = {}) {
  const transport = http(rpcUrl);
  const account = facilitatorAccount(privateKey);
  return {
    account,
    publicClient: createPublicClient({ chain: base, transport }),
    walletClient: account ? createWalletClient({ account, chain: base, transport }) : null,
  };
}

/**
 * Settle by broadcasting transferWithAuthorization ourselves.
 *
 * The facilitator pays the ETH gas; the payer signed only a message. Returns a
 * structured result rather than throwing, so a settlement failure downgrades a
 * response instead of taking the process down.
 */
export async function settlePayment(payload, requirements, { privateKey = process.env.BASE_FACILITATOR_KEY, rpcUrl = BASE_RPC_URL } = {}) {
  if (!privateKey) {
    return {
      success: false,
      errorReason: "facilitator_not_configured",
      message: "BASE_FACILITATOR_KEY is not set; the server can verify but not settle.",
      transaction: null,
    };
  }

  const { authorization, signature } = readAuthorization(payload);
  if (!authorization || !signature) {
    return { success: false, errorReason: "malformed_payload", transaction: null };
  }

  const { walletClient, publicClient } = clients(privateKey, { rpcUrl });
  const { r, s, v } = splitSignature(signature);
  const data = encodeFunctionData({
    abi: TRANSFER_WITH_AUTHORIZATION_ABI,
    functionName: "transferWithAuthorization",
    args: [
      authorization.from,
      authorization.to,
      BigInt(authorization.value),
      BigInt(authorization.validAfter ?? 0),
      BigInt(authorization.validBefore ?? 0),
      authorization.nonce,
      v,
      r,
      s,
    ],
  });

  try {
    const hash = await walletClient.sendTransaction({ to: requirements.asset ?? USDC.address, data });
    const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 90_000 });
    return {
      success: receipt.status === "success",
      errorReason: receipt.status === "success" ? null : "reverted",
      transaction: hash,
      network: BASE_CAIP2,
      payer: authorization.from,
      value: String(authorization.value),
    };
  } catch (error) {
    return {
      success: false,
      errorReason: "broadcast_failed",
      message: error.shortMessage ?? error.message,
      transaction: null,
    };
  }
}

export function splitSignature(signature) {
  const hex = String(signature).replace(/^0x/, "");
  if (hex.length !== 130) {
    throw new Error("signature must be 65 bytes");
  }
  const r = `0x${hex.slice(0, 64)}`;
  const s = `0x${hex.slice(64, 128)}`;
  let v = parseInt(hex.slice(128, 130), 16);
  if (v < 27) v += 27;
  return { r, s, v };
}
