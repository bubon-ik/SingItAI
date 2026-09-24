// Fixed Exa /search client. Queries/results stay in memory; only hashes persist.
import { createHash, randomUUID } from 'node:crypto';
import { isAddress } from '@solana/kit';
import { ClientError, canonical, NETWORK, USDC } from './config.mjs';
import { quoteHash } from './payments.mjs';
import { isTransactionId } from './chain.mjs';

export const EXA_URL = 'https://api.exa.ai/search';
export const MAX_SEARCH_ATOMIC = 20000n;
export function searchBody(query) {
  if (typeof query !== 'string' || !query.trim() || query.length > 2000) throw new ClientError('EXA_INVALID_QUERY', 'Invalid search query.');
  return { query: query.trim(), type: 'auto', numResults: 3, contents: { text: { maxCharacters: 1200 } } };
}
const bodyHash = body => createHash('sha256').update(canonical(body)).digest('hex');

export function selectExaRequirement(challenge) {
  if (challenge?.x402Version !== 2 || !Array.isArray(challenge.accepts)) throw new ClientError('EXA_INVALID_CHALLENGE', 'Expected x402 v2.');
  if (challenge.resource?.url && challenge.resource.url !== EXA_URL) throw new ClientError('EXA_RESOURCE_CHANGED', 'Unexpected search endpoint.');
  const matches = challenge.accepts.filter(r => r.scheme === 'exact' && r.network === NETWORK && r.asset === USDC);
  if (matches.length !== 1) throw new ClientError('EXA_UNSUPPORTED_PAYMENT', 'Expected one Solana mainnet USDC option.');
  const r = matches[0];
  if (typeof r.amount !== 'string' || !/^[1-9][0-9]*$/.test(r.amount) || BigInt(r.amount) > MAX_SEARCH_ATOMIC) throw new ClientError('EXA_PRICE_LIMIT', 'Search exceeds the per-call limit.');
  if (!isAddress(r.payTo) || !isAddress(r.extra?.feePayer)) throw new ClientError('EXA_INVALID_ADDRESS', 'Invalid merchant or fee payer.');
  if (!Number.isInteger(r.maxTimeoutSeconds) || r.maxTimeoutSeconds < 1 || r.maxTimeoutSeconds > 300) throw new ClientError('EXA_INVALID_TIMEOUT', 'Invalid payment lifetime.');
  return structuredClone(r);
}

export function publicResults(data) {
  if (!Array.isArray(data?.results)) return null;
  return data.results.slice(0, 3).flatMap(hit => {
    try {
      const url = new URL(hit.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.href.length > 2000) return [];
      return [{ url: url.href, title: String(hit.title || '').slice(0, 200), text: String(hit.text || (Array.isArray(hit.highlights) ? hit.highlights.join('\n') : '')).slice(0, 1200) }];
    } catch { return []; }
  });
}

export class ExaClient {
  constructor({ fetcher = fetch } = {}) { this.fetcher = fetcher; }
  async request(body, payload) {
    const headers = { accept: 'application/json', 'content-type': 'application/json' };
    if (payload) headers['PAYMENT-SIGNATURE'] = Buffer.from(JSON.stringify(payload)).toString('base64');
    let response;
    try { response = await this.fetcher(EXA_URL, { method: 'POST', headers, body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(payload ? 90000 : 20000) }); }
    catch { throw new ClientError('EXA_NETWORK_ERROR', 'Search response unavailable.'); }
    let data;
    try {
      const raw = await response.text();
      if (raw.length > 2000000) throw new Error();
      data = raw ? JSON.parse(raw) : {};
    } catch { throw new ClientError('EXA_INVALID_RESPONSE', 'Unreadable search response.'); }
    return { status: response.status, headers: response.headers, data };
  }
  async challenge(body) {
    const r = await this.request(body);
    if (r.status === 429) throw new ClientError('EXA_RATE_LIMIT', 'Search discovery is rate limited. Try later.');
    if (r.status !== 402) throw new ClientError('EXA_CHALLENGE_FAILED', 'Expected a payment challenge.');
    let challenge;
    try { challenge = JSON.parse(Buffer.from(r.headers.get('payment-required'), 'base64').toString()); }
    catch { throw new ClientError('EXA_INVALID_CHALLENGE', 'Missing or invalid payment header.'); }
    if (r.data.accepts && canonical(r.data.accepts) !== canonical(challenge.accepts)) throw new ClientError('EXA_INVALID_CHALLENGE', 'Conflicting challenge terms.');
    return { requirement: selectExaRequirement(challenge), resource: { url: EXA_URL, description: 'Exa web search', mimeType: 'application/json' } };
  }
}

export class ExaPayments {
  constructor({ wallet, exa, chain, store, now = Date.now }) { Object.assign(this, { wallet, exa, chain, store, now }); }
  async terms() {
    const { requirement } = await this.exa.challenge(searchBody('Search payment setup'));
    if (requirement.extra.feePayer === this.wallet.address) throw new ClientError('EXA_INVALID_FEE_PAYER', 'A sponsored payment is required.');
    return { network: NETWORK, asset: USDC, recipient: requirement.payTo, payer: this.wallet.address, amountAtomic: requirement.amount, endpoint: EXA_URL };
  }
  async prepare(query) {
    if (this.store.unresolved(this.wallet.address)) throw new ClientError('EXA_PAYMENT_PENDING', 'Resolve the existing search payment first.');
    const body = searchBody(query);
    const { requirement, resource } = await this.exa.challenge(body);
    if (requirement.extra.feePayer === this.wallet.address) throw new ClientError('EXA_INVALID_FEE_PAYER', 'A sponsored payment is required.');
    const quote = { id: randomUUID(), purpose: 'exa-search', payer: this.wallet.address, createdAt: this.now(), expiresAt: this.now() + Math.min(60, requirement.maxTimeoutSeconds) * 1000, requestHash: bodyHash(body), requirement, resource };
    quote.approvalHash = quoteHash(quote);
    this.store.saveQuote(quote);
    return { quoteId: quote.id, approvalHash: quote.approvalHash, payer: quote.payer, recipient: requirement.payTo, network: NETWORK, asset: USDC, amountAtomic: requirement.amount, endpoint: EXA_URL, expiresAt: quote.expiresAt };
  }
  owned(id) {
    const quote = this.store.quote(id);
    if (quote.payer !== this.wallet.address || quote.purpose !== 'exa-search') throw new ClientError('WRONG_WALLET', 'Search quote ownership mismatch.');
    return quote;
  }
  status(id) {
    this.owned(id);
    const attempt = this.store.attempt(id);
    return { quoteId: id, attempted: !!attempt, state: attempt?.state || 'quoted', transaction: attempt?.transaction_id || null };
  }
  async pay({ quoteId, approvalHash, query, authorization }) {
    const quote = this.owned(quoteId), body = searchBody(query);
    const validate = () => {
      if (quote.approvalHash !== approvalHash || quoteHash(quote) !== approvalHash || quote.requestHash !== bodyHash(body)) throw new ClientError('EXA_QUOTE_MISMATCH', 'Search request or quote changed.');
      selectExaRequirement({ x402Version: 2, accepts: [quote.requirement], resource: quote.resource });
      if (quote.resource.url !== EXA_URL || quote.expiresAt <= this.now()) throw new ClientError('QUOTE_EXPIRED', 'Search quote expired.');
      if (!authorization || !/^[a-f0-9]{64}$/.test(authorization.policyHash || '') || authorization.payer !== this.wallet.address || authorization.recipient !== quote.requirement.payTo || authorization.network !== NETWORK || authorization.asset !== USDC || authorization.endpoint !== EXA_URL || !Number.isSafeInteger(authorization.expiresAt) || authorization.expiresAt * 1000 <= this.now() || !Number.isSafeInteger(authorization.maxPerCallAtomic) || authorization.maxPerCallAtomic < 1 || authorization.maxPerCallAtomic > Number(MAX_SEARCH_ATOMIC) || BigInt(quote.requirement.amount) > BigInt(authorization.maxPerCallAtomic)) throw new ClientError('EXA_POLICY_MISMATCH', 'Search is outside the approved policy.');
    };
    validate();
    if (this.store.attempt(quoteId)) throw new ClientError('ALREADY_ATTEMPTED', 'This search payment was already attempted.');
    if (this.store.unresolved(this.wallet.address)) throw new ClientError('EXA_PAYMENT_PENDING', 'Resolve the existing search payment first.');
    const balance = await this.chain.balances(this.wallet.address);
    if (BigInt(balance.usdcAtomic) < BigInt(quote.requirement.amount)) throw new ClientError('INSUFFICIENT_USDC', 'Insufficient USDC for web search.');
    const built = await this.chain.build(quote.requirement, quote.resource, this.wallet);
    validate();
    this.store.claim(quoteId, this.wallet.address, built.messageHash);
    let response;
    try { response = await this.exa.request(body, built.payload); }
    catch {
      this.store.update(quoteId, 'uncertain');
      throw new ClientError('EXA_PAYMENT_UNCERTAIN', 'Search payment response lost. Do not pay again.');
    }
    let receipt;
    try { receipt = JSON.parse(Buffer.from(response.headers.get('payment-response'), 'base64').toString()); } catch { /* Remain uncertain. */ }
    const transaction = receipt?.network === NETWORK && isTransactionId(receipt.transaction) ? receipt.transaction : null;
    this.store.update(quoteId, 'uncertain', transaction);
    if (!transaction) throw new ClientError('EXA_PAYMENT_UNCERTAIN', 'Search payment has no usable receipt.');
    const proof = await this.reconcile(quoteId, transaction);
    const results = response.status === 200 && receipt.success === true && proof.state === 'confirmed' ? publicResults(response.data) : null;
    return { ...proof, results, delivered: results !== null };
  }
  async reconcile(id, signature) {
    this.owned(id);
    const attempt = this.store.attempt(id);
    if (!attempt || attempt.payer !== this.wallet.address) throw new ClientError('ATTEMPT_NOT_FOUND', 'No search payment attempt.');
    const transaction = signature || attempt.transaction_id;
    if (!isTransactionId(transaction)) throw new ClientError('TRANSACTION_REQUIRED', 'A search transaction signature is required.');
    if (['confirmed', 'failed'].includes(attempt.state)) {
      if (transaction !== attempt.transaction_id) throw new ClientError('ALREADY_RESOLVED', 'Attempt already resolved with another signature.');
      return { quoteId: id, state: attempt.state, transaction };
    }
    const proof = await this.chain.verify(transaction, attempt.message_hash);
    this.store.update(id, proof.state, transaction);
    return { quoteId: id, ...proof };
  }
}
