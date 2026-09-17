import { createHash } from 'node:crypto';
import { createSolanaRpc, getProgramDerivedAddress, getBase58Encoder, getBase58Decoder, getTransactionDecoder } from '@solana/kit';
import { ExactSvmScheme } from '@x402/svm/exact/client';
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
