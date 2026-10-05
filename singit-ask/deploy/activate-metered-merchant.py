"""Enable the additional Base upto endpoint; preserve existing exact endpoints.

Runs as the existing merchant service owner. Reuses already installed CDP SDK
and credentials. No inference or payment request is submitted by this script.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import sys
from urllib.request import urlopen

ROOT=Path.home()/"apps/sign402"
CONFIG=Path.home()/".config/singit-ask/service.env"
STAGE=Path.home()/".config/singit-ask/agent-staging/singit-ask/src"

def main():
    os.umask(0o077)
    raw=CONFIG.read_text()
    solana='--solana' in sys.argv or 'SINGIT_ASK_METERED_SOLANA=1' in raw.splitlines()
    wanted={"CDP_API_KEY_ID","CDP_API_KEY_SECRET"}
    entries={}
    for path in [CONFIG,Path('/etc/sign402-gateway.env'),Path.home()/'.hermes/.env',ROOT/'cdp-x402-service/.env']:
        try: lines=path.read_text().splitlines()
        except OSError: continue
        for line in lines:
            key, sep, value=line.strip().removeprefix('export ').partition('=')
            if sep and key in wanted and value.strip().strip('\"\''):
                entries.setdefault(key,line.strip().removeprefix('export '))
        if wanted.issubset(entries):break
    if not wanted.issubset(entries):raise RuntimeError('Existing CDP credentials not accessible; configuration unchanged')
    backup=Path.home()/'sign402-backups'/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-before-metered-merchant')
    backup.mkdir(parents=True,mode=0o700)
    shutil.copy2(CONFIG,backup/'service.env')
    shutil.copytree(ROOT/'singit-ask/src',backup/'src')
    target=ROOT/'singit-ask/src'
    for path in STAGE.glob('*.mjs'):shutil.copy2(path,target/path.name)
    clean=[line for line in raw.splitlines() if line.partition('=')[0].strip() not in wanted|{'SINGIT_ASK_METERED','SINGIT_ASK_METERED_SOLANA'}]
    CONFIG.write_text('\n'.join(clean+[entries[k] for k in sorted(wanted)]+['SINGIT_ASK_METERED=1',f'SINGIT_ASK_METERED_SOLANA={int(solana)}'])+'\n')
    CONFIG.chmod(0o600)
    env={**os.environ,'XDG_RUNTIME_DIR':'/run/user/1000'}
    def restart():subprocess.run(['systemctl','--user','restart','singit-ask'],env=env,check=True)
    try:
        restart()
        for attempt in range(20):
            try:
                with urlopen('http://127.0.0.1:8140/health',timeout=3) as response:health=json.load(response)
                if health.get('metered',{}).get('mode')=='actual_usage' and (not solana or health.get('meteredSolana',{}).get('settlementFeeAtomic')=='2000'):break
            except Exception:pass
            time.sleep(1)
        else:raise RuntimeError('Metered merchant failed health check')
    except BaseException:
        shutil.copy2(backup/'service.env',CONFIG)
        for path in (backup/'src').glob('*.mjs'):shutil.copy2(path,target/path.name)
        restart()
        raise RuntimeError('Previous merchant code/config restored; inspect service privately') from None
    print('Metered endpoint enabled. Backup:',backup)
    print('No model query or payment submitted. Gateway activation remains separate.')

if __name__=='__main__':main()
