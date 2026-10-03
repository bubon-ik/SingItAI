"""Install two reviewed chat-context files; no changes to payment logic or keys."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('activation_helpers', HERE / 'activate-direct-solana.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
ROOT, STAGE = helpers.ROOT, helpers.STAGE
MANIFEST = json.loads((HERE / 'chat-payment-context-update.json').read_text())


def checked():
    for name, hashes in MANIFEST.items():
        if helpers.digest(STAGE / name) != hashes['after']:
            raise RuntimeError('Staged source changed: ' + name)
        if helpers.digest(ROOT / name) != hashes['before']:
            raise RuntimeError('Production source changed: ' + name)


def healthy():
    for _ in range(30):
        try:
            for url in ('http://127.0.0.1:8099/health', 'http://127.0.0.1:8130/app/', 'http://127.0.0.1:8140/health'):
                with urlopen(url, timeout=2) as response:
                    assert response.status == 200
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError('Services did not become healthy')


def main():
    checked()
    healthy()
    if '--check-only' in sys.argv:
        print('Both chat-context source hashes and service health verified. No changes or payments made.')
        return
    helpers.run('sudo', '-v')
    os.umask(0o077)
    backup = Path.home() / 'sign402-backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-before-chat-payment-context')
    backup.mkdir(parents=True, mode=0o700)
    config = subprocess.check_output(['sudo', 'cat', '/etc/sign402-gateway.env'])
    (backup / 'gateway.env').write_bytes(config)
    values = helpers.read_env(config)
    paths = set((Path.home() / '.sign402').glob('*.db')) | set((Path.home() / '.sign402').glob('*.sqlite3'))
    paths.update(Path(values[k]).expanduser() for k in ('SIGN402_USER_WALLET_STORE_PATH',
        'SIGN402_ALLOWANCE_DB', 'SIGN402_SOLANA_ALLOWANCE_DB', 'SIGN402_WEB_DB') if values.get(k))
    for name in MANIFEST:
        target = backup / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    print('Private rollback snapshot:', backup)
    checked()
    try:
        helpers.run('sudo', 'systemctl', 'stop', 'sign402-web-api', 'sign402-gateway')
        identities = {p: helpers.keys(p) for p in paths if p.is_file()}
        for index, db in enumerate(identities):
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(backup / f'state-{index}.db') as dest:
                    source.backup(dest)
        (backup / 'state-paths.json').write_text(json.dumps([str(p) for p in identities]))
        for name in MANIFEST:
            shutil.copy2(STAGE / name, ROOT / name)
        helpers.run('sudo', 'systemctl', 'start', 'sign402-gateway', 'sign402-web-api')
        healthy()
        for db, before in identities.items():
            after = helpers.keys(db)
            if any(not rows.issubset(after.get(table, set())) for table, rows in before.items()):
                raise RuntimeError('Wallet identity changed; inspect private backup')
        assert all(helpers.digest(ROOT / name) == h['after'] for name, h in MANIFEST.items())
        print('Chat payment context updated. Existing wallet identities preserved. No payment was sent.')
    except BaseException:
        for name in MANIFEST:
            shutil.copy2(backup / 'code' / name, ROOT / name)
        subprocess.run(['sudo', 'systemctl', 'restart', 'sign402-gateway', 'sign402-web-api'], check=False)
        print('Previous chat code restored. State and configuration preserved:', backup)
        raise


if __name__ == '__main__':
    main()
