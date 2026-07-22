# Bitrefill MCP Runtime Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Sign402 Gateway's live Bitrefill v2 REST client with a headless Bitrefill eCommerce MCP client while preserving the existing agent, approval, wallet-funding, persistence, and protected-delivery flow.

**Architecture:** Keep the synchronous `BitrefillClient` protocol as the application boundary. Add a focused Streamable HTTP transport plus `McpBitrefillClient`, inject tool calls in tests, and use one SDK session per gateway operation so `ThreadingHTTPServer` threads do not share async state. Keep `TestBitrefillClient` local and remove all live REST fallback.

**Tech Stack:** Python 3.11+, `mcp>=1.27,<2`, `toons>=0.7,<1`, `httpx`, `unittest`, existing Sign402 Gateway and SQLite commerce store.

## Global Constraints

- Every live Bitrefill catalog, detail, purchase, status, and redemption call uses the eCommerce MCP server.
- Headless authentication uses `BITREFILL_API_KEY`; the key never enters logs, exceptions, store records, or responses.
- Live mode contains no `https://api.bitrefill.com/v2` path and no REST fallback.
- Test mode performs no network request and cannot spend funds.
- Public gateway endpoints and the Hermes/Telegram plugin contract remain unchanged.
- Existing confirmation, Sign402 approval, wallet ownership, spending caps, invoice-overage checks, single-use fulfillment, and protected delivery remain authoritative.
- Products requiring `submit-prepayment-step` fail before `buy-products`; conversational prepayment is outside this migration.
- Automated tests never perform a real purchase.

## File Structure

- Create `sign402-gateway/sign402_gateway/bitrefill_mcp.py` for SDK transport, response decoding, and the live adapter.
- Create `sign402-gateway/tests/test_bitrefill_mcp.py` for MCP-only unit coverage with injected fakes.
- Modify `sign402-gateway/sign402_gateway/bitrefill.py` to retain the protocol/test client and remove the REST live client.
- Modify `sign402-gateway/sign402_gateway/server.py` to select `McpBitrefillClient` in live mode.
- Modify `sign402-gateway/tests/test_gateway_server.py` and `test_bitrefill_client.py` for the cutover.
- Modify `sign402-gateway/pyproject.toml` for stable MCP v1 and TOON dependencies.
- Modify operator env/script/README/security documentation for the MCP runtime.

---

### Task 1: MCP Transport and Result Decoder

**Files:**
- Create: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Create: `sign402-gateway/tests/test_bitrefill_mcp.py`
- Modify: `sign402-gateway/pyproject.toml`

**Interfaces:**
- Produces: `decode_mcp_tool_result(result: Any, *, max_bytes: int = 1_048_576) -> dict[str, Any]`
- Produces: `McpToolCaller(server_url: str, *, timeout_seconds: float = 60.0, max_response_bytes: int = 1_048_576)`
- Produces: `McpToolCaller.__call__(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Write failing decoder tests**

Add fake MCP result blocks and tests for structured, JSON, TOON, tool-error, oversized, and scalar responses:

```python
class FakeText:
    def __init__(self, text):
        self.text = text


class FakeToolResult:
    def __init__(self, *, structured=None, text="", is_error=False):
        self.structuredContent = structured
        self.content = [FakeText(text)] if text else []
        self.isError = is_error


def test_decoder_accepts_toon_text(self):
    result = decode_mcp_tool_result(
        FakeToolResult(text="products[1]{id,name}:\n  steam-usa,Steam")
    )
    self.assertEqual(result["products"][0]["name"], "Steam")


def test_decoder_hides_tool_error_text(self):
    with self.assertRaisesRegex(ValueError, "Bitrefill MCP tool failed"):
        decode_mcp_tool_result(FakeToolResult(text="key_123", is_error=True))
```

- [ ] **Step 2: Verify RED**

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpDecodeTests -v`.

Expected: import failure because `bitrefill_mcp` does not exist.

- [ ] **Step 3: Add dependencies**

Add exactly:

```toml
"mcp>=1.27,<2",
"toons>=0.7,<1",
```

to the existing dependency list.

- [ ] **Step 4: Implement bounded decoding**

Implement JSON-first, TOON-second decoding without upstream text in errors:

```python
def decode_mcp_tool_result(result, *, max_bytes=MAX_MCP_RESPONSE_BYTES):
    if bool(getattr(result, "isError", False)):
        raise ValueError("Bitrefill MCP tool failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return deepcopy(structured)
    text = "\n".join(
        str(block.text) for block in getattr(result, "content", [])
        if hasattr(block, "text")
    ).strip()
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Bitrefill MCP response is too large")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        try:
            decoded = toons.loads(text)
        except Exception as exc:
            raise ValueError("Bitrefill MCP returned malformed data") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Bitrefill MCP returned a non-object response")
    return decoded
```

- [ ] **Step 5: Write and verify a failing transport lifecycle test**

Patch SDK contexts and assert initialize -> list tools -> call tool. Assert `repr(caller)` omits `key_123`.

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpTransportTests -v`.

Expected: failure because `McpToolCaller` is absent.

- [ ] **Step 6: Implement the Streamable HTTP caller**

Use the stable SDK API:

```python
class McpToolCaller:
    def __call__(self, tool_name, arguments):
        try:
            return asyncio.run(self._call(tool_name, arguments))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Bitrefill MCP request failed") from exc

    async def _call(self, tool_name, arguments):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds), follow_redirects=False
        ) as http_client:
            async with streamable_http_client(
                self._server_url, http_client=http_client
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if tool_name not in {tool.name for tool in tools.tools}:
                        raise ValueError(
                            f"required Bitrefill MCP tool is unavailable: {tool_name}"
                        )
                    result = await session.call_tool(
                        tool_name, arguments=deepcopy(arguments)
                    )
                    return decode_mcp_tool_result(
                        result, max_bytes=self.max_response_bytes
                    )
```

Store the URL only in `_server_url`; return a redacted custom `__repr__`.

- [ ] **Step 7: Verify GREEN and commit**

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpDecodeTests tests.test_bitrefill_mcp.BitrefillMcpTransportTests -v`.

Expected: all Task 1 tests pass.

Commit:

```bash
git add sign402-gateway/pyproject.toml sign402-gateway/sign402_gateway/bitrefill_mcp.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "Add Bitrefill MCP tool transport"
```

---

### Task 2: MCP Catalog, Details, and Quotes

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Modify: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Produces: `McpBitrefillClient` implementing `list_products`, `search_products`, `get_product_details`, and `quote_product`.
- Consumes: injected `call_tool(name, arguments) -> dict` and the existing normalized gateway shapes.

- [ ] **Step 1: Write failing catalog tests**

Use a recording fake:

```python
class FakeMcpCaller:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return deepcopy(self.responses.pop(0))
```

Assert `search-products` receives query/country, `get-product-details` receives `product_id` and `currency="USD"`, and packages preserve full ID plus `package_value`:

```python
self.assertEqual(details["packages"][0], {
    "packageId": "steam-usa<&>50",
    "value": "50",
    "priceUsd": "50.25",
})
```

Also test ranges, recipient requirements, pagination slicing, country mismatch, local category/type filtering, and prepayment detection.

- [ ] **Step 2: Verify RED**

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpCatalogTests -v`.

Expected: `McpBitrefillClient` import/attribute failure.

- [ ] **Step 3: Implement constructor and secret-safe URL**

Validate the key, HTTPS MCP URL, caps, overage, and payment method. Build the headless URL internally:

```python
server_url = f"{mcp_url.rstrip('/')}/{urllib.parse.quote(key, safe='')}"
self._call_tool = call_tool or McpToolCaller(server_url)
```

- [ ] **Step 4: Implement catalog/details normalization**

Call only:

```python
self._call_tool("search-products", {
    "query": query_text,
    "country": primary_country,
    "per_page": 100,
})
self._call_tool("get-product-details", {
    "product_id": product_id_text,
    "currency": "USD",
})
```

Accept snake/camel aliases and return the existing normalized contract, including `requiresPrepayment`.

- [ ] **Step 5: Implement quote validation**

Select by full ID or value, validate recipient fields, reject prepayment, and enforce `max_purchase_usd` before returning `productId`, `packageId`, `packageValue`, `priceUsd`, and recipient metadata.

- [ ] **Step 6: Verify GREEN and commit**

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpCatalogTests tests.test_bitrefill_quote -v`.

Commit:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_mcp.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "Route Bitrefill catalog through MCP"
```

---

### Task 3: MCP Purchase, Payment, Polling, and Redemption

**Files:**
- Modify: `sign402-gateway/sign402_gateway/bitrefill_mcp.py`
- Modify: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Produces: `buy_product(...)` and `refresh_purchase(...)` compatible with `BitrefillFulfillmentRunner` and `lookup_bitrefill_order`.

- [ ] **Step 1: Write and verify a failing balance-purchase test**

Fake responses: `buy-products` returns `invoice_id`, then `get-invoice-by-id` returns a complete invoice with nested delivered order/redemption. Assert calls are exactly:

```python
["buy-products", "get-invoice-by-id"]
```

and cart mapping is:

```python
{"cart_items": [{"product_id": "steam-usa", "package_id": "50"}],
 "payment_method": "balance", "return_payment_link": False}
```

Run the single test and expect failure because `buy_product` is absent.

- [ ] **Step 2: Implement cart mapping and balance purchase**

Use `packageValue`, never the display-only full package ID. Map the committed phone/email/account/username to `refill_input`. Checkpoint only invoice ID/status/payment/order IDs, then poll through `get-invoice-by-id` and normalize the nested order.

- [ ] **Step 3: Write and verify failing Base USDC safety tests**

Use the documented MCP shape:

```python
{"invoice_id": "inv_2", "status": "unpaid", "payment_info": {
    "address": "0xBitrefill", "amount": "5.01", "currency": "USDC",
    "network": "base", "contract_address": BASE_USDC_MAINNET,
}}
```

Prove valid payment transfers once and wrong amount/currency/network/contract/address transfers zero times.

- [ ] **Step 4: Implement Base USDC checks and transfer**

Require `USDC`, Base/8453, the configured Base USDC contract, a nonempty address, amount under the live cap, and amount within quote-overage basis points. Only then call:

```python
self.treasury_client.transfer_usdc(
    to_address=address, amount=format(payment_amount, "f"), chain="base"
)
```

- [ ] **Step 5: Implement bounded polling and refresh**

Poll only `get-invoice-by-id`. Stop on `complete`; fail immediately on `blocked`, `denied`, or `payment_error`; never retry invoice creation. `refresh_purchase` uses the same tool once.

- [ ] **Step 6: Verify GREEN and commit**

Run:

```bash
cd sign402-gateway
python3 -m unittest tests.test_bitrefill_mcp.BitrefillMcpPurchaseTests tests.test_bitrefill_mcp.BitrefillMcpUsdcPurchaseTests tests.test_bitrefill_runner -v
```

Commit:

```bash
git add sign402-gateway/sign402_gateway/bitrefill_mcp.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "Route Bitrefill purchases through MCP"
```

---

### Task 4: Live Factory Cutover and REST Removal

**Files:**
- Modify: `sign402-gateway/sign402_gateway/server.py`
- Modify: `sign402-gateway/sign402_gateway/bitrefill.py`
- Modify: `sign402-gateway/tests/test_gateway_server.py`
- Modify: `sign402-gateway/tests/test_bitrefill_client.py`
- Modify: `sign402-gateway/tests/test_bitrefill_mcp.py`

**Interfaces:**
- Produces: `build_bitrefill_client_from_env()` selecting only `TestBitrefillClient` or `McpBitrefillClient`.

- [ ] **Step 1: Write and verify failing factory tests**

Assert live mode with an API key returns `McpBitrefillClient`, missing key fails, and `SIGN402_BITREFILL_BASE_URL=https://attacker.example/v2` is ignored.

Run the focused factory tests and expect failure because the factory still returns `LiveBitrefillClient`.

- [ ] **Step 2: Cut the factory over**

Return:

```python
McpBitrefillClient(
    api_key=values["BITREFILL_API_KEY"],
    mcp_url=values.get("SIGN402_BITREFILL_MCP_URL", "https://api.bitrefill.com/mcp"),
    max_purchase_usd=values.get("SIGN402_BITREFILL_LIVE_MAX_USD", "5.00"),
    max_invoice_overage_bps=int(values.get(
        "SIGN402_BITREFILL_LIVE_MAX_INVOICE_OVERAGE_BPS", "500"
    )),
    payment_method=payment_method,
    treasury_client=treasury_client,
)
```

Do not read `SIGN402_BITREFILL_BASE_URL`.

- [ ] **Step 3: Remove REST live implementation and tests**

Delete `LiveBitrefillClient`, urllib HTTP transport, `/products`, `/invoices`, `/orders`, and response-reader code from `bitrefill.py`. Retain protocol, test client, fixtures, and shared helpers needed by MCP.

- [ ] **Step 4: Add a no-REST guard test**

Read `bitrefill.py`, `bitrefill_mcp.py`, and `server.py` and assert absence of:

```python
"api.bitrefill.com/v2"
'"/invoices"'
"urllib.request.urlopen"
```

- [ ] **Step 5: Verify GREEN and commit**

Run `cd sign402-gateway && python3 -m unittest tests.test_bitrefill_client tests.test_bitrefill_mcp tests.test_gateway_server -v`.

Commit:

```bash
git add sign402-gateway/sign402_gateway/server.py sign402-gateway/sign402_gateway/bitrefill.py sign402-gateway/tests/test_gateway_server.py sign402-gateway/tests/test_bitrefill_client.py sign402-gateway/tests/test_bitrefill_mcp.py
git commit -m "Cut Bitrefill live runtime over to MCP"
```

---

### Task 5: Operator Configuration, Documentation, and Full Verification

**Files:**
- Modify: `sign402-gateway/.env.wallet-bitrefill.example`
- Modify: `sign402-gateway/scripts/run-wallet-bitrefill.sh`
- Modify: `sign402-gateway/README.md`
- Modify: `sign402-gateway/SECURITY.md`

**Interfaces:**
- Produces: operator contract using `BITREFILL_API_KEY` and optional `SIGN402_BITREFILL_MCP_URL`.

- [ ] **Step 1: Write and verify failing config/script assertions**

Assert the launch path contains `SIGN402_BITREFILL_MCP_URL` and contains neither `SIGN402_BITREFILL_BASE_URL` nor `api.bitrefill.com/v2`.

- [ ] **Step 2: Update operator configuration**

Add to the env example:

```dotenv
# Headless eCommerce MCP; keep BITREFILL_API_KEY secret.
SIGN402_BITREFILL_MCP_URL=https://api.bitrefill.com/mcp
```

Export in the run script without printing the derived key-bearing URL:

```bash
export SIGN402_BITREFILL_MCP_URL="${SIGN402_BITREFILL_MCP_URL:-https://api.bitrefill.com/mcp}"
```

- [ ] **Step 3: Update README and security guidance**

Document the exact runtime path, server-side key, no REST fallback, and manual-only real smoke purchase:

```text
Hermes/Telegram -> Sign402 Gateway -> Bitrefill eCommerce MCP -> Base USDC payment -> protected redemption
```

- [ ] **Step 4: Run complete gateway suite**

Run `cd sign402-gateway && python3 -m unittest discover -s tests -v`.

Expected: zero failures/errors.

- [ ] **Step 5: Run complete Hermes plugin suite**

Run `cd hermes-plugins/sign402-wallet && python3 -m unittest discover -s tests -v`.

Expected: zero failures/errors and only local gateway calls.

- [ ] **Step 6: Verify no live REST path or secret leak**

Run:

```bash
rg -n "api\.bitrefill\.com/v2|SIGN402_BITREFILL_BASE_URL|urllib\.request\.urlopen" sign402-gateway/sign402_gateway sign402-gateway/scripts sign402-gateway/.env.wallet-bitrefill.example
git diff --check
```

Expected: ripgrep has no matches; diff check exits 0. Inspect the diff to confirm only synthetic test credential strings exist.

- [ ] **Step 7: Commit documentation and report unrelated changes separately**

```bash
git add sign402-gateway/.env.wallet-bitrefill.example sign402-gateway/scripts/run-wallet-bitrefill.sh sign402-gateway/README.md sign402-gateway/SECURITY.md
git commit -m "Document Bitrefill MCP runtime"
git status --short
```
