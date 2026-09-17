import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { ClientError, canonical, formatUsdc, NETWORK, USDC, TOP_UP_URL } from './config.mjs';
import { isTransactionId } from './chain.mjs';

export function quoteHash(quote) {
  const { approvalHash, ...content } = quote;
  return createHash('sha256').update(canonical(content)).digest('hex');
}
export function quoteSummary(quote) {
  return { quoteId: quote.id, service: 'Venice AI credit', network: quote.requirement.network, payer: quote.payer, recipient: quote.requirement.payTo, asset: quote.requirement.asset, amountUsdc: formatUsdc(quote.requirement.amount), feePayer: quote.requirement.extra.feePayer, expiresAt: new Date(quote.expiresAt).toISOString(), approvalHash: quote.approvalHash };
}
function approvalMatches(actual, expected) {
  return typeof actual === 'string' && /^[0-9a-f]{64}$/.test(actual) && timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'));
}
function transactionFrom(response) {
  let receipt;
  const header = response.headers.get('payment-response') || response.headers.get('x-payment-response');
  if (header) {
    try { receipt = JSON.parse(Buffer.from(header, 'base64').toString()); } catch { return null; }
    if (receipt.network && receipt.network !== NETWORK) return null;
  }
  return [receipt?.transaction, response.data.transaction, response.data.transactionHash, response.data.txHash, response.data.data?.transaction, response.data.data?.transactionHash, response.data.data?.txHash].find(isTransactionId) || null;
}

export class Payments {
  constructor({ wallet, venice, chain, store, now = Date.now }) { Object.assign(this, { wallet, venice, chain, store, now }); }
  async prepare() {
    const { requirement, resource } = await this.venice.challenge();
    const quote = { id: randomUUID(), payer: this.wallet.address, createdAt: this.now(), expiresAt: this.now() + 300000, requirement, resource };
    quote.approvalHash = quoteHash(quote);
    this.store.saveQuote(quote);
    return quoteSummary(quote);
  }
  validateQuote(quote, approval) {
    if (!approvalMatches(approval, quoteHash(quote))) throw new ClientError('APPROVAL_REQUIRED', 'The exact displayed quote hash must be explicitly approved.');
    if (quote.payer !== this.wallet.address) throw new ClientError('WRONG_WALLET', 'This quote belongs to another wallet.');
    if (this.now() >= quote.expiresAt) throw new ClientError('QUOTE_EXPIRED', 'The quote expired. Prepare and review a new quote.');
    if (quote.requirement.network !== NETWORK || quote.requirement.asset !== USDC || quote.resource.url !== TOP_UP_URL) throw new ClientError('UNSUPPORTED_PAYMENT', 'Only Venice USDC top-ups on Solana mainnet are supported.');
  }
  async pay(id, approval) {
    const quote = this.store.quote(id);
    this.validateQuote(quote, approval);
    if (this.store.attempt(id)) throw new ClientError('ALREADY_ATTEMPTED', 'A payment was already attempted for this quote. Use reconcile.');
    if (this.store.unresolved(this.wallet.address)) throw new ClientError('UNRESOLVED_PAYMENT', 'Resolve the previous payment before starting another top-up.');
    const fresh = await this.venice.challenge();
    if (canonical(fresh.requirement) !== canonical(quote.requirement) || canonical(fresh.resource) !== canonical(quote.resource)) throw new ClientError('TERMS_CHANGED', 'Venice changed the payment terms. Prepare and approve a new quote.');
    this.validateQuote(quote, approval);
    const funds = await this.chain.balances(this.wallet.address);
    if (BigInt(funds.usdcAtomic) < BigInt(quote.requirement.amount)) throw new ClientError('INSUFFICIENT_USDC', `Fund the displayed Solana wallet first; payment needs ${formatUsdc(quote.requirement.amount)} USDC.`);
    const built = await this.chain.build(quote.requirement, quote.resource, this.wallet);
    this.validateQuote(quote, approval);
    // Persist BEFORE the only submission. Only public metadata and a message
    // hash are stored; keys, auth headers and signed payloads remain in memory.
    this.store.claim(id, this.wallet.address, built.messageHash);
    let response;
    try { response = await this.venice.submit(built.payload); }
    catch {
      this.store.update(id, 'uncertain');
      throw new ClientError('PAYMENT_UNCERTAIN', 'The payment response was lost. Do not pay again. Check history and reconcile this quote.');
    }
    const transaction = transactionFrom(response);
    this.store.update(id, 'uncertain', transaction);
    if (!transaction) throw new ClientError('PAYMENT_UNCERTAIN', `Venice returned HTTP ${response.status} without a usable transaction receipt. Check history and reconcile; do not pay again.`);
    return this.reconcile(id, transaction);
  }
  async reconcile(id, transaction) {
    const attempt = this.store.attempt(id);
    if (!attempt || attempt.payer !== this.wallet.address) throw new ClientError('ATTEMPT_NOT_FOUND', 'No payment attempt for this wallet and quote.');
    const signature = transaction || attempt.transaction_id;
    if (!signature) throw new ClientError('TRANSACTION_REQUIRED', 'Find the transaction signature in Venice history, then pass --transaction. This command never sends money.');
    if (['confirmed', 'failed'].includes(attempt.state)) {
      if (signature !== attempt.transaction_id) throw new ClientError('ALREADY_RESOLVED', 'This attempt is already resolved with a different transaction.');
      let veniceBalance = null;
      try { veniceBalance = await this.venice.balance(); } catch { /* Preserve recorded chain proof. */ }
      return { quoteId: id, state: attempt.state, transaction: signature, veniceBalance, explorer: `https://solscan.io/tx/${signature}`, note: 'Previously verified on-chain result. Venice credit is shown separately.' };
    }
    const proof = await this.chain.verify(signature, attempt.message_hash);
    this.store.update(id, proof.state, signature);
    let veniceBalance = null;
    try { veniceBalance = await this.venice.balance(); } catch { /* Payment proof remains distinct from merchant credit. */ }
    return { quoteId: id, ...proof, veniceBalance, explorer: `https://solscan.io/tx/${signature}`, note: 'confirmed means the exact signed payment confirmed on Solana; inspect Venice balance separately for usable credit.' };
  }
}
