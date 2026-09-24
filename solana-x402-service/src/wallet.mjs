import { generateKeyPairSync, createPrivateKey, sign, randomBytes } from 'node:crypto';
import { readFile, writeFile, mkdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { createKeyPairSignerFromBytes, getBase58Encoder, getBase58Decoder } from '@solana/kit';
import { ClientError, NETWORK, VENICE } from './config.mjs';

export async function walletFromBytes(bytes) {
  if (bytes.length !== 64) throw new ClientError('INVALID_WALLET', 'A 64-byte Solana keypair is required.');
  let signer;
  try { signer = await createKeyPairSignerFromBytes(new Uint8Array(bytes)); }
  catch { throw new ClientError('INVALID_WALLET', 'The Solana keypair is invalid.'); }
  const key = createPrivateKey({ key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), Buffer.from(bytes).subarray(0, 32)]), format: 'der', type: 'pkcs8' });
  return { address: signer.address, signer, signMessage: message => getBase58Decoder().decode(sign(null, Buffer.from(message, 'utf8'), key)) };
}

export async function createWallet(file) {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const bytes = Buffer.concat([privateKey.export({ format: 'der', type: 'pkcs8' }).subarray(-32), publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)]);
  const wallet = await walletFromBytes(bytes);
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  try { await writeFile(file, `${JSON.stringify([...bytes])}\n`, { flag: 'wx', mode: 0o600 }); }
  catch (e) {
    if (e.code === 'EEXIST') throw new ClientError('WALLET_EXISTS', 'Wallet file already exists; it was not overwritten.');
    throw new ClientError('WALLET_WRITE_FAILED', 'Could not create the private wallet file.');
  } finally { bytes.fill(0); }
  return wallet;
}

export async function loadWallet(file, env = process.env) {
  if (env.SOLANA_PRIVATE_KEY) {
    try { return await walletFromBytes(getBase58Encoder().encode(env.SOLANA_PRIVATE_KEY)); }
    catch { throw new ClientError('INVALID_WALLET', 'SOLANA_PRIVATE_KEY must contain a valid base58 64-byte keypair.'); }
  }
  let values;
  try {
    const info = await stat(file);
    if ((info.mode & 0o077) !== 0) throw new ClientError('WALLET_PERMISSIONS', 'Wallet file permissions must be 0600.');
    values = JSON.parse(await readFile(file, 'utf8'));
  } catch (e) {
    if (e instanceof ClientError) throw e;
    throw new ClientError('WALLET_UNAVAILABLE', 'Create a wallet with wallet-init, or configure SOLANA_KEYPAIR_FILE.');
  }
  if (!Array.isArray(values) || values.length !== 64 || values.some(v => !Number.isInteger(v) || v < 0 || v > 255)) throw new ClientError('INVALID_WALLET', 'Wallet file must be a JSON array of 64 bytes.');
  return walletFromBytes(new Uint8Array(values));
}

export function authHeader(wallet, { now = Date.now(), nonce = randomBytes(16).toString('hex') } = {}) {
  const issued = new Date(now).toISOString();
  const expires = new Date(now + 300000).toISOString();
  const message = `api.venice.ai wants you to sign in with your Solana account:\n${wallet.address}\n\nSign in to Venice AI\n\nURI: ${VENICE}\nVersion: 1\nChain ID: ${NETWORK}\nNonce: ${nonce}\nIssued At: ${issued}\nExpiration Time: ${expires}`;
  return Buffer.from(JSON.stringify({ address: wallet.address, message, signature: wallet.signMessage(message), timestamp: now, chainId: NETWORK, type: 'ed25519' })).toString('base64');
}
