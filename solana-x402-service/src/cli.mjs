#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { configuration, ClientError, NETWORK, formatUsdc } from './config.mjs';
import { createWallet, loadWallet } from './wallet.mjs';
import { Store } from './store.mjs';
import { VeniceClient } from './venice.mjs';
import { SolanaChain } from './chain.mjs';
import { Payments, quoteSummary } from './payments.mjs';

const HELP = `SingIt Solana — Venice on mainnet

  npm start -- wallet-init
  npm start -- wallet
  npm start -- balance
  npm start -- models
  npm start -- quote
  npm start -- pay --quote ID --approve FULL_QUOTE_HASH
  npm start -- status --quote ID
  npm start -- history
  npm start -- reconcile --quote ID [--transaction SIGNATURE]
  npm start -- chat --model MODEL --message "Hello" [--max-tokens 256]

quote does not pay. pay requires explicit approval of the exact quote.
chat spends existing Venice credit and never automatically tops up.
reconcile only reads the chain and Venice; it never resubmits a payment.
Wallet keys and operation state stay in .local/ (ignored by Git).
Environment: SOLANA_RPC_URL, SOLANA_KEYPAIR_FILE, SOLANA_PRIVATE_KEY, SINGIT_STATE_DIR.
`;

async function main() {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { help: { type: 'boolean' }, quote: { type: 'string' }, approve: { type: 'string' }, transaction: { type: 'string' }, model: { type: 'string' }, message: { type: 'string' }, 'max-tokens': { type: 'string' } } });
  const [command] = positionals;
  if (!command || values.help) { process.stdout.write(HELP); return; }
  const commands = ['wallet-init', 'wallet', 'balance', 'models', 'quote', 'pay', 'status', 'history', 'reconcile', 'chat'];
  if (!commands.includes(command) || positionals.length !== 1) throw new ClientError('USAGE', 'Unknown command. Run with --help.');
  if (['pay', 'status', 'reconcile'].includes(command) && !values.quote) throw new ClientError('USAGE', '--quote is required.');
  if (command === 'pay' && !values.approve) throw new ClientError('APPROVAL_REQUIRED', '--approve must contain the full hash of the quote you reviewed.');
  const config = configuration();
  if (command === 'models') return print(await new VeniceClient().models());
  const wallet = command === 'wallet-init' ? await createWallet(config.walletFile) : await loadWallet(config.walletFile);
  if (command === 'wallet-init' || command === 'wallet') return print({ address: wallet.address, network: NETWORK, walletFile: process.env.SOLANA_PRIVATE_KEY ? 'environment' : config.walletFile });
  const venice = new VeniceClient({ wallet });
  const chain = new SolanaChain(config.rpcUrl);
  if (command === 'balance') {
    const results = await Promise.allSettled([chain.balances(wallet.address), venice.balance()]);
    return print({ address: wallet.address, network: NETWORK, onchain: results[0].status === 'fulfilled' ? { ...results[0].value, usdc: formatUsdc(results[0].value.usdcAtomic) } : { error: results[0].reason.code || 'UNAVAILABLE' }, venice: results[1].status === 'fulfilled' ? results[1].value : { error: results[1].reason.code || 'UNAVAILABLE' } });
  }
  if (command === 'history') return print(await venice.history());
  if (command === 'chat') return print(await venice.chat({ model: values.model, message: values.message, maxTokens: values['max-tokens'] === undefined ? 256 : Number(values['max-tokens']) }));
  const store = new Store(config.stateDir);
  try {
    const payments = new Payments({ wallet, venice, chain, store });
    if (command === 'quote') return print(await payments.prepare());
    if (command === 'pay') return print(await payments.pay(values.quote, values.approve));
    if (command === 'reconcile') return print(await payments.reconcile(values.quote, values.transaction));
    if (command === 'status') return print({ quote: quoteSummary(store.quote(values.quote)), attempt: store.attempt(values.quote) });
  } finally { store.close(); }
}
function print(value) { process.stdout.write(`${JSON.stringify(value, null, 2)}\n`); }
main().catch(error => {
  // Do not expose SDK/RPC exceptions: they may contain signed payloads or URLs
  // with credentials. Controlled ClientError messages never include secrets.
  print({ ok: false, code: error instanceof ClientError ? error.code : 'UNEXPECTED_ERROR', message: error instanceof ClientError ? error.message : 'Command failed. Check configuration and use --help.' });
  process.exitCode = 1;
});
