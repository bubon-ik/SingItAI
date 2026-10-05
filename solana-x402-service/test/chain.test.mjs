import test from 'node:test';
import assert from 'node:assert/strict';
import { getTransactionDecoder, getCompiledTransactionMessageDecoder, getBase58Decoder } from '@solana/kit';
import { SolanaChain } from '../src/chain.mjs';
import { TOKEN_PROGRAM, USDC } from '../src/config.mjs';
import { challenge, jsonResponse, testWallet } from './helpers.mjs';

test('real SDK creates a valid partial payment with the exact USDC transfer and memo; no broadcast occurs', async t => {
  const wallet = await testWallet();
  const originalFetch = globalThis.fetch; t.after(() => { globalThis.fetch = originalFetch; });
  const methods = [];
  const mint = Buffer.alloc(82); mint[44] = 6; mint[45] = 1;
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), 'https://rpc.invalid/');
    const request = JSON.parse(init.body); methods.push(request.method);
    let result;
    if (request.method === 'getAccountInfo') result = { context: { slot: 1 }, value: { data: [mint.toString('base64'), 'base64'], owner: TOKEN_PROGRAM, executable: false, lamports: 1000000, rentEpoch: 0, space: 82 } };
    else if (request.method === 'getLatestBlockhash') result = { context: { slot: 1 }, value: { blockhash: '11111111111111111111111111111111', lastValidBlockHeight: 100 } };
    else throw new Error(`Forbidden RPC method: ${request.method}`);
    return jsonResponse({ jsonrpc: '2.0', id: request.id, result });
  };
  const c = challenge(); c.requirement.extra.memo = 'approved-order';
  const chain = new SolanaChain('https://rpc.invalid/');
  const built = await chain.build(c.requirement, c.resource, wallet);
  const tx = getTransactionDecoder().decode(Buffer.from(built.payload.payload.transaction, 'base64'));
  assert.equal(tx.signatures[c.requirement.extra.feePayer], null);
  assert.equal(await crypto.subtle.verify('Ed25519', wallet.signer.keyPair.publicKey, tx.signatures[wallet.address], tx.messageBytes), true);
  const message = getCompiledTransactionMessageDecoder().decode(tx.messageBytes);
  const transfer = message.instructions.find(i => message.staticAccounts[i.programAddressIndex] === TOKEN_PROGRAM);
  const bytes = Buffer.from(transfer.data);
  assert.equal(bytes[0], 12); // SPL TransferChecked
  assert.equal(bytes.readBigUInt64LE(1), 5000000n);
  assert.equal(bytes[9], 6);
  assert.ok(message.staticAccounts.includes(USDC));
  assert.equal(Buffer.from(message.instructions.at(-1).data).toString(), 'approved-order');
  assert.deepEqual(methods.sort(), ['getAccountInfo', 'getLatestBlockhash'].sort());

  const signature = getBase58Decoder().decode(new Uint8Array(64).fill(9));
  chain.rpc = { getTransaction: () => ({ send: async () => ({ transaction: [built.payload.payload.transaction, 'base64'], meta: { err: null }, slot: 123n }) }) };
  assert.equal((await chain.verify(signature, built.messageHash)).state, 'confirmed');
  await assert.rejects(chain.verify(signature, 'wrong-message'), { code: 'TRANSACTION_MISMATCH' });
});
