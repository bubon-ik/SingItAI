import test from 'node:test';
import assert from 'node:assert/strict';
import { ExactSvmScheme as Facilitator } from '@x402/svm/exact/facilitator';
import { getTransactionDecoder, getCompiledTransactionMessageDecoder } from '@solana/kit';
import { SolanaChain } from '../src/chain.mjs';
import { usdcAccount } from '../src/allowance.mjs';
import { NETWORK, USDC } from '../src/config.mjs';
import { testWallet } from './helpers.mjs';

const BLOCKHASH = 'EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N';

async function setup() {
  const [owner, agent, merchant, feePayer] = [await testWallet(), await testWallet(), await testWallet(), await testWallet()];
  const chain = new SolanaChain('https://example.invalid');
  chain.rpc = { getLatestBlockhash: () => ({ send: async () => ({ value: { blockhash: BLOCKHASH, lastValidBlockHeight: 10n } }) }) };
  const requirement = { scheme: 'exact', network: NETWORK, asset: USDC, amount: '5000000', payTo: merchant.address,
    maxTimeoutSeconds: 300, extra: { feePayer: feePayer.address } };
  const facilitator = new Facilitator({ getAddresses: () => [feePayer.address], signTransaction: async tx => tx, simulateTransaction: async () => {} });
  return { owner, agent, merchant, feePayer, chain, requirement, facilitator };
}

test('x402’s own facilitator accepts a payment from the owner’s account signed by the delegate', async () => {
  const { owner, agent, chain, requirement, facilitator } = await setup();
  const built = await chain.buildDelegated(requirement, { url: 'https://api.venice.ai/api/v1/x402/top-up' }, agent, owner.address);
  const verdict = await facilitator.verify(built.payload, requirement);
  assert.equal(verdict.isValid, true, verdict.invalidReason);
  assert.equal(verdict.payer, agent.address);

  const tx = getTransactionDecoder().decode(Buffer.from(built.payload.payload.transaction, 'base64'));
  const message = getCompiledTransactionMessageDecoder().decode(tx.messageBytes);
  assert.equal(message.staticAccounts[0], requirement.extra.feePayer);  // the merchant pays the fee
  assert.ok(tx.signatures[agent.address]);
  assert.ok(!(owner.address in tx.signatures));                          // the owner signs nothing here
  const transfer = message.instructions[2];
  assert.equal(message.staticAccounts[transfer.accountIndices[0]], await usdcAccount(owner.address));  // from the owner's USDC
});

test('the facilitator refuses what was not asked for', async () => {
  const { owner, agent, chain, requirement, facilitator } = await setup();
  const built = await chain.buildDelegated(requirement, {}, agent, owner.address);
  assert.equal((await facilitator.verify(built.payload, { ...requirement, amount: '4000000' })).isValid, false);
  const other = await testWallet();
  assert.equal((await facilitator.verify(built.payload, { ...requirement, payTo: other.address })).isValid, false);
  await assert.rejects(chain.buildDelegated({ ...requirement, extra: { feePayer: owner.address } }, {}, agent, owner.address), /merchant-sponsored/);
});
