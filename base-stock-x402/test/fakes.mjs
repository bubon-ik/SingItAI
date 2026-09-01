import { ERC20_TRANSFER_TOPIC, MARKETS } from "../src/chain.mjs";
import { addressWord } from "../src/rpc.mjs";

export const PAYER = "0x1111111111111111111111111111111111111111";
export const TREASURY = "0x2222222222222222222222222222222222222222";
export const NVDA = MARKETS.NVDA;

// sqrtPriceX96 read from the live NVDA pool; ~$219.49 a share.
export const NVDA_SQRT = 0xaccc240bb21edaa9c6e17491n;

export function requirements(overrides = {}) {
  return {
    scheme: "exact",
    network: "eip155:8453",
    maxAmountRequired: "101000000",
    payTo: TREASURY,
    asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ...overrides,
  };
}

export function transferLog(token, to, amountAtomic) {
  return {
    address: token,
    topics: [ERC20_TRANSFER_TOPIC, "0x" + addressWord(TREASURY), "0x" + addressWord(to)],
    data: "0x" + amountAtomic.toString(16).padStart(64, "0"),
  };
}

/**
 * A wallet that records what it was asked to send and answers with whatever
 * receipt the test lined up. Order matters: the calls arrive as approve, swap,
 * refund.
 */
export function fakeWallet(receipts = []) {
  const sent = [];
  const queue = [...receipts];
  return {
    sent,
    walletClient: {
      async sendTransaction(tx) {
        sent.push(tx);
        const next = queue[sent.length - 1];
        if (next?.throws) throw new Error(next.throws);
        return `0xhash${sent.length}`;
      },
    },
    publicClient: {
      async waitForTransactionReceipt({ hash }) {
        const index = Number(String(hash).replace("0xhash", "")) - 1;
        const planned = queue[index] ?? {};
        return { status: planned.status ?? "success", logs: planned.logs ?? [] };
      },
    },
  };
}

export function deps({
  valid = true,
  invalidReason = null,
  settleOk = true,
  settleReason = "reverted",
  allowance = 0n,
  receipts = [],
  sqrt = NVDA_SQRT,
} = {}) {
  const wallet = fakeWallet(receipts);
  const calls = { verify: 0, settle: 0, price: 0 };
  return {
    wallet,
    calls,
    deps: {
      async verify() {
        calls.verify += 1;
        return { isValid: valid, invalidReason, payer: PAYER };
      },
      async readPrice(market) {
        calls.price += 1;
        const ratio = Number(sqrt) / Number(2n ** 96n);
        const sharesPerUsdc = ratio * ratio * 10 ** 6 / 10 ** market.decimals;
        return { sqrtPriceX96: sqrt, usdPerShare: 1 / sharesPerUsdc };
      },
      async settle() {
        calls.settle += 1;
        return settleOk
          ? { success: true, transaction: "0xsettle", value: "101000000", payer: PAYER }
          : { success: false, errorReason: settleReason, transaction: null };
      },
      makeClients() {
        return { account: { address: TREASURY }, ...wallet };
      },
      async readAllowance() {
        return allowance;
      },
    },
  };
}
