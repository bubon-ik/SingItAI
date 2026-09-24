#!/usr/bin/env python3
"""Install only the private Graph demo command into the existing Hermes plugin.

Existing production gateway code is not deployed. Remote files are backed up,
and the bridge token is transferred on SSH stdin rather than in shell arguments.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REMOTE = r'''
import json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

payload = json.load(sys.stdin)
home = Path.home()
plugin = (home / ".hermes/plugins/sign402-wallet").resolve()
init_path, env_path = plugin / "__init__.py", home / ".hermes/.env"
source = init_path.read_text()
anchor = '    capture_gateway_identity(event=event, **kwargs)\n    source = getattr(event, "source", None)'
insertion = '\n\n    graph_demo = handle_graph_demo(event=event, source=source, gateway=gateway,\n                                  send=_send_fixed_reply, background=_run_in_background)\n    if graph_demo:\n        return graph_demo'
if "from .graph_demo import handle_graph_demo" not in source:
    import_anchor = 'from .client import GatewayClient, GatewayClientError'
    if source.count(import_anchor) != 1 or source.count(anchor) != 1:
        sys.exit("Plugin differs from the inspected version; no files changed.")
    source = source.replace(import_anchor, import_anchor + '\nfrom .graph_demo import handle_graph_demo', 1)
    source = source.replace(anchor, anchor + insertion, 1)
compile(source, str(init_path), "exec")
compile(payload["plugin"], str(plugin / "graph_demo.py"), "exec")
values = {"SIGN402_GRAPH_DEMO_OWNER": payload["owner"],
          "SIGN402_GRAPH_DEMO_PORT": "8107", "SIGN402_GRAPH_DEMO_TOKEN": payload["token"]}
env = env_path.read_text()
updated_env = '\n'.join(line for line in env.splitlines()
    if line.split('=', 1)[0] not in values) + '\n'
updated_env += '\n'.join(key + '=' + value for key, value in values.items()) + '\n'
backup = home / '.hermes/graph-demo-backups' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
backup.mkdir(parents=True, mode=0o700)

def write_private(path, text, mode):
    fd, tmp = tempfile.mkstemp(prefix='.graph-demo-', dir=path.parent)
    os.fchmod(fd, mode)
    with os.fdopen(fd, 'w') as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)

write_private(backup / '__init__.py', init_path.read_text(), 0o600)
write_private(backup / 'hermes.env', env, 0o600)
if (plugin / 'graph_demo.py').exists():
    write_private(backup / 'graph_demo.py', (plugin / 'graph_demo.py').read_text(), 0o600)
write_private(plugin / 'graph_demo.py', payload['plugin'], 0o644)
write_private(init_path, source, 0o644)
write_private(env_path, updated_env, 0o600)
subprocess.run(['systemctl', '--user', 'restart', 'hermes-gateway'], check=True, capture_output=True)
status = subprocess.run(['systemctl', '--user', 'is-active', 'hermes-gateway'], capture_output=True, text=True)
print('bot_service:', status.stdout.strip())
print('backup:', str(backup))
request = Request('http://127.0.0.1:8107/status', data=json.dumps({'owner': payload['owner']}).encode(),
    headers={'Content-Type':'application/json', 'Authorization':'Bearer ' + payload['token']})
with urlopen(request, timeout=10) as response:
    result = json.load(response)
print('demo_bridge:', result['status'])
bot_token = None
for line in updated_env.splitlines():
    if line.startswith('TELEGRAM_BOT_TOKEN='):
        bot_token = line.split('=',1)[1].strip().strip('"').strip("'")
if bot_token:
    try:
        with urlopen('https://api.telegram.org/bot' + bot_token + '/getMe', timeout=10) as response:
            result = json.load(response)
        print('telegram_bot:', result.get('result', {}).get('username'))
    except Exception:
        print('telegram_bot: metadata temporarily unavailable')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    if not args.owner.isdecimal():
        parser.error("Use a numeric Telegram user ID")
    token = (ROOT / ".graph-live/telegram-demo/bridge-token").read_text().strip()
    payload = {"owner": args.owner, "token": token,
               "plugin": (ROOT / "hermes-plugins/sign402-wallet/graph_demo.py").read_text()}
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "hermes@164.68.104.44", "python3 -c " + shlex.quote(REMOTE)],
        input=json.dumps(payload), text=True, capture_output=True, timeout=60)
    if result.returncode:
        # Never echo a remote traceback or a command containing configuration.
        sys.exit("Demo installation did not complete; inspect remote file/service status before retrying.")
    print(result.stdout)


if __name__ == "__main__":
    main()
