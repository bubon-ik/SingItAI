/**
 * Will this signed EIP-3009 authorization settle right now?
 *
 * Asked before we spend gas and before we spend our own USDC buying shares.
 * Every check that can be answered without broadcasting is answered here, and
 * a failure names itself: "the buyer cannot pay" and "the buyer paid somebody
 * else" are different problems and must never arrive as one error string.
 */

import { getAddress, isAddress, recoverTypedDataAddress } from "viem";
import { SEL, USDC, USDC_DOMAIN } from "./chain.mjs";
import { rpcBatch, addressWord, word } from "./rpc.mjs";

export const TRANSFER_WITH_AUTHORIZATION_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
};

export async function preflightPayment({ authorization, signature, payTo, maxAmountRequired, asset = USDC.address, options = {} }) {
  const problems = [];

  if (!authorization || !signature) {
    return { willSettle: false, reasons: ["the payment payload is malformed"] };
  }
  if (normalize(asset) !== normalize(USDC.address)) {
    return { willSettle: false, reasons: ["this endpoint is paid in USDC on Base only"] };
  }

  const from = normalize(authorization.from);
  const to = normalize(authorization.to);
  if (!from || !to) {
    return { willSettle: false, reasons: ["the authorization names an invalid address"] };
  }

  const [chain, recovered] = await Promise.all([
    readChainState({ from, nonce: authorization.nonce, options }),
    recoverSigner({ authorization, signature }),
  ]);

  if (!recovered || normalize(recovered) !== from) {
    // The signature is the whole authorization. Nothing else is worth checking.
    return { willSettle: false, reasons: ["the signature does not match the stated payer"] };
  }
  if (chain.authorizationUsed) {
    problems.push("this authorization nonce has already been used");
  }
  const now = chain.blockTimestamp;
  if (now !== null) {
    if (BigInt(authorization.validAfter ?? 0) > now) {
      problems.push("the authorization is not valid yet");
    }
    if (BigInt(authorization.validBefore ?? 0) <= now) {
      problems.push("the authorization has expired");
    }
  }
  const value = BigInt(authorization.value ?? 0);
  if (chain.balance !== null && chain.balance < value) {
    problems.push("the payer does not hold enough USDC");
  }
  if (payTo && to !== normalize(payTo)) {
    problems.push("the authorization pays a different recipient");
  }
  if (maxAmountRequired !== undefined && value < BigInt(maxAmountRequired)) {
    problems.push("the authorization is for less than the price");
  }

  return { willSettle: problems.length === 0, reasons: problems, payer: from, value };
}

async function readChainState({ from, nonce, options }) {
  const [balance, used, block] = await rpcBatch(
    [
      ["eth_call", [{ to: USDC.address, data: SEL.balanceOf + addressWord(from) }, "latest"]],
      ["eth_call", [{ to: USDC.address, data: SEL.authorizationState + addressWord(from) + word(nonce) }, "latest"]],
      ["eth_getBlockByNumber", ["latest", false]],
    ],
    options,
  );
  return {
    balance: hexToBigInt(balance?.result),
    authorizationUsed: hexToBool(used?.result),
    blockTimestamp: hexToBigInt(block?.result?.timestamp),
  };
}

async function recoverSigner({ authorization, signature }) {
  try {
    return await recoverTypedDataAddress({
      domain: USDC_DOMAIN,
      types: TRANSFER_WITH_AUTHORIZATION_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from: getAddress(authorization.from),
        to: getAddress(authorization.to),
        value: BigInt(authorization.value),
        validAfter: BigInt(authorization.validAfter ?? 0),
        validBefore: BigInt(authorization.validBefore ?? 0),
        nonce: authorization.nonce,
      },
      signature,
    });
  } catch {
    return null;
  }
}

function normalize(value) {
  return isAddress(String(value ?? "")) ? String(value).toLowerCase() : null;
}

function hexToBigInt(value) {
  if (typeof value !== "string" || !value.startsWith("0x")) return null;
  try {
    return BigInt(value);
  } catch {
    return null;
  }
}

function hexToBool(value) {
  const parsed = hexToBigInt(value);
  return parsed === null ? false : parsed !== 0n;
}
