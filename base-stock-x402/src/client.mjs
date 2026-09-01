/**
 * The buyer side, so a real order can be placed with one command.
 *
 * Request, read the 402, sign one EIP-3009 authorization, retry. The payer
 * never sends a transaction and needs no ETH — the seller's facilitator pays
 * the gas for the whole sequence, including the swap that delivers the shares.
 *
 * This client reads the challenge from BOTH forms x402 v2 allows, because a
 * client that reads only one is a client that refuses to pay working sellers.
 * That is not hypothetical: Massive publishes its terms only in the header.
 */

import { privateKeyToAccount } from "viem/accounts";
import { toHex } from "viem";
import { USDC_DOMAIN } from "./chain.mjs";
import { TRANSFER_WITH_AUTHORIZATION_TYPES } from "./preflight.mjs";
import { X402_VERSION, encodePaymentHeader } from "./x402.mjs";

export async function fetchWithPayment(url, options = {}, { privateKey, maxAmountAtomic } = {}) {
  const first = await fetch(url, options);
  if (first.status !== 402) return first;

  const requirements = await readRequirements(first);
  if (!requirements) throw new Error("402 response carried no payment requirements");

  const value = BigInt(requirements.maxAmountRequired ?? requirements.amount);
  if (maxAmountAtomic !== undefined && value > BigInt(maxAmountAtomic)) {
    // The buyer-side cap. An agent should always set one: without it, the
    // price is whatever the seller decided to ask for this time.
    throw new Error(`price ${value} exceeds the allowed maximum ${maxAmountAtomic}`);
  }

  const header = await signPayment(requirements, privateKey);
  return fetch(url, {
    ...options,
    headers: { ...(options.headers ?? {}), "X-PAYMENT": header },
  });
}

/** Take the terms from the body, or from the header when the body has none. */
export async function readRequirements(response) {
  let body = null;
  try {
    body = await response.clone().json();
  } catch {
    body = null;
  }
  const fromBody = body?.accepts?.[0];
  if (fromBody) return fromBody;

  const header = response.headers.get("payment-required")
    || response.headers.get("x-payment-required");
  if (!header) return null;
  try {
    return JSON.parse(Buffer.from(header, "base64").toString("utf8"))?.accepts?.[0] ?? null;
  } catch {
    return null;
  }
}

/** Sign an authorization for these requirements and return the X-PAYMENT header. */
export async function signPayment(requirements, privateKey, { now = Math.floor(Date.now() / 1000) } = {}) {
  const account = privateKeyToAccount(privateKey);
  const authorization = {
    from: account.address,
    to: requirements.payTo,
    value: BigInt(requirements.maxAmountRequired ?? requirements.amount),
    validAfter: 0n,
    validBefore: BigInt(now + (requirements.maxTimeoutSeconds ?? 300)),
    nonce: randomNonce(),
  };

  const signature = await account.signTypedData({
    // The domain the seller pinned in `extra`, falling back to the one this
    // repository verified against USDC's own DOMAIN_SEPARATOR. A signature
    // made under any other domain verifies as somebody else's.
    domain: {
      name: requirements.extra?.name ?? USDC_DOMAIN.name,
      version: requirements.extra?.version ?? USDC_DOMAIN.version,
      chainId: Number(String(requirements.network).split(":")[1]),
      verifyingContract: requirements.asset,
    },
    types: TRANSFER_WITH_AUTHORIZATION_TYPES,
    primaryType: "TransferWithAuthorization",
    message: authorization,
  });

  return encodePaymentHeader({
    x402Version: X402_VERSION,
    scheme: requirements.scheme,
    network: requirements.network,
    payload: {
      signature,
      authorization: {
        ...authorization,
        value: authorization.value.toString(),
        validAfter: authorization.validAfter.toString(),
        validBefore: authorization.validBefore.toString(),
      },
    },
  });
}

function randomNonce() {
  return toHex(crypto.getRandomValues(new Uint8Array(32)));
}
