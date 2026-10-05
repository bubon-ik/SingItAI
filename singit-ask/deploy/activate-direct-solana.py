"""Operator activation: direct delegated Solana Ask; no transaction submission."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path('/home/hermes/apps/sign402')
PRIVATE = Path.home() / '.config/singit-ask'
STAGE = PRIVATE / 'direct-staging'
MANIFEST = json.loads((Path(__file__).parent / 'direct-solana-update.json').read_text())
GATEWAY_ENV = Path('/etc/sign402-gateway.env')
MERCHANT_ENV = PRIVATE / 'service.env'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def checked():
    for name, hashes in MANIFEST.items():
        if digest(STAGE / name) != hashes['after']:
            raise RuntimeError('Staged file changed: ' + name)
        if digest(ROOT / name) != hashes['before']:
            raise RuntimeError('Production file changed; review before installing: ' + name)
    for dependency in ('solana-x402-service/src/chain.mjs', 'singit-ask/src/solana-buyer.mjs'):
        if digest(STAGE / dependency) != digest(ROOT / dependency):
            raise RuntimeError('Tested payment dependency differs from production: ' + dependency)
    if not (ROOT / 'cdp-x402-service/node_modules/@coinbase/x402').exists():
        raise RuntimeError('Authenticated CDP SDK is missing')
    for port, route in [(8099, '/health'), (8130, '/app/'), (8140, '/health')]:
        with urlopen(f'http://127.0.0.1:{port}{route}', timeout=5) as r:
            if r.status != 200:
                raise RuntimeError('Existing service is not healthy')


def keys(path):
    """Compare encrypted identities without decrypting or displaying keys."""
    result = {}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in db.execute('PRAGMA table_info(' + quoted + ')')]
            encrypted = [c for c in columns if c in ('encrypted_private_key', 'encrypted_key')]
            if not encrypted:
                continue
            selected = encrypted + [c for c in columns if c in
                ('user_id', 'telegram_user_id', 'account', 'agent_address', 'wallet_address', 'chain')]
            query = 'SELECT ' + ','.join('"' + c + '"' for c in selected) + ' FROM ' + quoted
            result[table] = {hashlib.sha256(repr(row).encode()).hexdigest() for row in db.execute(query)}
    return result


def run(*args):
    subprocess.run(args, check=True)


def read_env(data):
    values = {}
    for line in data.decode().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            parsed = shlex.split(value)
            values[key.strip()] = parsed[0] if parsed else ''
    return values


def replace_env(data, updates):
    lines = [line for line in data.decode().splitlines()
             if line.partition('=')[0].strip() not in updates]
    return ('\n'.join(lines + [key + '=' + value for key, value in updates.items()]) + '\n').encode()


def healthy(token):
    last = None
    for _ in range(30):
        try:
            for port, route in [(8099, '/health'), (8130, '/app/')]:
                with urlopen(f'http://127.0.0.1:{port}{route}', timeout=2) as r:
                    assert r.status == 200
            with urlopen('http://127.0.0.1:8140/health', timeout=2) as r:
                health = json.load(r)
                assert health['directSolana']['settlementFeeAtomic'] == '1000'
                assert health['metered']['settlementFeeAtomic'] == '1000'
            url = 'http://127.0.0.1:8140/v1/chat/completions/quoted/solana/prepare'
            # Missing authentication and invalid authenticated input never run the model.
            for headers, expected in [({}, 401), ({'X-Singit-Ask-Token': token}, 400)]:
                request = Request(url, data=b'{}', headers={'Content-Type': 'application/json', **headers}, method='POST')
                try:
                    with urlopen(request, timeout=3):
                        raise RuntimeError('Unsafe preparation response')
                except HTTPError as e:
                    if e.code != expected:
                        raise RuntimeError('Preparation guard failed') from None
            # Both Python services must receive the same private token without printing it.
            for service in ('sign402-gateway', 'sign402-web-api'):
                pid = subprocess.check_output(['systemctl', 'show', service, '-p', 'MainPID', '--value'], text=True).strip()
                env = Path('/proc') / pid / 'environ'
                assert ('SINGIT_ASK_QUOTE_TOKEN=' + token).encode() in env.read_bytes().split(b'\0')
            return
        except Exception as error:
            last = type(error).__name__
            time.sleep(1)
    raise RuntimeError('Post-activation verification failed: ' + str(last))


def main():
    checked()
    if '--check-only' in sys.argv:
        print('All nine source hashes, installed dependencies and existing service health verified. No changes or payments made.')
        return
    run('sudo', '-v')
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path.home() / 'sign402-backups' / (stamp + '-before-direct-solana')
    backup.mkdir(parents=True, mode=0o700)
    gateway = subprocess.check_output(['sudo', 'cat', str(GATEWAY_ENV)])
    merchant = MERCHANT_ENV.read_bytes()
    (backup / 'gateway.env').write_bytes(gateway)
    (backup / 'merchant.env').write_bytes(merchant)
    values = read_env(gateway)
    if values.get('SIGN402_SOLANA_ALLOWANCE_ENABLED') != '1':
        raise RuntimeError('Solana allowance is not enabled; nothing changed')
    merchant_values = read_env(merchant)
    if merchant_values.get('SINGIT_ASK_METERED') != '1' or not all(merchant_values.get(k) for k in ('CDP_API_KEY_ID', 'CDP_API_KEY_SECRET')):
        raise RuntimeError('Authenticated CDP metering is not configured; nothing changed')
    token = secrets.token_urlsafe(48)
    new_gateway = backup / 'new-gateway.env'
    new_merchant = backup / 'new-merchant.env'
    new_gateway.write_bytes(replace_env(gateway, {'SINGIT_ASK_QUOTE_TOKEN': token}))
    new_merchant.write_bytes(replace_env(merchant, {'SINGIT_ASK_QUOTE_TOKEN': token,
        'SINGIT_ASK_DIRECT_SOLANA': '1', 'SINGIT_ASK_QUOTE_DB': str(PRIVATE / 'quotes.sqlite3')}))
    paths = set((Path.home() / '.sign402').glob('*.db')) | set((Path.home() / '.sign402').glob('*.sqlite3'))
    paths.update(Path(values[k]).expanduser() for k in ('SIGN402_USER_WALLET_STORE_PATH',
        'SIGN402_ALLOWANCE_DB', 'SIGN402_SOLANA_ALLOWANCE_DB', 'SIGN402_WEB_DB') if values.get(k))
    checked()
    identities = {}
    for name in MANIFEST:
        original = ROOT / name
        if original.exists():
            target = backup / 'code' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    print('Private rollback snapshot:', backup)
    try:
        run('sudo', 'systemctl', 'stop', 'sign402-web-api', 'sign402-gateway')
        run('systemctl', '--user', 'stop', 'singit-ask')
        # Snapshot quiesced state, including unresolved journals; never clear or recreate a wallet.
        journal = Path.home() / '.sign402/ask-metered'
        if journal.exists():
            shutil.copytree(journal, backup / 'ask-metered')
        for index, db in enumerate(sorted(paths)):
            if db.is_file():
                identities[db] = keys(db)
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as src:
                    with sqlite3.connect(backup / f'state-{index}.db') as dst:
                        src.backup(dst)
        (backup / 'state-paths.json').write_text(json.dumps([str(p) for p in sorted(paths)]))
        for name in MANIFEST:
            shutil.copy2(STAGE / name, ROOT / name)
        run('sudo', 'install', '-m', '600', str(new_gateway), str(GATEWAY_ENV))
        shutil.copyfile(new_merchant, MERCHANT_ENV)
        MERCHANT_ENV.chmod(0o600)
        run('systemctl', '--user', 'start', 'singit-ask')
        run('sudo', 'systemctl', 'start', 'sign402-gateway', 'sign402-web-api')
        healthy(token)
        for db, before in identities.items():
            after = keys(db)
            if any(not rows.issubset(after.get(table, set())) for table, rows in before.items()):
                raise RuntimeError('Existing wallet identities changed; inspect the private backup')
        if any(digest(ROOT / name) != hashes['after'] for name, hashes in MANIFEST.items()):
            raise RuntimeError('Installed source verification failed')
        print('Direct Solana Ask is active. Existing wallet identities preserved.')
        print('Actual token cost + 30% + 0.001 USDC; no agent SOL funding. No real payment was made.')
    except BaseException:
        subprocess.run(['sudo', 'systemctl', 'stop', 'sign402-web-api', 'sign402-gateway'], check=False)
        subprocess.run(['systemctl', '--user', 'stop', 'singit-ask'], check=False)
        for name, hashes in MANIFEST.items():
            old = backup / 'code' / name
            if old.exists():
                shutil.copy2(old, ROOT / name)
            elif hashes['before'] is None:
                (ROOT / name).unlink(missing_ok=True)
        run('sudo', 'install', '-m', '600', str(backup / 'gateway.env'), str(GATEWAY_ENV))
        shutil.copyfile(backup / 'merchant.env', MERCHANT_ENV)
        run('systemctl', '--user', 'start', 'singit-ask')
        run('sudo', 'systemctl', 'start', 'sign402-gateway', 'sign402-web-api')
        print('Previous code and configuration restored. Payment records were preserved:', backup)
        raise


if __name__ == '__main__':
    main()
