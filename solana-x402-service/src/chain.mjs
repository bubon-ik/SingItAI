import { createHash } from 'node:crypto';
import {
  appendTransactionMessageInstructions, createSolanaRpc, createTransactionMessage, getBase58Encoder, getBase58Decoder,
  getBase64EncodedWireTransaction, getProgramDerivedAddress, getTransactionDecoder, partiallySignTransactionMessageWithSigners,
  pipe, prependTransactionMessageInstruction, setTransactionMessageFeePayer, setTransactionMessageLifetimeUsingBlockhash,
} from '@solana/kit';
import { ExactSvmScheme } from '@x402/svm/exact/client';
import { DEFAULT_COMPUTE_UNIT_LIMIT, DEFAULT_COMPUTE_UNIT_PRICE_MICROLAMPORTS, MAX_MEMO_BYTES, MEMO_PROGRAM_ADDRESS } from '@x402/svm';
// Installed with @x402/svm, whose client builds its payments from them; used here the same way.
import { getSetComputeUnitLimitInstruction, setTransactionMessageComputeUnitPrice } from '@solana-program/compute-budget';
import { getTransferCheckedInstruction } from '@solana-program/token-2022';
import { ClientError, USDC, TOKEN_PROGRAM, ATA_PROGRAM, DEFAULT_RPC } from './config.mjs';

export function messageHash(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
export function isTransactionId(value) {
  try { return typeof value === 'string' && getBase58Encoder().encode(value).length === 64; } catch { return false; }
}

export class SolanaChain {
  constructor(rpcUrl = DEFAULT_RPC) { this.rpcUrl = rpcUrl; this.rpc = createSolanaRpc(rpcUrl); }
  async balances(owner) {
    const enc = getBase58Encoder();
    const [ata] = await getProgramDerivedAddress({ programAddress: ATA_PROGRAM, seeds: [enc.encode(owner), enc.encode(TOKEN_PROGRAM), enc.encode(USDC)] });
    let native, token;
    try {
      [native, token] = await Promise.all([
        this.rpc.getBalance(owner, { commitment: 'confirmed' }).send({ abortSignal: AbortSignal.timeout(20000) }),
        this.rpc.getAccountInfo(ata, { encoding: 'base64', commitment: 'confirmed' }).send({ abortSignal: AbortSignal.timeout(20000) }),
      ]);
    } catch { throw new ClientError('RPC_UNAVAILABLE', 'Could not read Solana balances. Check the mainnet RPC connection.'); }
    let usdc = 0n;
    if (token.value) {
      const data = Buffer.from(token.value.data[0], 'base64');
      if (token.value.owner !== TOKEN_PROGRAM || data.length < 165 || getBase58Decoder().decode(data.subarray(0, 32)) !== USDC || getBase58Decoder().decode(data.subarray(32, 64)) !== owner) throw new ClientError('INVALID_TOKEN_ACCOUNT', 'The USDC token account did not match the expected mint and owner.');
      if (data[108] !== 1) throw new ClientError('TOKEN_ACCOUNT_UNAVAILABLE', 'The USDC token account is not initialized or is frozen.');
      usdc = data.readBigUInt64LE(64);
    }
    return { solLamports: native.value.toString(), usdcAtomic: usdc.toString(), usdcAccount: ata };
  }
  // The x402 "exact" payment, built exactly as @x402/svm's client builds it, except that the USDC
  // comes from the owner's account, the agent signing as its approved delegate. The facilitator
  // checks the signer, mint, recipient account and amount, then simulates; the token program
  // honours the delegate within what the owner approved. The merchant still pays the fee.
  async buildDelegated(requirement, resource, wallet, owner) {
    if (requirement.extra?.feePayer === wallet.address || requirement.extra?.feePayer === owner) throw new ClientError('UNEXPECTED_FEE_PAYER', 'This client expects a merchant-sponsored payment.');
    if (requirement.asset !== USDC) throw new ClientError('UNSUPPORTED_PAYMENT', 'Only USDC is paid from an allowance.');
    const enc = getBase58Encoder();
    const ata = async who => (await getProgramDerivedAddress({ programAddress: ATA_PROGRAM, seeds: [enc.encode(who), enc.encode(TOKEN_PROGRAM), enc.encode(USDC)] }))[0];
    const memoBytes = requirement.extra?.memo
      ? new TextEncoder().encode(requirement.extra.memo)
      : new TextEncoder().encode(Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString('hex'));
    if (memoBytes.byteLength > MAX_MEMO_BYTES) throw new ClientError('INVALID_MEMO', 'Invalid payment memo.');
    let latest;
    try { latest = (await this.rpc.getLatestBlockhash().send({ abortSignal: AbortSignal.timeout(20000) })).value; }
    catch { throw new ClientError('RPC_UNAVAILABLE', 'Could not reach Solana to build the payment.'); }
    const transfer = getTransferCheckedInstruction({
      source: await ata(owner), mint: USDC, destination: await ata(requirement.payTo), authority: wallet.signer,
      amount: BigInt(requirement.amount), decimals: 6,
    }, { programAddress: TOKEN_PROGRAM });
    const message = pipe(
      createTransactionMessage({ version: 0 }),
      m => setTransactionMessageComputeUnitPrice(DEFAULT_COMPUTE_UNIT_PRICE_MICROLAMPORTS, m),
      m => setTransactionMessageFeePayer(requirement.extra.feePayer, m),
      m => prependTransactionMessageInstruction(getSetComputeUnitLimitInstruction({ units: DEFAULT_COMPUTE_UNIT_LIMIT }), m),
      m => appendTransactionMessageInstructions([transfer, { programAddress: MEMO_PROGRAM_ADDRESS, accounts: [], data: memoBytes }], m),
      m => setTransactionMessageLifetimeUsingBlockhash(latest, m),
    );
    let signed;
    try { signed = await partiallySignTransactionMessageWithSigners(message); }
    catch { throw new ClientError('BUILD_FAILED', 'Could not sign the Solana payment. Nothing was submitted.'); }
    const tx = getTransactionDecoder().decode(Buffer.from(getBase64EncodedWireTransaction(signed), 'base64'));
    if (!tx.signatures[wallet.address] || tx.signatures[requirement.extra.feePayer] !== null) throw new ClientError('INVALID_SIGNATURE_LAYOUT', 'Unexpected payment signature layout.');
    const partial = { x402Version: 2, payload: { transaction: getBase64EncodedWireTransaction(signed) } };
    // The blockhash lifetime proves later whether a lost submission can still land.
    return { payload: { ...partial, accepted: requirement, resource }, messageHash: messageHash(tx.messageBytes),
      lastValidBlockHeight: String(latest.lastValidBlockHeight) };
  }

  async build(requirement, resource, wallet) {
    if (requirement.extra.feePayer === wallet.address) throw new ClientError('UNEXPECTED_FEE_PAYER', 'This client expects a merchant-sponsored payment.');
    let partial;
    try { partial = await new ExactSvmScheme(wallet.signer, { rpcUrl: this.rpcUrl }).createPaymentPayload(2, requirement); }
    catch { throw new ClientError('BUILD_FAILED', 'Could not build the Solana payment. Nothing was submitted.'); }
    const tx = getTransactionDecoder().decode(Buffer.from(partial.payload.transaction, 'base64'));
    if (!tx.signatures[wallet.address] || tx.signatures[requirement.extra.feePayer] !== null) throw new ClientError('INVALID_SIGNATURE_LAYOUT', 'Unexpected payment signature layout.');
    return { payload: { ...partial, accepted: requirement, resource }, messageHash: messageHash(tx.messageBytes) };
  }
  async verify(transaction, expectedMessageHash) {
    if (!isTransactionId(transaction)) throw new ClientError('INVALID_TRANSACTION', 'A valid Solana transaction signature is required.');
    let result;
    try { result = await this.rpc.getTransaction(transaction, { encoding: 'base64', commitment: 'confirmed', maxSupportedTransactionVersion: 0 }).send({ abortSignal: AbortSignal.timeout(20000) }); }
    catch { throw new ClientError('RPC_UNAVAILABLE', 'Could not confirm the transaction through Solana RPC.'); }
    if (!result) return { state: 'uncertain', transaction };
    const tx = getTransactionDecoder().decode(Buffer.from(result.transaction[0], 'base64'));
    if (messageHash(tx.messageBytes) !== expectedMessageHash) throw new ClientError('TRANSACTION_MISMATCH', 'This transaction is not the payment signed for this quote.');
    if (!result.meta) return { state: 'uncertain', transaction };
    return { state: result.meta.err === null ? 'confirmed' : 'failed', transaction, slot: result.slot.toString() };
  }
}
