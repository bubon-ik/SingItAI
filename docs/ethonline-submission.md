# ETHOnline 2026 — Continuity submission

> Historical submission record, moved from the project README. Results and
> deployment descriptions below refer to the dates recorded here. For the
> current project overview, see the [README](../README.md); for the observed
> deployment revision and runbooks, see [operations](operations.md).

This is a live custodial payment system that has existed for months, entered in
a Continuity track. So the first thing this section does is draw the line
between what was already here and what was built during the event, because the
tracks are judged on the second only.

**Two repositories, one project.** This one holds the gateway, `/v1/decide`,
Ledger key ring and purchase approval, and the chat integration with The Graph.
[`spending-memory`](https://github.com/bubon-ik/spending-memory) holds the
reusable spending-policy library and paid Graph query adapter. The gateway
uses that library; other agents can use it independently.

## What was built during the event

The event work below was developed on `ethonline`, now renamed to `main`.
Its snapshot before the repository cleanup is pinned in
[`1ca72b4..29670c2`](https://github.com/bubon-ik/SingItAI/compare/1ca72b4...29670c2ed644b584768dae7bb9dc629c26c52958)
— starting at the phase 0 findings. The latest verified implementation commits
are [Ledger `4ca9c2c`](https://github.com/bubon-ik/SingItAI/commit/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3)
and [The Graph `d7d2030`](https://github.com/bubon-ik/SingItAI/commit/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916).
Hardware checks, real payments and automated tests are recorded separately in
[the Ledger runbook](ledger-v1.md) and [the verification report](checks.md).

The private **`/graph_demo` Telegram command** also connects these integrations:
The Graph quote → readable Ledger approval on the local Mac → x402 payment →
price, indexed block and transaction link in the same Telegram chat. A repeated
query within the cache lifetime costs nothing. It is limited to the configured
owner and one new paid query in the demo state; ordinary production wallet
routes are unchanged. See the [combined demo runbook](telegram-graph-ledger-demo.md)
for setup, scope, retry protection and validation.

These additions use the hackathon branch, isolated local state and the operator's
payment wallet. The private demo command is installed in the existing Telegram
bot and connects to the local Mac. The production payment gateway keeps its
existing Trezor setup; Ledger approval is limited to the private demo route.

It does **not** start at `x402Bnkr`. That range would sweep in five commits
dated 4 September which wired Spending Memory into the payment chokepoint, and
those belong to the Sibyl Labs hackathon, not this one. They are listed as prior
work below.

| Track | What it does | Where |
| --- | --- | --- |
| Ledger | **An optional Key Ring backend for the existing wallet encryption key.** On an enrolled host, startup decrypts `SIGN402_WALLET_MASTER_KEY` through `wallet-cli ring`, into process memory, and refuses to boot if decryption fails. Hardware checks establish the enrolled-host path; they do not establish that the USB-less production VPS was migrated. | [`keyring.py`](../sign402-gateway/sign402_gateway/keyring.py) · [scope and setup](ledger-v1.md#key-ring-is-a-separate-feature) |
| Ledger | **Resumable approval for one configured owner's GET x402 tools.** A policy escalation creates a persistent order. Compact EIP-191 text shows the purchase, amount/network and recipient; a SHA-256 reference binds the entire order. The gateway verifies the signature, rechecks the quote and limits, and consumes the operation before calling the payer. Retries return the encrypted saved result. The Ledger signs consent; the gateway wallet signs the payment. | [`ledger_payments.py`](https://github.com/bubon-ik/SingItAI/blob/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3/sign402-gateway/sign402_gateway/ledger_payments.py) · [local client](https://github.com/bubon-ik/SingItAI/blob/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3/tools/ledger-approve/purchase.py) · [scope and setup](ledger-v1.md) |
| Ledger | **Device and payment verification, 9 September.** A real Otto news purchase cost 0.001 USDC on Base using the earlier EIP-712 approval. Delivery, settlement and saved-result retries passed. Separately, the final readable EIP-191 message was confirmed on a Nano S Plus and verified by the HTTP gateway with a test payer. | [real payment check](checks.md#l7--real-purchase-after-ledger-approval-9-september) · [readable device check](checks.md#l8--readable-compact-approval-9-september) · [transaction](https://basescan.org/tx/0x4ab728a10ee6c35eb76c7270aa24ff4fb02fc3f67e26b8bbf3c5fd04d2a2ffc4) |
| Ledger | Ten developer-experience findings, kept from the first command rather than written from memory. The headline entry is that the Key Ring's advertised case — a host with no USB port — has no supported path in wallet-cli 2.1.0: `ring init` needs an attached device, no verb exports a membership, and the member key sits in an OS secret service a headless box does not run. Measured on the actual VPS, not argued from the docs | [`docs/ledger-dx-notes.md`](https://github.com/bubon-ik/SingItAI/blob/244a98fd3087d3e5a4138ad57b1c605e44cd98bf/docs/ledger-dx-notes.md) |
| Ledger | Verified on the hardware, not argued: `ring init` on the device, encrypt, decrypt, then the device **unplugged** and decrypt again — and, unasked, with the network off too, which is why booting the gateway does not depend on Ledger's service being up. Then the whole path end to end against the real `wallet-cli`, including corrupting the ciphertext to confirm the refusal to boot | [`checks.md`](https://github.com/bubon-ik/SingItAI/blob/e317b6699f952a50d0a53642085a37c7286df3f3/docs/checks.md) · [rehearsal script](https://github.com/bubon-ik/SingItAI/blob/e317b6699f952a50d0a53642085a37c7286df3f3/sign402-gateway/scripts/ledger-keyring-rehearsal.sh) |
| Bazantic | `POST /v1/decide` and `GET /v1/journal`: a read-only HTTP surface over the spending policy, so an agent can ask whether a payment should happen without being able to make one happen | [`decide.py`](https://github.com/bubon-ik/SingItAI/blob/df9d39bae8540b1a22a925fbebf5a51149f6a3ed/sign402-gateway/sign402_gateway/decide.py) · [OpenAPI](https://github.com/bubon-ik/SingItAI/blob/b20cab05ba8021455ec5fed6f803b2a1c6f7fc68/sign402-gateway/docs/decide-openapi.json) |
| The Graph | An agent pays The Graph's x402 gateway per subgraph query out of a budget: the daily cap applies to a cent, the first payment escalates like any unknown merchant, a moved payout address blocks and warns the whole fleet, and a question already bought inside the cache window is answered by **reading the journal** instead of paying again | [`thegraph.py`](https://github.com/bubon-ik/spending-memory/blob/cbc0739b2842e92f7d7c698580d48284a7063960/spending_memory/adapters/thegraph.py) · [live demo](https://github.com/bubon-ik/spending-memory/blob/e4a79d3eda55a4fa6043108fc909248516415b36/demo/graph_queries.py) |
| The Graph | **In the product:** chat price questions such as "price of WETH" and `$WETH` use Uniswap V3 pool data on Base. The answer includes the pool and indexed block; cached readings are identified explicitly. The same spending policy and daily cap authorise the query. Live gateway requests work, CDP transaction hashes are retained in the journal, and indexing errors or insufficient pool liquidity cause the client to decline the answer. | [`onchain_data.py`](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/sign402_gateway/onchain_data.py) · [regression tests](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/tests/test_onchain_data.py) |
| The Graph | **Real WETH query, 11 September:** one 0.01 USDC payment on Base, followed by two free cached repeats, including one in a separate Python process with network and payment callbacks disabled. The price matched the pool's contract state at the same indexed block. The journal retained the receipt and counted the spend once. | [live verification report](checks.md#g5--real-weth-price-purchase-and-free-repeats-11-september) · [check script](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/scripts/graph-live-check.py) · [tx `0x0d2ef841…`](https://basescan.org/tx/0x0d2ef8410a230f3e1b97532d5a7bdc8ad80d0d2a988c5ded318ee78e5ed2517a) |
| The Graph · Bazantic | `SKILL.md`: what an agent must **do** about each verdict — the part no schema can carry, and the independent variable of the Bazantic experiment | [`SKILL.md`](https://github.com/bubon-ik/spending-memory/blob/cb0cdb3a791dbbba3e6d9ef1ad04c96d165c0633/skills/paying-for-data/SKILL.md) · [experiment](https://github.com/bubon-ik/SingItAI/blob/0cd35796610900182a634b481de87c634239e45c/docs/bazantic-experiment.md) |
| All | Phase 0 findings, including the two checks that failed and changed the plan | [`docs/checks.md`](https://github.com/bubon-ik/SingItAI/blob/244a98fd3087d3e5a4138ad57b1c605e44cd98bf/docs/checks.md) |

Implementation links are pinned to the verified commits. Relative runbook and
report links follow the version of the README being read.

The Graph adapter lives in **`spending-memory`**, an MIT-licensed Python library
with no dependency on this gateway. It handles payment-requirement parsing,
policy authorisation, query fingerprints, the journal cache and spending
reports. This repository supplies the application integration and a real
WETH-query check, demonstrating how another agent can use the library.

## Verified results and scope

- **Ledger:** the final compact EIP-191 approval is readable on the tested Nano
  S Plus. The real 0.001 USDC purchase used the earlier EIP-712 format; the
  final-format device check used a test payer. This establishes purchase
  consent on the device, not ERC-7730 Clear Signing or hardware custody of the
  gateway's spending wallet. The configured approval lane is described in
  [the scope notes](ledger-v1.md#scope).
- **The Graph:** the 11 September query returned **2569.701079 USDC per WETH**
  at indexed Base block **51176748**. An independent RPC check of the pool's
  tokens and `slot0()` at that block confirmed the price. Settlement at block
  **51176753** transferred exactly **0.01 USDC**. Two repeats within the default
  **300-second cache TTL** used the journal without another payment. This
  exercised the onchain client and chat routing; it was not a full live
  Telegram/LLM conversation.
- **Automated checks, 11 September:** **1178 gateway tests** passed, including
  **30 onchain tests**, plus **33 Graph adapter tests** against the pinned
  dependency. The Ledger verification also includes **six JavaScript checks**.

## What was already here

None of this is offered for judging. It is listed so that a judge reading the
branch can tell at a glance which parts of it are not new.

- **The gateway itself** — Telegram bot, custodial Base wallets, spending
  limits, Bitrefill orders, second-device approval. Months old, in production,
  handling real USDC.
- **The x402 client** and Base Mainnet settlement. Months old.
- **Spending Memory** (`spending-memory` v0.5.1) and its integration at the
  spend chokepoint — built days earlier for the Sibyl Labs hackathon, on the
  `spending-memory` branch, commits dated 3–4 September. The work in this
  submission sits on top of it: `/decide` is a read-only surface over that
  policy and adds no second decision point to the payment path.
- **Hardware approval through Trezor**, with video. Prior work, and deliberately
  not shown in the Ledger material: it is evidence that the architecture wanted
  a hardware root of trust before Ledger was in the picture, which is what makes
  the difference legible — same flow, different root of trust.

## Use of AI tools

Development was spec-driven and AI-assisted with **Claude (Opus)** and
**OpenAI Codex**. Codex assisted with the later Ledger purchase lifecycle and
readable approval work, The Graph integration fixes, regression tests, live
verification scripts and documentation.

The spec — [`docs/ethonline-spec.md`](ethonline-spec.md) — was written
first and is committed unedited, including the parts the implementation later
departed from. It fixed the phase 0 checks, the acceptance criteria and the cut
lines before any code existed. `docs/checks.md` records what those checks
actually returned, including the two that failed and changed the plan.

`WALLET_CLI_MOCK=1` swaps wallet-cli's trustchain backend and leaves the device
transport alone, so it does not give CI a device-free path and the tests use a
stand-in binary instead. And no subgraph indexes arbitrary Base addresses — none
can, because a subgraph indexes a contract's events and "every address on Base"
is not a contract — so a planned rule that would have used onchain counterparty
history as evidence was cut rather than faked, and no subgraph was written to
rescue it.

The project owner set the scope, reviewed the device display and explicitly
approved the real purchases. Hardware observations, simulated payment tests
and actual onchain settlements are distinguished in [docs/checks.md](checks.md).
The commits record the implementation changes and their rationale.

## Running the new work

From the repository root, use Python 3.11 or later to create an environment
and install the gateway:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./sign402-gateway
cd sign402-gateway
python -m unittest tests.test_ledger_keyring tests.test_ledger_approval tests.test_ledger_payments tests.test_ledger_client tests.test_decide_endpoint tests.test_onchain_data -v
```

Run the whole gateway suite with `python -m unittest discover -s tests`.
For Ledger configuration, the resumable client, the hardware HTTP rehearsal and
the exact v1 boundary, see [docs/ledger-v1.md](ledger-v1.md).

The Ledger key ring is off by default (`SIGN402_LEDGER_KEYRING_ENABLED`), so an
unprovisioned checkout behaves exactly as it did before. **No Ledger device is
needed to run these**: the tests drive a stand-in binary, for the reason in
`docs/checks.md` under L2.

For the standalone Graph adapter tests, use a separate checkout and environment:

```bash
git clone https://github.com/bubon-ik/spending-memory
cd spending-memory
git checkout cbc0739b2842e92f7d7c698580d48284a7063960
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest tests/test_thegraph_adapter.py     # adapter tests, no network
```

The earlier [standalone demonstration](https://github.com/bubon-ik/spending-memory/blob/e4a79d3eda55a4fa6043108fc909248516415b36/demo/graph_queries.py)
uses a real unpaid quote but simulates the paid fetch unless `--live-pay` is
supplied. Its simulated transcript is not mainnet payment evidence. The
application check below is the one used for the real WETH query in the
verification report.

### The Graph: real application check

From the **SingItAI repository root**, using its configured Python environment:

```bash
python sign402-gateway/scripts/graph-live-check.py prepare
# After explicitly approving the displayed 0.01 USDC purchase:
python sign402-gateway/scripts/graph-live-check.py run
# Read the saved outcome and verify the receipt without paying:
python sign402-gateway/scripts/graph-live-check.py status
```

This requires the Node dependencies and local credentials described in
[the CDP service setup](../cdp-x402-service/README.md#setup), an explicit
`CDP_EVM_ACCOUNT_ADDRESS`, and at least 0.01 USDC in that operator account on
Base. `prepare` only reads the quote, balance and historical receipt. `run`
rechecks the fixed price, recipient and asset, performs at most one payer
invocation, then reopens the database and verifies a free cached chat answer.
State stays in ignored `.graph-live/`; a permanent attempt marker prevents the
script from paying again after a restart or an uncertain result. Keep that
state and inspect the existing attempt rather than starting a replacement.

For one deliberate new video recording after the original check has completed,
add `--video-demo` to each command. This uses the fixed `.graph-live/video-demo/`
directory and verifies the original receipt before proceeding. It refuses an
unresolved original attempt and retains the same permanent single-payment guard
for the video session. Run `prepare --video-demo` first and approve its displayed
terms before `run --video-demo`; use `status --video-demo` afterwards. Terminal
output shows the unpaid 402, paid 200, indexed price and free cache reuse.
The verified payment and query report are saved before the optional balance
read. If a previous run stopped after payment, `status` can recover the report
from its saved response and journal without fetching another query or paying.

The live check configures its own isolated client. The application feature
remains opt-in through `SIGN402_ONCHAIN_DATA_ENABLED=1` with Spending Memory
enabled. It uses the operator-funded query path and does not invoke the
separate Ledger purchase-approval lane. Exact results and the additional
separate-process cache check are in [G4–G5](checks.md#g4--application-transport-receipt-and-restart-cache-1011-september).
