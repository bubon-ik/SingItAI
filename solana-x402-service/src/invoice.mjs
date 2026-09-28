// Paying a Bitrefill invoice over x402 on Solana, as Bitrefill documents it for MCP invoices
// created with usdc_solana (docs.bitrefill.com, "Paying an MCP invoice with x402"):
//   1. POST /x402/invoice/pay {invoice_id} answers 402 with a PAYMENT-REQUIRED header;
//   2. the payment is signed and the POST repeated with a PAYMENT-SIGNATURE header; 200 = accepted.
// The wallet is the agent, which holds exactly what the invoice needs (pulled from the owner's
// allowance). Bitrefill's fee payer sponsors the transaction. One attempt per invoice, recorded
// before the only submission; an uncertain result is never retried here.
import { ClientError, NETWORK, USDC } from './config.mjs';
import { isTransactionId } from './chain.mjs';

export const BITREFILL_PAY_URL = 'https://api.bitrefill.com/x402/invoice/pay';

function decode(header) {
  try { return JSON.parse(Buffer.from(header, 'base64').toString('utf8')); }
  catch { throw new ClientError('INVALID_CHALLENGE', 'Bitrefill sent an unreadable payment request.'); }
}

export function selectInvoiceRequirement(challenge, maxAmount, payer) {
  const accepts = Array.isArray(challenge?.accepts) ? challenge.accepts : [];
  const matches = accepts.filter(r => r?.scheme === 'exact' && r.network === NETWORK && r.asset === USDC);
  if (matches.length !== 1) throw new ClientError('UNSUPPORTED_PAYMENT', 'Bitrefill offered no single USDC payment on Solana for this invoice.');
  const r = matches[0];
  if (typeof r.amount !== 'string' || !/^[1-9][0-9]*$/.test(r.amount)) throw new ClientError('INVALID_AMOUNT', 'Bitrefill asked for an invalid amount.');
  if (BigInt(r.amount) > BigInt(maxAmount)) throw new ClientError('PRICE_CHANGED', 'Bitrefill now asks more than the invoice you were quoted. Nothing was paid.');
  if (!r.extra?.feePayer || r.extra.feePayer === payer) throw new ClientError('UNEXPECTED_FEE_PAYER', 'This payment must be sponsored by Bitrefill.');
  return structuredClone(r);
}

function transactionOf(response) {
  const header = response.headers.get('payment-response') || response.headers.get('x-payment-response');
  let receipt = null;
  if (header) { try { receipt = JSON.parse(Buffer.from(header, 'base64').toString('utf8')); } catch { receipt = null; } }
  const found = [receipt?.transaction, receipt?.txHash];
  return found.find(isTransactionId) || null;
}

export class InvoicePayments {
  constructor({ wallet, chain, store, fetcher = fetch }) { Object.assign(this, { wallet, chain, store, fetcher }); }

  async post(url, headers = {}) {
    try {
      return await this.fetcher(url, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json', ...headers },
        body: JSON.stringify({ invoice_id: this.invoiceId }), redirect: 'error', signal: AbortSignal.timeout(120000) });
    } catch { throw new ClientError('NETWORK_ERROR', 'Bitrefill did not answer. Nothing was retried.'); }
  }

  async pay({ url, invoiceId, maxAmount }) {
    if (url !== BITREFILL_PAY_URL) throw new ClientError('UNSUPPORTED_MERCHANT', 'Only Bitrefill invoices are paid here.');
    if (typeof invoiceId !== 'string' || !/^[0-9a-f-]{8,64}$/i.test(invoiceId)) throw new ClientError('INVALID_INVOICE', 'Invalid invoice.');
    if (!/^[1-9][0-9]*$/.test(String(maxAmount))) throw new ClientError('INVALID_AMOUNT', 'Invalid maximum amount.');
    if (this.store.invoiceAttempt(invoiceId)) throw new ClientError('ALREADY_ATTEMPTED', 'This invoice was already paid or attempted. Nothing more was sent.');
    this.invoiceId = invoiceId;
    const challenge = await this.post(url);
    if (challenge.status !== 402) throw new ClientError('CHALLENGE_FAILED', `Bitrefill answered HTTP ${challenge.status} instead of a payment request.`);
    const header = challenge.headers.get('payment-required');
    if (!header) throw new ClientError('INVALID_CHALLENGE', 'Bitrefill sent no payment request.');
    const offer = decode(header);
    const requirement = selectInvoiceRequirement(offer, maxAmount, this.wallet.address);
    const funds = await this.chain.balances(this.wallet.address);
    if (BigInt(funds.usdcAtomic) < BigInt(requirement.amount)) throw new ClientError('INSUFFICIENT_USDC', 'The agent does not hold this invoice amount yet.');
    const resource = offer.resource || { url, description: 'Bitrefill invoice', mimeType: 'application/json' };
    const built = await this.chain.build(requirement, resource, this.wallet);
    this.store.claimInvoice(invoiceId, this.wallet.address, built.messageHash, requirement.amount);
    let response;
    try { response = await this.post(url, { 'PAYMENT-SIGNATURE': Buffer.from(JSON.stringify(built.payload)).toString('base64') }); }
    catch {
      this.store.updateInvoice(invoiceId, 'uncertain');
      throw new ClientError('PAYMENT_UNCERTAIN', 'The payment answer was lost. Do not pay again; check the invoice status.');
    }
    const transaction = transactionOf(response);
    if (response.status !== 200) {
      this.store.updateInvoice(invoiceId, 'uncertain', transaction);
      throw new ClientError('PAYMENT_UNCERTAIN', `Bitrefill answered HTTP ${response.status}. Do not pay again; check the invoice status.`);
    }
    this.store.updateInvoice(invoiceId, 'accepted', transaction);
    return { invoiceId, state: 'accepted', transaction, amount: requirement.amount };
  }
}
