/**
 * Place one order from the command line.
 *
 *   BUYER_KEY=0x… node src/buy.mjs --ticker NVDA --usd 100 --server https://…
 *
 * The buyer needs USDC on Base and nothing else: no ETH, no account, no key
 * with the seller. `--max` is the buyer-side ceiling in atomic USDC and is
 * always sent, because an agent that does not cap its own spend has not really
 * set a budget.
 */

import { fetchWithPayment } from "./client.mjs";
import { usdcAtomic } from "./chain.mjs";

const args = Object.fromEntries(
  process.argv.slice(2).flatMap((arg, i, all) =>
    arg.startsWith("--") ? [[arg.slice(2), all[i + 1]]] : []),
);

const ticker = String(args.ticker ?? "NVDA").toUpperCase();
const usd = String(args.usd ?? "10");
const server = String(args.server ?? "http://localhost:8413").replace(/\/$/, "");
const key = process.env.BUYER_KEY;
const cdpName = process.env.BUYER_CDP_ACCOUNT_NAME;

if (!key && !cdpName) {
  console.error(
    "Set BUYER_KEY (a private key holding USDC on Base) or BUYER_CDP_ACCOUNT_NAME " +
    "(a named CDP account, with CDP credentials in the environment)",
  );
  process.exit(2);
}

// The buyer only signs a message, so a wallet that will not export its key can
// still be the payer.
let signer = null;
if (cdpName) {
  const [{ CdpClient }, { cdpSigner, resolveCdpAccount }] = await Promise.all([
    import("@coinbase/cdp-sdk"),
    import("./cdp.mjs"),
  ]);
  const account = await resolveCdpAccount({ client: new CdpClient(), name: cdpName });
  signer = cdpSigner(account);
  console.log(`paying from CDP account "${cdpName}" (${account.address})`);
}

// Default ceiling: the order plus 10%. Enough for any sane fee, tight enough
// that a seller cannot quietly ask for double.
const max = args.max ?? String((BigInt(usdcAtomic(Number(usd).toFixed(6))) * 110n) / 100n);

const url = `${server}/paid/buy/${ticker}?usd=${usd}`;
console.log(`buying ~$${usd} of ${ticker} from ${server}`);
console.log(`buyer-side ceiling: ${max} atomic USDC\n`);

const response = await fetchWithPayment(url, { method: "POST" }, {
  privateKey: key,
  signer,
  maxAmountAtomic: max,
});

const body = await response.json().catch(() => null);
console.log(`HTTP ${response.status}`);
console.log(JSON.stringify(body, null, 2));
process.exit(response.ok ? 0 : 1);
