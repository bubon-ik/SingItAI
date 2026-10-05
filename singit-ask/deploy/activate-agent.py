"""Operator-run activation of the reviewed Ask integration; no payment or key creation."""
import base64
import hashlib
import json
import os
from pathlib import Path
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
STAGE = Path('/home/hermes/.config/singit-ask/agent-staging')
MANIFEST = json.loads((Path(__file__).parent / 'agent-update.json').read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def checked():
    for name, hashes in MANIFEST.items():
        if digest(STAGE / name) != hashes['after']:
            raise RuntimeError('Staged file changed: ' + name)
        if digest(ROOT / name) != hashes['before']:
            raise RuntimeError('Production file changed; review before installing: ' + name)
    for helper in ('buyer-cli.mjs', 'solana-buyer-cli.mjs'):
        if not (ROOT / 'singit-ask/src' / helper).is_file():
            raise RuntimeError('Metered buyer helper is missing: ' + helper)
    for suffix, network, fee in [('', 'eip155:8453', '1000'), ('/solana', 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp', '2000')]:
        request = Request('http://127.0.0.1:8140/v1/chat/completions/metered' + suffix,
            data=json.dumps({'messages': [{'role': 'user', 'content': 'Unpaid readiness check'}], 'max_tokens': 1}).encode(),
            headers={'Content-Type': 'application/json', 'Host': 'ask.singitai.app', 'X-Forwarded-Proto': 'https'}, method='POST')
        try:
            with urlopen(request, timeout=20):
                raise RuntimeError('Expected an unpaid 402 quote')
        except HTTPError as error:
            if error.code != 402:
                raise RuntimeError('Metered endpoint is not ready: ' + suffix) from None
            quote = json.loads(base64.b64decode(error.headers['payment-required']))
        offers = quote.get('accepts', [])
        if len(offers) != 1:
            raise RuntimeError('Unexpected metered offers')
        offer = offers[0]
        if (offer.get('scheme'), offer.get('network'), offer.get('amount')) != ('upto', network, '3000'):
            raise RuntimeError('Unexpected metered payment terms')
        if offer.get('extra', {}).get('billing') != {'version': 1, 'mode': 'actual_usage', 'markupBps': 3000,
            'settlementFeeAtomic': fee, 'maxChargeAtomic': '3000'}:
            raise RuntimeError('Unexpected metered billing terms')


def receiver_ready():
    result = subprocess.run([str(Path.home() / '.hermes/node/bin/node'), str(ROOT / 'singit-ask/src/solana-buyer-cli.mjs')],
        input=json.dumps({'checkOnly': True, 'body': {'messages': [{'role': 'user', 'content': 'Unpaid readiness check'}]}}),
        text=True, capture_output=True, timeout=60)
    try:
        status = json.loads(result.stdout)
    except ValueError:
        status = {}
    if status.get('ready') is True:
        return True
    if status.get('error') == 'receiver_not_ready':
        print('Activation blocked: receiving wallet 4an2sqamWWhny9mjLsMtGXCDXakeNtg6vSLq4QvhdmQu has no active native USDC account.')
        print('Create its native USDC account on Solana, for example by receiving native USDC there, then run this command again.')
    else:
        print('Activation blocked: Solana quote or recipient readiness could not be verified. No payment was made.')
    return False


def keys(path):
    """Fingerprint encrypted wallet identities without decrypting or printing them."""
    result = {}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
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


def healthy():
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:8099/health', timeout=2) as r:
                assert r.status == 200
            with urlopen('http://127.0.0.1:8130/app/', timeout=2) as r:
                assert r.status == 200
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError('Gateway or page did not become healthy')


def main():
    checked()
    ready = receiver_ready()
    if '--check-only' in sys.argv:
        print('Source hashes and both unpaid quotes verified. No changes made.')
        if not ready:
            sys.exit(2)
        return
    if not ready:
        sys.exit(2)
    run('sudo', '-v')  # Ask for the operator password before changing anything.
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path.home() / 'sign402-backups' / (stamp + '-before-metered-ask-agent')
    backup.mkdir(parents=True, mode=0o700)
    config = subprocess.check_output(['sudo', 'cat', '/etc/sign402-gateway.env'])
    (backup / 'gateway.env').write_bytes(config)
    readiness = {}
    for line in config.decode().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key in ('SIGN402_SOLANA_ALLOWANCE_ENABLED', 'SIGN402_SOLANA_FEE_PAYER_KEY'):
            parts = shlex.split(value)
            readiness[key] = parts[0] if parts else ''
    if readiness.get('SIGN402_SOLANA_ALLOWANCE_ENABLED') != '1':
        raise RuntimeError('Solana allowance must be enabled in the gateway configuration before activating both networks; no services changed')
    if not readiness.get('SIGN402_SOLANA_FEE_PAYER_KEY'):
        raise RuntimeError('Solana funding sponsor must be configured before activation; no services changed')
    readiness.clear()

    values = {}
    for line in config.decode().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            if key in ('SIGN402_USER_WALLET_STORE_PATH', 'SIGN402_ALLOWANCE_DB',
                       'SIGN402_SOLANA_ALLOWANCE_DB', 'SIGN402_WEB_DB'):
                values[key] = shlex.split(value)[0] if shlex.split(value) else ''
    paths = set((Path.home() / '.sign402').glob('*.db'))
    paths.update((Path.home() / '.sign402').glob('*.sqlite3'))
    paths.update(Path(v).expanduser() for v in values.values() if v)
    identities = {p: keys(p) for p in paths if p.is_file()}
    for index, path in enumerate(identities):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(backup / f'state-{index}.db') as dst:
                src.backup(dst)
    (backup / 'state-paths.json').write_text(json.dumps([str(p) for p in identities]))
    for name in MANIFEST:
        original = ROOT / name
        if original.exists():
            target = backup / 'code' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    print('Private rollback snapshot:', backup)
    checked()
    try:
        run('sudo', 'systemctl', 'stop', 'sign402-web-api', 'sign402-gateway')
        for name in MANIFEST:
            shutil.copy2(STAGE / name, ROOT / name)
        run('sudo', 'systemctl', 'start', 'sign402-gateway', 'sign402-web-api')
        healthy()
        for path, before in identities.items():
            after = keys(path)
            if any(not rows.issubset(after.get(table, set())) for table, rows in before.items()):
                raise RuntimeError('Existing wallet identities changed; inspect private backup before continuing')
        print('Active. Existing wallet identities preserved. No real payment was made.')
        print('Token billing active on app.singitai.app/app/: select DeepSeek V4.1 Flash / SingIt Ask on Base or Solana.')
    except BaseException:
        for name in MANIFEST:
            old = backup / 'code' / name
            if old.exists():
                shutil.copy2(old, ROOT / name)
        subprocess.run(['sudo', 'systemctl', 'restart', 'sign402-gateway', 'sign402-web-api'], check=False)
        print('Previous code restored; new module is inert. See private backup:', backup)
        raise


if __name__ == '__main__':
    main()
