import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BitrefillMcpConfigTests(unittest.TestCase):
    def test_live_launch_configuration_uses_only_mcp_endpoint(self):
        paths = (
            PROJECT_ROOT / ".env.wallet-bitrefill.example",
            PROJECT_ROOT / "scripts" / "run-wallet-bitrefill.sh",
            PROJECT_ROOT / "sign402_gateway" / "server.py",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

        self.assertIn("SIGN402_BITREFILL_MCP_URL", text)
        self.assertNotIn("SIGN402_BITREFILL_BASE_URL", text)
        self.assertNotIn("api.bitrefill.com/v2", text)
        self.assertNotIn("SIGN402_BITREFILL_REFUND_ADDRESS", text)


if __name__ == "__main__":
    unittest.main()
