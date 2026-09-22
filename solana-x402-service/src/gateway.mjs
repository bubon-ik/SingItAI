// Private JSON-over-stdin bridge. No wallet files, secret argv, or implicit pay.
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { ExaClient, ExaPayments } from './exa.mjs';
import { getBase58Encoder } from '@solana/kit';
import { walletFromBytes } from './wallet.mjs';
import { configuration, ClientError } from './config.mjs';
import { Store } from './store.mjs';
import { VeniceClient } from './venice.mjs';
import { SolanaChain } from './chain.mjs';
import { Payments, quoteSummary } from './payments.mjs';

export async function dispatch(input, { wallet, venice, chain, store, exa }) {
  if (wallet.address !== input.payer) throw new ClientError('WRONG_WALLET', 'Wallet mismatch.');
  if (typeof input.operation === 'string' && input.operation.startsWith('exa-')) {
    const payments = new ExaPayments({ wallet, exa, chain, store });
    if (input.operation === 'exa-terms') return payments.terms();
    if (input.operation === 'exa-quote') return payments.prepare(input.query);
    if (input.operation === 'exa-search') return payments.pay(input);
    if (input.operation === 'exa-status') return payments.status(input.quoteId);
    if (input.operation === 'exa-reconcile') return payments.reconcile(input.quoteId, input.transaction);
    throw new ClientError('INVALID_OPERATION', 'Unsupported search operation.');
  }
  const payments = new Payments({ wallet, venice, chain, store });
  if (input.operation === 'balance') return venice.balance();
  if (input.operation === 'quote') return payments.prepare();
  if (input.operation === 'chat') return venice.chat({ model: input.model, message: input.message, maxTokens: 1024, sources: input.sources });
  if (!['pay', 'status', 'reconcile'].includes(input.operation)) throw new ClientError('INVALID_OPERATION', 'Unsupported operation.');
  const quote = store.quote(input.quoteId);
  if (quote.payer !== wallet.address) throw new ClientError('WRONG_WALLET', 'Quote belongs to another wallet.');
  if (input.operation === 'pay') return payments.pay(input.quoteId, input.approvalHash);
  if (input.operation === 'status') {
    const attempt = store.attempt(input.quoteId);
    return { quote: quoteSummary(quote), attempted: !!attempt, state: attempt?.state || 'quoted', transaction: attempt?.transaction_id || null };
  }
  return payments.reconcile(input.quoteId, input.transaction);
}

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) {
    raw += chunk;
    if (Buffer.byteLength(raw) > 100000) throw new ClientError('INVALID_INPUT', 'Request too large.');
  }
  const input = JSON.parse(raw);
  const bytes = getBase58Encoder().encode(input.privateKey);
  const wallet = await walletFromBytes(bytes);
  bytes.fill(0);
  delete input.privateKey;
  const config = configuration();
  const store = new Store(input.operation?.startsWith('exa-') ? path.join(config.stateDir, 'exa') : config.stateDir);
  try {
    const result = await dispatch(input, { wallet, store, venice: new VeniceClient({ wallet }), exa: new ExaClient(), chain: new SolanaChain(config.rpcUrl) });
    process.stdout.write(JSON.stringify({ ok: true, result }));
  } finally { store.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    // Never include arbitrary SDK/provider errors, auth, signed payloads or input.
    process.stdout.write(JSON.stringify({ ok: false, code: error instanceof ClientError ? error.code : 'BRIDGE_FAILED' }));
    process.exitCode = 1;
  });
}
