// Paying one x402 data request on Solana for a web account: live weather, a flight's status, a web
// page... straight from the owner's USDC account, the agent as their approved delegate, the seller's
// fee payer paying the fee. Only the hosts listed here, only to the address the caller bound for that
// seller, never above the caller's ceiling. One attempt per call id, recorded before the only
// submission; an unclear result is never retried here.
import { isAddress } from '@solana/kit';
import { ClientError, NETWORK, USDC } from './config.mjs';
import { isTransactionId } from './chain.mjs';
import { assertDelegated, TokenAllowance } from './allowance.mjs';

export const DATA_HOSTS = new Set(['x402.ottoai.services', 'api.exa.ai', 'stabletravel.dev', 'tripadvisor.x402.paysponge.com',
  'stableemail.dev']);  // an email the user sent to themselves, after pressing Send
export const MAX_DATA_ATOMIC = 50000n;  // $0.05: data, never a purchase
const MAX_BODY = 1_500_000;

function decode(header) {
  try { return JSON.parse(Buffer.from(header, 'base64').toString('utf8')); }
  catch { throw new ClientError('INVALID_CHALLENGE', 'The seller sent an unreadable payment request.'); }
}

export function selectDataRequirement(challenge, { payTo, maxAmount, payer, owner }) {
  const accepts = Array.isArray(challenge?.accepts) ? challenge.accepts : [];
  const onSolana = accepts.filter(r => r?.scheme === 'exact' && r.network === NETWORK && r.asset === USDC);
  if (!onSolana.length) throw new ClientError('UNSUPPORTED_PAYMENT', 'This seller takes no USDC on Solana.');
  const r = onSolana.find(x => x.payTo === payTo);
  if (!r) throw new ClientError('MERCHANT_CHANGED', 'The seller asked to be paid somewhere unexpected. Nothing was paid.');
  if (typeof r.amount !== 'string' || !/^[1-9][0-9]*$/.test(r.amount)) throw new ClientError('INVALID_AMOUNT', 'The seller asked for an invalid amount.');
  if (BigInt(r.amount) > BigInt(maxAmount)) throw new ClientError('PRICE_CHANGED', 'The seller asks more than agreed. Nothing was paid.');
  if (!r.extra?.feePayer || [payer, owner].includes(r.extra.feePayer)) throw new ClientError('UNEXPECTED_FEE_PAYER', 'This payment must be sponsored by the seller.');
  return structuredClone(r);
}

// The seller's own words for a refusal, reduced to plain text: the x402 error code, never a payload.
export function refusalReason(response, text) {
  let reason = '';
  const header = response.headers.get('payment-required');
  if (header) { try { reason = JSON.parse(Buffer.from(header, 'base64').toString('utf8')).error || ''; } catch { reason = ''; } }
  if (!reason) { try { reason = JSON.parse(text).error || ''; } catch { reason = ''; } }
  return String(reason).replace(/[^\w\s:.,()/-]/g, '').slice(0, 160);
}

function transactionOf(response) {
  const header = response.headers.get('payment-response') || response.headers.get('x-payment-response');
  let receipt = null;
  if (header) { try { receipt = JSON.parse(Buffer.from(header, 'base64').toString('utf8')); } catch { receipt = null; } }
  return [receipt?.transaction, receipt?.txHash].find(isTransactionId) || null;
}

export function checkDataCall({ callId, url, method, body, payTo, maxAmount, owner }) {
  if (typeof callId !== 'string' || !/^data-[A-Za-z0-9_-]{8,64}$/.test(callId)) throw new ClientError('INVALID_CALL', 'Invalid call id.');
  let parsed;
  try { parsed = new URL(url); } catch { throw new ClientError('UNSUPPORTED_MERCHANT', 'Invalid data URL.'); }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || !DATA_HOSTS.has(parsed.hostname)) throw new ClientError('UNSUPPORTED_MERCHANT', 'Only listed data sellers are paid here.');
  if (!['GET', 'POST'].includes(method)) throw new ClientError('INVALID_CALL', 'Unsupported method.');
  if (method === 'GET' && body != null) throw new ClientError('INVALID_CALL', 'A GET request has no body.');
  if (!isAddress(String(payTo || ''))) throw new ClientError('INVALID_CALL', 'Invalid recipient.');
  if (!/^[1-9][0-9]*$/.test(String(maxAmount)) || BigInt(maxAmount) > MAX_DATA_ATOMIC) throw new ClientError('INVALID_AMOUNT', 'Invalid maximum amount.');
  if (!isAddress(String(owner || ''))) throw new ClientError('INVALID_CALL', 'Data is paid from the owner’s account only.');
}

export class ResourcePayments {
  constructor({ wallet, chain, store, fetcher = fetch }) { Object.assign(this, { wallet, chain, store, fetcher }); }

  async request(url, method, body, headers = {}) {
    try {
      return await this.fetcher(url, { method, redirect: 'error', signal: AbortSignal.timeout(90000),
        headers: { accept: 'application/json', ...(body != null ? { 'content-type': 'application/json' } : {}), ...headers },
        ...(body != null ? { body: JSON.stringify(body) } : {}) });
    } catch { throw new ClientError('NETWORK_ERROR', 'The seller did not answer. Nothing was retried.'); }
  }

  async pay({ callId, url, method = 'GET', body = null, payTo, maxAmount, owner }) {
    checkDataCall({ callId, url, method, body, payTo, maxAmount, owner });
    if (this.store.invoiceAttempt(callId)) throw new ClientError('ALREADY_ATTEMPTED', 'This request was already paid or attempted. Nothing more was sent.');
    const challenge = await this.request(url, method, body);
    if (challenge.status !== 402) throw new ClientError('CHALLENGE_FAILED', `The seller answered HTTP ${challenge.status} instead of a payment request. Nothing was paid.`);
    const header = challenge.headers.get('payment-required');
    let offer;
    if (header) offer = decode(header);
    else { try { offer = await challenge.json(); } catch { throw new ClientError('INVALID_CHALLENGE', 'The seller sent no payment request.'); } }
    const requirement = selectDataRequirement(offer, { payTo, maxAmount, payer: this.wallet.address, owner });
    const resource = offer.resource || { url, description: 'x402 data', mimeType: 'application/json' };
    const before = await assertDelegated(this.chain, owner, this.wallet.address, requirement.amount);
    const built = await this.chain.buildDelegated(requirement, resource, this.wallet, owner);
    this.store.claimInvoice(callId, this.wallet.address, built.messageHash, requirement.amount);
    let response;
    try { response = await this.request(url, method, body, { 'PAYMENT-SIGNATURE': Buffer.from(JSON.stringify(built.payload)).toString('base64') }); }
    catch {
      this.store.updateInvoice(callId, 'uncertain');
      throw new ClientError('PAYMENT_UNCERTAIN', 'The answer was lost after paying. It was not repeated.');
    }
    const transaction = transactionOf(response);
    let text = '';
    try { text = (await response.text()).slice(0, MAX_BODY); } catch { text = ''; }
    if (response.status !== 200) {
      this.store.updateInvoice(callId, 'uncertain', transaction);
      const reason = refusalReason(response, text);
      // A 402 to a signed payment is a refusal: when the owner's balance did not move, nothing was paid.
      let moved = true;
      if (response.status === 402 && !transaction) {
        try { moved = BigInt((await new TokenAllowance({ chain: this.chain }).state(owner, this.wallet.address)).amount) < BigInt(before.amount); }
        catch { moved = true; }
      }
      const error = moved
        ? new ClientError('PAYMENT_UNCERTAIN', `The seller answered HTTP ${response.status}. It was not repeated.`)
        : new ClientError('PAYMENT_REFUSED', 'The seller refused the payment. Nothing was paid.');
      error.reason = reason;
      throw error;
    }
    this.store.updateInvoice(callId, 'accepted', transaction);
    let data;
    try { data = JSON.parse(text); } catch { data = text; }
    return { callId, state: 'accepted', transaction, amount: requirement.amount, data };
  }
}
