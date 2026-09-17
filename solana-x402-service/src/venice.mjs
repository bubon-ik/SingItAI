import { isAddress } from '@solana/kit';
import { authHeader } from './wallet.mjs';
import { ClientError, NETWORK, USDC, VENICE, TOP_UP_URL, canonical } from './config.mjs';

export function selectRequirement(challenge) {
  if (challenge?.x402Version !== 2 || !Array.isArray(challenge.accepts)) throw new ClientError('INVALID_CHALLENGE', 'Expected an x402 v2 payment challenge.');
  if (challenge.resource?.url && challenge.resource.url !== TOP_UP_URL) throw new ClientError('RESOURCE_CHANGED', 'The payment resource does not match Venice top-up.');
  const matches = challenge.accepts.filter(r => r.scheme === 'exact' && r.network === NETWORK && r.asset === USDC);
  if (matches.length !== 1) throw new ClientError('UNSUPPORTED_PAYMENT', 'Expected exactly one Solana mainnet USDC exact payment option.');
  const r = matches[0];
  if (typeof r.amount !== 'string' || !/^[1-9][0-9]*$/.test(r.amount) || BigInt(r.amount) > 50000000n) throw new ClientError('INVALID_AMOUNT', 'Top-up must be positive and no more than 50 USDC in this prototype.');
  if (!isAddress(r.payTo) || !isAddress(r.extra?.feePayer)) throw new ClientError('INVALID_ADDRESS', 'The merchant or fee-payer address is invalid.');
  if (!Number.isInteger(r.maxTimeoutSeconds) || r.maxTimeoutSeconds < 1 || r.maxTimeoutSeconds > 300) throw new ClientError('INVALID_TIMEOUT', 'Unsupported payment lifetime.');
  if (r.extra.memo !== undefined && (typeof r.extra.memo !== 'string' || Buffer.byteLength(r.extra.memo) > 256)) throw new ClientError('INVALID_MEMO', 'Invalid payment memo.');
  return structuredClone(r);
}

export class VeniceClient {
  constructor({ wallet, fetcher = fetch, timeoutMs = 20000 } = {}) { this.wallet = wallet; this.fetcher = fetcher; this.timeoutMs = timeoutMs; }
  async request(endpoint, { method = 'GET', body, headers = {}, authenticated = true, timeoutMs = this.timeoutMs } = {}) {
    const requestHeaders = { accept: 'application/json', ...headers };
    if (body !== undefined) requestHeaders['content-type'] = 'application/json';
    if (authenticated && this.wallet) requestHeaders['X-Sign-In-With-X'] = authHeader(this.wallet);
    let response;
    try { response = await this.fetcher(`${VENICE}${endpoint}`, { method, headers: requestHeaders, body: body === undefined ? undefined : JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(timeoutMs) }); }
    catch { throw new ClientError('NETWORK_ERROR', 'Venice request failed or timed out. No automatic retry was made.'); }
    let data;
    try { const text = await response.text(); if (text.length > 2000000) throw new Error(); data = text ? JSON.parse(text) : {}; }
    catch { throw new ClientError('INVALID_RESPONSE', 'Venice returned an unreadable response.'); }
    return { status: response.status, ok: response.ok, headers: response.headers, data };
  }
  async challenge() {
    const response = await this.request('/x402/top-up', { method: 'POST', body: {} });
    if (response.status !== 402) throw new ClientError('CHALLENGE_FAILED', `Expected a payment challenge; Venice returned HTTP ${response.status}.`);
    let challenge = response.data;
    const encoded = response.headers.get('payment-required');
    if (encoded) {
      try { challenge = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8')); }
      catch { throw new ClientError('INVALID_CHALLENGE', 'The payment header is invalid.'); }
      if (response.data.accepts && canonical(response.data.accepts) !== canonical(challenge.accepts)) throw new ClientError('INCONSISTENT_CHALLENGE', 'Payment header and body disagree.');
    }
    return { requirement: selectRequirement(challenge), resource: { url: TOP_UP_URL, description: 'Venice AI credit top-up', mimeType: 'application/json' } };
  }
  async balance() {
    if (!this.wallet) throw new ClientError('WALLET_REQUIRED', 'A wallet is required.');
    const r = await this.request(`/x402/balance/${this.wallet.address}`);
    if (!r.ok) throw new ClientError('BALANCE_FAILED', `Venice balance request returned HTTP ${r.status}.`);
    const data = r.data.data || r.data;
    if (typeof data.canConsume !== 'boolean' || !Number.isFinite(Number(data.balanceUsd)) || Number(data.balanceUsd) < 0) throw new ClientError('INVALID_BALANCE', 'Venice returned an unexpected balance format.');
    return { canConsume: data.canConsume, balanceUsd: data.balanceUsd, minimumTopUpUsd: data.minimumTopUpUsd, suggestedTopUpUsd: data.suggestedTopUpUsd };
  }
  async history() {
    const r = await this.request(`/x402/transactions/${this.wallet.address}?limit=20&offset=0`);
    if (!r.ok) throw new ClientError('HISTORY_FAILED', `Venice history request returned HTTP ${r.status}.`);
    return r.data;
  }
  async submit(payload) {
    // Venice documents this header explicitly. Never let a fetch wrapper sign
    // a replacement payment or retry with newly offered terms.
    return this.request('/x402/top-up', { method: 'POST', body: {}, headers: { 'X-402-Payment': Buffer.from(JSON.stringify(payload)).toString('base64') }, timeoutMs: 120000 });
  }
  async models() {
    const r = await this.request('/models?type=text', { authenticated: false });
    if (!r.ok || !Array.isArray(r.data.data)) throw new ClientError('MODELS_FAILED', 'Could not read Venice models.');
    return r.data.data.map(m => ({ id: m.id, type: m.type }));
  }
  async chat({ model, message, maxTokens = 256 }) {
    if (!model || !message || !Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 2048) throw new ClientError('INVALID_CHAT', 'Specify model, message and max-tokens between 1 and 2048.');
    const balance = await this.balance();
    if (!balance.canConsume) throw new ClientError('TOP_UP_REQUIRED', 'Venice credit is insufficient. Prepare and approve a top-up first.');
    const r = await this.request('/chat/completions', { method: 'POST', body: { model, messages: [{ role: 'user', content: message }], max_tokens: maxTokens, stream: false }, timeoutMs: 60000 });
    if (r.status === 402) throw new ClientError('TOP_UP_REQUIRED', 'Venice requires more credit. No automatic top-up was attempted.');
    if (!r.ok) throw new ClientError('CHAT_FAILED', `Venice chat returned HTTP ${r.status}.`);
    const text = r.data.choices?.[0]?.message?.content;
    if (typeof text !== 'string') throw new ClientError('INVALID_CHAT_RESPONSE', 'Venice did not return a text completion.');
    return { model: r.data.model || model, text, usage: r.data.usage, balanceRemaining: r.headers.get('x-balance-remaining') };
  }
}
