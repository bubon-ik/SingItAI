/**
 * What the operator runs when health says a number other than zero.
 *
 * Prints the orders whose last recorded step is not an ending, and the two
 * facts needed to resolve each one from the chain: who paid, and the nonce
 * that says whether their authorization was ever consumed.
 */

import { Journal } from "./journal.mjs";

const log = new Journal();
const open = log.unresolved();
const stranded = log.read().filter((entry) => entry.step === "stranded");

console.log(`journal: ${log.path}`);
console.log(`${log.read().length} entries, ${open.length} unresolved, ${stranded.length} stranded\n`);

for (const entry of open) {
  console.log(`UNRESOLVED ${entry.orderId}`);
  console.log(`  stopped at : ${entry.step}`);
  console.log(`  payer      : ${entry.payer ?? "?"}`);
  console.log(`  nonce      : ${entry.nonce ?? "?"}`);
  console.log(`  ticker     : ${entry.ticker ?? "?"}  price ${entry.priceAtomic ?? "?"}`);
  console.log("  check      : USDC.authorizationState(payer, nonce) — true means they paid\n");
}

for (const entry of stranded) {
  console.log(`STRANDED ${entry.orderId} — owed ${entry.amountAtomic} atomic USDC to ${entry.payer}`);
  console.log(`  reason: ${entry.reason}\n`);
}

if (!open.length && !stranded.length) {
  console.log("Nothing owed.");
}
