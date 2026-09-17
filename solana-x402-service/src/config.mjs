import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const NETWORK = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp';
export const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
export const TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';
export const ATA_PROGRAM = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL';
export const VENICE = 'https://api.venice.ai/api/v1';
export const TOP_UP_URL = `${VENICE}/x402/top-up`;
export const ROOT = fileURLToPath(new URL('../', import.meta.url));
export const DEFAULT_RPC = 'https://api.mainnet-beta.solana.com';
export function configuration(env = process.env) {
  const stateDir = path.resolve(env.SINGIT_STATE_DIR || path.join(ROOT, '.local'));
  const rpcUrl = env.SOLANA_RPC_URL || DEFAULT_RPC;
  if (new URL(rpcUrl).protocol !== 'https:') throw new Error('SOLANA_RPC_URL must use HTTPS.');
  return { stateDir, walletFile: path.resolve(env.SOLANA_KEYPAIR_FILE || path.join(stateDir, 'wallet.json')), rpcUrl };
}
export class ClientError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}
export function formatUsdc(atomic) {
  const value = BigInt(atomic);
  return `${value / 1000000n}.${String(value % 1000000n).padStart(6, '0')}`;
}
