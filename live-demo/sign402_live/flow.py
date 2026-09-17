import base64
import hashlib
import json
import time
from typing import Any, Callable


def encode_payment_proof(proof: dict[str, Any]) -> str:
    payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def build_payment_commitment(
    requirement: dict[str, Any],
    policy_hash: str,
) -> dict[str, Any]:
    commitment = {
        "type": "sign402-payment",
        "policyHash": policy_hash.lower(),
        "network": requirement["network"],
        "asset": requirement["asset"],
        "amountAtomic": str(requirement["amountAtomic"]),
        "receiver": requirement["receiver"],
        "resource": requirement["resource"],
        "paymentIntent": requirement["paymentIntent"],
        "purpose": requirement.get("purpose"),
    }
    canonical = json.dumps(commitment, sort_keys=True, separators=(",", ":"))
    return {
        "commitment": commitment,
        "canonicalPaymentCommitment": canonical,
        "paymentHash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def run_paid_probe_flow(
    *,
    target: str,
    policy_hash: str,
    resource_client,
    payment_executor: Callable[[dict[str, Any], str], dict[str, Any]],
    proof_encoder: Callable[[dict[str, Any]], str],
    payment_approver: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    retry_delay_seconds: float = 2.0,
    max_retries: int = 8,
) -> dict[str, Any]:
    first_response = resource_client.get_probe_without_payment(target)
    if first_response.get("status") != 402:
        raise RuntimeError("Expected x402 resource server to return 402 Payment Required.")

    requirement = first_response["paymentRequirements"]
    payment_commitment = build_payment_commitment(requirement, policy_hash)
    payment_approval = None
    if payment_approver is not None:
        payment_approval = payment_approver(
            payment_commitment["paymentHash"],
            payment_commitment["commitment"],
        )
        if not payment_approval.get("approved"):
            return {
                "status": "payment_rejected",
                "target": target,
                "paymentRequirements": requirement,
                "paymentCommitment": payment_commitment["commitment"],
                "canonicalPaymentCommitment": payment_commitment["canonicalPaymentCommitment"],
                "paymentApprovalHash": payment_commitment["paymentHash"],
                "paymentApproval": payment_approval,
            }
        approved_hash = _payment_approval_hash(payment_approval)
        if approved_hash != payment_commitment["paymentHash"]:
            raise RuntimeError("Firefly approved hash does not match payment commitment hash.")

    payment = payment_executor(requirement, policy_hash)
    payment_proof = {
        "verificationMode": "algorand",
        "txId": payment["txId"],
        "network": requirement["network"],
        "receiver": requirement["receiver"],
        "amountAtomic": requirement["amountAtomic"],
        "asset": requirement["asset"],
        "resource": requirement["resource"],
        "paymentIntent": requirement["paymentIntent"],
        "policyHash": policy_hash,
        "paymentApprovalHash": payment_commitment["paymentHash"],
    }
    encoded_payment = proof_encoder(payment_proof)
    resource_result = _retry_paid_resource(
        resource_client,
        target,
        encoded_payment,
        retry_delay_seconds,
        max_retries,
    )

    if resource_result.get("status") == 402 or resource_result.get("error"):
        return {
            "status": "access_denied",
            "target": target,
            "paymentRequirements": requirement,
            "paymentCommitment": payment_commitment["commitment"],
            "canonicalPaymentCommitment": payment_commitment["canonicalPaymentCommitment"],
            "paymentApprovalHash": payment_commitment["paymentHash"],
            "paymentApproval": payment_approval,
            "payment": payment,
            "paymentProof": payment_proof,
            "resourceResult": resource_result,
        }

    return {
        "status": "access_granted",
        "target": target,
        "paymentRequirements": requirement,
        "paymentCommitment": payment_commitment["commitment"],
        "canonicalPaymentCommitment": payment_commitment["canonicalPaymentCommitment"],
        "paymentApprovalHash": payment_commitment["paymentHash"],
        "paymentApproval": payment_approval,
        "payment": payment,
        "paymentProof": payment_proof,
        "resourceResult": resource_result,
    }


def _retry_paid_resource(
    resource_client,
    target: str,
    encoded_payment: str,
    retry_delay_seconds: float,
    max_retries: int,
) -> dict[str, Any]:
    last_response: dict[str, Any] = {}
    for attempt in range(max_retries):
        last_response = resource_client.get_probe_with_payment(target, encoded_payment)
        if last_response.get("status") != 402 and not last_response.get("error"):
            return last_response
        if attempt < max_retries - 1:
            time.sleep(retry_delay_seconds)
    return last_response


def _payment_approval_hash(payment_approval: dict[str, Any]) -> str | None:
    if payment_approval.get("approvedHash"):
        return str(payment_approval["approvedHash"])

    firefly = payment_approval.get("firefly")
    if isinstance(firefly, dict) and firefly.get("approvedHash"):
        return str(firefly["approvedHash"])

    return None
