# Bankr LLM Key Purchase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated Telegram user create a personal Bankr LLM API key, fund Bankr credits with SINGIT from their managed Sign402 wallet after iMessage approval, and inspect the resulting credit balance.

**Architecture:** Add a focused `bankr_llm_purchase.py` module containing Bankr/Privy HTTP access, encrypted SQLite persistence, and the purchase state machine. Keep `server.py` as a thin HTTP/composition layer and extend the existing Hermes plugin with trusted Telegram-only commands; reuse the existing wallet, real-rate pricing, spending-limit, token-transfer, and iMessage approval components.

**Tech Stack:** Python 3.11+, `urllib.request`, SQLite, `cryptography.Fernet`, existing Sign402 gateway services, Hermes Python plugin, Bankr REST API, Privy email OTP.

---

## File Structure

- Create `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`: Bankr HTTP client, encrypted store, purchase service, validation, redaction, and response formatting.
- Create `sign402-gateway/tests/test_bankr_llm_purchase.py`: unit and service-level tests with fake Bankr, wallet, pricing, approval, transfer, and clock dependencies.
- Modify `sign402-gateway/sign402_gateway/server.py`: authenticated routes, dependency construction, spending-limit callbacks, and health endpoint registration.
- Modify `sign402-gateway/tests/test_gateway_server.py`: route/authentication/composition regression tests.
- Modify `hermes-plugins/sign402-wallet/client.py`: typed gateway calls for start, terms, OTP verification, and credits.
- Modify `hermes-plugins/sign402-wallet/tests/test_client.py`: request path, token, timeout, and safe-error tests.
- Modify `hermes-plugins/sign402-wallet/__init__.py`: Telegram handlers, parser, menu, and registration.
- Modify `hermes-plugins/sign402-wallet/identity.py`: allow trusted identity capture for the new commands.
- Modify `hermes-plugins/sign402-wallet/tests/test_plugin.py`: command behavior and secret-isolation tests.
- Modify `hermes-plugins/sign402-wallet/tests/test_identity.py`: trusted command capture tests.
- Modify `sign402-gateway/.env.example`: documented Bankr URL, store path, timeout, and OTP controls.

### Task 1: Bankr and Privy HTTP Client

**Files:**
- Create: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Create: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write failing request-shape and redaction tests**

Add tests using a fake opener that records `urllib.request.Request` objects:

```python
class BankrIdentityClientTests(unittest.TestCase):
    def test_send_otp_uses_privy_configuration(self):
        opener = QueueOpener(
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({}, status=200),
        )
        client = BankrIdentityClient(opener=opener)

        client.send_otp("user@example.com")

        self.assertEqual(opener.requests[0].full_url, "https://api.bankr.bot/cli/config")
        self.assertEqual(
            opener.requests[1].full_url,
            "https://auth.privy.io/api/v1/passwordless/init",
        )
        self.assertEqual(
            json.loads(opener.requests[1].data),
            {"email": "user@example.com", "type": "email"},
        )
        self.assertEqual(opener.requests[1].headers["Privy-app-id"], "app-1")

    def test_verify_and_create_key_uses_minimum_capabilities(self):
        opener = QueueOpener(
            json_response({"privyAppId": "app-1", "privyClientId": "client-1"}),
            json_response({"identity_token": "identity-secret"}),
            json_response({
                "evmAddress": "0x1111111111111111111111111111111111111111",
                "hasAcceptedTerms": True,
                "isNewUser": False,
            }),
            json_response({
                "apiKey": "bk_secret",
                "name": "Sign402-123",
                "llmGatewayEnabled": True,
            }),
        )
        client = BankrIdentityClient(opener=opener)

        result = client.verify_and_create_key(
            email="user@example.com",
            code="123456",
            key_name="Sign402-123",
            accept_terms=False,
        )

        key_request = opener.requests[-1]
        self.assertEqual(key_request.full_url, "https://api.bankr.bot/api-keys")
        self.assertEqual(
            json.loads(key_request.data),
            {
                "name": "Sign402-123",
                "walletApiEnabled": True,
                "agentApiEnabled": False,
                "readOnly": False,
                "tokenLaunchApiEnabled": False,
                "llmGatewayEnabled": True,
                "allowedIps": [],
                "allowedRecipients": {},
            },
        )
        self.assertEqual(result["apiKey"], "bk_secret")

    def test_http_error_never_contains_bankr_response_body(self):
        client = BankrIdentityClient(
            opener=QueueOpener(json_response({"secret": "do-not-leak"}, status=500))
        )
        with self.assertRaisesRegex(BankrLlmError, "Bankr authentication is unavailable"):
            client.send_otp("user@example.com")
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase.BankrIdentityClientTests -v
```

Expected: import failure because `bankr_llm_purchase.py` does not exist.

- [ ] **Step 3: Implement the HTTP client**

Define these public interfaces in `bankr_llm_purchase.py`:

```python
class BankrLlmError(RuntimeError):
    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


class BankrIdentityClient:
    def __init__(
        self,
        *,
        api_url: str = "https://api.bankr.bot",
        llm_url: str = "https://llm.bankr.bot",
        opener=urlopen,
        timeout: float = 20.0,
    ): ...

    def send_otp(self, email: str) -> None: ...

    def verify_and_create_key(
        self,
        *,
        email: str,
        code: str,
        key_name: str,
        accept_terms: bool,
    ) -> dict[str, Any]: ...

    def accept_terms(self, identity_token: str) -> None: ...

    def top_up(
        self,
        *,
        api_key: str,
        amount_usd: str,
        source_token: str,
        chain: str = "base",
    ) -> dict[str, Any]: ...

    def credits(self, *, api_key: str) -> dict[str, Any]: ...
```

Use:

- `GET /cli/config`;
- Privy `POST /api/v1/passwordless/init`;
- Privy `POST /api/v1/passwordless/authenticate`;
- Bankr `POST /cli/generate-wallet`;
- Bankr `POST /user/accept-terms` only when `accept_terms` is true and the wallet reports `hasAcceptedTerms == false`;
- Bankr `POST /api-keys`;
- Bankr `POST /llm/credits/topup` with `X-API-Key`;
- Bankr LLM `GET /v1/credits` with `X-API-Key`.

Validate emails with `^[^\s@]+@[^\s@]+\.[^\s@]+$`, OTPs with `^\d{6}$`, EVM
addresses with `^0x[0-9a-fA-F]{40}$`, and keys with prefix `bk_`. Never include
raw external response bodies in raised errors.

- [ ] **Step 4: Run the focused tests**

Run the Task 1 command again.

Expected: all `BankrIdentityClientTests` pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bankr_llm_purchase.py sign402-gateway/tests/test_bankr_llm_purchase.py
git commit -m "Add Bankr LLM identity client"
```

### Task 2: Encrypted Purchase Store

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Modify: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write failing encryption and transition tests**

```python
class BankrLlmStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode("ascii")
        self.store = BankrLlmStore(
            Path(self.tempdir.name) / "bankr-llm.db",
            master_key=self.key,
        )

    def test_api_key_is_encrypted_at_rest(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_OTP",
            expires_at=2000,
        )
        self.store.save_bankr_identity(
            purchase["purchaseId"],
            bankr_wallet_address="0x1111111111111111111111111111111111111111",
            api_key="bk_secret",
        )

        raw = Path(self.store.path).read_bytes()
        self.assertNotIn(b"bk_secret", raw)
        loaded = self.store.get_active_purchase("123")
        self.assertEqual(self.store.decrypt_api_key(loaded), "bk_secret")
        self.assertEqual(loaded["apiKeyFingerprint"], hashlib.sha256(b"bk_secret").hexdigest()[:12])

    def test_compare_and_set_rejects_duplicate_transfer_transition(self):
        purchase = self.store.create_purchase(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
            state="AWAITING_IMESSAGE_APPROVAL",
            expires_at=2000,
        )
        self.assertTrue(
            self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
            )
        )
        self.assertFalse(
            self.store.transition(
                purchase["purchaseId"],
                expected_state="AWAITING_IMESSAGE_APPROVAL",
                new_state="TRANSFERRING_SINGIT",
            )
        )
```

- [ ] **Step 2: Run the store tests and verify failure**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase.BankrLlmStoreTests -v
```

Expected: `BankrLlmStore` is missing.

- [ ] **Step 3: Implement the SQLite store**

Add:

```python
DEFAULT_BANKR_LLM_STORE_PATH = Path.home() / ".sign402" / "bankr-llm.db"


class BankrLlmStore:
    def __init__(self, path: Path, *, master_key: str): ...
    def create_purchase(..., state: str, expires_at: int) -> dict[str, Any]: ...
    def get_active_purchase(self, telegram_user_id: str) -> dict[str, Any] | None: ...
    def get_purchase(self, purchase_id: str) -> dict[str, Any] | None: ...
    def record_terms_acceptance(self, telegram_user_id: str, *, accepted_at: int) -> None: ...
    def has_accepted_terms(self, telegram_user_id: str) -> bool: ...
    def save_bankr_identity(self, purchase_id: str, *, bankr_wallet_address: str, api_key: str) -> None: ...
    def decrypt_api_key(self, purchase: Mapping[str, Any]) -> str: ...
    def transition(self, purchase_id: str, *, expected_state: str, new_state: str, fields: Mapping[str, Any] | None = None) -> bool: ...
```

Create `bankr_llm_purchases`, `bankr_llm_users`, and `bankr_llm_audit` tables.
Set the directory to `0700` and database to `0600`. Encrypt the API key with
Fernet, store only a 12-character SHA-256 fingerprint beside it, and implement
all money-moving state changes with SQL `UPDATE ... WHERE state = ?`.

- [ ] **Step 4: Run store tests**

Run the Task 2 command again.

Expected: all `BankrLlmStoreTests` pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bankr_llm_purchase.py sign402-gateway/tests/test_bankr_llm_purchase.py
git commit -m "Store Bankr LLM purchases securely"
```

### Task 3: Purchase State Machine Through OTP

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Modify: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write failing start, terms, and OTP tests**

```python
class BankrLlmPurchaseServiceAuthTests(unittest.TestCase):
    def test_start_requires_terms_before_sending_otp(self):
        result = self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.assertEqual(result["state"], "AWAITING_TERMS")
        self.assertEqual(self.bankr.sent_otps, [])
        self.assertIn("/llm_terms accept", result["telegramText"])

    def test_accept_terms_sends_otp(self):
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        result = self.service.accept_terms("123")
        self.assertEqual(result["state"], "AWAITING_OTP")
        self.assertEqual(self.bankr.sent_otps, ["user@example.com"])

    def test_verify_creates_one_key_and_requests_approval(self):
        self.approval.result = {"ok": False, "status": "rejected"}
        self.service.start(
            telegram_user_id="123",
            email="user@example.com",
            amount_usd="10",
        )
        self.service.accept_terms("123")
        result = self.service.verify_otp(telegram_user_id="123", code="123456")
        repeated = self.service.verify_otp(telegram_user_id="123", code="123456")

        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(repeated["purchaseId"], result["purchaseId"])
        self.assertEqual(self.bankr.created_key_count, 1)
        self.assertEqual(self.approval.calls[0]["action_type"], "sign402_bankr_llm")
```

- [ ] **Step 2: Run tests and verify failure**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase.BankrLlmPurchaseServiceAuthTests -v
```

Expected: `BankrLlmPurchaseService` is missing.

- [ ] **Step 3: Implement validation and auth states**

Add:

```python
class BankrLlmPurchaseService:
    def __init__(
        self,
        *,
        store: BankrLlmStore,
        bankr: BankrIdentityClient,
        wallet_service,
        pricer,
        approval_service,
        transfer_client,
        enforce_spend: Callable[[str, dict[str, Any]], None],
        record_spend: Callable[[str, dict[str, Any], dict[str, Any]], None],
        singit_token_address: str,
        now: Callable[[], float] = time.time,
        otp_ttl_seconds: int = 600,
        max_otp_attempts: int = 3,
    ): ...

    def start(self, *, telegram_user_id: str, email: str, amount_usd: str) -> dict[str, Any]: ...
    def accept_terms(self, telegram_user_id: str) -> dict[str, Any]: ...
    def verify_otp(self, *, telegram_user_id: str, code: str) -> dict[str, Any]: ...
    def credits(self, telegram_user_id: str) -> dict[str, Any]: ...
```

Rules:

- amount must be `Decimal`, integral cents or finer, and between `1` and `1000`;
- only one active purchase per Telegram user;
- OTP expires after 10 minutes and permits three invalid attempts;
- `verify_otp` calls `wallet_service.wallet_status`, Bankr auth/key creation,
  `pricer.price_for_usdc`, the spend-limit callback, and
  `approval_service.request_hash_approval`;
- the commitment is canonical JSON containing purchase ID, USD amount,
  SINGIT atomic maximum, source wallet, Bankr wallet, key fingerprint, and
  expiry;
- only the hash and safe context lines go to iMessage;
- `request_hash_approval` is a blocking call; after it returns, an approved
  purchase continues through `resume`, while rejection or expiration becomes a
  terminal safe response;
- no response before completion contains the full `bk_...` key.

- [ ] **Step 4: Run auth-state tests**

Run the Task 3 command again.

Expected: all `BankrLlmPurchaseServiceAuthTests` pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bankr_llm_purchase.py sign402-gateway/tests/test_bankr_llm_purchase.py
git commit -m "Add Bankr LLM onboarding state machine"
```

### Task 4: Approved Transfer, Top-Up, and Reconciliation

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bankr_llm_purchase.py`
- Modify: `sign402-gateway/tests/test_bankr_llm_purchase.py`

- [ ] **Step 1: Write failing payment and idempotency tests**

```python
class BankrLlmPurchasePaymentTests(unittest.TestCase):
    def test_approved_purchase_transfers_user_singit_then_tops_up(self):
        result = self.complete_purchase(approval={"ok": True, "status": "approved"})

        self.assertEqual(self.transfer.calls[0]["to_address"], BANKR_WALLET)
        self.assertEqual(self.transfer.calls[0]["token_address"], SINGIT_TOKEN)
        self.assertEqual(self.bankr.topups[0]["amount_usd"], "10")
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["apiKey"], "bk_secret")
        self.assertIn("10", result["telegramText"])

    def test_repeated_completion_does_not_transfer_twice_or_reveal_key(self):
        first = self.complete_purchase(approval={"ok": True, "status": "approved"})
        second = self.service.resume(first["purchaseId"])

        self.assertEqual(len(self.transfer.calls), 1)
        self.assertNotIn("apiKey", second)

    def test_topup_timeout_after_transfer_requires_reconciliation(self):
        self.bankr.topup_error = TimeoutError()
        result = self.complete_purchase(approval={"ok": True, "status": "approved"})

        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.service.resume(result["purchaseId"])
        self.assertEqual(len(self.transfer.calls), 1)
```

- [ ] **Step 2: Run payment tests and verify failure**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase.BankrLlmPurchasePaymentTests -v
```

Expected: payment continuation methods are missing.

- [ ] **Step 3: Implement the irreversible half of the state machine**

Add:

```python
def resume(self, purchase_id: str) -> dict[str, Any]: ...
def reconcile(self, purchase_id: str) -> dict[str, Any]: ...
```

`resume` must:

1. return a safe summary for terminal states;
2. use compare-and-set before `TRANSFERRING_SINGIT`;
3. re-price and reject when the fresh required amount exceeds the approved
   maximum;
4. re-run spending-limit and managed-wallet balance checks;
5. decrypt the managed wallet key only immediately before transfer;
6. call `UserWalletTokenTransferClient.transfer_token`;
7. persist the Base transaction before calling Bankr;
8. call Bankr top-up with the encrypted stored Bankr key and SINGIT source;
9. record the USD-denominated spend after confirmed top-up;
10. return the full API key only on the first `COMPLETE` transition.

`reconcile` must fetch Bankr credits first. If the expected credits are present,
mark complete. Otherwise retry only Bankr top-up from the already funded Bankr
wallet. It must never call the Sign402 transfer client.

- [ ] **Step 4: Run payment tests and the complete module**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase -v
```

Expected: all Bankr LLM tests pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/bankr_llm_purchase.py sign402-gateway/tests/test_bankr_llm_purchase.py
git commit -m "Fund Bankr LLM credits from user wallets"
```

### Task 5: Authenticated Gateway Routes and Composition

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/.env.example`

- [ ] **Step 1: Write failing route and auth tests**

Add server tests proving:

```python
def test_llm_key_start_requires_matching_user_token(self):
    status, body = post_json(
        self.server,
        "/agent/llm-key/start",
        {"telegramUserId": "123", "amountUsd": "10", "email": "user@example.com"},
        headers={"Authorization": f"Bearer {self.operator_token}"},
    )
    self.assertEqual(status, 401)

def test_llm_key_start_dispatches_authenticated_user(self):
    status, body = post_json(
        self.server,
        "/agent/llm-key/start",
        {"telegramUserId": "123", "amountUsd": "10", "email": "user@example.com"},
        headers={
            "Authorization": f"Bearer {self.operator_token}",
            "X-Sign402-User-Token": self.user_token,
        },
    )
    self.assertEqual(status, 200)
    self.assertEqual(self.llm_service.start_calls[0]["telegram_user_id"], "123")
```

Cover all four routes and assert `/health` lists them.

- [ ] **Step 2: Run focused gateway tests and verify failure**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest \
  tests.test_gateway_server.GatewayServerTests.test_llm_key_start_requires_matching_user_token \
  tests.test_gateway_server.GatewayServerTests.test_llm_key_start_dispatches_authenticated_user \
  -v
```

Expected: routes return `404`.

- [ ] **Step 3: Add routes and build the service**

Register:

```text
POST /agent/llm-key/start
POST /agent/llm-key/accept-terms
POST /agent/llm-key/verify
POST /agent/llm-credits
```

Each handler must call the same `_require_authenticated_user(...)` mechanism
used by wallet reads and Bitrefill, ignore any body user ID that does not match
the resolved token owner, and return `BankrLlmError.user_message` without raw
Bankr details.

Build the service with:

```python
bankr_llm_service = build_bankr_llm_purchase_service_from_env(
    env=dict(os.environ),
    wallet_service=user_wallet_service,
    pricer=real_rate_pricer,
    approval_service=imessage_approval_service,
    transfer_client=UserWalletTokenTransferClient(cdp_x402_service_dir),
    enforce_spend=lambda user_id, requirement: _enforce_user_wallet_spend_limits(
        server_ref(), user_id, requirement
    ),
    record_spend=record_bankr_llm_spend,
)
```

Avoid the circular `server_ref()` by constructing callbacks after the server
instance exists or by extracting the limit helpers into callables over
`UserSpendLimitStore` and operator limits.

Document:

```dotenv
SIGN402_BANKR_API_URL=https://api.bankr.bot
SIGN402_BANKR_LLM_URL=https://llm.bankr.bot
SIGN402_BANKR_LLM_STORE_PATH=/home/hermes/.sign402/bankr-llm.db
SIGN402_BANKR_HTTP_TIMEOUT_SECONDS=20
SIGN402_BANKR_OTP_TTL_SECONDS=600
SIGN402_BANKR_MAX_OTP_ATTEMPTS=3
```

- [ ] **Step 4: Run gateway and Bankr tests**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest tests.test_bankr_llm_purchase tests.test_gateway_server -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/.env.example
git commit -m "Expose authenticated Bankr LLM purchase routes"
```

### Task 6: Hermes Gateway Client

**Files:**
- Modify: `hermes-plugins/sign402-wallet/client.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_client.py`

- [ ] **Step 1: Write failing client tests**

```python
def test_execute_llm_start_uses_user_token_and_purchase_timeout(self):
    client, opener = make_client()
    result = client.execute_llm(
        "start",
        TelegramIdentity(user_id="123"),
        payload={"amountUsd": "10", "email": "user@example.com"},
        user_access_token="user-token",
    )
    request, timeout = opener.calls[0]
    self.assertEqual(request.full_url, "http://127.0.0.1:8099/agent/llm-key/start")
    self.assertEqual(request.headers["X-sign402-user-token"], "user-token")
    self.assertEqual(timeout, client.purchase_timeout)
    self.assertEqual(result["state"], "AWAITING_TERMS")
```

Also test `accept-terms`, `verify`, and `credits`; assert `_safe_http_error_message`
returns only the gateway `telegramText` for these operations.

- [ ] **Step 2: Run client tests and verify failure**

```bash
python -m unittest discover -s hermes-plugins/sign402-wallet/tests -p 'test_client.py' -v
```

Expected: `execute_llm` is missing.

- [ ] **Step 3: Implement the client method**

Add:

```python
_LLM_OPERATION_PATHS = {
    "start": "/agent/llm-key/start",
    "accept-terms": "/agent/llm-key/accept-terms",
    "verify": "/agent/llm-key/verify",
    "credits": "/agent/llm-credits",
}

def execute_llm(
    self,
    operation: str,
    identity: TelegramIdentity,
    *,
    payload: Mapping[str, Any] | None = None,
    user_access_token: str,
) -> dict[str, Any]:
    path = _LLM_OPERATION_PATHS.get(operation)
    if path is None:
        raise GatewayClientError(_UNSUPPORTED)
    body = {"telegramUserId": identity.user_id, **dict(payload or {})}
    return self._post(
        path,
        body,
        token=self.api_token,
        operation=f"llm-{operation}",
        timeout=self.purchase_timeout,
        user_token=user_access_token,
    )
```

- [ ] **Step 4: Run client tests**

Run the Task 6 command again.

Expected: all client tests pass.

- [ ] **Step 5: Commit**

```bash
git add hermes-plugins/sign402-wallet/client.py hermes-plugins/sign402-wallet/tests/test_client.py
git commit -m "Add Hermes client for Bankr LLM purchases"
```

### Task 7: Trusted Telegram Commands

**Files:**
- Modify: `hermes-plugins/sign402-wallet/__init__.py`
- Modify: `hermes-plugins/sign402-wallet/identity.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_plugin.py`
- Modify: `hermes-plugins/sign402-wallet/tests/test_identity.py`

- [ ] **Step 1: Write failing parser, identity, and handler tests**

```python
def test_parse_llm_buy_args(self):
    self.assertEqual(
        plugin._parse_llm_buy_args("10 user@example.com"),
        ("10", "user@example.com"),
    )
    self.assertIsNone(plugin._parse_llm_buy_args("10"))

async def test_llm_code_starts_background_purchase_and_never_logs_otp(self):
    source = telegram_source(user_id="123")
    result = await dispatch_command("/llm_code 123456", source)
    self.assertEqual(result["action"], "respond")
    self.assertIn("Approve it in iMessage", result["content"])
    self.assertNotIn("123456", self.log_output.getvalue())
    self.background_jobs[0]()
    self.assertEqual(self.client.llm_calls[0]["payload"], {"code": "123456"})
    self.assertIn("bk_", self.telegram_messages[-1])

def test_identity_captures_llm_commands(self):
    for command in ("llm-buy", "llm-terms", "llm-code", "llm-credits"):
        event = telegram_event(command=command, user_id="123")
        capture_gateway_identity(event=event)
        self.assertEqual(consume_gateway_identity().user_id, "123")
```

- [ ] **Step 2: Run plugin tests and verify failure**

```bash
python -m unittest discover -s hermes-plugins/sign402-wallet/tests -v
```

Expected: parser and handlers are missing.

- [ ] **Step 3: Implement command handlers**

Add menu entries and register:

```text
/llm_buy <usd> <email>
/llm_terms accept
/llm_code <six digits>
/llm_credits
```

Use `_build_llm_handler(operation)` for `llm-buy`, `llm-terms`, and
`llm-credits`: consume trusted identity, obtain the per-user access token, call
`GatewayClient.execute_llm`, and return `telegramText`.

Use a dedicated `_build_llm_code_handler()` for `llm-code`. It must consume and
copy the trusted Telegram identity before scheduling work, return
`Bankr LLM purchase started. Approve it in iMessage; I'll post the result here.`
immediately, execute the blocking verify/approval/top-up call through
`_background_runner`, and deliver the final `telegramText` through the existing
Telegram send helper. Validate all arguments before any gateway call and never
log the raw OTP.

Add normalized command names to `identity._TELEGRAM_COMMANDS`:

```python
"llm-buy",
"llm-terms",
"llm-code",
"llm-credits",
```

- [ ] **Step 4: Run all plugin tests**

Run the Task 7 command again.

Expected: all plugin tests pass.

- [ ] **Step 5: Commit**

```bash
git add hermes-plugins/sign402-wallet/__init__.py hermes-plugins/sign402-wallet/identity.py hermes-plugins/sign402-wallet/tests/test_plugin.py hermes-plugins/sign402-wallet/tests/test_identity.py
git commit -m "Add Telegram Bankr LLM purchase commands"
```

### Task 8: Regression, Secret Scan, and Deployment Checklist

**Files:**
- Modify: `docs/superpowers/specs/2026-07-05-bankr-llm-key-purchase-design.md` only if implementation reveals a necessary correction.

- [ ] **Step 1: Run the gateway suite**

```bash
cd sign402-gateway
. .venv/bin/activate
python -m unittest discover -s tests -v
```

Expected: all gateway tests pass.

- [ ] **Step 2: Run the Hermes plugin suite**

```bash
cd ../hermes-plugins/sign402-wallet
python -m unittest discover -s tests -v
```

Expected: all plugin tests pass.

- [ ] **Step 3: Run syntax and diff checks**

```bash
cd ../..
python -m compileall -q sign402-gateway/sign402_gateway hermes-plugins/sign402-wallet
git diff --check
```

Expected: both commands exit `0`.

- [ ] **Step 4: Scan tracked changes for accidentally committed secrets**

```bash
git diff HEAD~7..HEAD -- . ':!docs/superpowers/plans/*' | rg -n \
  'bk_[A-Za-z0-9_-]{10,}|identity_token[\"'\"']?\\s*[:=]\\s*[\"'\"'][^\"'\"']+|SIGN402_WALLET_MASTER_KEY=.+'
```

Expected: no output.

- [ ] **Step 5: Commit any final documentation correction**

If the implementation required a design correction:

```bash
git add docs/superpowers/specs/2026-07-05-bankr-llm-key-purchase-design.md
git commit -m "Align Bankr LLM design with implementation"
```

If no correction was required, do not create an empty commit.

- [ ] **Step 6: Deploy to the VPS after pushing**

On the VPS:

```bash
ssh hermes@164.68.104.44
cd ~/apps/sign402
git pull
cd sign402-gateway
. .venv/bin/activate
python -m unittest discover -s tests -v
sudo systemctl restart sign402-gateway
rsync -a ~/apps/sign402/hermes-plugins/sign402-wallet/ ~/.hermes/plugins/sign402-wallet/
hermes gateway restart
curl -sS http://127.0.0.1:8099/health
```

Expected: tests pass, both services restart, and health lists all four Bankr LLM
routes.

- [ ] **Step 7: Perform the 1 USD production smoke test**

In Telegram:

```text
/llm_buy 1 user@example.com
/llm_terms accept
/llm_code 123456
```

Use the actual emailed code in place of `123456`, approve the exact SINGIT
amount in iMessage, and verify:

- the Base SINGIT transfer sender is the Telegram user's managed wallet;
- Bankr credits increase by 1 USD;
- Telegram displays one real `bk_...` key;
- `/llm_credits` returns the balance and fingerprint without the full key;
- a second `/llm_code` does not create a key or transfer funds.
