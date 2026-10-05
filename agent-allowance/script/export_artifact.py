"""Export the compiled AgentAllowance for the gateway, or check the export.

    python3 script/export_artifact.py            # forge build, then write the artifact
    python3 script/export_artifact.py --check    # fail if the committed artifact is stale
    python3 script/export_artifact.py --check --onchain 0x<limiter> [--rpc URL]

The gateway deploys limiters from this file, not from the Solidity source: it
has no compiler. So the file must be exactly what `forge build` produces from
`src/AgentAllowance.sol` — the code the tests and the mutation check ran against.
`--check` rebuilds and compares. `--onchain` also compares a deployed limiter's
runtime code with the artifact, immutables masked: the deployment is the
audited code, not merely the same name.

The standard JSON input rides along so the gateway can publish each deployment's
source to Sourcify without a compiler either.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SOURCE = HERE / "src" / "AgentAllowance.sol"
TARGET = HERE.parent / "sign402-gateway" / "sign402_gateway" / "contracts" / "agent_allowance.json"
# The owner's machine checks a spender against the same code before the Trezor
# is asked to approve it; it keeps its own copy to stay independent of the gateway.
SIDECAR_TARGET = HERE.parent / "trezor-sidecar" / "trezor_sidecar" / "agent_allowance.json"
DUMMY = "0x000000000000000000000000000000000000dEaD"


def build() -> dict:
    subprocess.run(["forge", "build", "-q"], cwd=HERE, check=True)
    raw = json.loads((HERE / "out" / "AgentAllowance.sol" / "AgentAllowance.json").read_text())
    std_input = subprocess.run(
        ["forge", "verify-contract", "--show-standard-json-input", DUMMY, "src/AgentAllowance.sol:AgentAllowance"],
        cwd=HERE, check=True, capture_output=True, text=True,
    ).stdout
    references = raw["deployedBytecode"].get("immutableReferences") or {}
    return {
        "contractName": "AgentAllowance",
        "sourcePath": "agent-allowance/src/AgentAllowance.sol",
        "sourceSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "compilerVersion": "v" + raw["metadata"]["compiler"]["version"],
        "abi": raw["abi"],
        "bytecode": raw["bytecode"]["object"],
        "deployedBytecode": raw["deployedBytecode"]["object"],
        "immutableRanges": sorted(
            [ref["start"], ref["length"]] for refs in references.values() for ref in refs
        ),
        "standardJsonInput": json.loads(std_input),
    }


def masked(code_hex: str, ranges: list[list[int]]) -> bytes:
    code = bytearray(bytes.fromhex(code_hex.removeprefix("0x")))
    for start, length in ranges:
        code[start:start + length] = b"\x00" * length
    return bytes(code)


def onchain_code(address: str, rpc: str) -> str:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [address, "latest"]}).encode()
    request = urllib.request.Request(rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "sign402-artifact-check/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--onchain", help="a deployed limiter to compare with the artifact")
    parser.add_argument("--rpc", default="https://mainnet.base.org")
    args = parser.parse_args()

    fresh = build()
    if not args.check:
        for target in (TARGET, SIDECAR_TARGET):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n")
            print(f"wrote {target.relative_to(HERE.parent)} ({fresh['sourceSha256'][:12]})")
        return 0

    for target in (TARGET, SIDECAR_TARGET):
        committed = json.loads(target.read_text())
        for field in ("sourceSha256", "compilerVersion", "bytecode", "deployedBytecode", "immutableRanges"):
            if committed.get(field) != fresh[field]:
                print(f"STALE: {target.relative_to(HERE.parent)} {field} differs from a fresh build of {fresh['sourcePath']}")
                return 1
    print("both artifacts match a fresh build of the source")

    if args.onchain:
        live = onchain_code(args.onchain, args.rpc)
        if masked(live, fresh["immutableRanges"]) != masked(fresh["deployedBytecode"], fresh["immutableRanges"]):
            print(f"MISMATCH: {args.onchain} does not run this code")
            return 1
        print(f"{args.onchain} runs exactly this code (immutables masked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
