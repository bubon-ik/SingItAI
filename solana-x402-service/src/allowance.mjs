// An SPL Token allowance on the owner's own USDC account: the Solana side of SingIt's
// agent allowance. The owner approves the agent as delegate once, for a total, from
// their wallet. The agent then pulls exactly what a purchase needs into its own USDC
// account and pays the merchant from there (x402 needs the payer to own the account).
// Our fee payer covers network fees, so neither the owner nor the agent needs SOL.
import {
  AccountRole, appendTransactionMessageInstructions, compileTransaction, createTransactionMessage,
  decompileTransactionMessage, getCompiledTransactionMessageDecoder,
  getBase58Decoder, getBase58Encoder, getBase64EncodedWireTransaction, getProgramDerivedAddress,
  getTransactionDecoder, getSignatureFromTransaction, getPublicKeyFromAddress, isAddress,
  partiallySignTransaction, pipe, setTransactionMessageFeePayer, setTransactionMessageLifetimeUsingBlockhash,
  verifySignature,
} from '@solana/kit';
import { ClientError, USDC, TOKEN_PROGRAM, ATA_PROGRAM } from './config.mjs';
import { messageHash } from './chain.mjs';

const SYSTEM_PROGRAM = '11111111111111111111111111111111';
// What a wallet may add to a transaction it signs: fee settings, its own safety checks, a memo.
const COMPUTE_BUDGET = 'ComputeBudget111111111111111111111111111111';
const LIGHTHOUSE = 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95';  // Phantom's guard; x402 allows it too
const MEMO = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr';
const MAX_UNITS = 400_000;
const MAX_MICROLAMPORTS_PER_UNIT = 2_000_000n;  // with MAX_UNITS: at most 0.0008 SOL of priority fee
const DECIMALS = 6;
const MAX_ALLOWANCE = 10_000_000_000n; // 10,000 USDC: a sanity ceiling, not a policy

function u64(value) {
  const amount = BigInt(value);
  if (amount < 0n || amount > MAX_ALLOWANCE) throw new ClientError('INVALID_AMOUNT', 'Amount out of range.');
  const bytes = Buffer.alloc(8);
  bytes.writeBigUInt64LE(amount);
  return [...bytes];
}

function address(value, what) {
  if (typeof value !== 'string' || !isAddress(value)) throw new ClientError('INVALID_ADDRESS', `Invalid ${what} address.`);
  return value;
}

export async function usdcAccount(owner) {
  const enc = getBase58Encoder();
  const [ata] = await getProgramDerivedAddress({
    programAddress: ATA_PROGRAM, seeds: [enc.encode(owner), enc.encode(TOKEN_PROGRAM), enc.encode(USDC)],
  });
  return ata;
}

// SPL Token instructions, encoded by hand: ApproveChecked (13), Revoke (5), TransferChecked (12).
export function approveInstruction({ source, owner, delegate, amount }) {
  return {
    programAddress: TOKEN_PROGRAM,
    accounts: [
      { address: source, role: AccountRole.WRITABLE }, { address: USDC, role: AccountRole.READONLY },
      { address: delegate, role: AccountRole.READONLY }, { address: owner, role: AccountRole.READONLY_SIGNER },
    ],
    data: new Uint8Array([13, ...u64(amount), DECIMALS]),
  };
}

export function revokeInstruction({ source, owner }) {
  return {
    programAddress: TOKEN_PROGRAM,
    accounts: [{ address: source, role: AccountRole.WRITABLE }, { address: owner, role: AccountRole.READONLY_SIGNER }],
    data: new Uint8Array([5]),
  };
}

export function transferInstruction({ source, destination, authority, amount }) {
  return {
    programAddress: TOKEN_PROGRAM,
    accounts: [
      { address: source, role: AccountRole.WRITABLE }, { address: USDC, role: AccountRole.READONLY },
      { address: destination, role: AccountRole.WRITABLE }, { address: authority, role: AccountRole.READONLY_SIGNER },
    ],
    data: new Uint8Array([12, ...u64(amount), DECIMALS]),
  };
}

// Explicit wallet-signed SOL transfer to this account's own agent.
export function gasTransferInstruction({ owner, agent, amount }) {
  return { programAddress: SYSTEM_PROGRAM,
    accounts: [{ address: owner, role: AccountRole.WRITABLE_SIGNER },
      { address: address(agent, 'agent'), role: AccountRole.WRITABLE }],
    data: new Uint8Array([2, 0, 0, 0, ...u64(amount)]) };
}

// Associated Token Account program, CreateIdempotent (1): the agent's USDC account, paid by the fee payer.
export function createAccountInstruction({ payer, account, owner }) {
  return {
    programAddress: ATA_PROGRAM,
    accounts: [
      { address: payer, role: AccountRole.WRITABLE_SIGNER }, { address: account, role: AccountRole.WRITABLE },
      { address: owner, role: AccountRole.READONLY }, { address: USDC, role: AccountRole.READONLY },
      { address: SYSTEM_PROGRAM, role: AccountRole.READONLY }, { address: TOKEN_PROGRAM, role: AccountRole.READONLY },
    ],
    data: new Uint8Array([1]),
  };
}

// The SPL token account layout: mint 0..32, owner 32..64, amount 64..72, delegate COption 72..108,
// state 108, is_native COption 109..121, delegated_amount 121..129.
export function parseTokenAccount(data, owner) {
  const b58 = getBase58Decoder();
  if (data.length < 165 || b58.decode(data.subarray(0, 32)) !== USDC || b58.decode(data.subarray(32, 64)) !== owner) {
    throw new ClientError('INVALID_TOKEN_ACCOUNT', 'The USDC token account did not match the expected mint and owner.');
  }
  const hasDelegate = data.readUInt32LE(72) === 1;
  return {
    amount: data.readBigUInt64LE(64).toString(),
    delegate: hasDelegate ? b58.decode(data.subarray(76, 108)) : null,
    delegatedAmount: hasDelegate ? data.readBigUInt64LE(121).toString() : '0',
    frozen: data[108] === 2,
  };
}

// Before paying from the owner's account: it holds the amount and the agent is approved for it.
export async function assertDelegated(chain, owner, agent, amount) {
  const state = await new TokenAllowance({ chain }).state(owner, agent);
  if (BigInt(state.delegatedToAgent) < BigInt(amount)) throw new ClientError('ALLOWANCE_TOO_LOW', 'The approval in your wallet does not cover this payment. Nothing was paid.');
  if (BigInt(state.amount) < BigInt(amount)) throw new ClientError('INSUFFICIENT_USDC', 'Your wallet does not hold enough USDC for this payment. Nothing was paid.');
  return state;
}

// Wallets such as Phantom add fee settings and their own guard instructions before signing, so the
// signed message is not byte for byte the prepared one. It is accepted when it does exactly the
// prepared approve or revoke, once, with the expected fee payer, and nothing else but those additions.
export function doesOnly(messageBytes, expected, feePayer) {
  let message;
  try { message = decompileTransactionMessage(getCompiledTransactionMessageDecoder().decode(messageBytes)); }
  catch { return false; }  // e.g. address lookup tables: not something a wallet needs to add here
  if (message.feePayer?.address !== feePayer) return false;
  let ours = 0;
  for (const ix of message.instructions) {
    const data = ix.data ? Buffer.from(ix.data) : Buffer.alloc(0);
    if (ix.programAddress === expected.programAddress) {
      const accounts = (ix.accounts || []).map(a => a.address);
      if (!data.equals(Buffer.from(expected.data)) || accounts.join() !== expected.accounts.map(a => a.address).join()) return false;
      ours += 1;
    } else if (ix.programAddress === COMPUTE_BUDGET) {
      if (data[0] === 2 && data.length >= 5) { if (data.readUInt32LE(1) > MAX_UNITS) return false; }
      else if (data[0] === 3 && data.length >= 9) { if (data.readBigUInt64LE(1) > MAX_MICROLAMPORTS_PER_UNIT) return false; }
      else return false;
    } else if (ix.programAddress !== LIGHTHOUSE && ix.programAddress !== MEMO) {
      return false;
    }
  }
  return ours === 1;
}

export class TokenAllowance {
  constructor({ chain, sleep = ms => new Promise(r => setTimeout(r, ms)), confirmTimeoutMs = 60000 }) {
    Object.assign(this, { chain, rpc: chain.rpc, sleep, confirmTimeoutMs });
  }

  async state(owner, delegate) {
    address(owner, 'owner');
    const account = await usdcAccount(owner);
    let info;
    const read = () => this.rpc.getAccountInfo(account, { encoding: 'base64', commitment: 'confirmed' }).send({ abortSignal: AbortSignal.timeout(20000) });
    try { info = await read(); }
    catch {  // a read only: the public RPC refuses bursts, and a second look a moment later is harmless
      await this.sleep(1200);
      try { info = await read(); } catch { throw new ClientError('RPC_UNAVAILABLE', 'Could not read the Solana USDC account.'); }
    }
    if (!info.value) return { account, exists: false, amount: '0', delegate: null, delegatedAmount: '0', delegatedToAgent: '0' };
    if (info.value.owner !== TOKEN_PROGRAM) throw new ClientError('INVALID_TOKEN_ACCOUNT', 'The USDC account is not a token account.');
    const parsed = parseTokenAccount(Buffer.from(info.value.data[0], 'base64'), owner);
    return { account, exists: true, ...parsed, delegatedToAgent: parsed.delegate === delegate ? parsed.delegatedAmount : '0' };
  }

  async compile(feePayer, instructions) {
    let latest;
    try { latest = (await this.rpc.getLatestBlockhash({ commitment: 'confirmed' }).send({ abortSignal: AbortSignal.timeout(20000) })).value; }
    catch { throw new ClientError('RPC_UNAVAILABLE', 'Could not reach Solana to prepare the transaction.'); }
    const message = pipe(
      createTransactionMessage({ version: 0 }),
      m => setTransactionMessageFeePayer(feePayer, m),
      m => setTransactionMessageLifetimeUsingBlockhash(latest, m),
      m => appendTransactionMessageInstructions(instructions, m),
    );
    return compileTransaction(message);
  }

  // What the owner's wallet signs: approve the agent for a total, or revoke it. Our fee payer
  // signs after the owner, once the signed transaction is checked against this exact message.
  async prepare({ kind, owner, delegate, amount, feePayer }) {
    address(owner, 'owner'); address(feePayer, 'fee payer');
    const source = await usdcAccount(owner);
    const state = kind === 'fund-gas' ? { exists: true } : await this.state(owner, delegate);
    if (!state.exists) throw new ClientError('NO_USDC_ACCOUNT', 'This wallet has no USDC account on Solana yet. Add some USDC first.');
    const instruction = kind === 'fund-gas'
      ? gasTransferInstruction({owner, agent: delegate, amount}) : kind === 'approve'
      ? approveInstruction({ source, owner, delegate: address(delegate, 'agent'), amount })
      : kind === 'revoke' ? revokeInstruction({ source, owner }) : null;
    if (!instruction) throw new ClientError('INVALID_OPERATION', 'Approve or revoke only.');
    const tx = await this.compile(feePayer, [instruction]);
    return { transaction: getBase64EncodedWireTransaction(tx), messageHash: messageHash(tx.messageBytes) };
  }

  async submit({ transaction, expectedHash, owner, feePayer, kind = null, delegate = null, amount = '0' }) {
    let tx;
    try { tx = getTransactionDecoder().decode(Buffer.from(String(transaction), 'base64')); }
    catch { throw new ClientError('INVALID_TRANSACTION', 'The signed transaction could not be read.'); }
    if (messageHash(tx.messageBytes) !== expectedHash) {
      const source = await usdcAccount(owner);
      const expected = kind === 'fund-gas' ? gasTransferInstruction({owner, agent: delegate, amount}) : kind === 'approve' ? approveInstruction({ source, owner, delegate: address(delegate, 'agent'), amount })
        : kind === 'revoke' ? revokeInstruction({ source, owner }) : null;
      if (!expected || !doesOnly(tx.messageBytes, expected, feePayer ? feePayer.address : owner)) {
        throw new ClientError('TRANSACTION_MISMATCH', 'The wallet signed something other than what was prepared. Nothing was sent.');
      }
    }
    const signature = tx.signatures[owner];
    if (!signature || !(await verifySignature(await getPublicKeyFromAddress(owner), signature, tx.messageBytes))) {
      throw new ClientError('SIGNATURE_REQUIRED', 'The wallet did not sign the transaction.');
    }
    // The owner may be their own fee payer; then theirs is the only signature.
    const signed = feePayer ? await partiallySignTransaction([feePayer.signer.keyPair], tx) : tx;
    return this.send(signed);
  }

  async fundingTransaction({owner, amount, agent, feePayer}) {
    const agentAccount = await usdcAccount(agent.address);
    return this.compile(feePayer.address, [
      createAccountInstruction({payer: feePayer.address, account: agentAccount, owner: agent.address}),
      transferInstruction({source: await usdcAccount(owner), destination: agentAccount, authority: agent.address, amount}),
    ]);
  }

  async fundingCheck({owner, amount, agent, feePayer = agent}, tx = null) {
    tx ??= await this.fundingTransaction({owner, amount, agent, feePayer});
    const options = {abortSignal: AbortSignal.timeout(20000)};
    const balance = (await this.rpc.getBalance(feePayer.address, {commitment: 'confirmed'}).send(options)).value;
    const account = (await this.rpc.getAccountInfo(await usdcAccount(agent.address), {encoding: 'base64', commitment: 'confirmed'}).send(options)).value;
    const rent = account ? 0n : await this.rpc.getMinimumBalanceForRentExemption(165).send(options);
    const fee = (await this.rpc.getFeeForMessage(Buffer.from(tx.messageBytes).toString('base64'), {commitment: 'confirmed'}).send(options)).value;
    if (fee === null) throw new ClientError('RPC_UNAVAILABLE', 'Could not price the network fee.');
    const required = BigInt(rent) + BigInt(fee);
    return {ready: BigInt(balance) >= required, payer: feePayer.address,
      balanceLamports: String(balance), requiredLamports: String(required),
      networkFeeLamports: String(fee), accountRentLamports: String(rent)};
  }

  // The user's own agent pays funding gas and its token-account rent from its SOL.
  async pull({ owner, amount, agent, feePayer = agent }) {
    address(owner, 'owner');
    const tx = await this.fundingTransaction({owner, amount, agent, feePayer});
    const funding = await this.fundingCheck({owner, amount, agent, feePayer}, tx);
    if (!funding.ready) return {state: 'not_submitted', reason: 'agent_sol_required', ...funding};
    const signers = agent.address === feePayer.address ? [agent.signer.keyPair] : [agent.signer.keyPair, feePayer.signer.keyPair];
    const signed = await partiallySignTransaction(signers, tx);
    return this.send(signed);
  }

  async send(signed) {
    const id = getSignatureFromTransaction(signed);
    let refused = false;
    try {
      await this.rpc.sendTransaction(getBase64EncodedWireTransaction(signed), { encoding: 'base64', preflightCommitment: 'confirmed' })
        .send({ abortSignal: AbortSignal.timeout(20000) });
    } catch {
      // Refused in preflight (nothing sent), or the answer was lost: the status below decides.
      refused = true;
    }
    const deadline = Date.now() + (refused ? 15000 : this.confirmTimeoutMs);
    while (Date.now() < deadline) {
      let status;
      try { status = (await this.rpc.getSignatureStatuses([id]).send({ abortSignal: AbortSignal.timeout(20000) })).value[0]; }
      catch { status = undefined; }
      if (status?.err) return { transaction: id, state: 'failed' };
      if (status && ['confirmed', 'finalized'].includes(status.confirmationStatus)) return { transaction: id, state: 'confirmed' };
      await this.sleep(1500);
    }
    return { transaction: id, state: 'uncertain' };
  }
}
