// Base mainnet constants.
//
// Every value here was read from the chain, not from documentation, and
// `test/chain.test.mjs` re-reads them so drift fails loudly instead of failing
// a payment. The one value that cannot be read back — the USDC EIP-712 domain
// — is proven against the on-chain DOMAIN_SEPARATOR in the same test.

export const BASE_CHAIN_ID = 8453;
export const BASE_CAIP2 = "eip155:8453";
// A list, not one endpoint. Public Base RPCs do not all serve the same
// methods: base-rpc.publicnode.com answers eth_call and eth_getLogs and
// rejects eth_getTransactionReceipt outright, which reads as "invalid
// parameters" long after the transaction it cannot confirm has landed.
export const BASE_RPC_URLS = (process.env.BASE_RPC_URL || [
  "https://mainnet.base.org",
  "https://base.drpc.org",
  "https://1rpc.io/base",
].join(",")).split(",").map((url) => url.trim()).filter(Boolean);

export const BASE_RPC_URL = BASE_RPC_URLS[0];
export const BASE_EXPLORER = "https://basescan.org";

export const USDC = {
  address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  symbol: "USDC",
  name: "USD Coin",
  decimals: 6,
};

// Unlike USDG on Robinhood Chain, USDC exposes version() and the guessable
// name, so the domain needs no recovery. It is still pinned and tested: a
// signature made under the wrong domain verifies as somebody else's.
export const USDC_DOMAIN = {
  name: "USD Coin",
  version: "2",
  chainId: BASE_CHAIN_ID,
  verifyingContract: USDC.address,
};

export const USDC_DOMAIN_SEPARATOR =
  "0x02fa7265e7c5d81118673727957699e4d68f74cd74b7db77da710fe8a2c7834f";

// Aerodrome Slipstream router for the CL10 equity pools. Taken from Bankr's
// published aero-stock-lp skill (scripts/lib/markets.mjs), which is the
// canonical source, and checked against each pool on chain by the test.
export const ROUTER_EQUITY = "0x698cb2b6dd822994581fea6ea4fc755d1363a92f";

/**
 * The four Coinbase tokenized equities live on Base as B20 predeploys.
 *
 * They are 8 decimals — not 18, and not USDC's 6 — and every pool here has
 * USDC as token0 and the equity as token1. Both facts are load-bearing for
 * pricing, so both are asserted on chain rather than trusted.
 *
 * B20 places no transfer allowlist on secondary movement: KYC gates minting
 * and redemption against the underlying shares, not holding. That is why this
 * service can deliver to an arbitrary payer address at all.
 */
export const MARKETS = {
  NVDA: {
    ticker: "NVDA",
    symbol: "NVDAc",
    name: "NVIDIA Corporation",
    token: "0xb20000000000000000000078ee7ce2fE4908108C",
    pool: "0x853f5f1b92b16714fe6cda67caad0856b83c7ab9",
    decimals: 8,
    tickSpacing: 10,
    feeBps: 5,
  },
  AAPL: {
    ticker: "AAPL",
    symbol: "AAPLc",
    name: "Apple Inc.",
    token: "0xb200000000000000000000C2e324d24d7eEcd1fb",
    pool: "0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0",
    decimals: 8,
    tickSpacing: 10,
    feeBps: 5,
  },
  GOOGL: {
    ticker: "GOOGL",
    symbol: "GOOGLc",
    name: "Alphabet Inc.",
    token: "0xb2000000000000000000002D0BA3164cc74f58B7",
    pool: "0xb1987cad1682841b4b641d50e520777ec5ab5542",
    decimals: 8,
    tickSpacing: 10,
    feeBps: 5,
  },
  META: {
    ticker: "META",
    symbol: "METAc",
    name: "Meta Platforms Inc.",
    token: "0xb2000000000000000000008bC8786B856E61707C",
    pool: "0xeaf57753bc382e0324a1d43f72e7027705a2273e",
    decimals: 8,
    tickSpacing: 10,
    feeBps: 5,
  },
};

export const SEL = {
  balanceOf: "0x70a08231",
  allowance: "0xdd62ed3e",
  approve: "0x095ea7b3",
  slot0: "0x3850c7bd",
  token0: "0x0dfe1681",
  token1: "0xd21220a7",
  tickSpacingCall: "0xd0c93a7c",
  decimals: "0x313ce567",
  symbol: "0x95d89b41",
  domainSeparator: "0x3644e515",
  authorizationState: "0xe94a0102",
  // Slipstream takes tickSpacing where Uniswap takes fee, so this is NOT the
  // Uniswap exactInputSingle selector and the tuple is not interchangeable.
  exactInputSingle: "0xa026383e",
};

export const ERC20_TRANSFER_TOPIC =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

export function market(ticker) {
  return MARKETS[String(ticker || "").trim().toUpperCase()] ?? null;
}

/** Format an atomic USDC amount for humans. */
export function formatUsdc(atomic) {
  return formatUnits(atomic, USDC.decimals) + " USDC";
}

/** Format an atomic equity amount with its ticker. */
export function formatShares(atomic, marketEntry) {
  return `${formatUnits(atomic, marketEntry.decimals)} ${marketEntry.symbol}`;
}

function formatUnits(atomic, decimals) {
  const value = BigInt(atomic);
  const base = 10n ** BigInt(decimals);
  const whole = value / base;
  const fraction = (value % base).toString().padStart(decimals, "0");
  return `${whole}.${fraction}`;
}

/** Turn a USD string like "100.50" into atomic USDC. */
export function usdcAtomic(price) {
  const [whole, fraction = ""] = String(price).split(".");
  const padded = fraction.padEnd(USDC.decimals, "0").slice(0, USDC.decimals);
  return (BigInt(whole || "0") * 10n ** BigInt(USDC.decimals) + BigInt(padded || "0")).toString();
}
