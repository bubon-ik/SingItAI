import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from sign402_gateway import agent_allowance as aa
from sign402_gateway.base_balances import BaseBalanceError

OWNER = "0x1111111111111111111111111111111111111111"
GUARDIAN = "0x2222222222222222222222222222222222222222"
USER = "4242"
NOW = 1_800_000_000


class FakeRpc:
    """JSON-RPC at the method level, for EvmClient."""

    def __init__(self, replies=None, failures=0):
        self.replies = dict(replies or {})
        self.calls = []
        self.failures = failures

    def call(self, method, params):
        self.calls.append((method, params))
        if self.failures:
            self.failures -= 1
            raise BaseBalanceError("Base RPC is unavailable")
        reply = self.replies[method]
        return reply(params) if callable(reply) else reply


def rpc_for_send(report=None):
    def send_raw(params):
        raw = params[0]
        return report or "0x" + keccak(bytes.fromhex(raw[2:])).hex()

    return FakeRpc({
        "eth_chainId": hex(aa.BASE_CHAIN_ID),
        "eth_getTransactionCount": "0x7",
        "eth_maxPriorityFeePerGas": hex(1_000_000),
        "eth_getBlockByNumber": {"baseFeePerGas": hex(5_000_000)},
        "eth_estimateGas": hex(100_000),
        "eth_sendRawTransaction": send_raw,
    })


class EvmClientTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def client(self, rpc):
        return aa.EvmClient(rpc, sleep=self.sleeps.append, now=lambda: NOW)

    def test_send_signs_exactly_the_transaction_it_reports(self):
        key = Account.create().key.to_0x_hex()
        rpc = rpc_for_send()
        tx_hash = self.client(rpc).send(key, to=OWNER, data="0x1234", value=5)

        raw = next(p[0] for m, p in rpc.calls if m == "eth_sendRawTransaction")
        self.assertEqual(tx_hash, "0x" + keccak(bytes.fromhex(raw[2:])).hex())
        decoded = Account.recover_transaction(raw)
        self.assertEqual(decoded, Account.from_key(key).address)
        estimate = next(p[0] for m, p in rpc.calls if m == "eth_estimateGas")
        self.assertEqual(estimate["to"], OWNER)
        self.assertEqual(estimate["value"], "0x5")

    def test_a_node_refusal_is_named_and_not_retried(self):
        class Refusing(FakeRpc):
            def call(self, method, params):
                if method == "eth_sendRawTransaction":
                    self.calls.append((method, params))
                    raise aa.RpcRejected("insufficient funds for gas * price + value")
                return super().call(method, params)

        rpc = Refusing(rpc_for_send().replies)
        with self.assertRaises(aa.AllowanceError) as raised:
            self.client(rpc).send(Account.create().key.to_0x_hex(), to=OWNER)
        self.assertIn("insufficient funds", str(raised.exception))
        self.assertEqual([m for m, _ in rpc.calls].count("eth_sendRawTransaction"), 1)
        self.assertEqual(self.sleeps, [])

    def test_an_already_known_broadcast_is_success(self):
        class AlreadyKnown(FakeRpc):
            def call(self, method, params):
                if method == "eth_sendRawTransaction":
                    raise aa.RpcRejected("already known")
                return super().call(method, params)

        key = Account.create().key.to_0x_hex()
        tx_hash = self.client(AlreadyKnown(rpc_for_send().replies)).send(key, to=OWNER)
        self.assertEqual(len(tx_hash), 66)

    def test_a_different_reported_hash_is_not_trusted(self):
        rpc = rpc_for_send(report="0x" + "ab" * 32)
        with self.assertRaises(aa.AllowanceError) as raised:
            self.client(rpc).send(Account.create().key.to_0x_hex(), to=OWNER)
        self.assertIn("Check both", str(raised.exception))

    def test_refused_reads_are_retried_then_named(self):
        rpc = FakeRpc({"eth_chainId": hex(aa.BASE_CHAIN_ID)}, failures=3)
        self.client(rpc).require_base()
        self.assertEqual(self.sleeps, [1, 2, 4])

        rpc = FakeRpc({"eth_chainId": hex(aa.BASE_CHAIN_ID)}, failures=99)
        with self.assertRaises(aa.AllowanceError) as raised:
            self.client(rpc).require_base()
        self.assertIn("Nothing was changed", str(raised.exception))

    def test_another_chain_is_refused(self):
        with self.assertRaises(aa.AllowanceError):
            self.client(FakeRpc({"eth_chainId": "0x1"})).require_base()

    def test_the_chain_is_confirmed_once(self):
        rpc = FakeRpc({"eth_chainId": hex(aa.BASE_CHAIN_ID), "eth_getBalance": "0x5"})
        client = self.client(rpc)
        client.balance(OWNER)
        client.balance(OWNER)
        self.assertEqual([m for m, _ in rpc.calls].count("eth_chainId"), 1)

    def test_a_reverted_transaction_is_an_error_and_a_pending_one_is_waited_for(self):
        answers = iter([None, None, {"status": "0x1", "contractAddress": OWNER}])
        rpc = FakeRpc({"eth_getTransactionReceipt": lambda p: next(answers)})
        self.assertEqual(self.client(rpc).wait_receipt("0x" + "00" * 32)["contractAddress"], OWNER)

        rpc = FakeRpc({"eth_getTransactionReceipt": {"status": "0x0"}})
        with self.assertRaises(aa.AllowanceError):
            self.client(rpc).wait_receipt("0x" + "00" * 32)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.artifact = aa.Artifact.load()

    def test_the_shipped_artifact_is_the_tested_contract(self):
        data = json.loads(aa.ARTIFACT_PATH.read_text())
        self.assertEqual(data["sourcePath"], "agent-allowance/src/AgentAllowance.sol")
        self.assertEqual(self.artifact.compiler_version, "v0.8.24+commit.e11b9ed9")
        # Seven immutables, each referenced wherever the code reads it.
        self.assertGreaterEqual(len(self.artifact.immutable_ranges), 7)
        self.assertTrue(all(length == 32 for _, length in self.artifact.immutable_ranges))

    def test_creation_data_appends_the_seven_constructor_words(self):
        data = self.artifact.creation_data(OWNER, GUARDIAN, OWNER, 100, 10, NOW)
        tail = data[len(self.artifact.bytecode):]
        self.assertEqual(len(tail), 7 * 64)
        self.assertEqual(tail[:64], aa.USDC.lower()[2:].rjust(64, "0"))
        self.assertEqual(int(tail[-64:], 16), NOW)

    def test_code_check_ignores_immutables_and_nothing_else(self):
        code = bytearray(bytes.fromhex(self.artifact.deployed_bytecode[2:]))
        start, length = self.artifact.immutable_ranges[0]
        code[start:start + length] = b"\x42" * length
        self.assertTrue(self.artifact.runs("0x" + code.hex()))

        outside = next(i for i in range(len(code)) if all(not s <= i < s + n for s, n in self.artifact.immutable_ranges))
        code[outside] ^= 0xFF
        self.assertFalse(self.artifact.runs("0x" + code.hex()))


class FakeEvm:
    """The chain at the EvmClient interface, with a limiter that answers."""

    def __init__(self, artifact):
        self.artifact = artifact
        self.balances = {}
        self.sent = []
        self.limiter = None
        self.deployments = 0
        self.deployed = None
        self.code_override = None
        self.getter_override = {}
        self.allowance = 0

    def balance(self, address):
        return self.balances.get(address, 0)

    deploy_cost = 10_000_000_000_000  # 0.00001 ETH, as on Base mainnet

    def quote(self, sender, *, to, data="0x", value=0):
        return {"maxCostWei": self.deploy_cost}

    def send(self, key, *, to, data="0x", value=0):
        sender = Account.from_key(key).address
        self.sent.append({"from": sender, "to": to, "data": data, "value": value})
        if to is None:
            # Every deployment has its own address, as on chain.
            self.deployments += 1
            self.limiter = to_checksum_address("0x" + format(0xAB00 + self.deployments, "040x"))
            self.deployed = data
        else:
            self.balances[to] = self.balances.get(to, 0) + value
        return "0x" + format(len(self.sent), "064x")

    def wait_receipt(self, tx):
        return {"status": "0x1", "contractAddress": self.limiter if self.sent[-1]["to"] is None else None}

    def wait_until(self, read, accept):
        return read()

    def code(self, address):
        if self.code_override is not None:
            return self.code_override
        return self.artifact.deployed_bytecode if self.deployed else "0x"

    def _constructor(self):
        tail = self.deployed[len(self.artifact.bytecode):]
        return [int(tail[i:i + 64], 16) for i in range(0, len(tail), 64)]

    def call_word(self, to, data):
        if to == aa.USDC:
            return self.allowance if data.startswith(aa.selector("allowance(address,address)")) else 7_000_000
        token, owner, agent, guardian, daily, per, expiry = self._constructor()
        values = {
            "token()": token, "owner()": owner, "agent()": agent, "guardian()": guardian,
            "dailyCap()": daily, "perPurchaseCap()": per, "expiry()": expiry, "paused()": 0,
            "remainingToday()": daily,
        }
        for getter, value in values.items():
            if data == aa.selector(getter):
                return self.getter_override.get(getter, value)
        raise AssertionError(f"unexpected read {data}")

    def usdc_balance(self, address):
        return 7_000_000


class AllowanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fernet = Fernet(Fernet.generate_key())
        self.funder = Account.create()
        self.guardian = Account.create()
        self.artifact = aa.Artifact.load()
        self.evm = FakeEvm(self.artifact)
        self.published = []
        self.service = self.make_service()

    def make_service(self, **overrides):
        options = dict(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"),
            evm=self.evm, fernet=self.fernet, owners={USER: to_checksum_address(OWNER)},
            guardian_key=lambda: self.guardian.key.to_0x_hex(), gas_funder_key=lambda: self.funder.key.to_0x_hex(),
            artifact=self.artifact, max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90,
            publish_source=lambda limiter, tx, artifact: self.published.append((limiter, tx)) or "exact_match",
            now=lambda: NOW,
        )
        options.update(overrides)
        return aa.AllowanceService(**options)

    def test_only_listed_owners_are_served(self):
        with self.assertRaises(aa.AllowanceUnavailable):
            self.service.setup("9999", "100", "10", "30")
        with self.assertRaises(aa.AllowanceUnavailable):
            self.service.status("9999")
        self.assertEqual(self.evm.sent, [])

    def test_caps_are_checked_before_anything_is_created(self):
        for daily, per, days in (("0", "1", "30"), ("10", "11", "30"), ("101", "10", "30"),
                                 ("100", "26", "30"), ("10", "1", "0"), ("10", "1", "91"),
                                 ("abc", "1", "30"), ("10", "1.0000001", "30"), ("10", "1", "2.5")):
            with self.subTest(daily=daily, per=per, days=days):
                with self.assertRaises(aa.AllowanceError):
                    self.service.setup(USER, daily, per, days)
        self.assertEqual(self.evm.sent, [])
        self.assertIsNone(self.service.store.agent(USER))

    def test_setup_funds_the_agent_deploys_and_verifies(self):
        result = self.service.setup(USER, "100", "10", "30")

        agent_row = self.service.store.agent(USER)
        key = self.fernet.decrypt(agent_row["encrypted_key"].encode()).decode()
        self.assertEqual(Account.from_key(key).address, agent_row["agent_address"])
        self.assertNotIn(key[2:], agent_row["encrypted_key"])

        funding, deployment = self.evm.sent
        self.assertEqual(funding["from"], self.funder.address)
        self.assertEqual(funding["to"], agent_row["agent_address"])
        self.assertEqual(funding["value"], aa.AGENT_GAS_TARGET_WEI)
        self.assertEqual(deployment["from"], agent_row["agent_address"])
        self.assertEqual(deployment["data"], self.artifact.creation_data(
            OWNER, agent_row["agent_address"], self.guardian.address, 100_000_000, 10_000_000, NOW + 30 * 86400))

        self.assertTrue(result["created"])
        self.assertEqual(result["limiter"], self.evm.limiter)
        self.assertEqual(result["state"], "waiting for a grant from your Trezor")
        self.assertEqual(result["source"], "exact_match")
        self.assertIn(f"https://base.blockscout.com/address/{self.evm.limiter}?tab=contract", result["telegramText"])

    def test_setup_twice_with_the_same_caps_deploys_once(self):
        self.service.setup(USER, "100", "10", "30")
        again = self.service.setup(USER, "100", "10", "30")
        self.assertFalse(again["created"])
        self.assertEqual(len(self.evm.sent), 2)

    def test_gas_follows_the_price_of_the_deployment(self):
        self.evm.deploy_cost = 300_000_000_000_000  # 0.0003 ETH, a fee spike
        self.service.setup(USER, "100", "10", "30")
        self.assertEqual(self.evm.sent[0]["value"], 600_000_000_000_000)

    def test_a_top_up_above_the_ceiling_is_refused_before_anything_is_sent(self):
        self.evm.deploy_cost = aa.MAX_GAS_TOP_UP_WEI
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.setup(USER, "100", "10", "30")
        self.assertIn("unusually expensive", str(raised.exception))
        self.assertEqual(self.evm.sent, [])

    def test_gas_is_not_sent_to_an_agent_that_has_enough(self):
        address, _ = self.service.agent_key(USER)
        self.evm.balances[address] = aa.AGENT_GAS_MINIMUM_WEI
        self.service.setup(USER, "100", "10", "30")
        self.assertEqual([tx["to"] for tx in self.evm.sent], [None])

    def test_a_limiter_that_is_not_the_tested_code_is_rejected(self):
        code = bytearray(bytes.fromhex(self.artifact.deployed_bytecode[2:]))
        code[-1] ^= 0xFF
        self.evm.code_override = "0x" + code.hex()
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.setup(USER, "100", "10", "30")
        self.assertIn("not the tested AgentAllowance", str(raised.exception))
        self.assertIsNone(self.service.store.active_limiter(USER))
        self.assertEqual(self.published, [])

    def test_a_limiter_whose_fields_do_not_read_back_is_rejected(self):
        for getter in ("owner()", "agent()", "guardian()", "token()", "dailyCap()", "perPurchaseCap()", "expiry()", "paused()"):
            with self.subTest(getter=getter):
                self.evm.getter_override = {getter: 1}
                with self.assertRaises(aa.AllowanceError):
                    self.service.setup(USER, "100", "10", "30")
                self.assertIsNone(self.service.store.active_limiter(USER))

    def test_a_failed_publication_does_not_undo_a_verified_deployment(self):
        def fail(*args):
            raise OSError("sourcify down")

        service = self.make_service(publish_source=fail)
        result = service.setup(USER, "100", "10", "30")
        self.assertEqual(result["source"], "failed")
        self.assertEqual(service.status(USER)["limiter"], self.evm.limiter)

    def test_replacing_a_granted_limiter_warns_that_its_allowance_remains(self):
        first = self.service.setup(USER, "100", "10", "30")
        self.evm.allowance = 250_000_000
        second = self.service.setup(USER, "50", "5", "7")
        self.assertNotEqual(first["limiter"], second["limiter"])
        self.assertEqual(second["previousLimiter"], first["limiter"])
        self.assertIn("still holds an allowance of 250 USDC", second["telegramText"])

    def test_replacing_an_ungranted_limiter_says_nothing_more(self):
        self.service.setup(USER, "100", "10", "30")
        second = self.service.setup(USER, "50", "5", "7")
        self.assertNotIn("previousLimiter", second)

    def test_status_reads_the_chain(self):
        self.assertFalse(self.service.status(USER)["configured"])
        self.service.setup(USER, "100", "10", "30")
        self.evm.allowance = 300_000_000
        status = self.service.status(USER)
        self.assertTrue(status["configured"])
        self.assertEqual(status["state"], "granted")
        self.assertIn("Allowance from your Trezor: 300 USDC", status["telegramText"])
        self.assertIn("Caps: 100 USDC a day, 10 USDC a purchase", status["telegramText"])


class WiringTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(aa.build_allowance_service_from_env("", env={}))

    def test_on_needs_every_setting(self):
        key = Fernet.generate_key().decode()
        guardian_blob = Fernet(key.encode()).encrypt(Account.create().key.to_0x_hex().encode()).decode()
        base = {aa.ENABLED_ENV: "1", aa.OWNERS_ENV: f"{USER}:{OWNER}", aa.GUARDIAN_KEY_ENV: guardian_blob,
                aa.GAS_FUNDER_ENV: "blob", aa.DB_ENV: str(Path(tempfile.mkdtemp()) / "a.db")}
        for missing in (aa.OWNERS_ENV, aa.GUARDIAN_KEY_ENV, aa.GAS_FUNDER_ENV):
            with self.subTest(missing=missing):
                env = {k: v for k, v in base.items() if k != missing}
                with self.assertRaises(ValueError):
                    aa.build_allowance_service_from_env(key, env=env)
        with self.assertRaises(ValueError):
            aa.build_allowance_service_from_env("", env=base)
        self.assertIsInstance(aa.build_allowance_service_from_env(key, env=base), aa.AllowanceService)

    def test_owners_parse_to_checksummed_addresses(self):
        self.assertEqual(aa.parse_owners(f" {USER} : {OWNER.lower()} , 7:{GUARDIAN}"),
                         {USER: to_checksum_address(OWNER), "7": to_checksum_address(GUARDIAN)})
        with self.assertRaises(ValueError):
            aa.parse_owners("no-address")

    def test_operator_keys_are_only_ever_returned_encrypted(self):
        master = Fernet.generate_key().decode()
        address, blob = aa.encrypt_operator_key(master)
        key = Fernet(master.encode()).decrypt(blob.encode()).decode()
        self.assertEqual(Account.from_key(key).address, address)
        self.assertNotIn(key[2:], blob)


if __name__ == "__main__":
    unittest.main()


def signed_approve(account, spender, amount, **changes):
    tx = {
        "type": 2, "chainId": aa.BASE_CHAIN_ID, "nonce": 3, "maxPriorityFeePerGas": 1_000_000,
        "maxFeePerGas": 12_000_000, "gas": 60_000, "to": aa.USDC, "value": 0,
        "data": aa.encode_call("approve(address,uint256)", spender, amount), "accessList": [],
    }
    tx.update(changes)
    return Account.sign_transaction(tx, account.key).raw_transaction.to_0x_hex()


class VerifySignedApproveTests(unittest.TestCase):
    def setUp(self):
        self.owner = Account.create()
        self.spender = to_checksum_address("0x" + "ab" * 20)

    def test_exactly_the_requested_approve_is_accepted(self):
        raw = signed_approve(self.owner, self.spender, 5)
        self.assertEqual(aa.verify_signed_approve(raw, self.owner.address, self.spender, 5),
                         "0x" + keccak(bytes.fromhex(raw[2:])).hex())

    def test_anything_else_is_refused(self):
        other = Account.create()
        cases = {
            "other amount": signed_approve(self.owner, self.spender, 6),
            "other spender": signed_approve(self.owner, other.address, 5),
            "other signer": signed_approve(other, self.spender, 5),
            "other chain": signed_approve(self.owner, self.spender, 5, chainId=1),
            "other token": signed_approve(self.owner, self.spender, 5, to=other.address),
            "with value": signed_approve(self.owner, self.spender, 5, value=1),
            "a transfer": signed_approve(self.owner, self.spender, 5,
                                         data=aa.encode_call("transfer(address,uint256)", self.spender, 5)),
            "garbage": "0x02deadbeef",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                with self.assertRaises(aa.AllowanceError):
                    aa.verify_signed_approve(raw, self.owner.address, self.spender, 5)


class FakeBroker:
    def __init__(self, wallet):
        self.wallet = wallet
        self.jobs = {}
        self.created = []
        self.unreachable = False

    def companion(self, user_id):
        return None if self.wallet is None else {"walletAddress": self.wallet}

    def create_job(self, user_id, kind, key, payload, expires_at):
        job_id = f"job_{len(self.created) + 1:08d}"
        self.created.append({"user": user_id, "kind": kind, "key": key, "payload": payload, "expires": expires_at})
        self.jobs[job_id] = {"jobId": job_id, "state": "QUEUED"}
        return {"jobId": job_id}

    def job(self, job_id):
        if self.unreachable:
            raise aa.AllowanceError("The Trezor link on the server is not answering. Nothing was changed.")
        return self.jobs[job_id]


class DeviceLaneEvm(FakeEvm):
    """FakeEvm plus broadcasting, receipts, the owner's allowance and pause."""

    def __init__(self, artifact, owner):
        super().__init__(artifact)
        self.owner = owner
        self.rpc = self
        self.broadcasts = []
        self.broadcast_error = None
        self.receipt = None
        self.allowances = {}

    def call(self, method, params):
        if method == "eth_sendRawTransaction":
            self.broadcasts.append(params[0])
            if self.broadcast_error:
                raise aa.AllowanceError(self.broadcast_error)
            return "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
        if method == "eth_getTransactionReceipt":
            return self.receipt
        raise AssertionError(method)

    def send(self, key, *, to, data="0x", value=0):
        tx = super().send(key, to=to, data=data, value=value)
        if data == aa.selector("pause()"):
            self.getter_override["paused()"] = 1
        return tx

    def call_word(self, to, data):
        if to == aa.USDC and data.startswith(aa.selector("allowance(address,address)")):
            spender = to_checksum_address("0x" + data[-40:])
            return self.allowances.get(spender, 0)
        return super().call_word(to, data)


class DeviceLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner = Account.create()
        self.guardian = Account.create()
        self.artifact = aa.Artifact.load()
        self.evm = DeviceLaneEvm(self.artifact, self.owner.address)
        self.broker = FakeBroker(self.owner.address)
        self.clock = [NOW]
        self.service = aa.AllowanceService(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"), evm=self.evm,
            fernet=Fernet(Fernet.generate_key()), owners={USER: self.owner.address},
            guardian_key=lambda: self.guardian.key.to_0x_hex(),
            gas_funder_key=lambda: Account.create().key.to_0x_hex(), artifact=self.artifact,
            max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90, broker=self.broker,
            max_grant=300_000_000, now=lambda: self.clock[0],
        )
        self.limiter = self.service.setup(USER, "100", "10", "30")["limiter"]

    def job(self, n=1):
        return self.broker.jobs[f"job_{n:08d}"]

    def sign(self, spender, amount, account=None):
        self.job_result(signed_approve(account or self.owner, spender, amount))

    def job_result(self, raw, n=1):
        self.job(n).update(state="SUCCEEDED", result={"signedTransaction": raw})

    def ops(self):
        return self.service.store.recent_ops(USER, 10)

    def test_a_grant_goes_to_the_device_then_the_chain_then_reads_back(self):
        answer = self.service.grant(USER, "250")
        self.assertIn("Confirm on your Trezor: an approve of 250 USDC", answer["telegramText"])
        self.assertEqual(self.broker.created[0]["kind"], "usdc_approve")
        self.assertEqual(self.broker.created[0]["payload"], {"spender": self.limiter, "amountAtomic": 250_000_000})
        self.assertEqual(self.broker.created[0]["expires"], NOW + aa.DEVICE_JOB_SECONDS)

        self.job().update(state="LEASED")
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "WAITING_DEVICE")

        self.sign(self.limiter, 250_000_000)
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "BROADCAST")
        self.assertEqual(len(self.evm.broadcasts), 1)

        self.evm.receipt = {"status": "0x1"}
        self.evm.allowances[self.limiter] = 250_000_000
        status = self.service.status(USER)
        self.assertEqual(self.ops()[0]["state"], "DONE")
        self.assertEqual(status["state"], "granted")
        self.assertIn("grant 250 USDC: done — Allowance now 250 USDC.", status["telegramText"])
        self.assertEqual(len(self.evm.broadcasts), 1)

    def test_grants_are_refused_before_the_device_when_they_should_be(self):
        cases = [
            ("no companion", lambda: setattr(self.broker, "wallet", None), "No Trezor companion"),
            ("another Trezor", lambda: setattr(self.broker, "wallet", Account.create().address), "not the address on file"),
            ("above the ceiling", lambda: None, "limited to 300 USDC"),
            ("paused limiter", lambda: self.evm.getter_override.update({"paused()": 1}), "expired or paused"),
        ]
        for name, spoil, text in cases:
            with self.subTest(name):
                self.broker.wallet = self.owner.address
                self.evm.getter_override = {}
                spoil()
                with self.assertRaises(aa.AllowanceError) as raised:
                    self.service.grant(USER, "301" if name == "above the ceiling" else "10")
                self.assertIn(text, str(raised.exception))
        self.assertEqual(self.broker.created, [])

    def test_one_request_at_a_time(self):
        self.service.grant(USER, "10")
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.grant(USER, "20")
        self.assertIn("already waiting", str(raised.exception))
        self.assertEqual(len(self.broker.created), 1)

    def test_failures_on_the_owners_computer_are_named(self):
        for code, text in (("device_rejected", "You cancelled it"),
                           ("limiter_invalid", "treat this as an attack"),
                           ("limit_exceeded", "SIGN402_TREZOR_POC_MAX_USD"),
                           ("something_new", "It failed on your computer (something_new)")):
            with self.subTest(code=code):
                n = len(self.broker.created) + 1
                self.service.grant(USER, "10")
                self.job(n).update(state="FAILED", errorCode=code)
                self.service.advance(USER)
                self.assertEqual(self.ops()[0]["state"], "FAILED")
                self.assertIn(text, self.ops()[0]["detail"])
        self.assertEqual(self.evm.broadcasts, [])

    def test_an_unconfirmed_request_expires_without_changes(self):
        self.service.grant(USER, "10")
        self.job().update(state="EXPIRED")
        self.service.advance(USER)
        self.assertIn("Nobody confirmed on the Trezor in time", self.ops()[0]["detail"])

    def test_a_signature_for_something_else_is_never_broadcast(self):
        self.service.grant(USER, "10")
        self.job_result(signed_approve(self.owner, self.limiter, 11_000_000))
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "FAILED")
        self.assertIn("not the approve that was asked for", self.ops()[0]["detail"])
        self.assertEqual(self.evm.broadcasts, [])

    def test_a_transient_broker_failure_keeps_the_request_waiting(self):
        self.service.grant(USER, "10")
        self.broker.unreachable = True
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "WAITING_DEVICE")

    def test_a_lost_broadcast_is_offered_again_and_sent_once_on_chain(self):
        self.service.grant(USER, "10")
        self.sign(self.limiter, 10_000_000)
        self.evm.broadcast_error = "Base RPC is not answering. Nothing was changed; try again shortly."
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "BROADCAST")

        self.evm.broadcast_error = "Base refused eth_sendRawTransaction: already known"
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "BROADCAST")
        self.assertEqual(len(set(self.evm.broadcasts)), 1)

        self.evm.receipt = {"status": "0x1"}
        self.evm.allowances[self.limiter] = 10_000_000
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "DONE")

    def test_an_owner_without_gas_is_told_so(self):
        self.service.grant(USER, "10")
        self.sign(self.limiter, 10_000_000)
        self.evm.broadcast_error = "Base refused eth_sendRawTransaction: insufficient funds for gas * price + value"
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "FAILED")
        self.assertIn("needs a little ETH on Base", self.ops()[0]["detail"])

    def test_a_reverted_approve_fails(self):
        self.service.grant(USER, "10")
        self.sign(self.limiter, 10_000_000)
        self.service.advance(USER)
        self.evm.receipt = {"status": "0x0"}
        self.service.advance(USER)
        self.assertEqual(self.ops()[0]["state"], "FAILED")

    def test_revoke_is_an_approve_of_zero_and_reaches_superseded_limiters(self):
        old = self.limiter
        self.service.setup(USER, "50", "5", "7")
        self.service.revoke(USER, old)
        self.assertEqual(self.broker.created[-1]["payload"], {"spender": old, "amountAtomic": 0})
        with self.assertRaises(aa.AllowanceError):
            self.service.revoke(USER, "0x" + "99" * 20)

    def test_pause_goes_through_the_guardian_without_the_device(self):
        result = self.service.pause(USER)
        pause_tx = self.evm.sent[-1]
        self.assertEqual(pause_tx["from"], self.guardian.address)
        self.assertEqual(pause_tx["to"], self.limiter)
        self.assertEqual(pause_tx["data"], aa.selector("pause()"))
        self.assertTrue(result["paused"])
        self.assertIn("Paused.", result["telegramText"])
        self.assertEqual(self.broker.created, [])

        sent = len(self.evm.sent)
        self.service.pause(USER)
        self.assertEqual(len(self.evm.sent), sent)
