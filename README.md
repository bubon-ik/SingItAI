> **ETHOnline 2026 judges:** the work entered for this event, and the line
> between it and the production system it was built on, are in
> [ETHOnline 2026 — Continuity submission](#ethonline-2026--continuity-submission)
> at the bottom of this file. Everything above that heading is prior work.
>
> One project, **two repositories**. The Ledger and Bazantic work and the
> **Graph-powered chat integration** are here. The reusable Graph adapter is in
> [`bubon-ik/spending-memory`](https://github.com/bubon-ik/spending-memory) —
> the MIT library this gateway is one caller of. The submission section links
> to every file in both.

# SingIt

An AI agent that buys things for you, with spending limits and human approval
on a separate device when the payment policy requires it.

You talk to a Telegram bot. It holds a Base wallet for you, finds gift cards
and top-ups on Bitrefill, and quotes an exact price. In strict mode, covered
purchases require approval through iMessage or WhatsApp. With Spending Memory
enabled, supported x402-tool and Bitrefill payments may proceed within the
configured budget when the policy returns PAY; ESCALATE asks the owner and
BLOCK refuses the payment. This is not a universal approval policy for every
payment route: LLM credit purchases, Venice chat and web search have separate
flows.

Live at [singitai.app](https://singitai.app).

## How a purchase works

```text
Telegram          you pick a product and see an exact quote
   |
Gateway           checks spending limits and the payment policy
   |
Policy            BLOCK stops; PAY proceeds; ESCALATE asks you
   |
iMessage /        when required, approve this purchase on your phone
WhatsApp
   |
Base              USDC payment settles on Base Mainnet
   |
Bitrefill         the code is delivered
```

When requested, the approval is bound to one purchase: product, amount and recipient are
committed before you are asked, and the gateway refuses anything that does not
match what you approved.

## Using the bot

| Command | What it does |
| --- | --- |
| `/start` | Open the menu |
| `/wallet` | Create or show your managed Base wallet |
| `/balance` | Wallet balance |
| `/bitrefill` | Browse the catalog and buy |
| `/limits` | View and change spending limits |
| `/withdraw` | Move funds out to your own address |
| `/last_purchase` | Receipt for the most recent order |
| `/connect_imessage`, `/connect_whatsapp` | Set up the approval channel |
| `/llm_buy` | Top up LLM credits through Bankr |

Buying runs through the menu: **Buy Bitrefill → Browse Catalog** or **Search
Products**, then a quote and the applicable policy/approval checks.

## What protects your money

- **A separate approval channel.** Purchases requiring human approval are
  confirmed outside Telegram. Policy-authorised payments can proceed without
  another prompt within their configured limits.
- **Spending limits** enforced by the gateway, not by the agent.
- **Exact-purchase binding.** The approval covers one product at one price;
  a changed quote invalidates it.
- **The agent never sees a private key.** Wallet keys are encrypted at rest and
  used only by the gateway.

Honest limits: the wallet is custodial — keys live on the server, encrypted, so
a server compromise is a real risk. Hardware self-custody, where the key never
leaves a Trezor and you approve each payment on the device itself, is in
development on the `codex/trezor-local-sidecar` branch and is not part of the
service today.

## Components

| Path | Purpose |
| --- | --- |
| `sign402-gateway/` | The service. Wallets, limits, approvals, Bitrefill orders, payment execution |
| `hermes-plugins/sign402-wallet/` | Telegram command surface, loaded into Hermes |
| `cdp-x402-service/` | Base Mainnet payments and swaps via CDP and x402 |
| `website/` | The public site at singitai.app |
| `singit-risk-check/` | SINGIT-paid x402 endpoint for payment-requirement risk analysis |

Legacy components: `sign402-bridge` and `payment-executor` (the original
Firefly/Algorand lane), and `live-demo`. The gateway
still imports shared utilities from these directories at startup; keep them
installed even when the legacy routes are unused.

The bundled demo resource server, HTML dashboard and presentation script have
been retired. Existing gateway data under the historical `demo-dashboard/`
directory remains in place as ignored runtime state; do not delete that data
when updating a deployment. Local `.codex/` settings are also ignored and are
not required to run the service.

## Development

Running the service, the observed deployment version, and the test commands
are in [docs/operations.md](docs/operations.md). The
[documentation index](docs/README.md) separates current runbooks from historical
plans and prototype documentation.

---

# ETHOnline 2026 — Continuity submission

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

Everything below is on the `ethonline` branch, dated 5 September 2026 or later.
The diff that contains all of it, and nothing else, is
[`1ca72b4..ethonline`](https://github.com/bubon-ik/SingItAI/compare/1ca72b4...ethonline)
— starting at the phase 0 findings. The latest verified implementation commits
are [Ledger `4ca9c2c`](https://github.com/bubon-ik/SingItAI/commit/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3)
and [The Graph `d7d2030`](https://github.com/bubon-ik/SingItAI/commit/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916).
Hardware checks, real payments and automated tests are recorded separately in
[the Ledger runbook](docs/ledger-v1.md) and [the verification report](docs/checks.md).

The private **`/graph_demo` Telegram command** also connects these integrations:
The Graph quote → readable Ledger approval on the local Mac → x402 payment →
price, indexed block and transaction link in the same Telegram chat. A repeated
query within the cache lifetime costs nothing. It is limited to the configured
owner and one new paid query in the demo state; ordinary production wallet
routes are unchanged. See the [combined demo runbook](docs/telegram-graph-ledger-demo.md)
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
| Ledger | **An optional Key Ring backend for the existing wallet encryption key.** On an enrolled host, startup decrypts `SIGN402_WALLET_MASTER_KEY` through `wallet-cli ring`, into process memory, and refuses to boot if decryption fails. Hardware checks establish the enrolled-host path; they do not establish that the USB-less production VPS was migrated. | [`keyring.py`](sign402-gateway/sign402_gateway/keyring.py) · [scope and setup](docs/ledger-v1.md#key-ring-is-a-separate-feature) |
| Ledger | **Resumable approval for one configured owner's GET x402 tools.** A policy escalation creates a persistent order. Compact EIP-191 text shows the purchase, amount/network and recipient; a SHA-256 reference binds the entire order. The gateway verifies the signature, rechecks the quote and limits, and consumes the operation before calling the payer. Retries return the encrypted saved result. The Ledger signs consent; the gateway wallet signs the payment. | [`ledger_payments.py`](https://github.com/bubon-ik/SingItAI/blob/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3/sign402-gateway/sign402_gateway/ledger_payments.py) · [local client](https://github.com/bubon-ik/SingItAI/blob/4ca9c2c8289b91c4d92af10469d9d26a4a52bfb3/tools/ledger-approve/purchase.py) · [scope and setup](docs/ledger-v1.md) |
| Ledger | **Device and payment verification, 9 September.** A real Otto news purchase cost 0.001 USDC on Base using the earlier EIP-712 approval. Delivery, settlement and saved-result retries passed. Separately, the final readable EIP-191 message was confirmed on a Nano S Plus and verified by the HTTP gateway with a test payer. | [real payment check](docs/checks.md#l7--real-purchase-after-ledger-approval-9-september) · [readable device check](docs/checks.md#l8--readable-compact-approval-9-september) · [transaction](https://basescan.org/tx/0x4ab728a10ee6c35eb76c7270aa24ff4fb02fc3f67e26b8bbf3c5fd04d2a2ffc4) |
| Ledger | Ten developer-experience findings, kept from the first command rather than written from memory. The headline entry is that the Key Ring's advertised case — a host with no USB port — has no supported path in wallet-cli 2.1.0: `ring init` needs an attached device, no verb exports a membership, and the member key sits in an OS secret service a headless box does not run. Measured on the actual VPS, not argued from the docs | [`docs/ledger-dx-notes.md`](https://github.com/bubon-ik/SingItAI/blob/244a98fd3087d3e5a4138ad57b1c605e44cd98bf/docs/ledger-dx-notes.md) |
| Ledger | Verified on the hardware, not argued: `ring init` on the device, encrypt, decrypt, then the device **unplugged** and decrypt again — and, unasked, with the network off too, which is why booting the gateway does not depend on Ledger's service being up. Then the whole path end to end against the real `wallet-cli`, including corrupting the ciphertext to confirm the refusal to boot | [`checks.md`](https://github.com/bubon-ik/SingItAI/blob/e317b6699f952a50d0a53642085a37c7286df3f3/docs/checks.md) · [rehearsal script](https://github.com/bubon-ik/SingItAI/blob/e317b6699f952a50d0a53642085a37c7286df3f3/sign402-gateway/scripts/ledger-keyring-rehearsal.sh) |
| Bazantic | `POST /v1/decide` and `GET /v1/journal`: a read-only HTTP surface over the spending policy, so an agent can ask whether a payment should happen without being able to make one happen | [`decide.py`](https://github.com/bubon-ik/SingItAI/blob/df9d39bae8540b1a22a925fbebf5a51149f6a3ed/sign402-gateway/sign402_gateway/decide.py) · [OpenAPI](https://github.com/bubon-ik/SingItAI/blob/b20cab05ba8021455ec5fed6f803b2a1c6f7fc68/sign402-gateway/docs/decide-openapi.json) |
| The Graph | An agent pays The Graph's x402 gateway per subgraph query out of a budget: the daily cap applies to a cent, the first payment escalates like any unknown merchant, a moved payout address blocks and warns the whole fleet, and a question already bought inside the cache window is answered by **reading the journal** instead of paying again | [`thegraph.py`](https://github.com/bubon-ik/spending-memory/blob/cbc0739b2842e92f7d7c698580d48284a7063960/spending_memory/adapters/thegraph.py) · [live demo](https://github.com/bubon-ik/spending-memory/blob/e4a79d3eda55a4fa6043108fc909248516415b36/demo/graph_queries.py) |
| The Graph | **In the product:** chat price questions such as "price of WETH" and `$WETH` use Uniswap V3 pool data on Base. The answer includes the pool and indexed block; cached readings are identified explicitly. The same spending policy and daily cap authorise the query. Live gateway requests work, CDP transaction hashes are retained in the journal, and indexing errors or insufficient pool liquidity cause the client to decline the answer. | [`onchain_data.py`](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/sign402_gateway/onchain_data.py) · [regression tests](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/tests/test_onchain_data.py) |
| The Graph | **Real WETH query, 11 September:** one 0.01 USDC payment on Base, followed by two free cached repeats, including one in a separate Python process with network and payment callbacks disabled. The price matched the pool's contract state at the same indexed block. The journal retained the receipt and counted the spend once. | [live verification report](docs/checks.md#g5--real-weth-price-purchase-and-free-repeats-11-september) · [check script](https://github.com/bubon-ik/SingItAI/blob/d7d2030f7273e9b110ae54eb1d1dbe193b9aa916/sign402-gateway/scripts/graph-live-check.py) · [tx `0x0d2ef841…`](https://basescan.org/tx/0x0d2ef8410a230f3e1b97532d5a7bdc8ad80d0d2a988c5ded318ee78e5ed2517a) |
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
  [the scope notes](docs/ledger-v1.md#scope).
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

The spec — [`docs/ethonline-spec.md`](docs/ethonline-spec.md) — was written
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
and actual onchain settlements are distinguished in [docs/checks.md](docs/checks.md).
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
the exact v1 boundary, see [docs/ledger-v1.md](docs/ledger-v1.md).

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
[the CDP service setup](cdp-x402-service/README.md#setup), an explicit
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
separate-process cache check are in [G4–G5](docs/checks.md#g4--application-transport-receipt-and-restart-cache-1011-september).
