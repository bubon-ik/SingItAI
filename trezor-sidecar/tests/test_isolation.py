import ast
import tempfile
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

LOCAL_AGENT_ENV_PREFIX = """# This file is for a separate local Hermes test instance only.
# Keep the real copy outside the repository with mode 0600.
SIGN402_TREZOR_LOCAL_AGENT_ENABLED=0
SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED=0
SIGN402_TREZOR_POC_ENABLED=0
"""

EXPECTED_GATEWAY_IMPORT = (
    "poc_runner.py",
    "ImportFrom",
    "sign402_gateway.bitrefill_mcp",
    "McpBitrefillClient",
    None,
)

ARTIFACT_SUFFIXES = frozenset(
    {".json", ".jsonl", ".log", ".ndjson", ".out", ".trace", ".txt", ".yaml", ".yml"}
)

CANARIES = (
    "buyer@example.com",
    "invoice-access-canary",
    "REDEMPTION-CANARY",
    "sidecar-token-canary",
    "api-key-canary",
    "signature-canary",
    "raw-transaction-canary",
    "payment-link-canary",
)


def _gateway_imports(path: Path, source: str):
    imports = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sign402_gateway"):
                    imports.append((path.name, "Import", alias.name, None, alias.asname))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "sign402_gateway"
        ):
            for alias in node.names:
                imports.append(
                    (path.name, "ImportFrom", node.module, alias.name, alias.asname)
                )
    return imports


def _artifact_canary_hits(root: Path):
    hits = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        serialized = path.read_text(encoding="utf-8")
        hits.extend((path.relative_to(root), canary) for canary in CANARIES if canary in serialized)
    return hits


class IsolationBoundaryTests(unittest.TestCase):
    def test_sidecar_imports_only_the_narrow_bitrefill_gateway_module(self):
        # Break caught: the local proof becomes coupled to a production entrypoint.
        gateway_imports = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            gateway_imports.extend(_gateway_imports(path, source))
            for forbidden in FORBIDDEN_IMPORTS:
                self.assertNotIn(forbidden, source, f"{path.name} crosses boundary via {forbidden}")

        self.assertEqual(gateway_imports, [EXPECTED_GATEWAY_IMPORT])

    def test_gateway_import_shape_guard_distinguishes_aliases_and_extra_symbols(self):
        # Break caught: a relaxed guard accepts a module alias or a second production symbol.
        aliased = _gateway_imports(
            Path("poc_runner.py"),
            "from sign402_gateway.bitrefill_mcp import McpBitrefillClient as Client\n",
        )
        extra = _gateway_imports(
            Path("poc_runner.py"),
            "from sign402_gateway.bitrefill_mcp import McpBitrefillClient, Other\n",
        )
        self.assertNotEqual(aliased, [EXPECTED_GATEWAY_IMPORT])
        self.assertNotEqual(extra, [EXPECTED_GATEWAY_IMPORT])

    def test_example_environments_are_split_exactly_and_disabled(self):
        # Break caught: credentials or production-enabling defaults cross the process boundary.
        self.assertEqual((ROOT / ".env.sidecar.example").read_text(encoding="utf-8"), SIDECAR_ENV)
        self.assertEqual((ROOT / ".env.runner.example").read_text(encoding="utf-8"), RUNNER_ENV)
        local_agent = (ROOT / ".env.local-agent.example").read_text(encoding="utf-8")
        self.assertTrue(local_agent.startswith(LOCAL_AGENT_ENV_PREFIX))
        self.assertNotIn("SIGN402_TREZOR_MCP_TOKEN", local_agent)
        self.assertNotIn("SIGN402_TREZOR_BASE_RPC_URL", local_agent)

    def test_local_agent_plugin_is_separate_and_never_names_production_routes(self):
        plugin = ROOT / "hermes-local-plugin"
        manifest = (plugin / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: sign402-trezor-local", manifest)
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(plugin.glob("*.py"))
        )
        for forbidden in (
            "sign402_gateway.server",
            "ManagedBaseWalletService",
            "imessage_approvals",
            "whatsapp_cloud",
            "/agent/buy-wallet-bitrefill",
        ):
            self.assertNotIn(forbidden, combined)

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
        self.assertIn(
            "SIGN402_TREZOR_STATE_PATH=${HOME}/.sign402-trezor-poc/state.db",
            readme,
        )
        self.assertNotIn("an absolute proof-only state path", readme)

        safe_section = readme.split(safe_marker, 1)[1].split(live_marker, 1)[0]
        self.assertIn("sign402-trezor-poc pair", safe_section)
        self.assertIn("sign402-trezor-poc intent-test", safe_section)
        self.assertNotIn("sign402-trezor-poc buy", safe_section)
        local_marker = "## Separate local Hermes instance"
        self.assertGreater(readme.index(local_marker), readme.index(warning))
        local_section = readme.split(local_marker, 1)[1]
        self.assertIn("SIGN402_TREZOR_LOCAL_AGENT_ENABLED=0", local_section)
        self.assertIn("SIGN402_TREZOR_LOCAL_PURCHASES_ENABLED=0", local_section)
        self.assertIn("SIGN402_TREZOR_LOCAL_AGENT_HOME", local_section)
        self.assertIn("never writes to the working `~/.hermes`", local_section)
        self.assertIn("/trezor_prepare", local_section)
        self.assertIn("/trezor_confirm", local_section)

    def test_serialized_test_artifacts_exclude_secret_and_delivery_canaries(self):
        # Break caught: a generated fixture or captured log persists bearer or delivery value.
        self.assertEqual(_artifact_canary_hits(ROOT), [])

    def test_artifact_guard_scans_nested_non_source_artifacts_for_every_real_canary(self):
        # Break caught: a new artifact directory or a real runner canary escapes the scan.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "nested" / "serialized" / "runner.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("\n".join(CANARIES), encoding="utf-8")
            self.assertEqual(
                {canary for _, canary in _artifact_canary_hits(root)},
                set(CANARIES),
            )


if __name__ == "__main__":
    unittest.main()
