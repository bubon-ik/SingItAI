import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { walletFromBytes } from '../src/wallet.mjs';
import { NETWORK, USDC, TOP_UP_URL } from '../src/config.mjs';

export async function testWallet() {
  const pair = generateKeyPairSync('ed25519');
  const bytes = Buffer.concat([pair.privateKey.export({ format: 'der', type: 'pkcs8' }).subarray(-32), pair.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)]);
  return walletFromBytes(bytes);
}
export const directory = () => mkdtempSync(path.join(os.tmpdir(), 'singit-test-'));
export function challenge(overrides = {}) {
  return { requirement: { scheme: 'exact', network: NETWORK, asset: USDC, amount: '5000000', payTo: '8qUL23aSj7mDWdoLMXGHFvnVCT9wd7jXcysiekroADEL', maxTimeoutSeconds: 300, extra: { feePayer: 'BFK9TLC3edb13K6v4YyH3DwPb5DSUpkWvb7XnqCL9b4F' }, ...overrides }, resource: { url: TOP_UP_URL, description: 'Venice AI credit top-up', mimeType: 'application/json' } };
}
export const jsonResponse = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json', ...headers } });
