import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full Sign402 x402 paid probe demo.")
    parser.add_argument("--target", default="algorand.co")
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--resource-url", default=os.getenv("X402_RESOURCE_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--firefly-bridge-url", default=os.getenv("SIGN402_BRIDGE_URL"))
    parser.add_argument("--payment-executor-dir", default="../payment-executor")
    args = parser.parse_args()

    payment_executor_dir = Path(args.payment_executor_dir).resolve()
    sys.path.insert(0, str(payment_executor_dir))

    from .flow import encode_payment_proof, run_paid_probe_flow
    from .http_resource import X402ResourceClient

    env = _read_env(payment_executor_dir / ".env")
    payment_executor = _build_payment_executor(env)
    payment_approver = None
    if args.firefly_bridge_url:
        payment_approver = _build_payment_approver(args.firefly_bridge_url)

    result = run_paid_probe_flow(
        target=args.target,
        policy_hash=args.policy_hash,
        resource_client=X402ResourceClient(args.resource_url),
        payment_executor=payment_executor,
        proof_encoder=encode_payment_proof,
        payment_approver=payment_approver,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"')
    return env


def _build_payment_executor(env: dict[str, str]):
    from algosdk.v2client.algod import AlgodClient
    from sign402_executor.executor import execute_payment

    algod_client = AlgodClient("", env.get("ALGOD_URL", "https://testnet-api.algonode.cloud"))
    sender = env["ALGORAND_SENDER"]
    private_key = env["ALGORAND_PRIVATE_KEY"]

    def pay(requirement: dict, policy_hash: str) -> dict:
        return execute_payment(
            algod_client=algod_client,
            sender=sender,
            private_key=private_key,
            payment_request=requirement,
            policy_hash=policy_hash,
        )

    return pay


def _build_payment_approver(bridge_url: str):
    endpoint = bridge_url.rstrip("/") + "/approve-payment"

    def approve(payment_hash: str, commitment: dict) -> dict:
        body = json.dumps(
            {
                "paymentHash": payment_hash,
                "paymentCommitment": commitment,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"approved": False, "error": payload or str(exc)}

    return approve


if __name__ == "__main__":
    main()
