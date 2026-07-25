# P1a Exact Payment Approval Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind every covered payment to server-generated version 2 terms, allow one signing attempt per durable approval, reject changed CDP challenges before signing, and make unguarded Bankr x402 paths unable to move funds.

**Architecture:** Add a strict cross-language payment-terms format, a private SQLite approval ledger, and a small authorization service that owns provider decisions and one-time execution. Python and Node compare the same canonical terms at their respective trust boundaries; covered callers execute only claimed stored data. Bankr x402 remains fail-closed because its CLI exposes no equivalent pre-sign selector.

**Tech Stack:** Python 3.11+, standard-library `sqlite3`, `unittest`, existing `cryptography`/gateway dependencies, Node.js ESM, `node:test`, existing Coinbase x402 packages.

## Global Constraints

- Keep `49725745d18360561deb3e6a702fdcc3ab9e0855` as the reviewed design ancestor
  in the isolated worktree
  `/Users/mp/Documents/Berlin Hack/.worktrees/p0-containment`; the plan/spec
  documentation commit may be newer than that ancestor.
- Preserve the user's dirty `x402Bnkr` checkout and all unrelated changes.
- Do not read, rewrite, chmod, migrate, rotate, or otherwise touch live `.env`, wallet, ignored JSON, SQLite, or provider state.
- Every test remains offline: no Bankr, CDP, blockchain RPC, Firefly hardware, Telegram, WhatsApp, Photon, Bitrefill, or paid-resource call.
- Keep legacy routes disabled by default and protected by the current operator token when explicitly enabled.
- Do not add a runtime or test-mode bypass that can re-enable unbound Bankr x402 payments.
- Covered x402 execution is GET-only; reject POST/body-bearing requests before approval.
- Payment approvals expire after 120 seconds. Execution leases expire after five minutes.
- A claimed approval becomes `completed`, `cancelled_before_sign`, or `outcome_unknown`; it never automatically returns to `approved`.
- A completed replay returns the stored allowlisted receipt without calling a signer or incrementing policy spend again.
- Version 1 hashes may remain in excluded historical/demo flows, but the new approval store and covered P1a routes accept only version 2.
- Atomic daily-cap and policy-budget reservations remain P1b; do not expand this plan into that ledger redesign.

---

## File Responsibility Map

### New files

- `test-fixtures/payment-terms-v2.json` — neutral Python/Node canonical vectors and expected hashes.
- `sign402-gateway/sign402_gateway/payment_terms.py` — strict version 2 builders, URL rules, selected-requirement validation, canonical JSON, and hashing.
- `sign402-gateway/tests/test_payment_terms.py` — Python vectors and mutation/rejection coverage.
- `sign402-gateway/sign402_gateway/payment_approvals.py` — private SQLite store, records, errors, CAS transitions, lease recovery, and attempt fencing.
- `sign402-gateway/tests/test_payment_approvals.py` — store permissions, expiry, concurrency, replay, recovery, and persistence tests.
- `sign402-gateway/sign402_gateway/payment_authorization.py` — approval-provider verification, hardware context, claim orchestration, pause fence, receipt allowlisting, and one-shot execution.
- `sign402-gateway/tests/test_payment_authorization.py` — service tests with mocked providers/signers.
- `cdp-x402-service/src/payment-terms.mjs` — Node implementation of the restricted canonical format.
- `cdp-x402-service/src/x402-buyer.mjs` — exact selector wiring, redirect rejection, single-use signer wrapper, and allowlisted result.
- `cdp-x402-service/test/payment-terms.test.mjs` — shared-vector equality and invalid-domain tests.
- `cdp-x402-service/test/x402-buyer.test.mjs` — pre-sign mismatch, redirect, and single-sign tests.
- `scripts/tests/test_payment_approval_docs.py` — executable documentation/security contract assertions.

### Modified files

- `.gitignore` — ignore the private approval database plus its `-journal`,
  `-wal`, and `-shm` SQLite sidecars.
- `sign402-gateway/sign402_gateway/goplausible.py` — reject duplicate JSON keys and expose one size-capped selected raw requirement.
- `sign402-gateway/tests/test_goplausible_adapter.py` — duplicate-key and selected-requirement tests.
- `sign402-gateway/sign402_gateway/server.py` — signer identities, approval-service injection, handlers, CDP clients, external/user buyers, and Bankr fail-closed wiring.
- `sign402-gateway/tests/test_gateway_server.py` — handler/client/buyer regressions and `DummyServer` dependencies.
- `sign402-gateway/sign402_gateway/imessage_approvals.py` — embed complete terms/hash in the user-wallet approval envelope.
- `sign402-gateway/tests/test_imessage_approvals.py` — complete-envelope and expiry tests.
- `sign402-gateway/sign402_gateway/bitrefill_runner.py` — reject operator Bankr x402 before Firefly/CLI while preserving managed-wallet Bitrefill.
- `sign402-gateway/tests/test_bitrefill_runner.py` — fail-closed operator tests and retained wallet tests.
- `cdp-x402-service/src/payment-guard.mjs` — replace three loose caps with exact candidate fingerprinting.
- `cdp-x402-service/test/payment-guard.test.mjs` — mutate every bound challenge field.
- `cdp-x402-service/src/index.mjs` — require an approved stdin envelope for `buy` and `buy-user`.
- `cdp-x402-service/.env.example` — require the public CDP account address for legacy treasury signing.
- `cdp-x402-service/README.md` — document the stdin contract and exact guard.
- `sign402-gateway/.env.example` — document the approval-store path and CDP payer requirement.
- `sign402-gateway/README.md` — document `approvalId`, version 2 requests, replay semantics, and Bankr shutdown.
- `sign402-gateway/SECURITY.md` — record the new trust boundary, ambiguous-outcome behavior, and remaining P1b limitation.
- `README.md` — replace cap-only safety claims with the exact cross-language guard.

---

### Task 0: Reconfirm the Isolated Baseline and Protected Runtime Metadata

**Files:** Read-only verification; create metadata manifests only under
`/private/tmp`.

- [ ] **Step 1: Verify the intended worktree, branch, base, and original checkout**

```bash
pwd
git branch --show-current
git rev-parse HEAD
git merge-base --is-ancestor 49725745d18360561deb3e6a702fdcc3ab9e0855 HEAD
git diff --name-only 49725745d18360561deb3e6a702fdcc3ab9e0855..HEAD
git status --short --branch
git -C '/Users/mp/Documents/Berlin Hack' status --short --branch
```

Expected:

- current directory is
  `/Users/mp/Documents/Berlin Hack/.worktrees/p0-containment`;
- branch is `codex/p1a-approval-binding`;
- `49725745d18360561deb3e6a702fdcc3ab9e0855` is an ancestor and every newer
  pre-implementation change is limited to the P1a spec/plan;
- the implementation worktree is clean before Task 1;
- the original checkout's pre-existing changes are observed but not staged,
  edited, restored, or committed.

- [ ] **Step 2: Capture metadata only for protected live-state targets**

Run this against the original checkout without opening file contents:

```bash
for target in \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/private' \
  '/Users/mp/Documents/Berlin Hack/cdp-x402-service/.env' \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.env' \
  '/Users/mp/Documents/Berlin Hack/sign402-gateway/.env.wallet-bitrefill' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/bitrefill-orders.sqlite3' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/user-purchases.json'
do
  if [ -e "$target" ]; then
    stat -f '%N|%Sp|%z|%m' "$target"
  else
    printf '%s|MISSING\n' "$target"
  fi
done > /private/tmp/sign402-p1a-live-state-before.txt
git -C '/Users/mp/Documents/Berlin Hack' status --porcelain=v1 \
  > /private/tmp/sign402-p1a-original-status-before.txt
```

Do not hash, parse, query, chmod, or print these files.

- [ ] **Step 3: Run the complete clean baseline**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-bridge/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s payment-executor/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s demo-resource-server/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s live-demo/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s hermes-plugins/sign402-wallet/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s scripts/tests -q
npm --prefix cdp-x402-service test
npm --prefix singit-risk-check test
```

Expected: 730 total tests PASS: 715 Python and 15 Node. If the clean baseline
fails, stop implementation and diagnose the baseline before editing runtime
code.

---

### Task 1: Freeze Python Payment Terms V2 and Shared Vectors

**Files:**
- Create: `test-fixtures/payment-terms-v2.json`
- Create: `sign402-gateway/sign402_gateway/payment_terms.py`
- Create: `sign402-gateway/tests/test_payment_terms.py`
- Modify: `sign402-gateway/sign402_gateway/goplausible.py:1-230`
- Modify: `sign402-gateway/tests/test_goplausible_adapter.py`

**Interfaces:**
- Consumes: normalized gateway requirements and the raw selected x402 requirement.
- Produces:
  - `SignerIdentity(backend: str, payer: str)`
  - `PaymentTermsBundle(terms, canonical_json, commitment_hash, executor_requirements, selected_requirement)`
  - `SelectedX402Requirement(x402_version, normalized_requirement, selected_requirement)`
  - `build_direct_payment_terms_v2(*, requirement: Mapping[str, Any], policy_hash: str, signer: SignerIdentity, purpose: str) -> PaymentTermsBundle`
  - `build_x402_payment_terms_v2(*, requirement: Mapping[str, Any], x402_version: int, policy_hash: str, signer: SignerIdentity, resource_url: str, purpose: str, selected_requirement: Mapping[str, Any]) -> PaymentTermsBundle`
  - `canonicalize_payment_terms_v2(terms: Mapping[str, Any]) -> str`
  - `hash_payment_terms_v2(terms: Mapping[str, Any]) -> str`
  - `validate_stored_payment_terms_v2(*, terms: Mapping[str, Any], canonical_json: str, commitment_hash: str, executor_requirements: Mapping[str, Any], selected_requirement: Mapping[str, Any] | None) -> PaymentTermsBundle`
  - `select_x402_requirement(payload: Mapping[str, Any], *, resource_url: str, purpose: str) -> SelectedX402Requirement`

Define these server-owned route constants in `payment_terms.py`:

```python
LEGACY_DIRECT_PURPOSE = "x402_api_access"
INTERNAL_X402_PURPOSE = "x402_api_access"
USER_WALLET_X402_PURPOSE = "x402_api_access"
```

The separate names prevent a future route from silently borrowing
caller/provider purpose even though the current approved policies use the same
value.

- [ ] **Step 1: Add the shared valid vector and failing Python tests**

Create a fixture whose valid Base vector has these exact derived values:

```python
FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "test-fixtures"
    / "payment-terms-v2.json"
)
```

```json
{
  "id": "base-usdc-get",
  "expectedIntent": "x402:ef5721066c88291fe0605128e338556329be66a600b35b60cc1e08c9b363fd2c",
  "expectedCommitmentHash": "2b1fbdb1709382cf0811b8088c21666715384d7e973b9f461f507b80a006b974",
  "context": {
    "policyHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "signerBackend": "cdp-managed",
    "payer": "0x3333333333333333333333333333333333333333",
    "resource": "https://merchant.example/paid?item=1",
    "purpose": "x402_api_access"
  },
  "x402Version": 2,
  "requirement": {
    "scheme": "exact",
    "network": "eip155:8453",
    "maxAmountRequired": "1000",
    "payTo": "0x2222222222222222222222222222222222222222",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bDa02913",
    "maxTimeoutSeconds": 60,
    "extra": {
      "name": "USD Coin",
      "version": "2"
    }
  }
}
```

Its `expectedCanonicalJson` field must contain these exact bytes:

```text
{"amountAtomic":"1000","amountMode":"exact","asset":"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913","extra":{"name":"USD Coin","version":"2"},"httpMethod":"GET","maxTimeoutSeconds":60,"network":"eip155:8453","payer":"0x3333333333333333333333333333333333333333","paymentIntent":"x402:ef5721066c88291fe0605128e338556329be66a600b35b60cc1e08c9b363fd2c","paymentKind":"x402","policyHash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","purpose":"x402_api_access","receiver":"0x2222222222222222222222222222222222222222","requestBodySha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","resource":"https://merchant.example/paid?item=1","scheme":"exact","signerBackend":"cdp-managed","type":"sign402-payment","version":2,"x402Version":2}
```

Add this AVM vector so Python cannot accidentally apply EVM lowercasing to
case-sensitive Algorand identifiers:

```json
{
  "id": "algorand-avm-get",
  "expectedIntent": "x402:737836e76ef1919c61337f59f9ce9fef63726857b79211cf06c2623f6a38685a",
  "expectedCommitmentHash": "d60ee2a54126c47c4e1d08f1729018510b4af7293a2e883fb734cbc3928c9452",
  "context": {
    "policyHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "signerBackend": "algorand-local",
    "payer": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ",
    "resource": "https://merchant.example/avm",
    "purpose": "x402_api_access"
  },
  "x402Version": 2,
  "requirement": {
    "scheme": "exact",
    "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
    "amount": "10000",
    "payTo": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ",
    "asset": "10458941",
    "maxTimeoutSeconds": 60,
    "extra": {
      "feePayer": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ"
    }
  }
}
```

Its `expectedCanonicalJson` field must contain these exact bytes:

```text
{"amountAtomic":"10000","amountMode":"exact","asset":"10458941","extra":{"feePayer":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ"},"httpMethod":"GET","maxTimeoutSeconds":60,"network":"algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=","payer":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ","paymentIntent":"x402:737836e76ef1919c61337f59f9ce9fef63726857b79211cf06c2623f6a38685a","paymentKind":"x402","policyHash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","purpose":"x402_api_access","receiver":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ","requestBodySha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","resource":"https://merchant.example/avm","scheme":"exact","signerBackend":"algorand-local","type":"sign402-payment","version":2,"x402Version":2}
```

Add this direct Algorand vector:

```json
{
  "id": "algorand-direct",
  "expectedCommitmentHash": "bfac81a204f4858b80d265ae642065211e7b534bc2493e587c352a04c313bf3b",
  "context": {
    "policyHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "signerBackend": "algorand-local",
    "payer": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ",
    "purpose": "x402_api_access"
  },
  "requirement": {
    "scheme": "exact",
    "network": "algorand-testnet",
    "asset": "ALGO_TEST",
    "amountAtomic": "50000",
    "receiver": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ",
    "resource": "/probe?target=algorand.co",
    "paymentIntent": "intent-001"
  }
}
```

Its `expectedCanonicalJson` field must contain these exact bytes:

```text
{"amountAtomic":"50000","amountMode":"exact","asset":"ALGO_TEST","extra":{},"httpMethod":"DIRECT","maxTimeoutSeconds":null,"network":"algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=","payer":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ","paymentIntent":"intent-001","paymentKind":"direct","policyHash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","purpose":"x402_api_access","receiver":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ","requestBodySha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","resource":"/probe?target=algorand.co","scheme":"direct","signerBackend":"algorand-local","type":"sign402-payment","version":2,"x402Version":null}
```

Use a top-level `validVectors` array for the three entries above and add this
exact `invalidVectors` corpus. Both Python and Node iterate every entry:

```json
[
  {
    "id": "unsafe-integer-extra",
    "kind": "termsMutation",
    "path": ["extra", "decimals"],
    "value": 9007199254740992
  },
  {
    "id": "float-extra",
    "kind": "termsMutation",
    "path": ["extra", "rate"],
    "value": 1.5
  },
  {
    "id": "unicode-extra",
    "kind": "termsMutation",
    "path": ["extra", "name"],
    "value": "Café"
  },
  {
    "id": "bad-extra-key",
    "kind": "termsMutation",
    "path": ["extra", "bad key"],
    "value": "x"
  },
  {
    "id": "lowercase-percent-escape",
    "kind": "termsMutation",
    "path": ["resource"],
    "value": "https://merchant.example/paid%2fitem"
  },
  {
    "id": "encoded-dot-segment",
    "kind": "termsMutation",
    "path": ["resource"],
    "value": "https://merchant.example/a/%2E%2E/b"
  },
  {
    "id": "url-credentials",
    "kind": "termsMutation",
    "path": ["resource"],
    "value": "https://user@merchant.example/paid"
  },
  {
    "id": "post-method",
    "kind": "termsMutation",
    "path": ["httpMethod"],
    "value": "POST"
  },
  {
    "id": "nonempty-body",
    "kind": "termsMutation",
    "path": ["requestBodySha256"],
    "value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  {
    "id": "duplicate-raw-key",
    "kind": "rawJson",
    "rawJson": "{\"extra\":{\"name\":\"USDC\",\"name\":\"EVIL\"}}"
  },
  {
    "id": "lexical-float-raw",
    "kind": "rawJson",
    "rawJson": "{\"extra\":{\"decimals\":1.0}}"
  },
  {
    "id": "lexical-exponent-raw",
    "kind": "rawJson",
    "rawJson": "{\"extra\":{\"decimals\":1e3}}"
  },
  {
    "id": "non-json-constant-raw",
    "kind": "rawJson",
    "rawJson": "{\"extra\":{\"decimals\":NaN}}"
  }
]
```

Also add this top-level `canonicalValueVectors` entry so JavaScript's special
property names and integer-index enumeration cannot drift from Python's
lexicographic order:

```json
[
  {
    "id": "numeric-and-prototype-keys",
    "value": {
      "2": "two",
      "10": "ten",
      "__proto__": {
        "1": "one",
        "01": "zero-one"
      }
    },
    "expectedCanonicalJson": "{\"10\":\"ten\",\"2\":\"two\",\"__proto__\":{\"01\":\"zero-one\",\"1\":\"one\"}}"
  }
]
```

For each `termsMutation`, start from the Base `expectedCanonicalJson`, apply
the path, recompute canonical/hash when the restricted serializer permits it,
and then require full schema/URL validation to reject. For each `rawJson`, call
the duplicate-safe lexical parser first.

Add these exact tests:

```python
class PaymentTermsV2Tests(unittest.TestCase):
    def test_x402_terms_match_shared_fixture(self):
        vector = _fixture("base-usdc-get")
        normalized = _normalized_requirement(vector["requirement"], vector["context"]["resource"])
        bundle = build_x402_payment_terms_v2(
            requirement=normalized,
            x402_version=vector["x402Version"],
            policy_hash=vector["context"]["policyHash"],
            signer=SignerIdentity(
                vector["context"]["signerBackend"],
                vector["context"]["payer"],
            ),
            resource_url=vector["context"]["resource"],
            purpose=vector["context"]["purpose"],
            selected_requirement=vector["requirement"],
        )
        self.assertEqual(bundle.terms["paymentIntent"], vector["expectedIntent"])
        self.assertEqual(bundle.canonical_json, vector["expectedCanonicalJson"])
        self.assertEqual(bundle.commitment_hash, vector["expectedCommitmentHash"])

    def test_direct_terms_match_shared_fixture(self):
        vector = _fixture("algorand-direct")
        bundle = build_direct_payment_terms_v2(
            requirement=vector["requirement"],
            policy_hash=vector["context"]["policyHash"],
            signer=SignerIdentity(
                vector["context"]["signerBackend"],
                vector["context"]["payer"],
            ),
            purpose=vector["context"]["purpose"],
        )
        self.assertEqual(bundle.canonical_json, vector["expectedCanonicalJson"])
        self.assertEqual(bundle.commitment_hash, vector["expectedCommitmentHash"])
```

Also add table-driven tests named:

- `test_each_bound_field_mutation_changes_hash`
- `test_signer_backend_and_payer_are_bound`
- `test_avm_preserves_validated_case_sensitive_identifiers`
- `test_rejects_backend_network_namespace_mismatch`
- `test_x402_intent_is_deterministic`
- `test_rejects_post_body_and_nonempty_body_digest`
- `test_rejects_float_unsafe_integer_unicode_and_bad_extra_key`
- `test_rejects_excessive_json_depth_and_node_count`
- `test_rejects_credentials_fragment_dot_segments_and_bad_percent_escape`
- `test_rejects_url_controls_spaces_and_parser_normalization`
- `test_allows_https_and_only_explicit_loopback_http`
- `test_rejects_selected_requirement_over_64k`
- `test_rejects_duplicate_payment_requirement_keys`
- `test_rejects_nan_and_infinity_in_body_and_header`
- `test_rejects_excessive_transport_json_depth_and_node_count`
- `test_rejects_oversized_payment_required_header_before_base64_decode`
- `test_strict_selection_requires_integer_x402_version_two`
- `test_rejects_every_shared_invalid_vector`
- `test_canonical_value_vectors_include_numeric_and_prototype_keys`
- `test_executor_projection_uses_only_derived_legacy_aliases`
- `test_initial_x402_fetch_rejects_redirect_before_approval`
- `test_initial_x402_non_402_error_never_echoes_response_body`
- `test_paid_x402_fetch_never_forwards_payment_header_to_redirect`
- `test_paid_x402_resource_rejects_excessive_json_depth_and_node_count`

- [ ] **Step 2: Run the focused tests and verify the expected import failures**

Run from the worktree root:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_terms.py sign402-gateway/tests/test_goplausible_adapter.py -v
```

Expected: FAIL because `sign402_gateway.payment_terms` and the new duplicate-key parser do not exist.

- [ ] **Step 3: Implement the restricted canonical domain**

Use these exact public types and constants:

```python
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_SELECTED_REQUIREMENT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
EXTRA_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class PaymentTermsError(ValueError):
    pass


@dataclass(frozen=True)
class SignerIdentity:
    backend: str
    payer: str


@dataclass(frozen=True)
class PaymentTermsBundle:
    terms: dict[str, Any]
    canonical_json: str
    commitment_hash: str
    executor_requirements: dict[str, Any]
    selected_requirement: dict[str, Any] | None
```

The canonical function must handle booleans before Python's integer branch,
reject every float, reject non-ASCII strings, sort validated ASCII object
keys, and serialize only the approved recursive subset:

```python
@dataclass
class _JsonBudget:
    nodes: int = 0


def _restricted_value(
    value: Any,
    path: str,
    *,
    depth: int = 0,
    budget: _JsonBudget | None = None,
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise PaymentTermsError(f"{path} exceeds maximum JSON depth")
    budget = budget or _JsonBudget()
    budget.nodes += 1
    if budget.nodes > MAX_JSON_NODES:
        raise PaymentTermsError("payment terms exceed maximum JSON node count")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise PaymentTermsError(f"{path} exceeds the safe integer range")
        return value
    if isinstance(value, float):
        raise PaymentTermsError(f"{path} floats are not allowed")
    if isinstance(value, str):
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise PaymentTermsError(f"{path} must contain printable ASCII")
        return value
    if isinstance(value, list):
        return [
            _restricted_value(
                item,
                f"{path}[{index}]",
                depth=depth + 1,
                budget=budget,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        keys = list(value)
        if any(
            not isinstance(key, str) or not EXTRA_KEY_RE.fullmatch(key)
            for key in keys
        ):
            raise PaymentTermsError(f"{path} contains an invalid key")
        for key in sorted(keys):
            normalized[key] = _restricted_value(
                value[key],
                f"{path}.{key}",
                depth=depth + 1,
                budget=budget,
            )
        return normalized
    raise PaymentTermsError(f"{path} contains an unsupported value")


def canonicalize_payment_terms_v2(terms: Mapping[str, Any]) -> str:
    normalized = _restricted_value(dict(terms), "paymentTerms")
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_payment_terms_v2(terms: Mapping[str, Any]) -> str:
    canonical = canonicalize_payment_terms_v2(terms)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Before calling `urlsplit`, validate the original URL byte-for-byte as non-empty
ASCII with every character in `0x21..0x7e`; this prevents the parser from
silently stripping tabs, newlines, or leading control/space characters.
Implement URL normalization with `urlsplit`, `hostname.encode("idna")`, no
credentials/fragment/dot segments/default port, and exact uppercase percent
escapes. Reject backslashes and path segments that decode to `.` or `..`,
including percent-encoded spellings. Require HTTPS except for exact
`localhost`, `127.0.0.1`, or `[::1]` loopback test hosts. Emit `/` for an empty
path. Do not sort or decode the query.

- [ ] **Step 4: Implement direct and x402 builders**

Use the exact intent source fields below:

```python
def derive_x402_payment_intent(signing_terms: Mapping[str, Any]) -> str:
    canonical = canonicalize_payment_terms_v2(signing_terms)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"x402:{digest}"


X402_INTENT_FIELDS = (
    "amountAtomic",
    "asset",
    "extra",
    "httpMethod",
    "maxTimeoutSeconds",
    "network",
    "receiver",
    "requestBodySha256",
    "resource",
    "scheme",
    "x402Version",
)
```

`build_x402_payment_terms_v2` must require version `2`, scheme `exact`, GET,
the effective canonical URL, exact amount equality, `maxTimeoutSeconds` as a
positive safe integer or null, the complete restricted `extra`, and a selected
raw requirement no larger than 64 KiB. Dispatch identity validation by the
canonical CAIP-2 namespace:

- `eip155:8453` requires lowercase 20-byte EVM payer, receiver, and token
  address, plus signer backend `cdp-managed` or `user-wallet-base`; every other
  EVM chain is rejected in P1a;
- the repository's Algorand TestNet CAIP-2 value requires checksum-valid
  58-character payer/receiver addresses, a positive decimal asset ID without
  leading zeroes, and signer backend `algorand-local`; Algorand MainNet is
  rejected because the covered local AVM signer is TestNet-only;
- every other namespace or backend/network mismatch is rejected.

Implement Algorand checksum validation with standard-library Base32 decoding
and SHA-512/256 so `payment_terms.py` does not add a gateway dependency.
Validate `extra.feePayer` as an Algorand address when it is present. Preserve
validated Algorand identifier case exactly. If the raw candidate has
`resource`, compare its independently canonicalized value with the effective
URL.

Take canonical network from normalized `x402Network` and require it to equal
the selected raw candidate's `network`; never hash the internal alias
`base-mainnet` or `algorand-testnet`. Normalize `amount` versus
`maxAmountRequired` and `payTo` versus `receiver` deterministically, rejecting
contradictory duplicate aliases. Ignore a candidate's top-level `purpose` and
derive top-level `paymentIntent`; both values in the terms come only from the
server argument and the live signing-field derivation.

`build_direct_payment_terms_v2` must accept only
`algorand-testnet`/`ALGO_TEST`, map it to the existing Algorand TestNet CAIP-2
identifier, require `paymentIntent`, take `purpose` from the server-owned
argument, and emit `DIRECT` plus the empty-body hash. It may accept the legacy
input's `scheme: "exact"` only as an input compatibility field; the version 2
terms always contain `scheme: "direct"`.

`validate_stored_payment_terms_v2` validates the complete fixed schema with no
missing or unknown keys, recomputes canonical bytes/hash, derives the exact
executor projection from terms, and compares it with the stored projection.
For AVM it rebuilds terms from the selected raw requirement and compares again;
for EVM/direct it requires the selected requirement to be null.

- [ ] **Step 5: Reject duplicate provider JSON keys and expose the selected raw requirement**

In `goplausible.py`, parse x402 JSON with:

```python
def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ValueError(f"non-JSON numeric constant: {value}")


def _loads_x402_json(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_X402_RESPONSE_BYTES:
        raise ValueError("x402 response payload exceeds size limit")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("x402 response exceeds maximum JSON depth") from exc
    _validate_json_shape_iterative(
        payload,
        max_depth=MAX_JSON_DEPTH,
        max_nodes=MAX_JSON_NODES,
    )
    if not isinstance(payload, dict):
        raise ValueError("x402 response payload must be a JSON object")
    return payload
```

Implement `_validate_json_shape_iterative` with an explicit stack of
`(value, depth)` pairs, incrementing the node count before pushing children.
Do not recursively walk the parsed result. Preserve the existing bounded body
read: read at most `MAX_X402_RESPONSE_BYTES + 1` bytes before UTF-8 decoding,
and reject the extra byte.

For a non-402 initial response, bounded-drain and discard the body, then raise
a stable status-only error. Never parse, interpolate, return, or log that body.

For `_decode_payment_required_header`, require ASCII, reject an encoded length
that could decode above `MAX_X402_RESPONSE_BYTES` before calling Base64,
decode with `validate=True`, verify the actual decoded length, then UTF-8
decode and call `_loads_x402_json`. A valid header never bypasses duplicate,
constant, depth, node-count, or byte caps.

Define the covered-route selection record in `goplausible.py`:

```python
@dataclass(frozen=True)
class SelectedX402Requirement:
    x402_version: int
    normalized_requirement: dict[str, Any]
    selected_requirement: dict[str, Any]
```

`select_x402_requirement` must require the top-level `x402Version` to be the
integer `2` (not a boolean or numeric string), deterministically select index
zero from a non-empty `accepts`/`paymentRequirements` list or the single
object form, and return fresh deep copies in this record. Covered routes use
this record rather than independently rereading the payload. They pass its
`x402_version` and `selected_requirement` to the V2 builder; missing or
contradictory versions fail before policy approval.

Keep `originalPaymentRequirements` as the one selected raw requirement, never
the whole provider response. Measure its compact UTF-8 JSON before returning it
from normalization and reject values above `MAX_SELECTED_REQUIREMENT_BYTES`.

`PaymentTermsBundle.executor_requirements` is a fresh allowlisted object with
only `network`, `x402Network`, `asset`, `amountAtomic`, `receiver`, `resource`,
`paymentIntent`, `purpose`, `maxTimeoutSeconds`, and `extra` as applicable.
Never copy `originalPaymentRequirements`, `sourceFormat`, headers, or provider
metadata into it. Always overwrite `paymentIntent` and `purpose` with the
derived/server-owned values already present in complete terms.

Derive the executor's legacy `network` alias only from a closed canonical map:
`eip155:8453 -> base-mainnet` and Algorand TestNet CAIP-2 ->
`algorand-testnet`. For x402, also emit the same canonical value as
`x402Network`; for direct, emit only `algorand-testnet`. Never persist or reuse
a caller/provider alias. This preserves the existing executor/policy contract
while making the signed CAIP-2 value authoritative.

Route both JSON entry points through `_loads_x402_json`: the decoded body in
`fetch_x402_payment_required` and decoded bytes in
`_decode_payment_required_header`. Add separate
`test_rejects_duplicate_keys_in_payment_required_body` and
`test_rejects_duplicate_keys_in_payment_required_header` tests so neither
transport can silently overwrite a key.

Replace the default `urllib.request.urlopen` behavior with a local opener built
from an `HTTPRedirectHandler` whose `redirect_request` always raises
`X402RedirectRejected`. Use it for both `fetch_x402_payment_required` and
`fetch_x402_paid_resource`; never forward `PAYMENT-SIGNATURE` to a redirect
target. Preserve explicit opener injection for offline tests. A redirect from
the initial request fails before approval. A redirect returned only after the
paid AVM request is sent is a post-handoff error and therefore becomes
`outcome_unknown`; the client makes no redirected or retry request.

Paid resource JSON is not canonical payment input and may contain ordinary
Unicode/floats, but it still uses the same iterative 64-depth/100,000-node
shape budget after the existing 1 MiB byte cap. Reject an over-budget paid
body before constructing a transient result; because this is after handoff,
the service records `outcome_unknown` and never retries.

- [ ] **Step 6: Run focused and gateway regression tests**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_terms.py sign402-gateway/tests/test_goplausible_adapter.py -v
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
```

Expected: new focused tests PASS; existing gateway suite remains green.

- [ ] **Step 7: Commit the frozen Python contract**

```bash
git add test-fixtures/payment-terms-v2.json sign402-gateway/sign402_gateway/payment_terms.py sign402-gateway/sign402_gateway/goplausible.py sign402-gateway/tests/test_payment_terms.py sign402-gateway/tests/test_goplausible_adapter.py
git commit -m "feat: define exact payment terms v2"
```

---

### Task 2: Add the Durable SQLite Approval Store

**Files:**
- Create: `sign402-gateway/sign402_gateway/payment_approvals.py`
- Create: `sign402-gateway/tests/test_payment_approvals.py`
- Modify: `.gitignore:29-55`

**Interfaces:**
- Consumes: `PaymentTermsBundle` from Task 1.
- Produces:
  - `PaymentApprovalRecord`
  - `ClaimedPaymentApproval`
  - `PaymentApprovalStore.create_pending(bundle: PaymentTermsBundle, *, provider: str, ttl_seconds: int) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.mark_approved(approval_id: str, *, approved_hash: str, provider_metadata: Mapping[str, Any]) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.mark_denied(approval_id: str, *, failure_code: str, provider_metadata: Mapping[str, Any]) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.claim(approval_id: str) -> ClaimedPaymentApproval | PaymentApprovalRecord`
  - `PaymentApprovalStore.complete(approval_id: str, attempt_id: str, receipt: Mapping[str, Any]) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.cancel_before_sign(approval_id: str, attempt_id: str, failure_code: str) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.mark_outcome_unknown(approval_id: str, attempt_id: str, failure_code: str) -> PaymentApprovalRecord`
  - `PaymentApprovalStore.get(approval_id: str) -> PaymentApprovalRecord`
  - `thaw_payment_json(value: Any) -> Any`
  - `validate_payment_receipt(record: PaymentApprovalRecord, receipt: Mapping[str, Any]) -> Mapping[str, Any]`

- [ ] **Step 1: Write failing store tests**

Add tests with two independently constructed store instances pointed at the
same temporary database:

```python
class PaymentApprovalStoreTests(unittest.TestCase):
    def test_two_connections_claim_exactly_once(self):
        now = [1_800_000_000]
        path = Path(self.temp_dir.name) / "private" / "approvals.sqlite3"
        first = PaymentApprovalStore(path, clock=lambda: now[0])
        second = PaymentApprovalStore(path, clock=lambda: now[0])
        approval = first.create_pending(_bundle(), provider="firefly", ttl_seconds=120)
        first.mark_approved(
            approval.approval_id,
            approved_hash=approval.commitment_hash,
            provider_metadata={"deviceModel": "262", "deviceSerial": "1056"},
        )
        barrier = threading.Barrier(2)

        def claim(store):
            barrier.wait()
            try:
                return store.claim(approval.approval_id)
            except PaymentApprovalConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (first, second)))
        self.assertEqual(sum(result is not None for result in results), 1)
```

Add these exact test names:

- `test_database_and_parent_are_private`
- `test_default_path_uses_dedicated_private_subdirectory`
- `test_existing_nonprivate_parent_is_rejected_without_chmod`
- `test_pending_approved_denied_and_expired_transitions`
- `test_mark_approved_after_provider_delay_persists_expired`
- `test_claim_rejects_unknown_pending_denied_expired_cancelled_executing_and_unknown`
- `test_claim_requires_expiry_after_transaction_now`
- `test_two_connections_claim_exactly_once`
- `test_identical_terms_create_distinct_opaque_approval_ids`
- `test_create_pending_revalidates_and_copies_the_caller_bundle`
- `test_completed_replay_returns_allowlisted_receipt`
- `test_attempt_id_fences_late_complete_after_recovery`
- `test_younger_execution_is_not_recovered`
- `test_stale_execution_becomes_outcome_unknown`
- `test_hard_kill_after_signer_return_before_completion_recovers_unknown`
- `test_selected_requirement_is_capped_and_no_secrets_are_persisted`
- `test_store_rejects_unallowlisted_metadata_or_receipt_before_sql_write`
- `test_store_caps_provider_metadata_receipt_txid_and_failure_code`
- `test_evm_approval_persists_no_selected_provider_requirement`
- `test_tampered_terms_hash_or_executor_columns_fail_closed_on_read`
- `test_decoded_records_recursively_freeze_every_nested_json_value`
- `test_cancel_before_sign_and_unknown_are_terminal`

The hard-kill test claims a row, invokes a fake signer once, deliberately
performs no finalizer, advances the injected clock past 300 seconds, and opens
a new store instance. It must observe `outcome_unknown`; a later claim or stale
attempt completion must fail without another signer call.

- [ ] **Step 2: Run the store tests and verify they fail**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_approvals.py -v
```

Expected: FAIL because `PaymentApprovalStore` does not exist.

- [ ] **Step 3: Add the exact schema and record types**

Use one table with explicit status and timestamps:

```sql
CREATE TABLE IF NOT EXISTS payment_approvals (
    approval_id TEXT PRIMARY KEY,
    commitment_version INTEGER NOT NULL,
    commitment_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    executor_requirements_json TEXT NOT NULL,
    selected_requirement_json TEXT,
    signer_backend TEXT NOT NULL,
    payer TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_approval_id TEXT,
    provider_metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    execution_attempt_id TEXT,
    receipt_json TEXT,
    failure_code TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    decision_at INTEGER,
    claimed_at INTEGER,
    completed_at INTEGER
)
```

Define immutable records:

```python
@dataclass(frozen=True)
class PaymentApprovalRecord:
    approval_id: str
    commitment_version: int
    commitment_hash: str
    policy_hash: str
    canonical_json: str
    terms: Mapping[str, Any]
    executor_requirements: Mapping[str, Any]
    selected_requirement: Mapping[str, Any] | None
    signer: SignerIdentity
    provider: str
    provider_approval_id: str | None
    provider_metadata: Mapping[str, Any]
    status: str
    execution_attempt_id: str | None
    receipt: Mapping[str, Any] | None
    failure_code: str | None
    created_at: int
    expires_at: int
    decision_at: int | None
    claimed_at: int | None
    completed_at: int | None


@dataclass(frozen=True)
class ClaimedPaymentApproval:
    record: PaymentApprovalRecord
    execution_attempt_id: str
```

`frozen=True` is not sufficient for nested JSON. After validating a decoded
row, recursively freeze every mapping with `types.MappingProxyType` and every
list as a tuple before constructing either record. Provide
`thaw_payment_json` to create fresh plain dict/list values only at an explicit
adapter or serialization boundary. Store/service validation always uses the
frozen trusted record; callback-owned copies can never become the source of a
receipt check or finalizer.

Create dedicated `PaymentApprovalNotFound`, `PaymentApprovalExpired`,
`PaymentApprovalConflict`, and `PaymentApprovalStoreError` exceptions.

Use this exact closed failure-code set:

```python
PAYMENT_FAILURE_CODES = frozenset(
    {
        "approval_expired",
        "approval_expired_during_provider",
        "provider_denied",
        "provider_hash_mismatch",
        "provider_unavailable",
        "imessage_denied",
        "imessage_hash_mismatch",
        "imessage_unavailable",
        "transactions_paused",
        "payment_execution_failed",
        "receipt_validation_failed",
        "before_complete_failed",
        "execution_lease_expired",
    }
)
```

Generate every approval ID and execution-attempt ID independently with
`secrets.token_urlsafe(24)`. Validate IDs as bounded opaque ASCII tokens before
using them in SQL. Creating identical terms twice always creates two rows and
never treats the commitment hash as an ID.

Persist `selected_requirement_json` only when the complete terms use the
`algorand` CAIP-2 namespace and `paymentKind == "x402"`. For CDP and
user-wallet EVM approvals, store SQL NULL and reload `selected_requirement` as
`None`; Node needs only the complete terms envelope. Reject a non-null selected
requirement for direct payments or any other namespace.

On every row decode, call `validate_stored_payment_terms_v2`: recompute
canonical bytes and hash, require version/column policy/backend/payer equality,
validate the allowlisted executor projection, and revalidate the optional AVM
selection. Validate provider metadata, receipt shape, status, and timestamps
before returning a record. Any malformed or contradictory durable value raises
`PaymentApprovalStoreError` and reaches no policy validator or signer.

Use shared exact provider/receipt key constants in this store module.
`create_pending` first revalidates and copies the complete
`PaymentTermsBundle` through `validate_stored_payment_terms_v2`; caller-owned
bundle dictionaries are never serialized directly.
`mark_approved`/`mark_denied` validate, cap, allowlist, and freeze provider
metadata before any SQL write. `complete` calls
`validate_payment_receipt` against the transaction's trusted row before any SQL
write, then persists only a thawed copy of that validated receipt. Unknown
keys, unsupported values, arbitrary exception text, or secret-shaped keys are
rejected before bytes reach SQLite—not merely detected on the next read.
Provider metadata is at most 4 KiB compact JSON with printable scalar values
of at most 256 characters; receipts are at most 64 KiB compact JSON, `txId` is
at most 256 printable ASCII characters, and failure codes come from a closed
set with a 64-character maximum. The authorization service imports and reuses
this receipt validator before its hook, so the pre-hook and final store checks
cannot drift.

- [ ] **Step 4: Implement private initialization and transactional CAS**

Do not call `ensure_private_directory` on an existing configured parent because
that helper chmods shared directories. Add an approval-store-specific
preflight:

1. if the parent does not exist, create it with mode `0700`;
2. if it exists, reject symlinks, non-directories, or a mode other than `0700`
   without chmod;
3. create the database atomically with mode `0600`, then use
   `ensure_private_file` only on this store-owned database;
4. keep SQLite journals/sidecars inside that private parent.

Use a five-second SQLite timeout, `BEGIN IMMEDIATE`, and a transaction-local
integer timestamp from the injected clock. The claim predicate must be one
statement:

```sql
UPDATE payment_approvals
SET status = 'executing',
    execution_attempt_id = ?,
    claimed_at = ?
WHERE approval_id = ?
  AND status = 'approved'
  AND expires_at > ?
```

Every finalizer must fence on both status and attempt ID:

```sql
UPDATE payment_approvals
SET status = ?,
    receipt_json = ?,
    failure_code = ?,
    completed_at = ?
WHERE approval_id = ?
  AND status = 'executing'
  AND execution_attempt_id = ?
```

Before each read/claim and on initialization, recover only rows satisfying
`status = 'executing' AND claimed_at <= now - 300` to `outcome_unknown` with
failure code `execution_lease_expired`. Never clear `execution_attempt_id`
during recovery.

In the same transaction used by `get` or `claim`, change only
`pending`/`approved` rows satisfying `expires_at <= now` to `expired` with
failure code `approval_expired`.
Inside its own transaction, `mark_approved` first converts the target
`pending` row to `expired` with
`approval_expired_during_provider` when `expires_at <= now`, then performs its
`status = 'pending' AND expires_at > now` approval CAS and returns the actual
resulting record. A zero-row approval update can never strand an expired
provider decision as `pending` or revive an expired row. `mark_denied`
transactionally chooses `denied` only while `expires_at > now`, otherwise it
writes terminal `expired` with the safe code
`approval_expired_during_provider`. `claim` returns the stored receipt only for
`completed`, raises the dedicated expiry/not-found/conflict error for every
other terminal status, and never reads a row as approved before its CAS.

- [ ] **Step 5: Add the exact ignored runtime paths**

Append:

```gitignore
demo-dashboard/private/payment-approvals.sqlite3
demo-dashboard/private/payment-approvals.sqlite3-journal
demo-dashboard/private/payment-approvals.sqlite3-shm
demo-dashboard/private/payment-approvals.sqlite3-wal
```

Add a test that runs `git check-ignore` from the repository root for all four
paths. No test initializes the default path; all store instances use temporary
private parents.

- [ ] **Step 6: Run store tests, permission tests, and diff checks**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_approvals.py sign402-gateway/tests/test_secure_state.py -v
git diff --check
```

Expected: all focused tests PASS and no whitespace errors.

- [ ] **Step 7: Commit the approval ledger**

```bash
git add .gitignore sign402-gateway/sign402_gateway/payment_approvals.py sign402-gateway/tests/test_payment_approvals.py
git commit -m "feat: add durable payment approval ledger"
```

---

### Task 3: Put Provider Decisions and One-Shot Execution Behind One Service

**Files:**
- Create: `sign402-gateway/sign402_gateway/payment_authorization.py`
- Create: `sign402-gateway/tests/test_payment_authorization.py`
- Modify: `sign402-gateway/sign402_gateway/payment_approvals.py`
- Modify: `sign402-gateway/tests/test_payment_approvals.py`

**Interfaces:**
- Consumes: raw requirements, resolved `SignerIdentity`, a Firefly or iMessage
  decision, the pause predicate, a policy validator, and a payment-capable
  callback.
- Produces: a durable approval record or a validated, allowlisted execution
  result.

- [ ] **Step 1: Write failing service tests**

Use a temporary SQLite path and deterministic fake providers. Add these exact
tests:

- `test_firefly_pending_row_exists_before_provider_call`
- `test_firefly_receives_exact_three_safe_context_lines`
- `test_firefly_assertion_mismatch_fails_before_pending_or_provider`
- `test_firefly_hash_mismatch_is_persisted_denied`
- `test_firefly_error_is_persisted_denied_without_raw_body`
- `test_imessage_hash_mismatch_is_persisted_denied`
- `test_imessage_delivery_failure_without_channel_metadata_is_terminal`
- `test_imessage_decision_after_expiry_is_persisted_expired_not_denied`
- `test_service_uses_fixed_120_second_ttl_without_runtime_override`
- `test_validation_and_signer_mismatch_fail_before_claim`
- `test_pause_after_claim_cancels_before_sign`
- `test_executor_is_called_once_with_stored_requirements`
- `test_executor_exception_after_handoff_marks_outcome_unknown`
- `test_receipt_mismatch_marks_outcome_unknown`
- `test_receipt_rejects_boolean_signer_invocation_count`
- `test_before_complete_runs_after_receipt_validation_and_only_once`
- `test_before_complete_cannot_mutate_the_persisted_validated_receipt`
- `test_executor_cannot_mutate_claimed_terms_or_requirements`
- `test_unknown_finalizer_failure_never_retries_and_lease_recovers`
- `test_completed_replay_validates_assertions_without_policy_signer_or_executor`
- `test_concurrent_execution_invokes_executor_once`
- `test_provider_and_receipt_allowlists_drop_secrets`
- `test_transient_resource_result_is_returned_once_and_never_persisted`
- `test_transient_resource_result_rejects_excessive_depth_nodes_and_bytes`
- `test_store_and_firefly_failure_never_invoke_executor`

The executor test must mutate the caller-owned requirements after approval and
assert that the callback still receives the copy decoded from SQLite:

```python
seen: list[dict[str, Any]] = []
caller_requirements = _direct_requirements()
approval = service.approve_with_firefly(
    payment_kind="direct",
    policy_hash="a" * 64,
    requirements=caller_requirements,
    signer=ALGORAND_SIGNER,
    firefly=firefly,
    purpose=LEGACY_DIRECT_PURPOSE,
    assertions={},
)
caller_requirements["amountAtomic"] = "999999"

result = service.execute_once(
    approval_id=approval.approval_id,
    assertions={},
    signer_resolver=lambda: ALGORAND_SIGNER,
    policy_validator=lambda record: None,
    paused=lambda: False,
    executor=lambda claimed: PaymentCallbackResult(
        receipt=(
            seen.append(
                thaw_payment_json(claimed.record.executor_requirements)
            )
            or _valid_receipt(claimed.record)
        ),
        transient_result=None,
    ),
    before_complete=lambda record, receipt: None,
)

self.assertEqual(seen, [_direct_requirements()])
self.assertFalse(result.replayed)
```

- [ ] **Step 2: Run the service tests and verify the missing-module failure**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_authorization.py -v
```

Expected: FAIL because `sign402_gateway.payment_authorization` does not exist.

- [ ] **Step 3: Define the public service contract**

Create these exact records and protocols:

```python
@dataclass(frozen=True)
class PaymentCallbackResult:
    receipt: dict[str, Any]
    transient_result: dict[str, Any] | None


@dataclass(frozen=True)
class PaymentExecutionResult:
    approval: PaymentApprovalRecord
    receipt: dict[str, Any]
    transient_result: dict[str, Any] | None
    replayed: bool
```

Use these exact callable signatures:

```text
FireflyApprovalProvider.approve_payment_hash(
    payment_hash: str,
    *,
    context_lines: list[str] | None = None,
) -> dict[str, Any]

PaymentApprovalService(
    store: PaymentApprovalStore,
)

PaymentApprovalService.approve_with_firefly(
    *,
    payment_kind: Literal["direct", "x402"],
    policy_hash: str,
    requirements: Mapping[str, Any],
    signer: SignerIdentity,
    firefly: FireflyApprovalProvider,
    purpose: str,
    assertions: Mapping[str, Any],
    resource_url: str | None = None,
    x402_version: int | None = None,
    selected_requirement: Mapping[str, Any] | None = None,
) -> PaymentApprovalRecord

PaymentApprovalService.create_pending_imessage(
    *,
    policy_hash: str,
    requirements: Mapping[str, Any],
    signer: SignerIdentity,
    purpose: str,
    resource_url: str,
    x402_version: int,
    selected_requirement: Mapping[str, Any],
) -> PaymentApprovalRecord

PaymentApprovalService.record_imessage_decision(
    approval_id: str,
    *,
    approved: bool,
    approved_hash: str | None = None,
    channel_approval_id: str | None = None,
    approval_method: str | None = None,
    provider_failed: bool = False,
) -> PaymentApprovalRecord

PaymentApprovalService.execute_once(
    *,
    approval_id: str,
    assertions: Mapping[str, Any],
    signer_resolver: Callable[[], SignerIdentity],
    policy_validator: Callable[[PaymentApprovalRecord], None],
    paused: Callable[[], bool],
    executor: Callable[[ClaimedPaymentApproval], PaymentCallbackResult],
    before_complete: Callable[[PaymentApprovalRecord, Mapping[str, Any]], None],
) -> PaymentExecutionResult
```

`PaymentApprovalService` has no public runtime TTL knob. It uses the module
constant `APPROVAL_TTL_SECONDS = 120` for every production approval. Arbitrary
expiry values remain available only in lower-level store helpers and tests
that exercise boundary conditions.

`approve_with_firefly` and `create_pending_imessage` choose the Task 1 builder
inside the service. `approve_with_firefly` validates optional hash/commitment
assertions after building and before inserting `pending`. No caller may pass a
precomputed trusted hash.

- [ ] **Step 4: Implement deterministic hardware context and provider handling**

The service must derive exactly three lines from the complete terms:

```text
1000 0x8335..2913 eip155:8453
0x3333..3333>0x2222..2222
GET merchant.example/paid
```

The direct variant's third example is `DIRECT /probe?target=algo`. The first
line is exact amount plus deterministic asset/network abbreviations, the second
is deterministic payer-to-receiver abbreviation, and the third is `DIRECT`
plus logical resource or `GET` plus canonical host/path prefix. Each line must
be printable ASCII and at most 31 characters. Abbreviations retain
deterministic leading and trailing characters and never replace amount digits.
If all five security values cannot be represented, raise
`PaymentAuthorizationError` before inserting a row.

After context validation:

1. insert `pending`;
2. call `firefly.approve_payment_hash` with the stored commitment hash and the
   three lines;
3. accept only `approved is True` and an exact lowercase `approvedHash`;
4. persist every denial, missing/mismatched hash, or provider exception as
   `denied`, unless the 120-second row expired during the provider call, in
   which case persist `expired`;
5. persist only `deviceModel`, `deviceSerial`, `approvalMethod`, and
   `providerApprovalId`;
6. return no raw provider body and never persist arbitrary exception text.

For iMessage, allowlist only `channelApprovalId` and `approvalMethod`. Require
the returned hash to equal the pending row before calling `mark_approved`.
`approved=True` requires a non-empty channel ID, method, and exact hash.
Explicit denial may omit the hash; delivery/provider exceptions call this same
method with `approved=False`, `provider_failed=True`, and optional channel
metadata. Map these cases only to stable `imessage_denied`,
`imessage_unavailable`, or `imessage_hash_mismatch` codes and call
`mark_denied`; its transaction writes `expired` instead when the durable row
has already expired. No raw channel exception crosses the service boundary.

- [ ] **Step 5: Implement assertions, validation order, pause fencing, and receipts**

Before `claim`, compare every supplied compatibility assertion against the
stored row:

```python
ASSERTION_FIELDS = {
    "policyHash": "policy_hash",
    "paymentApprovalHash": "commitment_hash",
    "paymentRequirements": "executor_requirements",
}
```

For structured assertions, first copy/validate the caller value, then compare
it with `thaw_payment_json` of the frozen stored value. This preserves exact
JSON list/object semantics—tuples used only for internal freezing never make an
otherwise identical assertion fail or become caller-controlled.

For approval-time assertions, compare `paymentHash` with the built commitment
hash and `paymentCommitment` with the complete built terms before `pending`:

```python
APPROVAL_ASSERTION_FIELDS = frozenset({"paymentHash", "paymentCommitment"})
```

After assertions, return a valid stored `completed` receipt immediately with
`replayed=True`; do not resolve a signer or rerun the spend validator, whose
used-intent check would correctly reject a new payment but must not erase a
historical success. For every non-completed row, call `signer_resolver`, require
the result to equal the stored identity, and call `policy_validator(record)`
before claim. Validation failure must leave an approved row unclaimed until
expiry. To close a completion race, reread once after a preclaim validation
failure; return replay only if that reread is now a valid `completed` row,
otherwise re-raise the original validation error.

After a successful claim, call `paused()` before `executor`. If paused, persist
`cancelled_before_sign` with code `transactions_paused` and invoke no
payment-capable callback. Treat entry into `executor` as the handoff boundary:
every exception from that point, including selector rejection or receipt
validation, persists `outcome_unknown`.

If the immediate `mark_outcome_unknown` write itself fails, return a stable
store-unavailable error without retrying the payment callback. The row remains
`executing`; the five-minute lease recovery changes it to `outcome_unknown` on
the next successful store startup/read. Test that sequence explicitly.

Define this set beside `validate_payment_receipt` in `payment_approvals.py` and
import it into the service; do not duplicate it. Allow only these receipt keys:

```python
RECEIPT_KEYS = frozenset(
    {
        "txId",
        "network",
        "receiver",
        "amountAtomic",
        "asset",
        "paymentIntent",
        "policyHash",
        "paymentApprovalHash",
        "payer",
        "selectedCommitmentHash",
        "signerInvocationCount",
    }
)
```

Require exact equality with the stored network, receiver, amount, asset,
payment intent, policy hash, approval hash, and payer. For CDP receipts, also
require `selectedCommitmentHash == commitment_hash` and
`type(signerInvocationCount) is int` and `signerInvocationCount == 1`; a
boolean is not an integer for this boundary. Direct and AVM adapters must emit
the common fields but may omit the two CDP-only fields. `txId` must be
non-empty printable ASCII of at most 256 characters. Missing, mistyped, or
contradictory required fields becomes `outcome_unknown`.
Immediately copy, validate, allowlist, and recursively freeze the callback
receipt; all following comparisons and persistence use this private trusted
snapshot, never the callback-owned mapping or a record returned by the
callback.

The callback may separately return a transient resource result with only
`ok`, `status`, and `resourceBody`. Validate `ok` as boolean and `status` as an
integer in `100..599`. Deep-copy `resourceBody` by strict JSON serialization
with `allow_nan=False`, iteratively enforce depth 64 and 100,000 nodes, reject
unsupported objects, and cap the resulting UTF-8 bytes at 1 MiB. Return this
object only on the first in-process completion. Never place it in SQLite, a
receipt, an event, or an error/log message; completed replay sets
`transient_result=None` and returns only the cached receipt.

After receipt allowlisting/validation and before the SQLite `completed`
transition, invoke `before_complete(frozen_record, frozen_receipt)`. Policy or
user spend recording lives in this hook, not inside a signer/client adapter.
The hook receives only recursively read-only values; persist a freshly thawed
copy of the service's original validated receipt after it returns. Hook
failure becomes `outcome_unknown`; replay never invokes the hook.

Perform assertion validation before the completed-replay branch. For a
non-completed row, perform signer/policy validation before asking the store to
claim. If a claim race returns a completed record, return its stored receipt
with `replayed=True` and do not call `paused` or `executor`.
Construct every `PaymentExecutionResult` with freshly thawed plain dict/list
copies of the validated receipt and transient result, so HTTP serialization or
caller mutation can never expose or modify the store's frozen record.

- [ ] **Step 6: Run focused service/store tests**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_approvals.py sign402-gateway/tests/test_payment_authorization.py -v
git diff --check
```

Expected: all focused tests PASS and no whitespace errors.

- [ ] **Step 7: Commit the authorization service**

```bash
git add sign402-gateway/sign402_gateway/payment_approvals.py sign402-gateway/sign402_gateway/payment_authorization.py sign402-gateway/tests/test_payment_approvals.py sign402-gateway/tests/test_payment_authorization.py
git commit -m "feat: enforce one-shot payment authorization"
```

---

### Task 4: Make Every Operator Bankr x402 Path Fail Closed

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py:2680-2799`
- Modify: `sign402-gateway/sign402_gateway/server.py:3559-3612`
- Modify: `sign402-gateway/sign402_gateway/bitrefill_runner.py:420-491`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/tests/test_bitrefill_runner.py`

**Invariant:** The stable error is
`exact approval binding is unavailable for Bankr x402`, and no configuration
or test flag can bypass it.

- [ ] **Step 1: Replace positive Bankr-payment tests with failing-boundary tests**

Add or rename to these exact tests:

- `test_external_x402_buyer_rejects_singit_before_firefly_or_bankr`
- `test_bankr_cli_x402_payment_client_is_fail_closed_before_block_fetch_or_subprocess`
- `test_operator_bankr_bitrefill_fails_before_firefly_or_cli`
- `test_operator_bankr_bitrefill_legacy_opt_in_cannot_bypass_exact_binding`
- retain `test_wallet_runner_fulfills_without_bankr_x402_payment`
- retain Bankr pricing, swap, and non-x402 LLM-credit tests unchanged.

Every new test supplies mocks for Firefly, the block-number fetcher, and the
subprocess runner and asserts zero calls.

- [ ] **Step 2: Run the boundary tests and observe the current unsafe calls**

```bash
cd sign402-gateway
env PYTHONPATH=.:../sign402-bridge:../payment-executor:../demo-resource-server:../live-demo:../hermes-plugins/sign402-wallet ../payment-executor/.venv/bin/python -m unittest tests.test_gateway_server.GatewayServerTests.test_external_x402_buyer_rejects_singit_before_firefly_or_bankr tests.test_gateway_server.GatewayServerTests.test_bankr_cli_x402_payment_client_is_fail_closed_before_block_fetch_or_subprocess tests.test_bitrefill_runner.BitrefillRunnerTests.test_operator_bankr_bitrefill_fails_before_firefly_or_cli tests.test_bitrefill_runner.BitrefillRunnerTests.test_operator_bankr_bitrefill_legacy_opt_in_cannot_bypass_exact_binding -v
cd ..
```

Expected: FAIL because the current code still reaches approval or the CLI.

- [ ] **Step 3: Add the hard stops at all three boundaries**

In `ExternalX402Buyer`, reject `_is_singit_x402_requirement` immediately after
strict challenge selection and before commitment construction or Firefly.

Make `BankrCliX402PaymentClient.__call__` unconditionally raise
`PaymentExecutionUnavailable` before its block-number fetcher and subprocess
runner. Delete its payment-capable branch rather than guarding it with an
environment variable.

In `BitrefillPurchaseRunner.buy`, detect the operator Bankr x402 funding path
and raise the same exception before `approve_payment_hash` and before
`bankr_payment_client`. Do not change `WalletBitrefillPurchaseRunner`.

- [ ] **Step 4: Run Bankr and gateway regressions**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_bitrefill_runner.py sign402-gateway/tests/test_gateway_server.py -q
```

Expected: both suites PASS, including managed-wallet Bitrefill and non-payment
Bankr operations.

- [ ] **Step 5: Commit the fail-closed boundary**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/sign402_gateway/bitrefill_runner.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/tests/test_bitrefill_runner.py
git commit -m "fix: disable unbound Bankr x402 payments"
```

---

### Task 5: Enforce the Same Exact Terms in the Node CDP Boundary

**Files:**
- Create: `cdp-x402-service/src/payment-terms.mjs`
- Create: `cdp-x402-service/src/x402-buyer.mjs`
- Create: `cdp-x402-service/test/payment-terms.test.mjs`
- Create: `cdp-x402-service/test/x402-buyer.test.mjs`
- Modify: `cdp-x402-service/src/payment-guard.mjs`
- Modify: `cdp-x402-service/test/payment-guard.test.mjs`
- Modify: `cdp-x402-service/src/index.mjs:25-169`

**Interfaces:**

```text
parseJsonObjectRejectingDuplicateKeys(rawText: string) -> object
canonicalizePaymentTermsV2(terms: object) -> string
hashPaymentTermsV2(terms: object) -> string
makeExactPaymentRequirementsSelector(
    approvedEnvelope: object,
    context: { effectiveResource: string, purpose: string },
) -> PaymentRequirementsSelector
wrapSingleUseSigner(signer: EvmSigner) -> SingleUseEvmSigner
makeValidatedChallengeFetch(fetchImpl: typeof fetch) -> typeof fetch
readResponseBodyBounded(response: Response, maxBytes: number) -> Promise<Uint8Array>
writeJsonResultBounded(value: object, maxBytes: number) -> void
buyPaidResourceWithSigner({
    url: string,
    signer: EvmSigner,
    signerBackend: string,
    approvedEnvelope: object,
    fetchImpl: typeof fetch,
    wrapFetch: typeof wrapFetchWithPaymentFromConfig,
}) -> Promise<object>
readApprovedEnvelopeFromStdin(inputStream: NodeJS.ReadableStream) -> Promise<object>
```

- [ ] **Step 1: Add shared-vector and invalid-domain tests**

Load
`new URL("../../test-fixtures/payment-terms-v2.json", import.meta.url)` from
the test module and add these exact Node test names:

- `canonicalizes every shared expectedCanonicalJson byte identically`
- `hashes every shared canonical fixture identically`
- `canonicalizes numeric-looking and prototype keys lexicographically`
- `builds the Base candidate to the shared approved envelope`
- `rejects duplicate object keys before JSON parse`
- `rejects floats unsafe integers unicode controls and invalid extra keys`
- `rejects sparse arrays and array extra properties`
- `rejects symbol non-enumerable and accessor JSON lookalikes`
- `rejects credentials fragments dot segments default ports and bad percent escapes`
- `rejects URL controls spaces and parser normalization before URL construction`
- `rejects post and non-empty request body digest`
- `rejects malformed canonical JSON and hash envelope`
- `rejects every shared invalid vector`
- `rejects duplicate keys in raw live challenge before selector`
- `rejects decimal and exponent tokens in raw live challenge before selector`
- `rejects excessive JSON depth and node count before selector`
- `rejects oversized raw challenge while streaming before selector`
- `does not sign a valid-header challenge with an oversized companion body`

For duplicate keys, pass this literal raw stdin value to the duplicate-safe
parser:

```javascript
const raw =
  '{"approvalId":"a","commitment":{"version":2,"extra":{"name":"USDC","name":"EVIL"}},"canonicalPaymentCommitment":"x","paymentApprovalHash":"y"}';
assert.throws(
  () => parseJsonObjectRejectingDuplicateKeys(raw),
  /duplicate JSON key: name/,
);
```

- [ ] **Step 2: Replace cap-selector tests with exact-mutation tests**

Delete the positive test that permits the first requirement when caps are
absent. The selector must never have an unapproved fallback.

For a valid approved envelope, mutate one field per subtest:

- x402 version;
- scheme;
- network;
- exact amount;
- receiver;
- asset;
- timeout;
- each `extra` value and one added `extra` key;
- effective resource;
- candidate-declared resource;
- HTTP method;
- empty-body digest;
- payment intent;
- policy hash;
- signer backend;
- payer.

Each mutation must throw before the signer mock is observed. Add the exact test
names:

- `selects only the byte-identical approved candidate`
- `rejects mutation of each bound candidate field`
- `rejects candidate declared resource mismatch`
- `rejects no exact candidate without fallback`
- `rejects wrong signer backend or payer`

- [ ] **Step 3: Add paid-fetch lifecycle tests**

Use injected fake `fetchImpl` and `wrapFetch`; never open a socket. Add:

- `rejects redirect response before signing`
- `permits exactly one signTypedData invocation`
- `blocks recovery driven second signing invocation`
- `rejects oversized paid resource after one sign without retry`
- `rejects deeply nested or node-dense paid JSON after one sign without retry`
- `does not sign when the live second challenge changed`
- `does not sign duplicate float or exponent raw second challenges`
- `rejects command url and approved resource mismatch before signing`
- `returns selected hash signer count payer and allowlisted receipt`
- `requires a valid stdin envelope for buy and buy-user`

The recovery test calls the wrapped signer's `signTypedData` twice inside the
fake paid-fetch wrapper and asserts the second call throws before the
underlying signer receives it.

- [ ] **Step 4: Run Node tests and then implement the restricted canonical domain**

First run:

```bash
cd cdp-x402-service
node --test test/payment-terms.test.mjs test/payment-guard.test.mjs test/x402-buyer.test.mjs
```

Expected: FAIL because the new modules and exact selector do not exist.

Implement the same recursive domain as Python:

```javascript
export const EMPTY_SHA256 =
  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
export const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
export const MAX_JSON_DEPTH = 64;
export const MAX_JSON_NODES = 100_000;
export const EXTRA_KEY_RE = /^[A-Za-z0-9_.:-]+$/;

function restrictedValue(value, path, depth = 0, budget = { nodes: 0 }) {
  if (depth > MAX_JSON_DEPTH) {
    throw new TypeError(`${path} exceeds maximum JSON depth`);
  }
  budget.nodes += 1;
  if (budget.nodes > MAX_JSON_NODES) {
    throw new TypeError("payment terms exceed maximum JSON node count");
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(`${path} must be a safe integer`);
    }
    return value;
  }
  if (typeof value === "string") {
    for (const character of value) {
      const code = character.codePointAt(0);
      if (code < 32 || code > 126) {
        throw new TypeError(`${path} must contain printable ASCII`);
      }
    }
    return value;
  }
  if (Array.isArray(value)) {
    const ownKeys = Reflect.ownKeys(value);
    if (ownKeys.length !== value.length + 1 || !ownKeys.includes("length")) {
      throw new TypeError(`${path} array properties are not allowed`);
    }
    const result = [];
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (
        descriptor === undefined ||
        !descriptor.enumerable ||
        !Object.prototype.hasOwnProperty.call(descriptor, "value")
      ) {
        throw new TypeError(`${path} sparse arrays are not allowed`);
      }
      result.push(
        restrictedValue(
          descriptor.value,
          `${path}[${index}]`,
          depth + 1,
          budget,
        ),
      );
    }
    return result;
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`${path} must be a plain object`);
    }
    const result = Object.create(null);
    const ownKeys = Reflect.ownKeys(value);
    if (ownKeys.some((key) => typeof key !== "string")) {
      throw new TypeError(`${path} symbol keys are not allowed`);
    }
    for (const key of ownKeys.sort()) {
      if (!EXTRA_KEY_RE.test(key)) {
        throw new TypeError(`${path} contains an invalid key`);
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (
        descriptor === undefined ||
        !descriptor.enumerable ||
        !Object.prototype.hasOwnProperty.call(descriptor, "value")
      ) {
        throw new TypeError(`${path}.${key} must be an enumerable data value`);
      }
      result[key] = restrictedValue(
        descriptor.value,
        `${path}.${key}`,
        depth + 1,
        budget,
      );
    }
    return result;
  }
  throw new TypeError(`${path} contains an unsupported value`);
}

function canonicalJson(value) {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "number" ||
    typeof value === "string"
  ) {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
    .join(",")}}`;
}

export function canonicalizePaymentTermsV2(terms) {
  return canonicalJson(restrictedValue(terms, "paymentTerms"));
}

export function hashPaymentTermsV2(terms) {
  return createHash("sha256")
    .update(canonicalizePaymentTermsV2(terms), "utf8")
    .digest("hex");
}
```

Implement `parseJsonObjectRejectingDuplicateKeys` as a bounded recursive JSON
parser, not `JSON.parse` alone:

- skip only JSON whitespace;
- decode a string by locating its closing quote with escape tracking and then
  applying `JSON.parse` to that one string token;
- parse objects into `Object.create(null)`, with a `Set` of decoded keys, and
  throw on reuse; never assign untrusted `__proto__` to a normal object;
- parse arrays recursively;
- accept only `null`, `true`, `false`, and the integer token grammar
  `-?(0|[1-9][0-9]*)`;
- reject decimal points/exponents and every non-safe-integer number;
- enforce maximum depth 64 and maximum parsed node count 100,000 before
  descending or allocating the next value;
- require one top-level object and end-of-input;
- stream stdin in chunks, check the running total before concatenation, destroy
  the input on overflow, and cap it at 256 KiB before parsing.

Implement `readResponseBodyBounded` with `response.body.getReader()`. Check the
running byte count before appending each chunk, cancel the reader on overflow
or parse failure, and never call `response.text()`, `response.json()`, or
`arrayBuffer()` before the cap. Empty bodies are valid zero-byte results.

Use the same parser at the live network boundary, not only on stdin. Implement
`makeValidatedChallengeFetch(fetchImpl)`:

1. call the injected fetch exactly once with redirects rejected;
2. for every HTTP 402, clone the `Response` before x402 consumes it and always
   drain the clone through `readResponseBodyBounded` with a 1 MiB cap, even
   when a `Payment-Required` header is present;
3. reject an encoded `Payment-Required` header whose length can decode above
   1 MiB before Base64 allocation, then verify the decoded size; when the
   header is absent, use the already bounded body bytes as the raw challenge;
   when it is present, parse the bounded header but still reject an oversized
   companion body;
4. run the duplicate-safe, integer-token-only parser on those raw bytes;
5. validate the challenge's restricted values;
6. return the original unconsumed `Response` only after validation. On any
   clone overflow/validation failure, cancel both response bodies so no tee
   branch keeps buffering.

The paid x402 wrapper receives this guarded fetch. This is mandatory because
its own ordinary JSON parser would erase duplicate keys and normalize `1.0` or
`1e3` before `paymentRequirementsSelector` sees them. Tests construct raw 402
responses containing a duplicate nested `extra` key, `1.0`, and `1e3` and
require zero signer calls.

Implement the same canonical URL checks as Task 1 before constructing `URL` so
that the runtime cannot silently normalize dot segments, percent escapes,
credentials, fragments, or an explicit default port.

- [ ] **Step 5: Build candidate terms and compare canonical bytes before selection**

Validate the approved envelope shape exactly:

```json
{
  "approvalId": "opaque-random-id",
  "commitment": {},
  "canonicalPaymentCommitment": "canonical JSON bytes",
  "paymentApprovalHash": "64 lowercase hex"
}
```

Recompute canonical JSON and SHA-256 and require equality with both envelope
fields. No extra top-level envelope keys are accepted.

`makeExactPaymentRequirementsSelector` must independently canonicalize
`effectiveResource`. For each candidate:

1. independently canonicalize a candidate-declared resource when present;
2. build complete version 2 terms from the live callback's x402 version and
   candidate;
3. reuse only the approved server-owned `policyHash`, `signerBackend`, `payer`,
   and `purpose`;
4. derive payment intent from the live candidate fields;
5. select only when canonical JSON and hash both equal the approved envelope.

Do not compare a maximum; exact amount equality is mandatory. Do not select a
different-but-cheaper candidate.

- [ ] **Step 6: Add redirect rejection and the one-sign fence**

`wrapSingleUseSigner` returns a proxy preserving the address and all
non-signing properties. It increments a private counter before delegating
`signTypedData`; call two throws `Payment signer invocation limit exceeded`
without delegating. Expose the count through a non-enumerable
`signerInvocationCount` getter.

`buyPaidResourceWithSigner` must:

1. validate the envelope, normalize the resolved signer address to the
   lowercase 20-byte EVM form, and require exact equality with the approved
   payer before wrapping;
2. canonicalize `url`, require it to equal `commitment.resource`, and use that
   canonical value for the only fetch;
3. construct `ExactEvmScheme` with the one-use proxy;
4. always set `paymentRequirementsSelector`;
5. wrap `fetchImpl` with `makeValidatedChallengeFetch`, then call the injected
   paid-fetch wrapper with `redirect: "error"`,
   `method: "GET"`, and no body;
6. reject any 3xx result defensively before reading a response body;
7. stream the paid resource body through `readResponseBodyBounded` with a
   1 MiB cap, decode UTF-8 without replacement, and only then apply the
   JSON-or-text interpretation. When JSON parsing succeeds, iteratively enforce
   depth 64 and 100,000 nodes before returning it; an over-budget JSON value is
   an error, not an opaque-text fallback;
8. return resource data separately from this allowlisted receipt:

```javascript
{
  ok: response.ok,
  status: response.status,
  payer: canonicalSignerAddress,
  selectedCommitmentHash,
  signerInvocationCount,
  resourceBody,
  receipt: {
    txId,
    network,
    receiver,
    amountAtomic,
    asset,
    paymentIntent,
    policyHash,
    paymentApprovalHash,
    payer,
    selectedCommitmentHash,
    signerInvocationCount,
  },
}
```

Every receipt value comes from either the selected terms, the verified signer,
or allowlisted settlement-header transaction fields. Never copy arbitrary
settlement keys into `receipt`.

Before the CLI writes its final JSON, serialize it once, require at most
8 MiB of UTF-8 (covering worst-case escaping of the 1 MiB resource body), and
write those bytes through `writeJsonResultBounded`. No command prints the raw
body or provider error separately.

- [ ] **Step 7: Make the CLI require the stdin envelope**

Keep `index.mjs` as the command dispatcher. Both `buy` and `buy-user` call
`readApprovedEnvelopeFromStdin(process.stdin)` before creating an account or
reading a private key.

- `buy` requires `CDP_EVM_ACCOUNT_ADDRESS`, resolves the CDP account, and
  requires backend `cdp-managed`.
- `buy-user` derives the public address from `SIGN402_EVM_PRIVATE_KEY`, requires
  backend `user-wallet-base`, and keeps the key only in the environment.
- Remove `--max-atomic`, `--expected-receiver`, and `--expected-asset` from
  these commands.
- There is no command-line or environment bypass for a missing envelope.

- [ ] **Step 8: Run all Node tests and commit**

```bash
cd cdp-x402-service
npm test
cd ..
git diff --check
git add test-fixtures/payment-terms-v2.json cdp-x402-service/src/payment-terms.mjs cdp-x402-service/src/payment-guard.mjs cdp-x402-service/src/x402-buyer.mjs cdp-x402-service/src/index.mjs cdp-x402-service/test/payment-terms.test.mjs cdp-x402-service/test/payment-guard.test.mjs cdp-x402-service/test/x402-buyer.test.mjs
git commit -m "feat: guard CDP signing with exact terms"
```

Expected: the complete Node suite PASS and no whitespace errors.

---

### Task 6: Expose Signer Identities and Bind Python CDP Clients to the Envelope

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py:2407-2462`
- Modify: `sign402-gateway/sign402_gateway/server.py:3615-3708`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `cdp-x402-service/.env.example`

**Interfaces:**

```python
@dataclass(frozen=True)
class BoundPaymentExecutor:
    signer: SignerIdentity
    invoke: Callable[[dict[str, Any], str], dict[str, Any]]

    def __call__(
        self,
        requirements: dict[str, Any],
        policy_hash: str,
    ) -> dict[str, Any]:
        return self.invoke(requirements, policy_hash)


@dataclass(frozen=True)
class BoundX402SignatureBuilder:
    signer: SignerIdentity
    invoke: Callable[[dict[str, Any]], str]

    def __call__(self, payment_required: dict[str, Any]) -> str:
        return self.invoke(payment_required)
```

The existing factory names remain stable but return these callable objects.

- [ ] **Step 1: Write failing identity and client-boundary tests**

Add these exact tests:

- `test_payment_executor_factory_exposes_algorand_signer_identity`
- `test_avm_signature_factory_exposes_algorand_signer_identity`
- `test_cdp_base_x402_client_requires_configured_payer`
- `test_cdp_base_x402_client_sends_complete_envelope_on_stdin`
- `test_cdp_base_x402_client_does_not_put_terms_on_argv_or_env`
- `test_cdp_base_x402_client_rejects_selected_hash_payer_or_count_mismatch`
- `test_clients_reject_coerced_ok_status_and_boolean_signer_count`
- `test_user_wallet_base_x402_client_keeps_key_in_env_and_terms_on_stdin`
- `test_user_wallet_base_x402_client_rejects_post_before_process`
- `test_clients_return_only_normalized_resource_and_receipt_fields`
- `test_bounded_node_runner_kills_stdout_flood_without_unbounded_capture`
- `test_bounded_node_runner_kills_stderr_flood_and_timeout`
- `test_clients_reject_oversized_or_deep_node_json_output`
- `test_clients_reject_duplicate_keys_and_non_json_constants_in_node_output`

Inject a subprocess runner in every client test. Assert that the serialized
stdin contains the four exact envelope keys from Task 5, argv contains only the
command and URL, and the private key appears only in the copied child
environment for `buy-user`. Use the exact non-secret sentinel
`TEST_PRIVATE_KEY_DO_NOT_USE` in those tests.

- [ ] **Step 2: Run the focused tests and verify current contract failures**

```bash
cd sign402-gateway
env PYTHONPATH=.:../sign402-bridge:../payment-executor:../demo-resource-server:../live-demo:../hermes-plugins/sign402-wallet ../payment-executor/.venv/bin/python -m unittest tests.test_gateway_server.GatewayServerTests.test_payment_executor_factory_exposes_algorand_signer_identity tests.test_gateway_server.GatewayServerTests.test_avm_signature_factory_exposes_algorand_signer_identity tests.test_gateway_server.GatewayServerTests.test_cdp_base_x402_client_sends_complete_envelope_on_stdin tests.test_gateway_server.GatewayServerTests.test_user_wallet_base_x402_client_keeps_key_in_env_and_terms_on_stdin -v
cd ..
```

Expected: FAIL because factories expose no identity and clients still use
loose caps/argv.

- [ ] **Step 3: Return callable signer-bound objects from factories**

`build_payment_executor` and `build_x402_payment_signature_builder` must resolve
the configured Algorand sender address once and bind:

```python
SignerIdentity(backend="algorand-local", payer=configured_sender)
```

Missing or invalid sender identity makes the covered executor unavailable
before approval. Preserve the callable behavior expected by existing callers.

- [ ] **Step 4: Make the CDP treasury payer mandatory and inject runners**

Change `CdpBaseX402PaymentClient` to require:

```text
BoundedSubprocessRunner(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str,
    timeout: int,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> subprocess.CompletedProcess[str]

CdpBaseX402PaymentClient(
    *,
    service_dir: Path,
    payer: str,
    runner: BoundedSubprocessRunner = run_bounded_subprocess,
)

CdpBaseX402PaymentClient.__call__(
    resource_url: str,
    *,
    approved_envelope: Mapping[str, Any],
) -> dict[str, Any]
```

Validate `payer` as a canonical EVM address during construction. The server
must not lazily discover a different CDP account after approval. Use
`CDP_EVM_ACCOUNT_ADDRESS` as the configured public identity and fail closed
when the CDP path is selected without it.

Invoke:

```python
runner(
    ["node", "src/index.mjs", "buy", "--url", resource_url],
    cwd=service_dir,
    env=child_env,
    input=json.dumps(
        thaw_payment_json(approved_envelope),
        separators=(",", ":"),
    ),
    timeout=120,
    stdout_limit_bytes=8 * 1024 * 1024,
    stderr_limit_bytes=64 * 1024,
)
```

Implement `run_bounded_subprocess` with `subprocess.Popen` and
`selectors.DefaultSelector`: interleave bounded stdin writes and fixed-size
stdout/stderr reads under a monotonic deadline. Check each running byte total
before appending. On timeout or either limit, kill and reap the child, discard
captured bytes, and raise only a stable error code. Never use
`communicate()`, `capture_output=True`, or a post-capture length check.

Cap the serialized stdin envelope at 256 KiB before spawning. Do not add terms,
hashes, or wallet material to argv. Parse stdout only after a zero exit status;
use duplicate-key and `NaN`/`Infinity`-rejecting hooks, then require one JSON
object with depth at most 64 and at most 100,000 nodes before field
normalization. Do not return stderr or raw stdout in an error.

- [ ] **Step 5: Apply the same contract to the user-wallet client**

`UserWalletBaseX402PaymentClient.__call__` accepts:

```text
UserWalletBaseX402PaymentClient.__call__(
    self,
    resource_url: str,
    *,
    private_key: str,
    approved_envelope: Mapping[str, Any],
) -> dict[str, Any]
```

Validate GET/no body before constructing the child environment. Copy the
environment, add only `SIGN402_EVM_PRIVATE_KEY`, send the envelope on stdin,
and invoke `buy-user --url resource_url`. Never log or persist the key.

Both clients require the Node result's `selectedCommitmentHash`, payer, and
signer count to equal the approved envelope and signer identity. Before
normalization, validate the untrusted Node object without coercion:

```python
ok = payload.get("ok")
status = payload.get("status")
signer_invocation_count = payload.get("signerInvocationCount")
if type(ok) is not bool:
    raise PaymentClientError("invalid_node_result")
if type(status) is not int or not 100 <= status <= 599:
    raise PaymentClientError("invalid_node_result")
if type(signer_invocation_count) is not int or signer_invocation_count != 1:
    raise PaymentClientError("invalid_node_result")
```

In particular, reject string booleans, numeric strings, and boolean signer
counts; Python's `bool(...)`/`int(...)` coercions and the equality
`True == 1` are forbidden at this trust boundary. Normalize the result to:

```python
{
    "ok": ok,
    "status": status,
    "resourceBody": payload.get("resourceBody"),
    "receipt": allowlisted_receipt,
}
```

- [ ] **Step 6: Update configuration and run focused regressions**

Change `cdp-x402-service/.env.example` so
`CDP_EVM_ACCOUNT_ADDRESS=0x0000000000000000000000000000000000000000`
is required for the treasury `buy` command. Do not put a real address in the
example.

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -p 'test_gateway_server.py' -q
git diff --check
```

Expected: gateway tests PASS without spawning Node.

- [ ] **Step 7: Commit the signer/client boundary**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py cdp-x402-service/.env.example
git commit -m "feat: bind CDP clients to approved envelopes"
```

---

### Task 7: Wire the Store into the Gateway and Replace the Legacy Hash Credential

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py:22-113`
- Modify: `sign402-gateway/sign402_gateway/server.py:500-737`
- Modify: `sign402-gateway/sign402_gateway/server.py:1880-1943`
- Modify: `sign402-gateway/sign402_gateway/server.py:2186-2541`
- Modify: `sign402-gateway/sign402_gateway/server.py:4380-4435`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/.env.example`

**Interfaces:**
- `POST /approve-payment` returns a new opaque `approvalId`.
- `POST /execute-payment` accepts that ID; a hash alone cannot execute.
- `AgentStateStore.read_policy_by_hash(policy_hash)` revalidates the exact
  stored policy rather than selecting whichever same-asset policy is newest.

- [ ] **Step 1: Write failing constructor, CLI, policy-lookup, and handler tests**

Update `DummyServer` with a real temporary `PaymentApprovalStore` and mocked
`PaymentApprovalService`. Add these exact tests:

- `test_build_server_injects_payment_approval_service_into_handlers`
- `test_build_server_fails_closed_when_approval_store_cannot_initialize`
- `test_cli_passes_payment_approval_store_path_and_imessage_store_path`
- `test_default_payment_approval_database_is_gitignored`
- `test_agent_state_reads_exact_policy_by_hash`
- `test_approve_payment_recomputes_v2_terms_and_ignores_caller_as_authority`
- `test_approve_payment_rejects_random_hash_before_firefly`
- `test_approve_payment_rejects_mismatched_complete_commitment_before_firefly`
- `test_approve_payment_requires_requirements_not_only_old_hash`
- `test_approve_payment_infers_direct_only_for_exact_legacy_shape`
- `test_approve_payment_requires_explicit_x402_kind`
- `test_execute_payment_requires_approval_id`
- `test_execute_payment_rejects_random_approval_hash_assertion`
- `test_execute_payment_rejects_tampered_compatibility_assertions`
- `test_execute_payment_rejects_changed_policy_or_signer_before_claim`
- `test_execute_payment_maps_unknown_expired_conflict_and_store_errors`
- `test_execute_payment_concurrent_calls_execute_once`
- `test_execute_payment_completed_replay_does_not_execute_or_record_spend`
- `test_approve_and_execute_response_dtos_thaw_frozen_records`
- `test_completed_replay_response_is_plain_json_serializable_data`
- `test_execute_payment_survives_server_restart_and_uses_stored_terms`
- `test_execute_payment_pause_after_claim_cancels_before_executor`
- `test_execute_payment_post_handoff_failure_is_unknown`
- `test_payment_handlers_never_echo_provider_subprocess_or_store_text`
- `test_legacy_operator_and_global_pause_guards_remain_effective`

Every `build_server` test passes a temporary
`payment_approval_store_path`; none may create the runtime default.

The random-hash test must pass syntactically valid requirements plus `"b" * 64`
and assert Firefly and the store remain untouched. The restart test constructs
a second server/service against the same temporary SQLite file and completes
the first server's approved ID.

- [ ] **Step 2: Run the focused gateway tests and observe old behavior**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -p 'test_gateway_server.py' -q
```

Expected: FAIL because the service is not injected and the handlers still trust
the caller's hash/requirements split.

- [ ] **Step 3: Add the default path, builder parameter, and CLI plumbing**

At module constants:

```python
DEFAULT_PAYMENT_APPROVAL_STORE_PATH = (
    ROOT_DIR / "demo-dashboard" / "private" / "payment-approvals.sqlite3"
)
```

Add this parameter to `build_server`:

```text
payment_approval_store_path: Path = DEFAULT_PAYMENT_APPROVAL_STORE_PATH
```

Construct one `PaymentApprovalStore` and one `PaymentApprovalService` after
`AgentStateStore`; inject it into `Sign402GatewayServer` for the migrated
legacy handlers. Do not partially inject it into buyers whose call contracts
are still legacy in this task: Task 8 updates and injects
`AgentBuyProbeRunner`/`ExternalX402Buyer`, and Task 9 updates and injects
`UserWalletX402Buyer`. This keeps every task's full-suite gate coherent instead
of adding an unused transitional constructor dependency.

Add:

```text
--payment-approval-store-path
SIGN402_PAYMENT_APPROVAL_STORE_PATH
```

to the CLI/env layer, pass the path through `main`, and print it with other
runtime paths. At the same time, add the already-supported
`imessage_approval_store_path` to parser and `main` pass-through so the new
wiring does not preserve that existing omission.

Update `sign402-gateway/.env.example` with:

```dotenv
SIGN402_PAYMENT_APPROVAL_STORE_PATH=../demo-dashboard/private/payment-approvals.sqlite3
CDP_EVM_ACCOUNT_ADDRESS=0x0000000000000000000000000000000000000000
```

- [ ] **Step 4: Add exact policy lookup and use it during execution**

Beside `read_policy_for_requirement`, implement:

```python
def read_policy_by_hash(self, policy_hash: str) -> dict[str, Any]:
    normalized_hash = str(policy_hash)
    if normalized_hash != normalized_hash.lower() or not HEX_32_RE.fullmatch(normalized_hash):
        raise ValueError("policyHash must be 64 lowercase hex characters")
    state = self._read_state()
    policy_state = self._policy_approvals_by_hash(state).get(normalized_hash)
    if not isinstance(policy_state, dict):
        raise ValueError("No Firefly-approved policy matches policyHash.")
    return copy.deepcopy(policy_state)
```

Use the existing `_policy_approvals_by_hash` compatibility helper instead of
adding a second index. The execution preflight loads this exact hash, requires
its stored Firefly policy approval hash to match, and calls
`validate_policy_allows` with
`thaw_payment_json(approval.executor_requirements)`.

- [ ] **Step 5: Replace `/approve-payment` processing**

Keep the existing legacy operator guard and initial transaction-pause check.
Parse:

```json
{
  "paymentKind": "direct",
  "policyHash": "64 lowercase hex",
  "paymentRequirements": {},
  "paymentHash": "optional exact assertion",
  "paymentCommitment": {}
}
```

Processing must be:

1. validate the exact policy by hash;
2. recognize omitted `paymentKind` as `direct` only when the requirement is
   exactly the current `algorand-testnet`/`ALGO_TEST` direct shape with no x402
   fields;
3. require explicit `x402` and strictly select its version 2 raw requirement;
4. resolve the current signer identity, require the policy's allowed purpose
   to equal `LEGACY_DIRECT_PURPOSE` for direct or `INTERNAL_X402_PURPOSE` for
   x402, pass that route constant to the builder, and reject a contradictory
   caller requirement purpose;
5. pass optional `paymentHash` and complete `paymentCommitment` as assertions
   to `PaymentApprovalService.approve_with_firefly`;
6. let the service build terms, compare those assertions before `pending`, and
   then perform its durable Firefly workflow;
7. return only:

```json
{
  "approvalId": "opaque id",
  "paymentApprovalHash": "stored v2 hash",
  "paymentCommitment": {},
  "policyHash": "stored policy hash",
  "expiresAt": 1800000120,
  "status": "approved",
  "firefly": {
    "approved": true,
    "approvedHash": "stored v2 hash",
    "deviceModel": 262,
    "deviceSerial": 1056,
    "approvalMethod": "firefly"
  }
}
```

Build the compatibility Firefly object from allowlisted stored metadata, not
the raw provider response. Build the entire response DTO with
`thaw_payment_json` at this one serialization boundary; never hand a
`MappingProxyType` or tuple directly to the HTTP encoder.

Map `/approve-payment` outcomes exactly:

| Condition | HTTP/body |
|---|---|
| approved with exact hash | `200`, `status: "approved"` |
| explicit provider denial | `400`, `status: "denied"`, `failureCode: "provider_denied"` |
| approved hash missing/mismatched | `409`, `status: "denied"`, `failureCode: "provider_hash_mismatch"` |
| expired while provider was pending | `410`, `status: "expired"` |
| provider unavailable after `pending` | `503`, stored `denied`/`provider_unavailable` or `expired` |
| store unavailable before/during persistence | `503`, stable error only; never claim approval success |
| validation/assertion/context error before pending | `400`, no approval ID |

Every response containing an approval ID includes only the stored commitment,
expiry, status, safe failure code, and allowlisted provider metadata.
If a decision write fails, do not fabricate a terminal status or return an
executable ID; a still-pending row can only expire and cannot be claimed.

- [ ] **Step 6: Replace `/execute-payment` processing**

Require:

```json
{"approvalId": "opaque id"}
```

Accept `policyHash`, `paymentApprovalHash`, and `paymentRequirements` only as
optional exact assertions. Pass a current-signer resolver and a
`policy_validator` closure around `read_policy_by_hash` and
`validate_policy_allows`; the service invokes neither for an already completed
replay.

The executor closure must:

1. call the bound payment executor with only
   `thaw_payment_json(claimed.record.executor_requirements)` and
   `claimed.record.policy_hash`;
2. normalize its proof into the common Task 3 receipt;
3. return `PaymentCallbackResult(receipt=receipt, transient_result=None)`.

Pass a `before_complete` hook that calls `AgentStateStore.record_payment` once
using the stored policy hash, intent, and amount after the service validates
the receipt.

If executor, proof normalization, spend recording, or completion persistence
fails after step 1 begins, the service leaves no retryable approval and records
`outcome_unknown`. A completed replay bypasses this closure, so spend is not
counted twice.

Preserve `policyHash`, `paymentApprovalHash`, and `payment` in the successful
response and add:

```json
{
  "approvalId": "opaque id",
  "status": "completed",
  "replayed": false
}
```

First completion and replay both build fresh plain response DTOs from the
service result. Mutating or JSON-encoding either response cannot affect the
frozen store record or the next replay.

Map errors exactly:

| Condition | HTTP |
|---|---:|
| malformed input or assertion mismatch | 400 |
| unknown approval | 404 |
| expired approval | 410 |
| pending, denied, `cancelled_before_sign`, executing, or `outcome_unknown` | 409 |
| unavailable/corrupt approval store, including completed without receipt | 503 |
| first completion or completed replay | 200 |

Map internal exceptions to stable non-sensitive error codes. Never put
`str(exc)`, raw provider bodies, SQLite messages, stderr, or Node stdout into
these two handler responses or logs.

- [ ] **Step 7: Run focused handler and full gateway tests**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_terms.py sign402-gateway/tests/test_payment_approvals.py sign402-gateway/tests/test_payment_authorization.py -v
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
git diff --check
```

Expected: all gateway tests PASS, with no real database outside temporary test
directories.

- [ ] **Step 8: Commit the gateway/API migration**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/.env.example
git commit -m "feat: require durable approval IDs for execution"
```

---

### Task 8: Move Internal Direct, AVM, and CDP Buyers onto Claimed Stored Terms

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py:2186-2404`
- Modify: `sign402-gateway/sign402_gateway/server.py:2544-2799`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Verify unchanged: `payment-executor/tests/test_executor.py`

**Invariant:** Once an approval is claimed, no original provider object or
caller-owned requirements object crosses into a signer.

- [ ] **Step 1: Write failing internal-buyer tests**

Add these exact tests:

- `test_agent_buy_probe_uses_durable_v2_approval_before_direct_executor`
- `test_build_server_injects_service_and_pause_into_internal_buyers`
- `test_agent_buy_probe_records_spend_inside_one_shot_callback`
- `test_external_x402_buyer_rejects_post_before_fetch_or_firefly`
- `test_external_x402_buyer_rejects_initial_redirect_before_firefly`
- `test_external_x402_buyer_rejects_selected_requirement_over_64k_before_firefly`
- `test_external_x402_buyer_rejects_changed_signer_identity_before_firefly`
- `test_external_x402_buyer_cdp_passes_only_claimed_v2_envelope`
- `test_external_x402_buyer_uses_only_canonical_stored_resource_for_transport`
- `test_external_x402_buyer_cdp_mismatched_selected_hash_marks_unknown`
- `test_external_x402_buyer_avm_reconstructs_only_stored_selected_requirement`
- `test_external_x402_buyer_avm_never_passes_original_provider_response`
- `test_external_x402_buyer_avm_paid_redirect_is_not_followed_and_marks_unknown`
- `test_external_x402_buyer_avm_over_budget_paid_json_marks_unknown`
- `test_external_x402_buyer_client_exception_is_not_retried`
- `test_external_x402_buyer_records_spend_once_before_completion`
- retain `test_build_x402_avm_payment_signature_header_filters_to_algorand_accept`

Use a malicious provider object containing marker keys and secrets outside the
selected requirement. After execution, assert the signer argument, approval
database bytes, receipt, and event contain none of those markers.

- [ ] **Step 2: Run focused buyer/executor tests and observe direct-sign bypasses**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -p 'test_gateway_server.py' -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s payment-executor/tests -p 'test_executor.py' -q
```

Expected: FAIL because the runners still call Firefly and signers directly.

- [ ] **Step 3: Migrate `AgentBuyProbeRunner`**

Keep its first local resource probe and policy validation. Replace version 1
hash approval and direct executor invocation with:

1. require constructor-injected `payment_approval_service` and `paused`,
   update `build_server` to pass the shared service and `_purchases_paused`,
   then resolve `self.payment_executor.signer`;
2. call `approve_with_firefly(payment_kind="direct")`;
3. call `execute_once` with exact-policy preflight and the current pause
   predicate;
4. adapt the direct executor proof to
   `PaymentCallbackResult(receipt=normalized_receipt,
   transient_result=None)`, passing it only
   `thaw_payment_json(claimed.record.executor_requirements)`;
5. build the existing payment proof from the stored terms and validated
   receipt;
6. retry only resource access with the already completed proof; never retry the
   payment executor;
7. write `approvalId`, status, and replay flag to the event.

Pass `record_payment` as the one-shot service's `before_complete` hook. Remove
the old direct `build_payment_commitment` authorization from this runner.

- [ ] **Step 4: Establish `ExternalX402Buyer` pre-approval order**

Its new order is:

1. require the same constructor-injected service/pause dependencies, then
   reject `request_body is not None` and every method except GET;
2. canonicalize the caller URL and fetch that canonical URL once;
3. reject duplicate keys, strictly normalize, select one raw requirement, and
   enforce the 64 KiB cap;
4. reject SINGIT/Bankr using Task 4;
5. read and validate the exact policy;
6. require `policy["allowedPurpose"] == INTERNAL_X402_PURPOSE`, pass that
   constant as purpose, and resolve AVM or CDP `SignerIdentity`;
7. build and durably approve version 2 terms;
8. claim through `PaymentApprovalService.execute_once`;
9. enter exactly one payment-capable callback.

Caller-supplied display/marketing context may be recorded in a non-security
event field but cannot replace or prepend the service's three Firefly lines.

- [ ] **Step 5: Reconstruct the AVM input from the claimed row**

Inside the AVM callback, build only:

```python
payment_required = {
    "x402Version": claimed.record.terms["x402Version"],
    "accepts": [thaw_payment_json(claimed.record.selected_requirement)],
}
```

Require a present selected requirement and recheck its compact canonical UTF-8
size at the boundary. Pass `payment_required` to the AVM signature builder.
Never pass the original 402 response or its other candidates after claim.
Call `fetch_x402_paid_resource` with
`claimed.record.terms["resource"]`, never the outer `resource_url`.

Normalize the AVM settlement result to the common receipt and return
`PaymentCallbackResult` with only `ok`, `status`, and `resourceBody` in its
transient result. The service, not the adapter, decides what is persisted.

- [ ] **Step 6: Pass the complete claimed envelope to CDP**

Build:

```python
approved_envelope = {
    "approvalId": claimed.record.approval_id,
    "commitment": thaw_payment_json(claimed.record.terms),
    "canonicalPaymentCommitment": claimed.record.canonical_json,
    "paymentApprovalHash": claimed.record.commitment_hash,
}
```

Pass it to `CdpBaseX402PaymentClient`. Require its already-normalized receipt
to contain the same selected hash, payer, and signer count, then return a
`PaymentCallbackResult` containing that receipt plus the client's normalized
transient resource result. Treat every client exception after callback entry
as `outcome_unknown`; never refetch, reapprove, or call another payment client
automatically.

The CDP client's URL argument is also
`claimed.record.terms["resource"]`. A raw caller URL, noncanonical alias, or
first-response object must not cross the claim boundary.

Pass `record_payment` as the service's `before_complete` hook so it runs after
receipt validation and before durable completion. Return the paid resource
payload only through `PaymentExecutionResult.transient_result`; keep it outside
the persisted receipt and every durable event.

- [ ] **Step 7: Run internal-buyer, executor, and Node regressions**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s payment-executor/tests -q
npm --prefix cdp-x402-service test
git diff --check
```

Expected: all three suites PASS and payment-capable mocks observe one call per
approval.

- [ ] **Step 8: Commit internal buyer binding**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/tests/test_gateway_server.py
git commit -m "feat: execute internal buyers from claimed terms"
```

---

### Task 9: Bind User-Wallet iMessage Approval and Claim Before Key Decryption

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py:1384-1467`
- Modify: `sign402-gateway/sign402_gateway/server.py:2186-2404`
- Modify: `sign402-gateway/sign402_gateway/server.py:3644-3708`
- Modify: `sign402-gateway/sign402_gateway/server.py:3986-4057`
- Modify: `sign402-gateway/sign402_gateway/server.py:4195-4380`
- Modify: `sign402-gateway/sign402_gateway/server.py:5640-5725`
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py:985-1148`
- Modify: `sign402-gateway/sign402_gateway/imessage_approvals.py:1700-1765`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/tests/test_imessage_approvals.py`

**Invariant:** The payment authorization is pending before iMessage delivery,
approved only for the same complete terms hash, and claimed before any private
key is decrypted or passed to Node.

- [ ] **Step 1: Write failing complete-envelope and ordering tests**

Add these exact tests:

- `test_user_wallet_policy_hash_binds_effective_limit_snapshot`
- `test_build_server_injects_service_and_pause_into_user_wallet_buyer`
- `test_user_wallet_imessage_approval_embeds_complete_v2_terms`
- `test_user_wallet_imessage_payment_approval_expires_in_120_seconds`
- `test_user_wallet_channel_expiry_equals_durable_payment_expiry`
- `test_user_wallet_payment_row_exists_before_imessage_request`
- `test_user_wallet_rejects_mismatched_imessage_terms_hash`
- `test_user_wallet_uses_payment_terms_hash_not_channel_commitment_hash`
- `test_user_wallet_maps_channel_statuses_to_terminal_payment_status`
- `test_user_wallet_rejects_post_before_fetch_imessage_decrypt_or_node`
- `test_user_wallet_claims_before_private_key_decryption`
- `test_user_wallet_cdp_receives_only_claimed_v2_envelope`
- `test_user_wallet_uses_only_canonical_stored_resource_for_fetch_and_node`
- `test_user_wallet_concurrent_claim_invokes_decrypt_and_node_once`
- `test_user_wallet_completed_replay_returns_receipt_without_node`
- `test_user_wallet_unknown_outcome_never_retries_node`
- `test_user_wallet_spend_record_is_inside_one_shot_execution`
- `test_user_wallet_event_records_channel_and_payment_approval_ids`
- `test_imessage_store_does_not_persist_reduced_or_raw_provider_requirements`

Use ordered mock side effects that append `payment_pending`, `imessage`,
`claimed`, `decrypt`, and `node`; require that exact order. The POST test
supplies a tool with `requestBody` and asserts even the initial fetch is not
called.

- [ ] **Step 2: Run the focused tests and observe current reduced snapshot/key order**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_imessage_approvals.py -v
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -p 'test_gateway_server.py' -q
```

Expected: FAIL because iMessage stores a reduced snapshot for ten minutes and
the handler decrypts before a durable claim.

- [ ] **Step 3: Define the server-owned user-wallet policy hash**

Version 2 requires a real policy hash even though this path uses
`UserSpendLimitStore` instead of `AgentStateStore`. Add a deterministic helper
that hashes only this canonical effective policy snapshot:

```python
def user_wallet_policy_hash(
    telegram_user_id: str,
    limit_settings: Mapping[str, Any],
) -> str:
    policy = {
        "version": 1,
        "kind": "user-wallet-spend-limits",
        "telegramUserId": _require_telegram_user_id(telegram_user_id),
        "maxPerTxAtomic": limit_settings["maxPerTxAtomic"],
        "dailyCapAtomic": limit_settings["dailyCapAtomic"],
        "operatorCeilingPerTxAtomic": limit_settings["operatorCeilingPerTxAtomic"],
        "operatorCeilingDailyAtomic": limit_settings["operatorCeilingDailyAtomic"],
    }
    canonical = canonicalize_payment_terms_v2(policy)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Refactor `_enforce_user_wallet_spend_limits` to return the exact effective
limit settings it validated. Build the hash from that snapshot before pending
approval. The execution preflight rereads effective settings and requires the
same hash. Daily spend remains a separate non-atomic P1b concern and is not
included in the hash.

- [ ] **Step 4: Replace the iMessage reduced snapshot with complete terms**

Change the purchase method contract to:

```text
request_purchase_approval(
    *,
    telegram_user_id: str,
    tool_name: str,
    payment_authorization_id: str,
    payment_expires_at: int,
    payment_terms: Mapping[str, Any],
    payment_terms_hash: str,
) -> dict[str, Any]
```

The method reads the wallet's public address, requires it to equal
`payment_terms["payer"]`, and persists this canonical channel envelope:

```json
{
  "schemaVersion": 2,
  "actionType": "sign402_purchase",
  "walletAddress": "0x1111111111111111111111111111111111111111",
  "toolName": "approved tool name",
  "paymentAuthorizationId": "opaque payment id",
  "paymentTerms": {},
  "paymentTermsHash": "64 lowercase hex",
  "nonce": "32 lowercase hex",
  "createdAt": 1800000000,
  "expiresAt": 1800000120
}
```

Recompute the terms canonical JSON/hash before writing. Set a dedicated
payment-purchase limit to 120 seconds, and require the supplied
`payment_expires_at` to satisfy `now < payment_expires_at <= now + 120`; use
that exact supplied expiry in the channel envelope without recomputing it. The
handler must pass `pending_record.expires_at`, and the ordering test requires
the channel row's expiry to equal the durable payment row's expiry byte-for-byte.
Do not change unrelated test-approval lifetimes. Retire
`_approval_payment_requirements` from this path.

Return both:

```json
{
  "approvalId": "channel approval id",
  "paymentAuthorizationId": "payment approval id",
  "paymentTermsHash": "v2 hash",
  "commitmentHash": "channel envelope hash",
  "approvalMethod": "imessage",
  "status": "approved"
}
```

Every return after the channel row is created, including denial, expiry,
delivery failure, and timeout, includes that row's `approvalId`,
`paymentAuthorizationId`, and `paymentTermsHash`. Failures before channel-row
creation may omit the channel ID; the handler still terminalizes its already
created payment row through the optional-metadata decision contract.

The user-facing message can retain friendly tool text, but its bound resource,
amount, asset, network, payer, and receiver must be derived from the embedded
terms.

- [ ] **Step 5: Reorder the handler around pending approval and exact decision**

`_handle_agent_buy_tool_for_user` must:

1. authenticate and reject a body-bearing tool before fetch;
2. preflight the private event store;
3. resolve wallet public address without decrypting a key;
4. canonicalize the tool URL, then fetch/strictly select the GET challenge at
   that canonical URL;
5. validate Base USDC and the effective spend-limit snapshot;
6. create a pending payment authorization with backend `user-wallet-base` and
   `USER_WALLET_X402_PURPOSE`;
7. request iMessage approval using its stored terms/hash and exact
   `pending_record.expires_at`;
8. call `record_imessage_decision` with the returned decision/hash and optional
   channel metadata; on delivery/provider exception call it with
   `approved=False` and `provider_failed=True`;
9. pass the approved payment authorization ID and a key-loader callback to
   `UserWalletX402Buyer`.

Delivery failure, denial, or mismatched hash leaves an unexpired payment row
terminally `denied`; if its durable expiry elapsed before the decision write,
the store records terminal `expired` instead. Do not expose raw provider
errors or the private key to the handler's event or error payload.

Use this exact channel-to-payment mapping:

| iMessage result | `record_imessage_decision` arguments |
|---|---|
| `status == "approved"` | `approved=True`, `approved_hash=approval["paymentTermsHash"]`, `channel_approval_id=approval["approvalId"]`, `approval_method="imessage"` |
| `status == "denied"` | `approved=False`, `provider_failed=False`, `approval_method="imessage"`, optional channel ID |
| `delivery_failed`, `approval_channel_not_linked`, `approval_pending`, `wallet_missing`, `missing`, `timeout`, or provider exception | `approved=False`, `provider_failed=True`, `approval_method="imessage"`, optional channel ID |
| `expired` | `approved=False`, `approval_method="imessage"`; the equal durable expiry makes `mark_denied` persist `expired` |

Never pass the channel envelope's `commitmentHash`, an `approvedHash` alias, or
the handler's known expected hash as the returned `approved_hash`; the
iMessage result must independently echo `paymentTermsHash`, and absence or
mismatch is terminal `imessage_hash_mismatch`.

- [ ] **Step 6: Make `UserWalletX402Buyer` own claim-to-Node execution**

Change its boundary to accept:

```text
UserWalletX402Buyer(
    *,
    base_payment_client,
    payment_approval_service,
    paused,
)

buyer(
    resource_url,
    *,
    payment_authorization_id,
    channel_approval_id,
    signer,
    private_key_loader,
    policy_validator,
    spend_recorder,
    payment_context=None,
)
```

It must not accept an approval dictionary or payment requirements as trusted
execution input. Update `build_server` in this task to pass the already-created
shared `PaymentApprovalService` and `_purchases_paused`; direct constructor
tests inject temporary services and mutable pause fakes.

Call `PaymentApprovalService.execute_once`. Only inside the claimed executor
callback:

1. call `private_key_loader`;
2. build the four-key approved envelope from the claimed row, using
   `thaw_payment_json` only for its `commitment` value;
3. call `UserWalletBaseX402PaymentClient` with
   `claimed.record.terms["resource"]`;
4. return `PaymentCallbackResult(receipt=normalized_receipt,
   transient_result={"ok": client_result["ok"],
   "status": client_result["status"],
   "resourceBody": client_result.get("resourceBody")})`.

Pass `spend_recorder` as `before_complete`; it runs once after common receipt
validation and before completion persistence.

The final event uses stored terms and records:

```json
{
  "approvalId": "iMessage channel id",
  "paymentAuthorizationId": "durable payment id",
  "paymentApprovalHash": "v2 hash",
  "status": "completed",
  "replayed": false
}
```

Any exception after entering the client callback is `outcome_unknown` and
cannot trigger automatic Node retry. A completed duplicate returns the cached
receipt with `transient_result=None` and skips key decryption, Node, and spend
recording. The HTTP response may include the first completion's transient
resource result, but the durable event above never does.

- [ ] **Step 7: Run user-wallet, iMessage, gateway, and Node tests**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_imessage_approvals.py -v
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
npm --prefix cdp-x402-service test
git diff --check
```

Expected: all tests PASS; no test invokes Messages, Node, CDP, or a paid URL
without a mock.

- [ ] **Step 8: Commit the user-wallet binding**

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/sign402_gateway/imessage_approvals.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/tests/test_imessage_approvals.py
git commit -m "feat: bind iMessage approval to exact payment"
```

---

### Task 10: Document the Boundary and Run the Full Offline Release Gate

**Files:**
- Create: `scripts/tests/test_payment_approval_docs.py`
- Modify: `README.md`
- Modify: `sign402-gateway/README.md`
- Modify: `sign402-gateway/SECURITY.md`
- Modify: `cdp-x402-service/README.md`
- Modify: `sign402-gateway/.env.example`
- Modify: `cdp-x402-service/.env.example`

- [ ] **Step 1: Create one exact failing documentation contract test**

Create `scripts/tests/test_payment_approval_docs.py`:

```python
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class PaymentApprovalDocumentationTests(unittest.TestCase):
    def test_gateway_documents_opaque_two_step_execution(self):
        readme = _text("sign402-gateway/README.md")
        self.assertIn("approve → approvalId → execute", readme)
        self.assertIn("not an execution credential", readme)
        self.assertIn('"paymentKind": "direct"', readme)
        self.assertIn('"approvalId":', readme)

    def test_security_document_binds_every_signing_field(self):
        security = _text("sign402-gateway/SECURITY.md")
        for field in (
            "httpMethod",
            "requestBodySha256",
            "amountAtomic",
            "receiver",
            "asset",
            "network",
            "maxTimeoutSeconds",
            "extra",
            "resource",
            "purpose",
            "policyHash",
            "signerBackend",
            "payer",
        ):
            self.assertIn(field, security)
        self.assertIn("outcome_unknown", security)
        self.assertIn("cancelled_before_sign", security)
        self.assertIn("five-minute execution lease", security)
        self.assertIn("demo-dashboard/private/payment-approvals.sqlite3", security)

    def test_cdp_documents_exact_stdin_guard(self):
        readme = _text("cdp-x402-service/README.md")
        self.assertIn("canonicalPaymentCommitment", readme)
        self.assertIn("paymentApprovalHash", readme)
        self.assertIn("stdin", readme)
        self.assertIn("exact second challenge", readme)
        self.assertIn("one signing invocation", readme)
        self.assertIn("CDP_EVM_ACCOUNT_ADDRESS", readme)

    def test_bankr_boundary_and_p1b_limit_are_explicit(self):
        root = _text("README.md")
        security = _text("sign402-gateway/SECURITY.md")
        stable_error = "exact approval binding is unavailable for Bankr x402"
        self.assertIn(stable_error, root)
        self.assertIn(stable_error, security)
        self.assertIn("managed-wallet Bitrefill", security)
        self.assertIn("Bankr pricing and swap", security)
        self.assertIn("LLM-credit", security)
        self.assertIn("P1b", security)
        self.assertIn("atomic", security)


if __name__ == "__main__":
    unittest.main()
```

Run:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest scripts/tests/test_payment_approval_docs.py -v
```

Expected: FAIL on missing old-prose assertions before documentation edits.

- [ ] **Step 2: Update API, security, CDP, and root documentation**

Update:

- `sign402-gateway/README.md` operator examples to send complete requirements
  to approval and only `approvalId` to execution;
- `sign402-gateway/SECURITY.md` with the private database, state machine, CAS,
  attempt fencing, five-minute lease, replay, unknown-outcome policy, receipt
  allowlist, GET-only boundary, Bankr shutdown, and the dedicated
  `demo-dashboard/private` directory that leaves the shared dashboard mode
  unchanged;
- `cdp-x402-service/README.md` so `buy` and `buy-user` are gateway-internal,
  stdin-envelope-only commands and treasury use requires
  `CDP_EVM_ACCOUNT_ADDRESS`;
- root `README.md` so it describes exact second-challenge verification instead
  of the old three-cap selector.
- both `.env.example` files with comments explaining that the private approval
  path and public CDP payer are mandatory security bindings, while approval
  envelopes arrive on stdin rather than through environment variables.

Use fake hashes, addresses, IDs, and paths in every example.

- [ ] **Step 3: Run focused security regressions**

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest sign402-gateway/tests/test_payment_terms.py sign402-gateway/tests/test_payment_approvals.py sign402-gateway/tests/test_payment_authorization.py sign402-gateway/tests/test_goplausible_adapter.py sign402-gateway/tests/test_imessage_approvals.py sign402-gateway/tests/test_bitrefill_runner.py -v
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest scripts/tests/test_payment_approval_docs.py -v
npm --prefix cdp-x402-service test
```

Expected: focused security tests PASS.

- [ ] **Step 4: Run formatting, placeholder, secret-name, and unsafe-bypass scans**

```bash
git add README.md sign402-gateway/README.md sign402-gateway/SECURITY.md cdp-x402-service/README.md sign402-gateway/.env.example cdp-x402-service/.env.example scripts/tests/test_payment_approval_docs.py
git diff --cached --check
git diff --cached --stat
git diff --cached --name-only
git diff --check
PLACEHOLDER_RE='T''BD|T''ODO|F''IXME|X''XX|implement ''later|unsafe approval ''bypass'
if rg -n "$PLACEHOLDER_RE" \
  sign402-gateway/sign402_gateway/payment_terms.py \
  sign402-gateway/sign402_gateway/payment_approvals.py \
  sign402-gateway/sign402_gateway/payment_authorization.py \
  sign402-gateway/sign402_gateway/server.py \
  cdp-x402-service/src \
  sign402-gateway/tests \
  cdp-x402-service/test; then
  exit 1
fi
test ! -e demo-dashboard/private/payment-approvals.sqlite3
test ! -e demo-dashboard/private/payment-approvals.sqlite3-journal
test ! -e demo-dashboard/private/payment-approvals.sqlite3-wal
test ! -e demo-dashboard/private/payment-approvals.sqlite3-shm
if rg -n 'ALLOW_UNBOUND_BANKR|SKIP_PAYMENT_APPROVAL|DISABLE_EXACT_SELECTOR' \
  sign402-gateway cdp-x402-service; then
  exit 1
fi
p1a_review_dir=$(mktemp -d)
p1a_review_diff="$p1a_review_dir/source-review.diff"
p1a_keyword_review="$p1a_review_dir/keyword-review.txt"
p1a_changed_files="$p1a_review_dir/changed-files.txt"
: > "$p1a_review_diff"
: > "$p1a_keyword_review"
: > "$p1a_changed_files"
chmod 700 "$p1a_review_dir"
chmod 600 "$p1a_review_diff" "$p1a_keyword_review" "$p1a_changed_files"
trap 'rm -f "$p1a_review_diff" "$p1a_keyword_review" "$p1a_changed_files"; rmdir "$p1a_review_dir"' EXIT
git diff 49725745d18360561deb3e6a702fdcc3ab9e0855 -- \
  README.md \
  .gitignore \
  test-fixtures/payment-terms-v2.json \
  sign402-gateway/sign402_gateway \
  sign402-gateway/tests \
  sign402-gateway/README.md \
  sign402-gateway/SECURITY.md \
  sign402-gateway/.env.example \
  cdp-x402-service/src \
  cdp-x402-service/test \
  cdp-x402-service/README.md \
  cdp-x402-service/.env.example \
  scripts/tests/test_payment_approval_docs.py \
  > "$p1a_review_diff"
if rg -q 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Authorization:[[:space:]]*Bearer[[:space:]]+[A-Za-z0-9._-]{12,}|"(mnemonic|seedPhrase)"[[:space:]]*:[[:space:]]*"[a-z]+([[:space:]]+[a-z]+){11,23}"' \
  "$p1a_review_diff"; then
  exit 1
fi
git diff --name-only 49725745d18360561deb3e6a702fdcc3ab9e0855 -- \
  README.md .gitignore test-fixtures/payment-terms-v2.json \
  sign402-gateway cdp-x402-service scripts/tests/test_payment_approval_docs.py \
  > "$p1a_changed_files"
while IFS= read -r p1a_changed_file
do
  if [ -f "$p1a_changed_file" ]; then
    rg -H -n -o -i \
      'private.?key|mnemonic|wallet.?secret|bearer|redemption|activation.?code' \
      "$p1a_changed_file" || true
  fi
done < "$p1a_changed_files" > "$p1a_keyword_review"
sed -n '1,$p' "$p1a_keyword_review"
```

Expected: all commands exit zero; the staged name list contains only the seven
Task 10 files; all default approval DB paths are absent and are never read.
The secret-pattern scan is quiet and exposes no matching line. The keyword
report prints only source path, line number, and the matched keyword—never the
surrounding value. Manually inspect each reported source location without
copying a possible value into logs, classifying it as a variable name,
environment-variable name, explicit fake test sentinel, or documentation
prohibition. Any literal credential, seed phrase, bearer value, redemption
value, or unexplained match blocks the commit; the trap removes all three
temporary files.

- [ ] **Step 5: Run every Python and Node suite**

From the repository root:

```bash
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-gateway/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s sign402-bridge/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s payment-executor/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s demo-resource-server/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s live-demo/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s hermes-plugins/sign402-wallet/tests -q
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest discover -s scripts/tests -q
npm --prefix cdp-x402-service test
npm --prefix singit-risk-check test
```

Expected: all nine commands exit zero. Record exact test counts in the final
implementation handoff; the total must exceed the clean 730-test baseline.

- [ ] **Step 6: Compare protected runtime metadata and inspect the final diff**

Create the after manifest without opening file contents:

```bash
for target in \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/private' \
  '/Users/mp/Documents/Berlin Hack/cdp-x402-service/.env' \
  '/Users/mp/Documents/Berlin Hack/payment-executor/.env' \
  '/Users/mp/Documents/Berlin Hack/sign402-gateway/.env.wallet-bitrefill' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/bitrefill-orders.sqlite3' \
  '/Users/mp/Documents/Berlin Hack/demo-dashboard/user-purchases.json'
do
  if [ -e "$target" ]; then
    stat -f '%N|%Sp|%z|%m' "$target"
  else
    printf '%s|MISSING\n' "$target"
  fi
done > /private/tmp/sign402-p1a-live-state-after.txt
git -C '/Users/mp/Documents/Berlin Hack' status --porcelain=v1 \
  > /private/tmp/sign402-p1a-original-status-after.txt
```

Then run:

```bash
diff -u /private/tmp/sign402-p1a-live-state-before.txt /private/tmp/sign402-p1a-live-state-after.txt
diff -u /private/tmp/sign402-p1a-original-status-before.txt /private/tmp/sign402-p1a-original-status-after.txt
git status --short
git diff --stat 49725745d18360561deb3e6a702fdcc3ab9e0855
git diff --check 49725745d18360561deb3e6a702fdcc3ab9e0855
git diff --cached --check
git log --oneline --decorate 49725745d18360561deb3e6a702fdcc3ab9e0855..HEAD
```

Expected: protected metadata is unchanged, only planned source/test/docs files
appear, no whitespace errors, and the implementation commits are easy to
review.

- [ ] **Step 7: Review every design success criterion**

Check the implementation against
`docs/superpowers/specs/2026-07-25-p1a-approval-binding-design.md` and record
evidence for each criterion:

1. exact approved terms are the signed terms;
2. one approval ID produces at most one signer invocation;
3. completed replay uses a cached receipt;
4. pause after claim cancels before signing;
5. changed CDP challenge fails before signing and becomes unknown after claim;
6. AVM receives only the stored selected requirement;
7. user-wallet iMessage binds the same complete terms/hash;
8. both autonomous Bankr x402 paths fail before approval/CLI;
9. no live state or network was touched;
10. P1b budget atomicity is explicitly not claimed.

- [ ] **Step 8: Commit documentation after all gates pass**

```bash
git add README.md sign402-gateway/README.md sign402-gateway/SECURITY.md cdp-x402-service/README.md sign402-gateway/.env.example cdp-x402-service/.env.example scripts/tests/test_payment_approval_docs.py
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
git commit -m "docs: document exact payment approval binding"
env PYTHONPATH=sign402-gateway:sign402-bridge:payment-executor:demo-resource-server:live-demo:hermes-plugins/sign402-wallet:. payment-executor/.venv/bin/python -m unittest scripts/tests/test_payment_approval_docs.py -q
git diff --check 49725745d18360561deb3e6a702fdcc3ab9e0855..HEAD
git log --oneline --decorate 49725745d18360561deb3e6a702fdcc3ab9e0855..HEAD
git status --short --branch
```

Expected: the cached name list contains exactly the seven Task 10 files, the
commit succeeds, the post-commit documentation test passes, the branch is
clean, and all release evidence is captured in the implementation handoff, not
in secret-bearing files.
