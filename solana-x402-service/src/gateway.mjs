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
import { TokenAllowance } from './allowance.mjs';
import { InvoicePayments } from './invoice.mjs';
import { ResourcePayments } from './resource.mjs';

export async function dispatch(input, { wallet, venice, chain, store, exa, feePayer = null, allowance = null }) {
  if (wallet.address !== input.payer) throw new ClientError('WRONG_WALLET', 'Wallet mismatch.');
  if (input.operation === 'bitrefill-invoice-pay') {
    return new InvoicePayments({ wallet, chain, store }).pay({ url: input.url, invoiceId: input.invoiceId, maxAmount: input.maxAmount, owner: input.owner || null });
  }
  if (input.operation === 'bitrefill-invoice-attempt') return store.invoiceAttempt(input.invoiceId);
  if (input.operation === 'data-pay') {
    return new ResourcePayments({ wallet, chain, store }).pay({ callId: input.callId, url: input.url, method: input.method || 'GET',
      body: input.body ?? null, payTo: input.payTo, maxAmount: input.maxAmount, owner: input.owner });
  }
  if (typeof input.operation === 'string' && input.operation.startsWith('allowance-')) {
    // `wallet` is the agent: the owner's delegate and the payer of every purchase.
    const lane = allowance || new TokenAllowance({ chain });
    const needFeePayer = () => { if (!feePayer) throw new ClientError('FEE_PAYER_REQUIRED', 'No fee payer is configured.'); return feePayer; };
    if (input.operation === 'allowance-state') {
      const [owner, agent, ownerSol] = await Promise.all([lane.state(input.owner, wallet.address), chain.balances(wallet.address), chain.balances(input.owner)]);
      return { owner: { ...owner, solLamports: ownerSol.solLamports }, agent: { usdcAtomic: agent.usdcAtomic, solLamports: agent.solLamports }, feePayer: feePayer?.address || null };
    }
    if (input.operation === 'allowance-prepare') {
      // The owner pays this one small fee from their own SOL, unless our fee payer is asked to.
      const payer = input.ownerPaysFee ? input.owner : needFeePayer().address;
      return lane.prepare({ kind: input.kind, owner: input.owner, delegate: wallet.address, amount: input.amount || '0', feePayer: payer });
    }
    if (input.operation === 'allowance-submit') {
      return lane.submit({ transaction: input.transaction, expectedHash: input.messageHash, owner: input.owner,
        feePayer: input.ownerPaysFee ? null : needFeePayer(), kind: input.kind, delegate: wallet.address, amount: input.amount || '0' });
    }
    if (input.operation === 'allowance-pull') {
      return lane.pull({ owner: input.owner, amount: input.amount, agent: wallet, feePayer: needFeePayer() });
    }
    throw new ClientError('INVALID_OPERATION', 'Unsupported allowance operation.');
  }
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
  if (input.operation === 'quote') return payments.prepare(input.amount ?? null);
  if (input.operation === 'chat') return venice.chat({ model: input.model, message: input.message, conversation: input.conversation, maxTokens: 1024, sources: input.sources, offerSearch: input.offerSearch === true });
  if (!['pay', 'status', 'reconcile'].includes(input.operation)) throw new ClientError('INVALID_OPERATION', 'Unsupported operation.');
  const quote = store.quote(input.quoteId);
  if (quote.payer !== wallet.address) throw new ClientError('WRONG_WALLET', 'Quote belongs to another wallet.');
  if (input.operation === 'pay') return payments.pay(input.quoteId, input.approvalHash, input.owner || null);
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
  let feePayer = null;
  if (input.feePayerKey) {
    const payerBytes = getBase58Encoder().encode(input.feePayerKey);
    feePayer = await walletFromBytes(payerBytes);
    payerBytes.fill(0);
  }
  delete input.feePayerKey;
  const config = configuration();
  const store = new Store(input.operation?.startsWith('exa-') ? path.join(config.stateDir, 'exa') : config.stateDir);
  try {
    const result = await dispatch(input, { wallet, store, feePayer, venice: new VeniceClient({ wallet }), exa: new ExaClient(), chain: new SolanaChain(config.rpcUrl) });
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
