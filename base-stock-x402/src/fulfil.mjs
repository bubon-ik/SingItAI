/**
 * One order, start to finish.
 *
 * The shape of this file is dictated by one asymmetry: everything before
 * settlement is free to fail, and everything after it is not. Before we take
 * the money, a refusal costs the buyer nothing and needs no cleanup. After we
 * take it, there are exactly two acceptable endings — the shares arrive, or
 * the USDC goes back — and "we are looking into it" is not one of them.
 *
 * So the swap sends the shares straight to the payer. `exactInputSingle` takes
 * a recipient, which means the equity never sits on our books, cannot be
 * seized there, and needs no second transfer that could itself fail.
 */

import { encodeFunctionData, erc20Abi } from "viem";
import {
  ERC20_TRANSFER_TOPIC,
  ROUTER_EQUITY,
  SEL,
  USDC,
  formatShares,
  formatUsdc,
} from "./chain.mjs";
import { addressWord, ethCall, word, wordAt } from "./rpc.mjs";
import { clients, readAuthorization, settlePayment, verifyPayment } from "./x402.mjs";
import { DEFAULT_SLIPPAGE_BPS, quoteBuy, readPoolPrice } from "./quote.mjs";
import { newOrderId, nullJournal } from "./journal.mjs";

// How long the swap may sit in the mempool before it is no longer the trade we
// priced. Short, because a stale fill is worse than a refund.
const SWAP_DEADLINE_SECONDS = 300;

/**
 * Everything that touches the chain or the wallet, in one place so a test can
 * replace it. Injected rather than imported at the call site for the reason
 * the chat lane injects its settle callable: a test that can move real money
 * eventually does.
 */
export const liveDeps = {
  verify: verifyPayment,
  readPrice: readPoolPrice,
  settle: settlePayment,
  makeClients: clients,
  readAllowance: async ({ owner, spender, options }) => {
    const { data } = await ethCall(
      USDC.address,
      SEL.allowance + addressWord(owner) + addressWord(spender),
      options,
    );
    return data && data !== "0x" ? BigInt(wordAt(data, 0)) : 0n;
  },
};

export async function fulfilOrder({
  payload,
  requirements,
  market,
  amountInAtomic,
  slippageBps = DEFAULT_SLIPPAGE_BPS,
  privateKey = process.env.BASE_FACILITATOR_KEY,
  rpcUrl,
  now = () => Math.floor(Date.now() / 1000),
  journal: log = nullJournal,
  orderId = newOrderId(),
  deps = {},
}) {
  const { verify, readPrice, settle, makeClients, readAllowance } = { ...liveDeps, ...deps };
  const journal = [];
  const options = rpcUrl ? { rpcUrl } : {};

  // ---- before the money moves: every refusal here is free ----------------
  const verified = await verify(payload, requirements, options);
  if (!verified.isValid) {
    return refused("payment_invalid", verified.invalidReason, journal);
  }
  const payer = verified.payer;
  journal.push({ step: "verify", ok: true, payer });

  let price;
  try {
    price = await readPrice(market, options);
  } catch (error) {
    return refused("pool_unreadable", error.message, journal);
  }
  const quote = quoteBuy({
    market,
    amountInAtomic,
    usdPerShare: price.usdPerShare,
    slippageBps,
  });
  journal.push({ step: "quote", ok: true, usdPerShare: +price.usdPerShare.toFixed(4) });

  const { account, walletClient, publicClient } = makeClients(privateKey, options);
  if (!walletClient) {
    return refused("facilitator_not_configured", "the server cannot settle", journal);
  }

  // ---- the money moves ---------------------------------------------------
  // Written and flushed BEFORE the broadcast. If the process dies between
  // these two lines, `payer` and `nonce` are enough to ask USDC's
  // authorizationState whether the money actually moved.
  log.append({
    orderId,
    step: "intent",
    payer,
    ticker: market.ticker,
    amountInAtomic: String(amountInAtomic),
    priceAtomic: String(requirements.maxAmountRequired),
    nonce: readAuthorization(payload).authorization?.nonce ?? null,
  });

  // The same wallet settles and swaps. Handing it over rather than handing over
  // a key is what lets a CDP account, which has no key to hand over, do both.
  const settlement = await settle(payload, requirements, {
    privateKey,
    walletClient,
    publicClient,
    ...options,
  });
  journal.push({ step: "settle", ok: settlement.success, tx: settlement.transaction });
  if (!settlement.success) {
    // Settlement failing is still a free refusal: the authorization was never
    // consumed, so the buyer has not paid.
    log.append({ orderId, step: "abandoned", reason: settlement.errorReason ?? "settlement_failed" });
    return refused(settlement.errorReason ?? "settlement_failed", settlement.message ?? null, journal);
  }
  log.append({ orderId, step: "settled", tx: settlement.transaction, value: settlement.value ?? null });

  const paidAtomic = BigInt(settlement.value ?? requirements.maxAmountRequired);

  // ---- from here, deliver or refund -------------------------------------
  try {
    await ensureAllowance({
      account,
      walletClient,
      publicClient,
      amount: BigInt(amountInAtomic),
      readAllowance,
      options,
      journal,
    });

    const swapHash = await walletClient.sendTransaction({
      to: ROUTER_EQUITY,
      data: swapCalldata({
        market,
        recipient: payer,
        amountIn: BigInt(amountInAtomic),
        minOut: quote.minOutAtomic,
        deadline: BigInt(now() + SWAP_DEADLINE_SECONDS),
      }),
    });
    const swapReceipt = await publicClient.waitForTransactionReceipt({ hash: swapHash, timeout: 90_000 });
    journal.push({ step: "swap", ok: swapReceipt.status === "success", tx: swapHash });
    if (swapReceipt.status !== "success") {
      throw new Error("the swap reverted");
    }

    const delivered = deliveredAmount(swapReceipt, market, payer);
    if (delivered <= 0n) {
      // The swap succeeded but nothing reached the payer. Refunding is the
      // only honest response: we cannot tell them what they own.
      throw new Error("the swap delivered nothing to the payer");
    }

    log.append({
      orderId,
      step: "delivered",
      tx: swapHash,
      deliveredAtomic: String(delivered),
    });

    return {
      ok: true,
      orderId,
      ticker: market.ticker,
      symbol: market.symbol,
      payer,
      paid: formatUsdc(paidAtomic),
      paidAtomic: String(paidAtomic),
      delivered: formatShares(delivered, market),
      deliveredAtomic: String(delivered),
      usdPerShare: +price.usdPerShare.toFixed(4),
      // What they were quoted versus what the pool actually gave them. Stated
      // every time, because the difference is the honest cost of the trade.
      expectedAtomic: String(quote.expectedOutAtomic),
      settlementTx: settlement.transaction,
      swapTx: swapHash,
      journal,
    };
  } catch (error) {
    const refund = await refundPayer({ walletClient, publicClient, payer, amount: paidAtomic, journal });
    log.append({
      orderId,
      // `stranded` is the one ending that means a human owes somebody money.
      step: refund.ok ? "refunded" : "stranded",
      tx: refund.tx,
      payer,
      amountAtomic: String(paidAtomic),
      reason: error.shortMessage ?? error.message,
    });
    return {
      ok: false,
      orderId,
      stage: "delivery",
      error: error.shortMessage ?? error.message,
      payer,
      refunded: refund.ok ? formatUsdc(paidAtomic) : null,
      refundTx: refund.tx,
      // A failed refund is the one state a human has to look at, so it says so
      // rather than hiding inside a generic error.
      needsOperator: !refund.ok,
      settlementTx: settlement.transaction,
      journal,
    };
  }
}

function refused(reason, detail, journal) {
  return { ok: false, stage: "pre-settlement", error: reason, detail: detail ?? null, charged: false, journal };
}

/**
 * Approve the router once, for exactly what this order needs or more.
 *
 * Deliberately not an infinite approval: this key holds float, and an
 * unlimited allowance to a third-party router is a standing invitation.
 */
async function ensureAllowance({ account, walletClient, publicClient, amount, readAllowance, options, journal }) {
  const current = await readAllowance({
    owner: account.address,
    spender: ROUTER_EQUITY,
    options,
  });
  if (current >= amount) {
    journal.push({ step: "approve", ok: true, skipped: true });
    return;
  }
  const hash = await walletClient.sendTransaction({
    to: USDC.address,
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: "approve",
      args: [ROUTER_EQUITY, amount],
    }),
  });
  const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 90_000 });
  journal.push({ step: "approve", ok: receipt.status === "success", tx: hash });
  if (receipt.status !== "success") {
    throw new Error("the router approval reverted");
  }
}

/**
 * Slipstream's exactInputSingle: an eight-word static tuple carrying
 * tickSpacing where Uniswap carries fee. The selector and the layout travel
 * together — using Uniswap's tuple against this selector encodes a different
 * trade that still decodes, which is the expensive kind of wrong.
 */
export function swapCalldata({ market, recipient, amountIn, minOut, deadline }) {
  return (
    SEL.exactInputSingle +
    addressWord(USDC.address) +
    addressWord(market.token) +
    word(market.tickSpacing) +
    addressWord(recipient) +
    word(deadline) +
    word(amountIn) +
    word(minOut) +
    word(0)
  );
}

/** What actually reached the payer, read off the receipt rather than assumed. */
export function deliveredAmount(receipt, market, payer) {
  const token = market.token.toLowerCase();
  const to = "0x" + addressWord(payer);
  let total = 0n;
  for (const log of receipt.logs ?? []) {
    if (String(log.address).toLowerCase() !== token) continue;
    if (String(log.topics?.[0]).toLowerCase() !== ERC20_TRANSFER_TOPIC) continue;
    if (String(log.topics?.[2]).toLowerCase() !== to) continue;
    total += BigInt(log.data);
  }
  return total;
}

async function refundPayer({ walletClient, publicClient, payer, amount, journal }) {
  try {
    const hash = await walletClient.sendTransaction({
      to: USDC.address,
      data: encodeFunctionData({
        abi: erc20Abi,
        functionName: "transfer",
        args: [payer, amount],
      }),
    });
    const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 90_000 });
    journal.push({ step: "refund", ok: receipt.status === "success", tx: hash });
    return { ok: receipt.status === "success", tx: hash };
  } catch (error) {
    journal.push({ step: "refund", ok: false, error: error.shortMessage ?? error.message });
    return { ok: false, tx: null };
  }
}
