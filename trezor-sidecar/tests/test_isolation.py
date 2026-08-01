import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "trezor_sidecar"

FORBIDDEN_IMPORTS = (
    "sign402_gateway.server",
    "hermes",
    "imessage_approvals",
    "whatsapp_cloud",
    "ManagedBaseWalletService",
    "decrypt_private_key_for_future_signing",
)

SIDECAR_ENV = """SIGN402_TREZOR_POC_ENABLED=0
SIGN402_TREZOR_MCP_TOKEN=replace-with-local-trezor-suite-token
SIGN402_TREZOR_SIDECAR_TOKEN=replace-with-independent-random-token
SIGN402_TREZOR_POC_MAX_USD=1.00
SIGN402_TREZOR_BASE_RPC_URL=https://replace-with-base-rpc.example
SIGN402_TREZOR_STATE_PATH=/Users/replace-with-user/.sign402-trezor-poc/state.db
"""

RUNNER_ENV = """SIGN402_TREZOR_POC_ENABLED=0
SIGN402_TREZOR_SIDECAR_TOKEN=replace-with-the-same-local-token
SIGN402_TREZOR_POC_MAX_USD=1.00
BITREFILL_API_KEY=replace-with-test-operator-key
"""


class IsolationBoundaryTests(unittest.TestCase):
    def test_sidecar_imports_only_the_narrow_bitrefill_gateway_module(self):
        # Break caught: the local proof becomes coupled to a production entrypoint.
        gateway_imports = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    gateway_imports.extend(
                        alias.name for alias in node.names if alias.name.startswith("sign402_gateway")
                    )
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "sign402_gateway"
                ):
                    gateway_imports.append(node.module or "")
            for forbidden in FORBIDDEN_IMPORTS:
                self.assertNotIn(forbidden, source, f"{path.name} crosses boundary via {forbidden}")

        self.assertEqual(gateway_imports, ["sign402_gateway.bitrefill_mcp"])

    def test_example_environments_are_split_exactly_and_disabled(self):
        # Break caught: credentials or production-enabling defaults cross the process boundary.
        self.assertEqual((ROOT / ".env.sidecar.example").read_text(encoding="utf-8"), SIDECAR_ENV)
        self.assertEqual((ROOT / ".env.runner.example").read_text(encoding="utf-8"), RUNNER_ENV)

    def test_runbook_keeps_no_purchase_steps_before_operator_only_live_steps(self):
        # Break caught: an operator can mistake a live purchase command for a safe smoke test.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        safe_marker = "## Safe local proof (no purchase)"
        live_marker = "## Operator-only live purchase"
        warning = "PRODUCTION SERVICES MUST NOT BE RESTARTED"
        self.assertLess(readme.index(safe_marker), readme.index(live_marker))
        self.assertLess(readme.index(live_marker), readme.index(warning))
        self.assertIn("chmod 600", readme)
        self.assertIn("outside this repository", readme)
        self.assertIn("exact purchase summary", readme)
        self.assertIn("before device approval", readme)

        safe_section = readme.split(safe_marker, 1)[1].split(live_marker, 1)[0]
        self.assertIn("sign402-trezor-poc pair", safe_section)
        self.assertIn("sign402-trezor-poc intent-test", safe_section)
        self.assertNotIn("sign402-trezor-poc buy", safe_section)

    def test_serialized_test_artifacts_exclude_secret_and_delivery_canaries(self):
        # Break caught: a generated fixture or captured log persists bearer or delivery value.
        canaries = (
            "sidecar-token-canary",
            "invoice-access-canary",
            "api-key-canary",
            "buyer@example.com",
            "REDEMPTION-CANARY-DO-NOT-STORE",
        )
        artifact_paths = []
        for pattern in ("*.log", "*.json", "*.jsonl"):
            artifact_paths.extend((ROOT / "tests").rglob(pattern))
        for path in sorted(set(artifact_paths)):
            serialized = path.read_text(encoding="utf-8")
            for canary in canaries:
                self.assertNotIn(canary, serialized, f"{path} persisted {canary}")


if __name__ == "__main__":
    unittest.main()
