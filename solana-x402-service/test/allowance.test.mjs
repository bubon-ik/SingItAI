import test from 'node:test';
import assert from 'node:assert/strict';
import { getBase58Encoder, getTransactionDecoder, partiallySignTransaction, getCompiledTransactionMessageDecoder } from '@solana/kit';
import { TokenAllowance, approveInstruction, parseTokenAccount, usdcAccount } from '../src/allowance.mjs';
import { dispatch } from '../src/gateway.mjs';
import { USDC, TOKEN_PROGRAM, ATA_PROGRAM } from '../src/config.mjs';
import { testWallet } from './helpers.mjs';

const BLOCKHASH = '11111111111111111111111111111111';

function tokenAccount(owner, { amount = 20_000_000n, delegate = null, delegated = 0n } = {}) {
  const data = Buffer.alloc(165);
  const enc = getBase58Encoder();
  Buffer.from(enc.encode(USDC)).copy(data, 0);
  Buffer.from(enc.encode(owner)).copy(data, 32);
  data.writeBigUInt64LE(amount, 64);
  if (delegate) { data.writeUInt32LE(1, 72); Buffer.from(enc.encode(delegate)).copy(data, 76); data.writeBigUInt64LE(delegated, 121); }
  data[108] = 1;
  return data;
}

function fakeRpc({ account = null, status = 'confirmed' } = {}) {
  const sent = [];
  const call = value => ({ send: async () => value });
  return {
    sent,
    getAccountInfo: () => call({ value: account && { owner: TOKEN_PROGRAM, data: [account.toString('base64'), 'base64'] } }),
    getLatestBlockhash: () => call({ value: { blockhash: BLOCKHASH, lastValidBlockHeight: 100n } }),
    sendTransaction: wire => { sent.push(wire); return call('sig'); },
    getSignatureStatuses: () => call({ value: [status && { confirmationStatus: status, err: null }] }),
    getBalance: () => call({ value: 0n }),
  };
}

function lane(rpc) {
  return new TokenAllowance({ chain: { rpc }, sleep: async () => {}, confirmTimeoutMs: 10 });
}

function instructionsOf(wire) {
  const tx = getTransactionDecoder().decode(Buffer.from(wire, 'base64'));
  return { tx, message: getCompiledTransactionMessageDecoder().decode(tx.messageBytes) };
}

test('approve is ApproveChecked for the owner USDC account, in USDC decimals', async () => {
  const [owner, agent] = [await testWallet(), await testWallet()];
  const ix = approveInstruction({ source: await usdcAccount(owner.address), owner: owner.address, delegate: agent.address, amount: 20_000_000n });
  assert.equal(ix.programAddress, TOKEN_PROGRAM);
  assert.deepEqual([...ix.data], [13, 0x00, 0x2d, 0x31, 0x01, 0, 0, 0, 0, 6]);
  assert.deepEqual(ix.accounts.map(a => a.address), [await usdcAccount(owner.address), USDC, agent.address, owner.address]);
});

test('the token account tells what is delegated to whom', async () => {
  const [owner, agent] = [await testWallet(), await testWallet()];
  const parsed = parseTokenAccount(tokenAccount(owner.address, { delegate: agent.address, delegated: 7_000_000n }), owner.address);
  assert.deepEqual(parsed, { amount: '20000000', delegate: agent.address, delegatedAmount: '7000000', frozen: false });
  assert.throws(() => parseTokenAccount(tokenAccount(agent.address), owner.address), /did not match/);
});

test('the owner signs the prepared approval, then our fee payer signs and it is sent', async () => {
  const [owner, agent, payer] = [await testWallet(), await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const prepared = await lane(rpc).prepare({ kind: 'approve', owner: owner.address, delegate: agent.address, amount: '20000000', feePayer: payer.address });
  const { tx, message } = instructionsOf(prepared.transaction);
  assert.deepEqual(Object.keys(tx.signatures), [payer.address, owner.address]);  // fee payer first; nobody signed yet
  assert.equal(message.staticAccounts[0], payer.address);

  const signedByOwner = await partiallySignTransaction([owner.signer.keyPair], tx);
  const wire = Buffer.from((await import('@solana/kit')).getBase64EncodedWireTransaction(signedByOwner)).toString();
  const result = await lane(rpc).submit({ transaction: wire, expectedHash: prepared.messageHash, owner: owner.address, feePayer: payer });
  assert.equal(result.state, 'confirmed');
  const sent = getTransactionDecoder().decode(Buffer.from(rpc.sent[0], 'base64'));
  assert.ok(sent.signatures[payer.address] && sent.signatures[owner.address]);
});

test('a different or unsigned transaction is never sent', async () => {
  const [owner, agent, payer] = [await testWallet(), await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const prepared = await lane(rpc).prepare({ kind: 'approve', owner: owner.address, delegate: agent.address, amount: '20000000', feePayer: payer.address });
  const other = await lane(rpc).prepare({ kind: 'approve', owner: owner.address, delegate: agent.address, amount: '99000000', feePayer: payer.address });
  await assert.rejects(lane(rpc).submit({ transaction: other.transaction, expectedHash: prepared.messageHash, owner: owner.address, feePayer: payer }), /other than what was prepared/);
  await assert.rejects(lane(rpc).submit({ transaction: prepared.transaction, expectedHash: prepared.messageHash, owner: owner.address, feePayer: payer }), /did not sign/);
  assert.equal(rpc.sent.length, 0);
});

test('the agent pulls into its own account, which the fee payer creates if needed', async () => {
  const [owner, agent, payer] = [await testWallet(), await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const result = await lane(rpc).pull({ owner: owner.address, amount: '5000000', agent, feePayer: payer });
  assert.equal(result.state, 'confirmed');
  const { tx, message } = instructionsOf(rpc.sent[0]);
  assert.ok(tx.signatures[agent.address] && tx.signatures[payer.address]);
  const programs = message.instructions.map(i => message.staticAccounts[i.programAddressIndex]);
  assert.deepEqual(programs, [ATA_PROGRAM, TOKEN_PROGRAM]);
  assert.equal(message.instructions[1].data[0], 12);  // TransferChecked
});

test('the bridge runs allowance operations only for the agent it was given', async () => {
  const [owner, agent, payer] = [await testWallet(), await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address, { delegate: agent.address, delegated: 3_000_000n }) });
  const chain = { rpc, balances: async () => ({ usdcAtomic: '0', solLamports: '0' }) };
  const state = await dispatch({ operation: 'allowance-state', payer: agent.address, owner: owner.address }, { wallet: agent, chain, feePayer: payer, allowance: lane(rpc) });
  assert.equal(state.owner.delegatedToAgent, '3000000');
  await assert.rejects(dispatch({ operation: 'allowance-state', payer: owner.address, owner: owner.address }, { wallet: agent, chain }), /mismatch/);
  await assert.rejects(dispatch({ operation: 'allowance-pull', payer: agent.address, owner: owner.address, amount: '1' }, { wallet: agent, chain, allowance: lane(rpc) }), /fee payer/);
});

test('the owner may pay their own approve’s fee: then theirs is the only signature', async () => {
  const [owner, agent] = [await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const prepared = await lane(rpc).prepare({ kind: 'approve', owner: owner.address, delegate: agent.address, amount: '20000000', feePayer: owner.address });
  const { tx, message } = instructionsOf(prepared.transaction);
  assert.deepEqual(Object.keys(tx.signatures), [owner.address]);
  assert.equal(message.staticAccounts[0], owner.address);
  const signed = await partiallySignTransaction([owner.signer.keyPair], tx);
  const wire = (await import('@solana/kit')).getBase64EncodedWireTransaction(signed);
  const result = await lane(rpc).submit({ transaction: wire, expectedHash: prepared.messageHash, owner: owner.address, feePayer: null });
  assert.equal(result.state, 'confirmed');
  assert.equal(rpc.sent.length, 1);
});

// What Phantom does before signing: its compute budget and a Lighthouse guard around our instruction.
async function asPhantomWould(prepared, owner, extra = []) {
  const kit = await import('@solana/kit');
  const { getSetComputeUnitLimitInstruction, getSetComputeUnitPriceInstruction } = await import('@solana-program/compute-budget');
  const tx = getTransactionDecoder().decode(Buffer.from(prepared.transaction, 'base64'));
  let message = kit.decompileTransactionMessage(getCompiledTransactionMessageDecoder().decode(tx.messageBytes));
  message = kit.prependTransactionMessageInstructions([
    getSetComputeUnitLimitInstruction({ units: 60_000 }), getSetComputeUnitPriceInstruction({ microLamports: 50_000n })], message);
  message = kit.appendTransactionMessageInstructions([
    { programAddress: 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95', accounts: [], data: new Uint8Array([1, 2, 3]) }, ...extra], message);
  const signed = await partiallySignTransaction([owner.signer.keyPair], kit.compileTransaction(message));
  return kit.getBase64EncodedWireTransaction(signed);
}

test('a wallet’s own additions are accepted when the approval is exactly the prepared one', async () => {
  const [owner, agent] = [await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const args = { kind: 'approve', owner: owner.address, delegate: agent.address, amount: '20000000', feePayer: owner.address };
  const prepared = await lane(rpc).prepare(args);
  const wire = await asPhantomWould(prepared, owner);
  const result = await lane(rpc).submit({ transaction: wire, expectedHash: prepared.messageHash, owner: owner.address, feePayer: null,
    kind: 'approve', delegate: agent.address, amount: '20000000' });
  assert.equal(result.state, 'confirmed');
});

test('an addition that moves value, or another amount, is never sent', async () => {
  const [owner, agent, thief] = [await testWallet(), await testWallet(), await testWallet()];
  const rpc = fakeRpc({ account: tokenAccount(owner.address) });
  const prepared = await lane(rpc).prepare({ kind: 'approve', owner: owner.address, delegate: agent.address, amount: '20000000', feePayer: owner.address });
  const drain = { programAddress: '11111111111111111111111111111111', accounts: [
    { address: owner.address, role: 3 }, { address: thief.address, role: 1 }], data: new Uint8Array([2, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]) };
  const attempts = [
    [await asPhantomWould(prepared, owner, [drain]), '20000000'],     // plus a SOL transfer
    [await asPhantomWould(prepared, owner), '99000000'],              // checked against another amount
  ];
  for (const [wire, amount] of attempts) {
    await assert.rejects(lane(rpc).submit({ transaction: wire, expectedHash: prepared.messageHash, owner: owner.address, feePayer: null,
      kind: 'approve', delegate: agent.address, amount }), /other than what was prepared/);
  }
  assert.equal(rpc.sent.length, 0);
});
