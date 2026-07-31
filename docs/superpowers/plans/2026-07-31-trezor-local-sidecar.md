# Local Trezor Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a completely isolated local proof in which a paired Trezor approves a Bitrefill purchase intent and signs the exact Base Mainnet USDC invoice payment without changing any working production flow.

**Architecture:** Add one new standalone Python project containing two entry points: a loopback-only Trezor sidecar and a local Bitrefill proof runner. The sidecar owns the Trezor MCP token, pairing, intent approval, transaction verification, state, and broadcast; the runner owns the Bitrefill credential and reuses the existing `McpBitrefillClient` through an injected sidecar treasury adapter. Neither process is imported, started, configured, or routed by the production gateway or Hermes plugin.

**Tech Stack:** Python 3.11+, `mcp>=1.27,<2`, `httpx`, `eth-account>=0.13`, `eth-utils`, `rlp`, SQLite, `http.server.ThreadingHTTPServer`, `unittest`.

## Global Constraints

- Create files only under `trezor-sidecar/` plus this implementation plan; do not modify existing application, deployment, environment, or test files.
- Do not modify `sign402-gateway/sign402_gateway/server.py` or any file under `hermes-plugins/sign402-wallet/`.
- Do not start, stop, restart, reconfigure, or deploy a production service.
- Do not open or migrate production wallet, commerce, approval, or user databases.
- Bind the sidecar only to `127.0.0.1:8111` and reject non-loopback peers.
- Keep live mode disabled unless `SIGN402_TREZOR_POC_ENABLED=1` and a positive `SIGN402_TREZOR_POC_MAX_USD` are present in both process environments.
- Support exactly one paired address at `m/44'/60'/0'/0/0`, Base Mainnet chain ID `8453`, and canonical Base USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
- Never accept a caller-supplied MCP tool name, chain, token contract, transaction object, or calldata.
- Never fall back to a managed private key, iMessage, WhatsApp, or another production approval path.
- Never log or persist Trezor tokens, Bitrefill keys, RPC credentials, signatures, raw signed transactions, calldata, recipient values, payment links, or redemption data.
- Automated tests must not call a real MCP server, invoke `buy-products`, prompt a device, or broadcast a transaction.
- Do not perform the manual live purchase during plan execution; it remains an explicit operator-only step after all automated and production regression tests pass.
- Every task uses TDD and commits only its listed files.

---

## File Structure

Create one independent project:

```text
trezor-sidecar/
├── pyproject.toml                     # Isolated package and two console entry points
├── .env.sidecar.example               # Safe sidecar-only configuration names
├── .env.runner.example                # Safe runner-only configuration names
├── README.md                          # Local setup and operator-only smoke test
├── trezor_sidecar/
│   ├── __init__.py                    # Package version only
│   ├── config.py                      # Strict split-process settings
│   ├── errors.py                      # Fixed safe error codes/messages
│   ├── models.py                      # Purchase, payment, and state types
│   ├── mcp_client.py                  # Allow-listed Trezor MCP adapter
│   ├── intent.py                      # EIP-712 intent construction and verification
│   ├── store.py                       # Separate SQLite pairing/intent/payment store
│   ├── base.py                        # Base RPC, USDC calldata, signed-tx verification
│   ├── service.py                     # Pairing, approval, signing, and broadcast orchestration
│   ├── server.py                      # Loopback authenticated HTTP API
│   ├── sidecar_client.py              # Runner-side authenticated HTTP client
│   ├── poc_runner.py                  # Bitrefill quote/approve/invoice/pay orchestration
│   └── __main__.py                    # Starts the sidecar only
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_mcp_client.py
    ├── test_intent.py
    ├── test_store.py
    ├── test_base.py
    ├── test_service.py
    ├── test_server.py
    ├── test_poc_runner.py
    └── test_isolation.py
```

The runner may import only `sign402_gateway.bitrefill_mcp.McpBitrefillClient` from the existing application. The sidecar package must not import `sign402_gateway.server`, Hermes, iMessage, WhatsApp, managed-wallet, or deployment modules.

---

### Task 1: Isolated Package and Strict Configuration

**Files:**
- Create: `trezor-sidecar/pyproject.toml`
- Create: `trezor-sidecar/trezor_sidecar/__init__.py`
- Create: `trezor-sidecar/trezor_sidecar/errors.py`
- Create: `trezor-sidecar/trezor_sidecar/config.py`
- Create: `trezor-sidecar/tests/__init__.py`
- Create: `trezor-sidecar/tests/test_config.py`

**Interfaces:**
- Produces: `SidecarSettings.from_env(env: Mapping[str, str]) -> SidecarSettings`
- Produces: `RunnerSettings.from_env(env: Mapping[str, str]) -> RunnerSettings`
- Produces: `SafeError(code: str, message: str, status: int = 400)`
- Consumes: no application runtime code.

- [ ] **Step 1: Write failing configuration tests**

```python
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from trezor_sidecar.config import RunnerSettings, SidecarSettings


class SidecarSettingsTests(TestCase):
    def test_live_mode_requires_every_secret_and_positive_cap(self):
        with self.assertRaisesRegex(ValueError, "SIGN402_TREZOR_MCP_TOKEN"):
            SidecarSettings.from_env({"SIGN402_TREZOR_POC_ENABLED": "1"})

    def test_sidecar_is_fixed_to_loopback_and_base(self):
        settings = SidecarSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_MCP_TOKEN": "mcp-secret",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "SIGN402_TREZOR_BASE_RPC_URL": "https://base.example.invalid",
            "SIGN402_TREZOR_STATE_PATH": "/tmp/trezor-poc-test.db",
        })
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8111)
        self.assertEqual(settings.chain_id, 8453)
        self.assertEqual(settings.max_usd, Decimal("2.50"))
        self.assertEqual(settings.state_path, Path("/tmp/trezor-poc-test.db"))

    def test_runner_does_not_accept_trezor_mcp_token_as_configuration(self):
        settings = RunnerSettings.from_env({
            "SIGN402_TREZOR_POC_ENABLED": "1",
            "SIGN402_TREZOR_SIDECAR_TOKEN": "sidecar-secret",
            "SIGN402_TREZOR_POC_MAX_USD": "2.50",
            "BITREFILL_API_KEY": "bitrefill-secret",
            "SIGN402_TREZOR_MCP_TOKEN": "must-not-be-copied",
        })
        self.assertFalse(hasattr(settings, "mcp_token"))
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_config -v`

Expected: import failure because `trezor_sidecar.config` does not exist.

- [ ] **Step 3: Add the isolated package metadata**

Create `pyproject.toml` with these exact runtime bounds and entry points:

```toml
[project]
name = "sign402-trezor-sidecar"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "eth-account>=0.13,<1",
  "eth-utils>=5,<6",
  "httpx>=0.27,<1",
  "mcp>=1.27,<2",
  "rlp>=4,<5",
]

[project.scripts]
sign402-trezor-sidecar = "trezor_sidecar.server:main"
sign402-trezor-poc = "trezor_sidecar.poc_runner:main"
```

- [ ] **Step 4: Implement strict settings and fixed safe errors**

Implement frozen dataclasses with these fields:

```python
@dataclass(frozen=True)
class SidecarSettings:
    enabled: bool
    mcp_token: str
    api_token: str
    max_usd: Decimal
    base_rpc_url: str
    state_path: Path
    host: str = "127.0.0.1"
    port: int = 8111
    chain_id: int = 8453
    derivation_path: str = "m/44'/60'/0'/0/0"

@dataclass(frozen=True)
class RunnerSettings:
    enabled: bool
    sidecar_token: str
    max_usd: Decimal
    bitrefill_api_key: str
    sidecar_url: str = "http://127.0.0.1:8111"
```

Require HTTPS for `SIGN402_TREZOR_BASE_RPC_URL`, reject query fragments in the fixed MCP URL, default the state path to `~/.sign402-trezor-poc/state.db`, and treat every value other than literal `1` as disabled. Disabled configuration may omit secrets but cannot execute live operations.

Implement `SafeError` without provider details:

```python
class SafeError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
```

- [ ] **Step 5: Run the configuration tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_config -v`

Expected: all configuration tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add trezor-sidecar/pyproject.toml trezor-sidecar/trezor_sidecar/__init__.py trezor-sidecar/trezor_sidecar/errors.py trezor-sidecar/trezor_sidecar/config.py trezor-sidecar/tests/__init__.py trezor-sidecar/tests/test_config.py
git commit -m "feat: scaffold isolated Trezor sidecar"
```

---

### Task 2: Allow-listed Trezor MCP Client

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/mcp_client.py`
- Create: `trezor-sidecar/tests/test_mcp_client.py`

**Interfaces:**
- Consumes: `SafeError`
- Produces: `decode_tool_result(result: Any, max_bytes: int = 65536) -> dict[str, Any]`
- Produces: `McpToolCaller(token: str, timeout_seconds: float = 120.0)`
- Produces: `TrezorMcpClient(call_tool: Callable[[str, dict[str, Any]], dict[str, Any]])`
- Produces methods: `get_base_address(path)`, `sign_typed_data(path, data)`, `sign_base_transaction(path, to, data)`, and `push_base_transaction(tx)`.

- [ ] **Step 1: Write failing allow-list and redaction tests**

```python
from unittest import TestCase

from trezor_sidecar.mcp_client import TrezorMcpClient


class RecordingCaller:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        return self.response


class TrezorMcpClientTests(TestCase):
    def test_pairing_forces_base_path_and_device_display(self):
        caller = RecordingCaller({"address": "0x1111111111111111111111111111111111111111"})
        client = TrezorMcpClient(caller)
        client.get_base_address("m/44'/60'/0'/0/0")
        self.assertEqual(caller.calls, [("trezor_get_address", {
            "coin": "base",
            "path": "m/44'/60'/0'/0/0",
            "showOnTrezor": True,
        })])

    def test_signing_forces_base_chain_and_disables_broadcast(self):
        caller = RecordingCaller({"payload": {"serializedTx": "0x02aa"}})
        client = TrezorMcpClient(caller)
        client.sign_base_transaction(
            path="m/44'/60'/0'/0/0",
            to="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            data="0xa9059cbb" + "00" * 64,
        )
        name, arguments = caller.calls[0]
        self.assertEqual(name, "trezor_send_transaction")
        self.assertEqual(arguments["coin"], "base")
        self.assertEqual(arguments["chainId"], 8453)
        self.assertEqual(arguments["value"], "0")
        self.assertIs(arguments["broadcast"], False)
```

- [ ] **Step 2: Run the MCP tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_mcp_client -v`

Expected: import failure because `trezor_sidecar.mcp_client` does not exist.

- [ ] **Step 3: Implement bounded MCP decoding and transport**

Use `mcp.client.streamable_http.streamable_http_client`, a fixed URL of `http://127.0.0.1:21340/mcp`, and an `httpx.AsyncClient` with `Authorization: Bearer <token>`. Initialize a fresh MCP session per call, confirm the requested tool exists, and decode only mapping-shaped structured content or JSON text. Convert upstream errors to `SafeError("trezor_unavailable", "Trezor Suite is unavailable.", 503)` without upstream content.

The custom representation must be exactly secret-free:

```python
def __repr__(self) -> str:
    return f"McpToolCaller(timeout_seconds={self.timeout_seconds})"
```

- [ ] **Step 4: Implement the closed Trezor adapter**

Hard-code this set:

```python
ALLOWED_TOOLS = frozenset({
    "trezor_get_address",
    "trezor_sign_typed_data",
    "trezor_send_transaction",
    "trezor_push_transaction",
})
```

`sign_base_transaction` must construct the exact arguments shown in Step 1. `push_base_transaction` must send `{"coin": "base", "tx": tx}`. Do not expose a public generic `call(name, arguments)` method on `TrezorMcpClient`.

- [ ] **Step 5: Add decoder, tool-error, oversized-response, and repr tests**

Cover structured content, JSON text, `isError`, malformed JSON, a 65,537-byte response, unavailable required tool, and assert that neither the token nor fixed URL appears in `repr(McpToolCaller("canary-secret"))`.

- [ ] **Step 6: Run the MCP tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_mcp_client -v`

Expected: all MCP tests pass with no network access.

- [ ] **Step 7: Commit Task 2**

```bash
git add trezor-sidecar/trezor_sidecar/mcp_client.py trezor-sidecar/tests/test_mcp_client.py
git commit -m "feat: add allow-listed Trezor MCP client"
```

---

### Task 3: Purchase Intent Model and EIP-712 Verification

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/models.py`
- Create: `trezor-sidecar/trezor_sidecar/intent.py`
- Create: `trezor-sidecar/tests/test_intent.py`

**Interfaces:**
- Produces: `PaymentState` enum with the states from the design.
- Produces: `PurchaseIntent`, `IntentRecord`, `PaymentRequest`, `Pairing`, and `PaymentView` frozen dataclasses.
- Produces: `build_typed_data(intent: PurchaseIntent) -> dict[str, Any]`
- Produces: `recover_intent_signer(intent: PurchaseIntent, signature: str) -> str`
- Produces: `recipient_hash(recipient: Mapping[str, Any]) -> str`

- [ ] **Step 1: Write failing deterministic intent tests**

```python
from unittest import TestCase

from trezor_sidecar.intent import build_typed_data, recipient_hash
from trezor_sidecar.models import PurchaseIntent


class PurchaseIntentTests(TestCase):
    def test_typed_data_is_bound_to_base_and_exact_purchase(self):
        intent = PurchaseIntent(
            intent_id="0x" + "11" * 32,
            product_slug="amazon-de",
            package_id="25",
            denomination="25 EUR",
            quoted_total_usd_micros=27_000_000,
            max_payment_usdc_atomic=27_100_000,
            recipient_hash="0x" + "22" * 32,
            expires_at=1_800_000_000,
        )
        typed = build_typed_data(intent)
        self.assertEqual(typed["domain"], {
            "name": "SingIt Trezor Purchase",
            "version": "1",
            "chainId": 8453,
        })
        self.assertEqual(typed["primaryType"], "PurchaseIntent")
        self.assertEqual(typed["message"]["paymentAsset"], "USDC")
        self.assertEqual(typed["message"]["paymentNetwork"], "Base Mainnet")

    def test_recipient_hash_is_order_independent_and_does_not_return_values(self):
        left = recipient_hash({"email": "buyer@example.com", "country": "DE"})
        right = recipient_hash({"country": "DE", "email": "buyer@example.com"})
        self.assertEqual(left, right)
        self.assertNotIn("buyer", left)
```

- [ ] **Step 2: Run the intent tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_intent -v`

Expected: import failure for the new model or intent modules.

- [ ] **Step 3: Implement strict domain types and validation**

Validate bytes32 values as `0x` plus 64 lowercase hex characters, EVM addresses with `eth_utils.to_checksum_address`, integer amounts as positive and non-boolean, and expiration as a positive epoch. `PurchaseIntent` fixes `payment_asset="USDC"` and `payment_network="Base Mainnet"` as properties rather than caller fields.

Define states exactly:

```python
class PaymentState(str, Enum):
    QUOTED = "QUOTED"
    DEVICE_APPROVED = "DEVICE_APPROVED"
    INVOICE_CREATED = "INVOICE_CREATED"
    TX_SIGNED = "TX_SIGNED"
    TX_BROADCAST = "TX_BROADCAST"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
```

- [ ] **Step 4: Implement canonical recipient hashing and EIP-712 data**

Canonicalize recipient mappings with sorted keys, UTF-8 JSON, compact separators, and no ASCII escaping, then return `0x` plus SHA-256 hex. Build the exact `PurchaseIntent` type listed in the design. Use `eth_account.messages.encode_typed_data(full_message=typed_data)` and `Account.recover_message(signable_message, signature=signature)` for signer recovery.

- [ ] **Step 5: Add signature recovery and invalid-field tests**

Generate a temporary test-only `Account.create()`, sign the typed data in the test, recover the signer, and assert equality. Test invalid bytes32, zero amount, expired timestamp validation input, extra recipient nesting deeper than one mapping/list level, and non-finite numeric recipient values.

- [ ] **Step 6: Run intent tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_intent -v`

Expected: all intent tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add trezor-sidecar/trezor_sidecar/models.py trezor-sidecar/trezor_sidecar/intent.py trezor-sidecar/tests/test_intent.py
git commit -m "feat: bind purchases to Trezor intents"
```

---

### Task 4: Separate State Store and Idempotency

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/store.py`
- Create: `trezor-sidecar/tests/test_store.py`

**Interfaces:**
- Consumes: `Pairing`, `PurchaseIntent`, `PaymentRequest`, `PaymentState`
- Produces: `SidecarStore(path: Path)`
- Produces methods: `save_pairing`, `get_pairing`, `insert_intent`, `approve_intent`, `get_intent`, `create_payment`, `transition_payment`, `get_payment`, `record_purchase`.

- [ ] **Step 1: Write failing state and uniqueness tests**

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trezor_sidecar.models import PaymentState, PurchaseIntent
from trezor_sidecar.store import SidecarStore


class SidecarStoreTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SidecarStore(Path(self.temporary.name) / "state.db")
        self.intent = PurchaseIntent(
            intent_id="0x" + "11" * 32,
            product_slug="test-gift",
            package_id="1",
            denomination="1 USD",
            quoted_total_usd_micros=1_000_000,
            max_payment_usdc_atomic=1_000_000,
            recipient_hash="0x" + "22" * 32,
            expires_at=1_800_000_000,
        )
        self.store.insert_intent(self.intent, created_at=1_700_000_000)
        self.store.approve_intent(
            self.intent.intent_id,
            approved_at=1_700_000_001,
        )

    def test_invoice_and_idempotency_keys_are_unique(self):
        self.store.create_payment(
            payment_id="pay-1",
            intent_id=self.intent.intent_id,
            invoice_id="invoice-1",
            idempotency_key="key-1",
            pay_to="0x1111111111111111111111111111111111111111",
            amount_atomic="1000000",
            expires_at=1_800_000_000,
        )
        repeated = self.store.create_payment(
            payment_id="pay-2",
            intent_id=self.intent.intent_id,
            invoice_id="invoice-1",
            idempotency_key="key-1",
            pay_to="0x1111111111111111111111111111111111111111",
            amount_atomic="1000000",
            expires_at=1_800_000_000,
        )
        self.assertEqual(repeated.payment_id, "pay-1")

    def test_transition_requires_expected_state(self):
        self.store.create_payment(
            payment_id="pay-1",
            intent_id=self.intent.intent_id,
            invoice_id="invoice-1",
            idempotency_key="key-1",
            pay_to="0x1111111111111111111111111111111111111111",
            amount_atomic="1000000",
            expires_at=1_800_000_000,
        )
        self.store.transition_payment(
            payment_id="pay-1",
            expected=PaymentState.INVOICE_CREATED,
            target=PaymentState.TX_SIGNED,
            updated_at=1_700_000_002,
        )
        with self.assertRaisesRegex(ValueError, "payment state changed"):
            self.store.transition_payment(
                payment_id="pay-1",
                expected=PaymentState.INVOICE_CREATED,
                target=PaymentState.TX_BROADCAST,
                updated_at=1_700_000_003,
            )
```

- [ ] **Step 2: Run store tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_store -v`

Expected: import failure because `trezor_sidecar.store` does not exist.

- [ ] **Step 3: Implement the isolated SQLite schema**

Create `pairings`, `intents`, `payments`, and `purchase_log` tables. Store no signature or raw transaction columns. Enforce `UNIQUE(intent_id)`, `UNIQUE(invoice_id)`, and `UNIQUE(idempotency_key)`. Use explicit expected-state updates:

```sql
UPDATE payments
SET state = ?, tx_hash = ?, updated_at = ?
WHERE payment_id = ? AND state = ?
```

Require exactly one changed row. Use WAL mode, foreign keys, bounded text lengths, and chmod the directory to `0700` and database to `0600` after creation.

- [ ] **Step 4: Implement safe purchase logging**

`record_purchase` accepts exactly `invoice_id`, `product_slug`, `amount`, `payment_method`, and `timestamp`. It must not accept arbitrary metadata or dictionaries.

- [ ] **Step 5: Complete tests for legal transitions and persistence**

Cover pairing replacement rejection, explicit re-pairing, intent approval, invoice uniqueness, idempotent replay, illegal transitions, reopening the database, and assert via `PRAGMA table_info` that no column includes `signature`, `recipient`, `raw`, `calldata`, `token`, or `redemption`.

- [ ] **Step 6: Run store tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_store -v`

Expected: all store tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add trezor-sidecar/trezor_sidecar/store.py trezor-sidecar/tests/test_store.py
git commit -m "feat: add isolated Trezor payment state"
```

---

### Task 5: Base RPC, USDC Calldata, and Signed Transaction Verification

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/base.py`
- Create: `trezor-sidecar/tests/test_base.py`

**Interfaces:**
- Produces constants: `BASE_CHAIN_ID`, `BASE_USDC_ADDRESS`, `EVM_DERIVATION_PATH`
- Produces: `encode_usdc_transfer(to_address: str, amount_atomic: int) -> str`
- Produces: `BaseRpcClient(url: str, timeout_seconds: float = 10.0)`
- Produces: `BaseBalances(eth_wei: int, usdc_atomic: int)`
- Produces: `verify_signed_usdc_transfer(raw_tx: str, expected_signer: str, expected_recipient: str, expected_amount_atomic: int) -> str`

- [ ] **Step 1: Write failing calldata tests**

```python
from unittest import TestCase

from trezor_sidecar.base import BASE_USDC_ADDRESS, encode_usdc_transfer


class BaseTransactionTests(TestCase):
    def test_usdc_transfer_calldata_is_exact(self):
        data = encode_usdc_transfer(
            "0x1111111111111111111111111111111111111111",
            1_250_000,
        )
        self.assertTrue(data.startswith("0xa9059cbb"))
        self.assertEqual(len(data), 2 + 8 + 64 + 64)
        self.assertEqual(int(data[-64:], 16), 1_250_000)

    def test_contract_is_canonical_base_usdc(self):
        self.assertEqual(
            BASE_USDC_ADDRESS,
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
```

- [ ] **Step 2: Run Base tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_base -v`

Expected: import failure because `trezor_sidecar.base` does not exist.

- [ ] **Step 3: Implement strict RPC reads**

Use JSON-RPC methods `eth_chainId`, `eth_getBalance`, and `eth_call` with `balanceOf(address)` against canonical USDC. Refuse redirects, responses over 64 KiB, wrong IDs, JSON-RPC errors, non-hex quantities, and any chain ID other than `8453`. Return `BaseBalances` only after the chain check.

- [ ] **Step 4: Implement EIP-1559 signed transaction decoding**

Accept only type-2 transactions whose first byte is `0x02`. Decode the remaining RLP list into the exact EIP-1559 fields:

```text
chain_id, nonce, max_priority_fee_per_gas, max_fee_per_gas,
gas_limit, destination, value, data, access_list, y_parity, r, s
```

Use `Account.recover_transaction(raw_tx)` for the signer. Reject legacy and unknown transaction types, non-empty access lists, zero gas limit, zero fees, malformed destinations, and trailing or missing fields.

- [ ] **Step 5: Implement exact post-sign verification**

`verify_signed_usdc_transfer` must verify chain ID `8453`, signer, destination equal to canonical USDC, native value zero, selector `a9059cbb`, exactly two 32-byte arguments, zero-padded recipient, exact atomic amount, positive fees, and return `0x` plus `keccak(raw_bytes).hex()`.

- [ ] **Step 6: Add locally signed fixture tests**

In tests only, use `Account.sign_transaction` with a generated key to build an EIP-1559 USDC transfer. Assert the valid transaction passes, then independently mutate chain ID, signer, contract, native value, recipient, amount, selector, and trailing calldata and assert each fails before broadcast.

- [ ] **Step 7: Run Base tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_base -v`

Expected: all Base tests pass without RPC access by injecting a fake JSON-RPC callable.

- [ ] **Step 8: Commit Task 5**

```bash
git add trezor-sidecar/trezor_sidecar/base.py trezor-sidecar/tests/test_base.py
git commit -m "feat: verify signed Base USDC payments"
```

---

### Task 6: Pairing and Purchase Intent Approval Service

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/service.py`
- Create: `trezor-sidecar/tests/test_service.py`

**Interfaces:**
- Consumes: `SidecarSettings`, `TrezorMcpClient`, `SidecarStore`, intent helpers, and Base constants.
- Produces: `TrezorSidecarService.pair(allow_repair: bool = False) -> Pairing`
- Produces: `TrezorSidecarService.approve_intent(intent: PurchaseIntent, now: int) -> PurchaseIntent`
- Later tasks add payment methods to the same service.

- [ ] **Step 1: Write failing pairing and approval tests**

```python
class TrezorSidecarServiceTests(TestCase):
    def test_pairing_requires_device_address_and_refuses_silent_change(self):
        service, store, trezor = self.make_service(
            address="0x1111111111111111111111111111111111111111"
        )
        pairing = service.pair()
        self.assertEqual(pairing.address.lower(), trezor.address.lower())
        trezor.address = "0x2222222222222222222222222222222222222222"
        with self.assertRaisesRegex(SafeError, "different Trezor"):
            service.pair()

    def test_intent_is_approved_only_for_paired_signer(self):
        service, store, trezor = self.make_service_with_test_account()
        service.pair()
        intent = self.valid_intent(max_payment_usdc_atomic=2_000_000)
        approved = service.approve_intent(intent, now=1_700_000_000)
        self.assertEqual(approved.intent_id, intent.intent_id)
        self.assertEqual(store.get_intent(intent.intent_id).state, PaymentState.DEVICE_APPROVED)
```

- [ ] **Step 2: Run service tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_service.TrezorSidecarServiceTests -v`

Expected: import or missing-method failure.

- [ ] **Step 3: Implement pairing and cap checks**

Pair only through `get_base_address(settings.derivation_path)`. Normalize the address and call `store.save_pairing`. Permit replacement only when `allow_repair=True`, making re-pairing an explicit operator action.

Before device approval, reject disabled mode, missing pairing, expired intent, `max_payment_usdc_atomic` above `settings.max_usd * 1_000_000`, reused intent ID with different content, and fixed-field mismatch.

- [ ] **Step 4: Implement Trezor typed-data approval and local verification**

Call `sign_typed_data(settings.derivation_path, build_typed_data(intent))`, extract only a closed-set signature field (`signature` at the result root or `payload.signature`), recover the signer, compare it with the paired address, and persist only the approved state and timestamp. Never persist the signature.

- [ ] **Step 5: Add rejection, expiry, cap, replay, and no-signature-persistence tests**

Assert device rejection maps to `SafeError("device_rejected", "Purchase approval was cancelled on Trezor.")`, timeout maps to `device_timeout`, mismatched signer fails, identical replay returns the existing approved intent without another MCP call, and changed content under the same intent ID fails.

- [ ] **Step 6: Run service tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_service.TrezorSidecarServiceTests -v`

Expected: all pairing and intent tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add trezor-sidecar/trezor_sidecar/service.py trezor-sidecar/tests/test_service.py
git commit -m "feat: approve purchase intents on Trezor"
```

---

### Task 7: Payment Job, Hardware Signing, Verification, and Broadcast

**Files:**
- Modify: `trezor-sidecar/trezor_sidecar/service.py`
- Modify: `trezor-sidecar/tests/test_service.py`

**Interfaces:**
- Consumes: `BaseRpcClient`, `encode_usdc_transfer`, `verify_signed_usdc_transfer`
- Produces: `TrezorSidecarService.create_payment(request: PaymentRequest, idempotency_key: str, now: int) -> PaymentView`
- Produces: `TrezorSidecarService.run_payment(payment_id: str, now: Callable[[], int]) -> PaymentView`
- Produces: `TrezorSidecarService.get_payment(payment_id: str) -> PaymentView`

- [ ] **Step 1: Write failing happy-path payment test**

```python
def test_payment_signs_verifies_then_broadcasts_once(self):
    service, store, trezor, rpc = self.make_approved_payment_service()
    payment = service.create_payment(
        self.valid_payment_request(),
        idempotency_key="pay-key-1",
        now=1_700_000_000,
    )
    completed = service.run_payment(payment.payment_id, now=lambda: 1_700_000_001)
    self.assertEqual(completed.state, PaymentState.TX_BROADCAST)
    self.assertEqual(len(trezor.sign_transaction_calls), 1)
    self.assertEqual(len(trezor.push_transaction_calls), 1)
    self.assertEqual(trezor.sign_transaction_calls[0]["broadcast"], False)
```

- [ ] **Step 2: Run the focused payment test and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_service.TrezorPaymentServiceTests -v`

Expected: missing payment methods.

- [ ] **Step 3: Implement atomic invoice binding and pre-sign checks**

`create_payment` validates the approved intent, invoice expiry, positive amount, amount at or below the intent maximum and sidecar cap, EVM destination, and unique invoice/idempotency bindings. `run_payment` acquires a non-blocking process lock so a second active device request returns `SafeError("device_busy", "Another Trezor approval is active.", 409)`.

Before MCP signing, independently fetch balances through Base RPC, require `usdc_atomic >= amount_atomic`, and require ETH greater than a constant `MIN_ETH_GAS_RESERVE_WEI = 100_000_000_000_000`.

- [ ] **Step 4: Implement sign-without-broadcast and response extraction**

Build calldata internally and call `sign_base_transaction`. Accept signed hex only from `payload.serializedTx` or `payload.signed.serializedTx`. Validate a `0x`-prefixed even-length hex string and keep it in a local variable only. Transition `INVOICE_CREATED -> TX_SIGNED` without persisting the raw transaction.

- [ ] **Step 5: Verify, push, and classify ambiguous results**

Call `verify_signed_usdc_transfer` before `push_base_transaction`. If verification fails, transition to `FAILED` and do not push. Immediately before push, recheck intent/invoice expiry. After invoking push:

- a response with `txid`, `txId`, or `hash` matching the locally calculated hash transitions to `TX_BROADCAST`;
- a different returned hash transitions to `RECONCILIATION_REQUIRED`;
- any exception or malformed response after the push invocation transitions to `RECONCILIATION_REQUIRED`;
- no code path calls push a second time automatically.

- [ ] **Step 6: Add fail-closed and replay tests**

Cover insufficient USDC, insufficient ETH, expired invoice, wrong signed chain/signer/contract/recipient/amount, device cancellation, signing timeout, push exception, mismatched hash, identical idempotent replay, changed replay, and two concurrent payment jobs. Assert signing and push call counts after every failure.

- [ ] **Step 7: Run all service tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_service -v`

Expected: all service tests pass; no test performs network I/O.

- [ ] **Step 8: Commit Task 7**

```bash
git add trezor-sidecar/trezor_sidecar/service.py trezor-sidecar/tests/test_service.py
git commit -m "feat: sign and verify Trezor USDC payments"
```

---

### Task 8: Loopback-only Authenticated Sidecar HTTP API

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/server.py`
- Create: `trezor-sidecar/trezor_sidecar/__main__.py`
- Create: `trezor-sidecar/tests/test_server.py`

**Interfaces:**
- Consumes: `SidecarSettings`, `TrezorSidecarService`, domain parsers.
- Produces: `build_server(settings, service) -> ThreadingHTTPServer`
- Produces routes from the approved design and `main()`.

- [ ] **Step 1: Write failing HTTP boundary tests**

```python
class SidecarHttpTests(TestCase):
    def test_mutation_requires_bearer_timestamp_and_idempotency_key(self):
        for headers in ({}, {"Authorization": "Bearer local-secret"}):
            response = self.request("POST", "/v1/pair", headers=headers, body=b"{}")
            self.assertIn(response.status, {401, 400})

    def test_unknown_route_never_becomes_generic_mcp_proxy(self):
        response = self.request(
            "POST",
            "/v1/tools/call",
            headers=self.valid_headers("unknown-tool"),
            body=b'{"name":"trezor_sign_message","arguments":{}}',
        )
        self.assertEqual(response.status, 404)
```

- [ ] **Step 2: Run HTTP tests and verify RED**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_server -v`

Expected: import failure because the server module does not exist.

- [ ] **Step 3: Implement bounded request parsing and authentication**

Reject non-loopback `client_address`, bodies over 65,536 bytes, invalid JSON, arrays/scalars, bearer mismatch using `hmac.compare_digest`, timestamps outside 60 seconds, missing idempotency keys, and keys outside `[A-Za-z0-9._:-]{8,128}`. Return only `{ok, code, message}` for errors.

- [ ] **Step 4: Implement exact routes**

Implement only:

- `GET /health`
- `POST /v1/pair`
- `POST /v1/purchase-intents/approve`
- `POST /v1/payments`
- `GET /v1/payments/{paymentId}`

`POST /v1/payments` creates one daemon worker thread for a newly created payment; an idempotent replay returns existing status without another thread. Long device prompts therefore do not require the caller to repeat the mutating request.

- [ ] **Step 5: Add safe health and serialization tests**

Assert `/health` returns only `ready`, `disabled`, `suite_unavailable`, or `device_unavailable`; response JSON never contains address, MCP session ID, exception text, or tokens. Assert payment responses omit signature, raw transaction, calldata, recipient data, and upstream payloads.

- [ ] **Step 6: Run HTTP tests and verify GREEN**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_server -v`

Expected: all HTTP boundary tests pass on an ephemeral test port.

- [ ] **Step 7: Commit Task 8**

```bash
git add trezor-sidecar/trezor_sidecar/server.py trezor-sidecar/trezor_sidecar/__main__.py trezor-sidecar/tests/test_server.py
git commit -m "feat: expose local Trezor sidecar API"
```

---

### Task 9: Runner-side Client and Bitrefill Proof Orchestration

**Files:**
- Create: `trezor-sidecar/trezor_sidecar/sidecar_client.py`
- Create: `trezor-sidecar/trezor_sidecar/poc_runner.py`
- Create: `trezor-sidecar/tests/test_poc_runner.py`

**Interfaces:**
- Produces: `SidecarClient.approve_intent(intent) -> dict[str, Any]`
- Produces: `SidecarClient.pay_invoice(intent_id, invoice_id, pay_to, amount_atomic, expires_at, idempotency_key) -> dict[str, Any]`
- Produces: `SidecarTreasuryClient.bind_prepared(intent_id: str, prepared: Mapping[str, Any]) -> None`
- Produces: `SidecarTreasuryClient.transfer_token_exact(token_address, to_address, amount_atomic, chain, idempotency_key) -> dict[str, Any]`
- Produces: `TrezorPocRunner.quote(*, product_id: str, package_id: str, country: str, recipient: dict[str, Any]) -> dict[str, Any]`
- Produces: `TrezorPocRunner.build_intent(quote: dict[str, Any], recipient: dict[str, Any], now: int) -> PurchaseIntent`
- Produces: `TrezorPocRunner.buy(*, quote: dict[str, Any], recipient: dict[str, Any], buyer_email: str, now: int) -> dict[str, Any]`
- Consumes: existing `sign402_gateway.bitrefill_mcp.McpBitrefillClient` without modifying it.
- Consumes: `SidecarStore` at the same isolated default path solely to finalize the proof payment and write the non-secret purchase record.

- [ ] **Step 1: Write failing orchestration-order test**

```python
class TrezorPocRunnerTests(TestCase):
    def test_invoice_is_created_only_after_device_approval(self):
        events = []
        bitrefill = FakeBitrefillClient(events)
        sidecar = FakeSidecarClient(events, approved=True)
        runner = TrezorPocRunner(bitrefill=bitrefill, sidecar=sidecar, max_usd="2.00")
        quote = runner.quote(
            product_id="test-gift",
            package_id="1",
            country="US",
            recipient={"email": "buyer@example.com"},
        )
        result = runner.buy(quote=quote, recipient={"email": "buyer@example.com"})
        self.assertEqual(events[:3], ["display-summary", "approve-intent", "prepare-purchase"])
        self.assertEqual(result["status"], "complete")

    def test_rejected_intent_never_calls_prepare_purchase(self):
        events = []
        runner = self.make_runner(events=events, approved=False)
        with self.assertRaisesRegex(SafeError, "cancelled"):
            runner.buy(quote=self.valid_quote(), recipient={"email": "buyer@example.com"})
        self.assertNotIn("prepare-purchase", events)
```

- [ ] **Step 2: Run runner tests and verify RED**

Run: `cd trezor-sidecar && PYTHONPATH=../sign402-gateway python3 -m unittest tests.test_poc_runner -v`

Expected: missing runner/client modules.

- [ ] **Step 3: Implement authenticated sidecar client**

Use fixed loopback URL, `Authorization: Bearer`, `X-Sign402-Timestamp`, and `Idempotency-Key`. Refuse redirects, responses over 64 KiB, non-object JSON, non-loopback configured URLs, and error bodies outside `{ok, code, message}`. Poll payment status with a bounded attempt count and interval; `RECONCILIATION_REQUIRED` is terminal and never resubmitted.

- [ ] **Step 4: Implement the treasury adapter expected by `McpBitrefillClient`**

`bind_prepared` stores in memory only the intent ID, invoice ID, expiration, approved payment address, and amount. `transfer_token_exact` must require:

```python
token_address.casefold() == BASE_USDC_ADDRESS.casefold()
chain == "base"
idempotency_key == f"bitrefill-pay:{invoice_id}"
to_address.casefold() == prepared_payment_address.casefold()
str(amount_atomic) == prepared_amount_atomic
```

It then calls the sidecar once and returns exactly:

```python
{"txId": tx_hash, "network": "base", "asset": "USDC", "amountAtomic": str(amount_atomic)}
```

- [ ] **Step 5: Implement quote, summary, intent approval, and purchase sequencing**

Use the existing client in this explicit order:

```python
quote = bitrefill.quote_product(
    product_id=product_id,
    package_id=package_id,
    country=country,
    recipient=recipient,
)
intent = build_purchase_intent(quote, recipient, max_usd, expires_at)
summary_sink(render_exact_summary(quote, recipient, intent))
sidecar.approve_intent(intent)
prepared = bitrefill.prepare_purchase(quote=quote, recipient=recipient, buyer_email=buyer_email)
treasury.bind_prepared(intent.intent_id, prepared)
result = bitrefill.complete_purchase(
    quote=quote,
    prepared=prepared,
    checkpoint_callback=checkpoint,
    invoice_access_token=str(prepared.get("invoiceAccessToken") or ""),
)
```

Construct `McpBitrefillClient` with `payment_method="usdc_base"` and the sidecar treasury adapter. The displayed summary must include product name/slug, package/denomination, quoted total, maximum USDC, Base Mainnet, recipient fields, and expiration. Do not call `prepare_purchase` from `quote()` or `approve()`.

- [ ] **Step 6: Implement the non-secret purchase record**

After Bitrefill reports completion, transition the existing payment from `TX_BROADCAST` to `COMPLETE` using the transaction hash returned by the sidecar. Then call `store.record_purchase` with exactly invoice ID, product slug, amount, `usdc_base`, and timestamp. The runner opens only the proof database at `~/.sign402-trezor-poc/state.db`; it never accepts a production database path. Return redemption data to stdout only for the initiating command and never log or persist it.

- [ ] **Step 7: Add invoice mismatch, cap, replay, and secret tests**

Cover invoice amount above intent max, wrong network/token/address, expired invoice, duplicate invoice, sidecar reconciliation state, polling timeout, prepare exception, and exact one-call counts. Capture logs and assert absence of the buyer email, Trezor/sidecar/Bitrefill tokens, payment link, signature, and redemption code.

- [ ] **Step 8: Implement and test the local-only CLI**

Expose exactly three subcommands:

```text
sign402-trezor-poc pair
sign402-trezor-poc intent-test
sign402-trezor-poc buy --product-id ID --package-id VALUE --country CC
```

`pair` calls the sidecar pairing route. `intent-test` signs a fixed local test intent and never constructs a Bitrefill client. `buy` obtains required recipient fields from `get_product_details`, reads each value interactively with `getpass.getpass` so it does not enter shell history, displays the exact summary, and then starts Trezor intent approval. Do not add a command that accepts recipient JSON, a private key, arbitrary calldata, token address, chain ID, destination, or amount.

Patch `input`, `getpass.getpass`, the Bitrefill client, and sidecar client in CLI tests. Assert `intent-test` has zero Bitrefill calls and that `buy` cannot call `prepare_purchase` before the sidecar returns a verified intent approval.

- [ ] **Step 9: Run runner tests and verify GREEN**

Run: `cd trezor-sidecar && PYTHONPATH=../sign402-gateway python3 -m unittest tests.test_poc_runner -v`

Expected: all runner tests pass with injected fakes and no network access.

- [ ] **Step 10: Commit Task 9**

```bash
git add trezor-sidecar/trezor_sidecar/sidecar_client.py trezor-sidecar/trezor_sidecar/poc_runner.py trezor-sidecar/tests/test_poc_runner.py
git commit -m "feat: orchestrate Trezor Bitrefill proof"
```

---

### Task 10: Secret Scan, Isolation Guard, Documentation, and Full Verification

**Files:**
- Create: `trezor-sidecar/tests/test_isolation.py`
- Create: `trezor-sidecar/.env.sidecar.example`
- Create: `trezor-sidecar/.env.runner.example`
- Create: `trezor-sidecar/README.md`

**Interfaces:**
- Consumes all completed proof components.
- Produces an operator-readable local-only setup and smoke-test procedure.
- Produces a machine-enforced import and secret boundary.

- [ ] **Step 1: Write the isolation guard test**

Scan every `trezor_sidecar/*.py` file and assert:

```python
FORBIDDEN_IMPORTS = (
    "sign402_gateway.server",
    "hermes",
    "imessage_approvals",
    "whatsapp_cloud",
    "ManagedBaseWalletService",
    "decrypt_private_key_for_future_signing",
)
```

Allow the runner's exact import of `sign402_gateway.bitrefill_mcp` and no other `sign402_gateway` import. Scan test logs and serialized fixtures for known canary secrets and recipient/redemption canaries.

- [ ] **Step 2: Run isolation test and verify RED before documentation**

Run: `cd trezor-sidecar && python3 -m unittest tests.test_isolation -v`

Expected: failure because example configuration and README boundary assertions are not yet satisfied.

- [ ] **Step 3: Add split example environments**

The sidecar example contains only:

```text
SIGN402_TREZOR_POC_ENABLED=0
SIGN402_TREZOR_MCP_TOKEN=replace-with-local-trezor-suite-token
SIGN402_TREZOR_SIDECAR_TOKEN=replace-with-independent-random-token
SIGN402_TREZOR_POC_MAX_USD=1.00
SIGN402_TREZOR_BASE_RPC_URL=https://replace-with-base-rpc.example
SIGN402_TREZOR_STATE_PATH=/Users/replace-with-user/.sign402-trezor-poc/state.db
```

The runner example contains only:

```text
SIGN402_TREZOR_POC_ENABLED=0
SIGN402_TREZOR_SIDECAR_TOKEN=replace-with-the-same-local-token
SIGN402_TREZOR_POC_MAX_USD=1.00
BITREFILL_API_KEY=replace-with-test-operator-key
```

Examples remain disabled. README instructions require copying them outside the repository and `chmod 600` before inserting real values.

- [ ] **Step 4: Document setup without touching production**

Document creating a dedicated venv inside `trezor-sidecar/.venv`, installing the new package plus the existing gateway package in editable mode, enabling Trezor Suite MCP, starting the sidecar and runner in separate terminals, pairing, a no-purchase intent signature test, and the operator-only live sequence. Put a boxed warning before live steps: production services must not be restarted and the live command must display the exact purchase summary before device approval.

- [ ] **Step 5: Run the entire new-project suite**

Run:

```bash
cd trezor-sidecar
PYTHONPATH=../sign402-gateway python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all new tests pass; zero network, MCP, hardware, invoice-creation, or broadcast calls occur.

- [ ] **Step 6: Run production gateway regression suites**

Run:

```bash
cd sign402-gateway
python3 -m unittest \
  tests.test_user_wallets \
  tests.test_bitrefill_mcp \
  tests.test_bitrefill_runner \
  tests.test_imessage_approvals \
  tests.test_whatsapp_cloud \
  tests.test_gateway_server -v
```

Expected: all selected production regression tests pass unchanged.

- [ ] **Step 7: Run Hermes wallet plugin regressions**

Run:

```bash
python3 -m unittest discover \
  -s hermes-plugins/sign402-wallet/tests \
  -p 'test_*.py' -v
```

Expected: all Hermes wallet plugin tests pass unchanged.

- [ ] **Step 8: Prove the diff remains outside production**

Run:

```bash
git diff --name-only 6d37401..HEAD | \
  rg -v '^(trezor-sidecar/|docs/superpowers/plans/2026-07-31-trezor-local-sidecar.md$)'
```

Expected: no output. Any output is a release blocker and must be reverted before completion.

- [ ] **Step 9: Run repository secret and formatting checks**

Run:

```bash
git diff --check 6d37401..HEAD
rg --pcre2 -n '(BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|SIGN402_TREZOR_MCP_TOKEN=(?!replace-with-)[^[:space:]]+|BITREFILL_API_KEY=(?!replace-with-)[^[:space:]]+)' trezor-sidecar
```

Expected: `git diff --check` succeeds and the secret scan has no matches.

- [ ] **Step 10: Commit Task 10**

```bash
git add trezor-sidecar/tests/test_isolation.py trezor-sidecar/.env.sidecar.example trezor-sidecar/.env.runner.example trezor-sidecar/README.md
git commit -m "docs: add safe Trezor proof runbook"
```

---

## Execution Stop Point

After Task 10, stop. Do not run a live Bitrefill purchase, do not deploy, and do not connect Hermes or a production user. Report automated and regression evidence to the operator. The operator separately decides whether to perform the documented low-value manual smoke test with Trezor physically present.
