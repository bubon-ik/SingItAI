import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, statSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { getBase58Encoder } from '@solana/kit';
import { authHeader, createWallet, loadWallet } from '../src/wallet.mjs';
import { NETWORK } from '../src/config.mjs';
import { testWallet, directory } from './helpers.mjs';

test('Solana SIWX carries an authentic Ed25519 signature bound to address, network and expiry', async () => {
  const wallet = await testWallet();
  const now = Date.UTC(2026, 8, 17, 12);
  const auth = JSON.parse(Buffer.from(authHeader(wallet, { now, nonce: '0123456789abcdef' }), 'base64').toString());
  assert.equal(auth.type, 'ed25519');
  assert.equal(auth.chainId, NETWORK);
  assert.equal(auth.timestamp, now);
  assert.match(auth.message, /your Solana account:/);
  assert.ok(auth.message.includes(wallet.address));
  assert.ok(auth.message.includes('Expiration Time: 2026-09-17T12:05:00.000Z'));
  assert.equal(await crypto.subtle.verify('Ed25519', wallet.signer.keyPair.publicKey, getBase58Encoder().encode(auth.signature), new TextEncoder().encode(auth.message)), true);
  assert.notEqual(authHeader(wallet), authHeader(wallet));
});

test('wallet creation is private, reloadable and never overwrites an existing wallet', async t => {
  const dir = directory(); t.after(() => rmSync(dir, { recursive: true }));
  const file = path.join(dir, 'wallet.json');
  const wallet = await createWallet(file);
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.equal((await loadWallet(file, {})).address, wallet.address);
  const before = readFileSync(file, 'utf8');
  await assert.rejects(createWallet(file), { code: 'WALLET_EXISTS' });
  assert.equal(readFileSync(file, 'utf8'), before);
});

test('malformed private-key errors do not disclose the supplied secret', async () => {
  const secret = 'invalid-secret-that-must-not-appear';
  await assert.rejects(loadWallet('/unused', { SOLANA_PRIVATE_KEY: secret }), e => e.code === 'INVALID_WALLET' && !e.message.includes(secret));
});

test('wallet rejects invalid key bytes', async t => {
  const dir = directory(); t.after(() => rmSync(dir, { recursive: true }));
  const file = path.join(dir, 'wallet.json');
  writeFileSync(file, JSON.stringify(new Array(64).fill(300)), { mode: 0o600 });
  await assert.rejects(loadWallet(file, {}), { code: 'INVALID_WALLET' });
});
