import test from 'node:test';
import { dispatch } from '../src/gateway.mjs';
import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { getBase58Encoder, getBase58Decoder, getTransactionDecoder, getBase64EncodedWireTransaction } from '@solana/kit';
import { SolanaChain, VeniceClient, Store, Payments, NETWORK, USDC } from '../src/index.mjs';
import { TOKEN_PROGRAM } from '../src/config.mjs';
import { directory, testWallet, challenge, jsonResponse } from './helpers.mjs';

for (const gateway of [false, true]) test(`full ${gateway ? 'gateway bridge' : 'client'} flow: quote → approval → payment → chain proof → credit → chat, offline`, async t => {
  const wallet = await testWallet(), sponsor = await testWallet(), merchant = await testWallet();
  const dir = directory(), store = new Store(dir);
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; store.close(); rmSync(dir, { recursive: true }); });
  let paidRequests = 0, credit = 0, settledWire, transaction;
  const mint = Buffer.alloc(82); mint[44] = 6; mint[45] = 1;
  const account = Buffer.alloc(165);
  Buffer.from(getBase58Encoder().encode(USDC)).copy(account, 0);
  Buffer.from(getBase58Encoder().encode(wallet.address)).copy(account, 32);
  account.writeBigUInt64LE(10000000n, 64); account[108] = 1;
  const terms = challenge({ payTo: merchant.address, extra: { feePayer: sponsor.address } });
  const fetcher = async (url, init) => {
    url = String(url);
    if (url.startsWith('https://rpc.invalid')) {
      const q = JSON.parse(init.body); let result;
      if (q.method === 'getBalance') result = { context: { slot: 1 }, value: 0 };
      else if (q.method === 'getAccountInfo') { const data = q.params[0] === USDC ? mint : account; result = { context: { slot: 1 }, value: { owner: TOKEN_PROGRAM, data: [data.toString('base64'), 'base64'], lamports: 2000000, executable: false, rentEpoch: 0, space: data.length } }; }
      else if (q.method === 'getLatestBlockhash') result = { context: { slot: 1 }, value: { blockhash: '11111111111111111111111111111111', lastValidBlockHeight: 100 } };
      else if (q.method === 'getTransaction') { assert.equal(q.params[0], transaction); result = { transaction: [settledWire, 'base64'], meta: { err: null }, slot: 2, blockTime: null, version: 0 }; }
      else throw new Error(`Unexpected RPC: ${q.method}`);
      return jsonResponse({ jsonrpc: '2.0', id: q.id, result });
    }
    assert.ok(url.startsWith('https://api.venice.ai/api/v1/'));
    const auth = JSON.parse(Buffer.from(init.headers['X-Sign-In-With-X'], 'base64').toString());
    assert.equal(auth.address, wallet.address);
    assert.equal(await crypto.subtle.verify('Ed25519', wallet.signer.keyPair.publicKey, getBase58Encoder().encode(auth.signature), new TextEncoder().encode(auth.message)), true);
    if (url.endsWith('/x402/top-up')) {
      const payment = init.headers['X-402-Payment'];
      if (!payment) return jsonResponse({ x402Version: 2, accepts: [terms.requirement] }, 402);
      paidRequests++;
      const payload = JSON.parse(Buffer.from(payment, 'base64').toString());
      assert.deepEqual(payload.accepted, terms.requirement);
      const tx = getTransactionDecoder().decode(Buffer.from(payload.payload.transaction, 'base64'));
      assert.equal(await crypto.subtle.verify('Ed25519', wallet.signer.keyPair.publicKey, tx.signatures[wallet.address], tx.messageBytes), true);
      assert.equal(tx.signatures[sponsor.address], null);
      const sponsorSignature = new Uint8Array(await crypto.subtle.sign('Ed25519', sponsor.signer.keyPair.privateKey, tx.messageBytes));
      settledWire = getBase64EncodedWireTransaction({ ...tx, signatures: { ...tx.signatures, [sponsor.address]: sponsorSignature } });
      transaction = getBase58Decoder().decode(sponsorSignature);
      credit = 5;
      return jsonResponse({ success: true }, 200, { 'payment-response': Buffer.from(JSON.stringify({ success: true, network: NETWORK, transaction })).toString('base64') });
    }
    if (url.includes('/x402/balance/')) return jsonResponse({ data: { canConsume: credit > 0, balanceUsd: credit, minimumTopUpUsd: 5 } });
    if (url.endsWith('/chat/completions')) { assert.equal(credit, 5); return jsonResponse({ model: 'fixture-model', choices: [{ message: { content: 'Solana client works.' } }] }); }
    throw new Error(`Unexpected endpoint: ${url}`);
  };
  globalThis.fetch = fetcher;
  const venice = new VeniceClient({ wallet, fetcher });
  const context = { wallet, venice, chain: new SolanaChain('https://rpc.invalid/'), store };
  const payments = gateway ? {
    prepare: () => dispatch({ operation: 'quote', payer: wallet.address }, context),
    pay: (quoteId, approvalHash) => dispatch({ operation: 'pay', payer: wallet.address, quoteId, approvalHash }, context),
  } : new Payments(context);
  const quote = await payments.prepare();
  assert.equal(paidRequests, 0);
  await assert.rejects(payments.pay(quote.quoteId, '0'.repeat(64)), { code: 'APPROVAL_REQUIRED' });
  const result = await payments.pay(quote.quoteId, quote.approvalHash);
  assert.equal(result.state, 'confirmed');
  assert.equal(result.veniceBalance.balanceUsd, 5);
  const reply = gateway ? await dispatch({ operation: 'chat', payer: wallet.address, model: 'fixture-model', message: 'hello' }, context) : await venice.chat({ model: 'fixture-model', message: 'hello' });
  assert.equal(reply.text, 'Solana client works.');
  await assert.rejects(payments.pay(quote.quoteId, quote.approvalHash), { code: 'ALREADY_ATTEMPTED' });
  assert.equal(paidRequests, 1);
});
