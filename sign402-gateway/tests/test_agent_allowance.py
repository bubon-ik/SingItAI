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


class JsonRpcTests(unittest.TestCase):
    def http_error(self, code, body):
        import io
        import urllib.error
        return urllib.error.HTTPError("https://rpc.example", code, "error", {}, io.BytesIO(body))

    def test_a_refusal_sent_as_http_400_is_an_answer_not_an_outage(self):
        from unittest.mock import patch
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32600, "message": "eth_getLogs is limited to a 10 block range"}}).encode()
        with patch("urllib.request.urlopen", side_effect=self.http_error(400, body)):
            with self.assertRaises(aa.RpcRejected) as raised:
                aa.JsonRpc("https://rpc.example").call("eth_getLogs", [{}])
        self.assertIn("10 block range", str(raised.exception))

    def test_rate_limits_and_bare_http_errors_stay_retryable(self):
        from unittest.mock import patch
        for code, body in ((429, b'{"error":{"code":429,"message":"Too many requests"}}'), (502, b"<html>bad gateway")):
            with self.subTest(code=code):
                with patch("urllib.request.urlopen", side_effect=self.http_error(code, body)):
                    with self.assertRaises(BaseBalanceError):
                        aa.JsonRpc("https://rpc.example").call("eth_blockNumber", [])


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


def funded(evm):
    """The key of a gas wallet holding 0.003 ETH on the fake chain."""
    funder = Account.create()
    evm.balances[funder.address] = 3_000_000_000_000_000
    return lambda: funder.key.to_0x_hex()


class AllowanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fernet = Fernet(Fernet.generate_key())
        self.funder = Account.create()
        self.guardian = Account.create()
        self.artifact = aa.Artifact.load()
        self.evm = FakeEvm(self.artifact)
        self.evm.balances[self.funder.address] = 3_000_000_000_000_000
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

    def test_an_empty_gas_wallet_is_named_before_anything_is_sent(self):
        self.evm.balances[self.funder.address] = 0
        with self.assertRaises(aa.AllowanceError) as raised:
            self.service.setup(USER, "100", "10", "30")
        self.assertIn(f"gas wallet {self.funder.address} has 0 ETH", str(raised.exception))
        self.assertIn("send ETH on Base to that address", str(raised.exception))
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

    def test_web_accounts_are_owners_without_an_env_list(self):
        from sign402_gateway.web_accounts import WebAccountStore, account_id_for

        key = Fernet.generate_key().decode()
        blob = Fernet(key.encode()).encrypt(Account.create().key.to_0x_hex().encode()).decode()
        tmp = Path(tempfile.mkdtemp())
        env = {aa.ENABLED_ENV: "1", aa.GUARDIAN_KEY_ENV: blob, aa.GAS_FUNDER_ENV: blob,
               aa.DB_ENV: str(tmp / "a.db"), "SIGN402_WEB_ENABLED": "1", "SIGN402_WEB_DB": str(tmp / "web.db")}
        service = aa.build_allowance_service_from_env(key, env=env)
        WebAccountStore(tmp / "web.db").ensure_account(OWNER, NOW)
        self.assertEqual(service.owner_of(account_id_for(OWNER)), to_checksum_address(OWNER))
        with self.assertRaises(aa.AllowanceUnavailable):
            service.owner_of(USER)

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
            gas_funder_key=funded(self.evm), artifact=self.artifact,
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

    def finish_revoke(self, limiter, n=1):
        self.service.revoke(USER, limiter)
        self.sign(limiter, 0)
        self.service.advance(USER)
        self.evm.receipt = {"status": "0x1"}
        self.evm.allowances[limiter] = 0
        self.service.advance(USER)
        return self.ops()[0]

    def test_a_revoke_that_closes_the_lane_returns_the_float_to_the_owner(self):
        agent = self.service.store.agent(USER)["agent_address"]
        sent = len(self.evm.sent)
        op = self.finish_revoke(self.limiter)
        transfer = self.evm.sent[-1]
        self.assertEqual((transfer["from"], transfer["to"]), (agent, aa.USDC))
        self.assertEqual(transfer["data"], aa.encode_call("transfer(address,uint256)", self.owner.address, 7_000_000))
        self.assertEqual(len(self.evm.sent), sent + 1)
        self.assertEqual(op["state"], "DONE")
        self.assertIn("float of 7 USDC went back to your wallet", op["detail"])

    def test_a_mined_grant_nobody_read_back_does_not_block_the_revoke(self):
        """Live: grant broadcast, a purchase spent part of it, no /allowance; then /allowance_revoke."""
        self.service.grant(USER, "1")
        self.sign(self.limiter, 1_000_000)
        self.service.advance(USER)
        self.evm.receipt = {"status": "0x1"}
        self.evm.allowances[self.limiter] = 800_000  # a purchase already spent from it
        self.clock[0] += 600

        answer = self.service.revoke(USER)
        self.assertIn("approve of 0 (revoke)", answer["telegramText"])
        grant = [op for op in self.ops() if op["kind"] == "GRANT"][0]
        self.assertEqual((grant["state"], grant["detail"]), ("DONE", "Allowance now 0.8 USDC."))

    def test_a_revoke_of_an_old_limiter_keeps_the_float_while_the_new_one_is_granted(self):
        old = self.limiter
        new = self.service.setup(USER, "50", "5", "7")["limiter"]
        self.evm.allowances[new] = 5_000_000
        sent = len(self.evm.sent)
        op = self.finish_revoke(old)
        self.assertEqual(len(self.evm.sent), sent)
        self.assertEqual(op["detail"], "Allowance now 0 USDC.")

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


class SpendingEvm(DeviceLaneEvm):
    """DeviceLaneEvm plus balances, spend(), blocks and USDC Transfer logs."""

    def __init__(self, artifact, owner, clock):
        super().__init__(artifact, owner)
        self.usdc = {owner: 6_000_000}
        self.block = 100
        self.logs = []
        self.clock = clock
        self.spends = []

    def sleep(self, seconds):
        self.clock[0] += seconds

    def usdc_balance(self, address):
        return self.usdc.get(address, 0)

    def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.block)
        if method == "eth_getLogs":
            query = params[0]
            return [entry for entry in self.logs
                    if entry["topics"][1:] == query["topics"][1:] and int(entry["blockNumber"], 16) >= int(query["fromBlock"], 16)]
        return super().call(method, params)

    def quote(self, sender, *, to, data="0x", value=0):
        return {"maxCostWei": 1}

    def send(self, key, *, to, data="0x", value=0):
        tx = super().send(key, to=to, data=data, value=value)
        if data.startswith(aa.selector("spend(address,uint256,bytes32)")):
            payee = to_checksum_address("0x" + data[10 + 24:10 + 64])
            size = int(data[10 + 64:10 + 128], 16)
            self.spends.append((payee, size))
            self.usdc[self.owner] -= size
            self.usdc[payee] = self.usdc.get(payee, 0) + size
            self.allowances[to] = self.allowances.get(to, 0) - size
        self.block += 1
        return tx

    def settle(self, sender, pay_to, amount):
        self.block += 1
        self.usdc[sender] -= amount
        self.logs.append({
            "topics": [aa.TRANSFER_TOPIC, "0x" + aa._word(sender), "0x" + aa._word(pay_to)],
            "data": hex(amount), "blockNumber": hex(self.block), "transactionHash": "0x" + format(self.block, "064x"),
        })


class FakeX402:
    def __init__(self, evm, *, status=200, settle=True, first_insufficient=False):
        self.evm, self.status, self.settle, self.first_insufficient = evm, status, settle, first_insufficient
        self.calls = []

    def __call__(self, resource_url, **kwargs):
        self.calls.append((resource_url, kwargs))
        if self.first_insufficient and len(self.calls) == 1:
            return {"ok": False, "status": 402, "body": {"reason": "insufficient_funds"}}
        agent = Account.from_key(kwargs["private_key"]).address
        if self.settle:
            self.evm.settle(agent, kwargs["expected_receiver"], int(kwargs["max_atomic"]))
        return {"ok": 200 <= self.status < 300, "status": self.status, "body": {"answer": 42}, "transactionHash": None}


class SpendingLaneTests(unittest.TestCase):
    PAY_TO = to_checksum_address("0x" + "8a" * 20)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner = Account.create()
        self.clock = [NOW]
        self.artifact = aa.Artifact.load()
        self.evm = SpendingEvm(self.artifact, self.owner.address, self.clock)
        self.service = aa.AllowanceService(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"), evm=self.evm,
            fernet=Fernet(Fernet.generate_key()), owners={USER: self.owner.address},
            guardian_key=lambda: Account.create().key.to_0x_hex(),
            gas_funder_key=funded(self.evm), artifact=self.artifact,
            max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90, broker=FakeBroker(self.owner.address),
            float_target=200_000, float_low=50_000, exact_above=50_000, now=lambda: self.clock[0],
        )
        self.limiter = self.service.setup(USER, "0.50", "0.30", "30")["limiter"]
        self.evm.allowances[self.limiter] = 1_000_000
        self.agent = self.service.store.agent(USER)["agent_address"]

    def requirements(self, amount):
        return {"amountAtomic": str(amount), "receiver": self.PAY_TO, "asset": aa.USDC, "network": "base-mainnet"}

    def pay(self, amount, client=None, **kwargs):
        client = client or FakeX402(self.evm)
        return self.service.pay_x402(USER, "https://seller.example/r", self.requirements(amount), client, **kwargs), client

    def test_who_is_on_the_lane(self):
        self.assertIsNone(self.service.lane_for("someone-else"))
        self.assertEqual(self.service.lane_for(USER)["limiter_address"], self.limiter)
        for spoil, text in ((lambda: self.evm.allowances.update({self.limiter: 0}), "Nothing is granted"),
                            (lambda: self.evm.getter_override.update({"paused()": 1}), "paused"),
                            (lambda: self.clock.__setitem__(0, NOW + 31 * 86400), "expired")):
            with self.subTest(text):
                self.setUp()
                spoil()
                with self.assertRaises(aa.AllowanceError) as raised:
                    self.service.lane_for(USER)
                self.assertIn(text, str(raised.exception))

    def test_a_micro_payment_refills_the_float_once_then_pays_from_it(self):
        first, client = self.pay(5_000)
        self.assertTrue(first["ok"])
        self.assertEqual(first["funding"], "refill 0.2 USDC")
        self.assertEqual(self.evm.spends, [(self.agent, 200_000)])
        self.assertEqual(client.calls[0][1]["max_atomic"], "5000")
        self.assertEqual(client.calls[0][1]["expected_receiver"], self.PAY_TO)
        self.assertEqual(client.calls[0][1]["expected_asset"], aa.USDC)
        self.assertIsNotNone(first["settlementTx"])

        second, _ = self.pay(5_000)
        self.assertTrue(second["ok"])
        self.assertEqual(second["funding"], "float")
        self.assertEqual(len(self.evm.spends), 1)
        self.assertNotEqual(first["settlementTx"], second["settlementTx"])

    def test_a_larger_payment_is_funded_exactly(self):
        result, _ = self.pay(120_000)
        self.assertTrue(result["ok"])
        self.assertEqual(self.evm.spends, [(self.agent, 120_000)])
        self.assertEqual(self.evm.usdc[self.agent], 0)

    def test_what_the_limiter_cannot_fund_is_refused_before_anything_moves(self):
        for spoil in (lambda: self.evm.allowances.update({self.limiter: 100_000}),
                      lambda: self.evm.usdc.update({self.owner.address: 100_000})):
            with self.subTest():
                self.setUp()
                spoil()
                client = FakeX402(self.evm)
                with self.assertRaises(aa.AllowanceError) as raised:
                    self.pay(120_000, client)
                self.assertIn("cannot fund", str(raised.exception))
                self.assertEqual(self.evm.spends, [])
                self.assertEqual(client.calls, [])

    def test_delivered_without_settlement_is_not_paid(self):
        result, _ = self.pay(120_000, FakeX402(self.evm, settle=False))
        self.assertTrue(result["delivered"])
        self.assertIsNone(result["settlementTx"])
        self.assertFalse(result["ok"])

    def test_charged_without_delivery_is_reported_as_such(self):
        result, _ = self.pay(120_000, FakeX402(self.evm, status=500))
        self.assertFalse(result["delivered"])
        self.assertIsNotNone(result["settlementTx"])
        self.assertFalse(result["ok"])

    def test_a_facilitator_behind_the_funding_is_given_one_retry(self):
        client = FakeX402(self.evm, first_insufficient=True)
        result, _ = self.pay(120_000, client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(self.evm.spends), 1)

    def test_a_post_resource_keeps_its_method_and_body(self):
        _, client = self.pay(120_000, method="POST", request_body={"q": "weth"})
        self.assertEqual(client.calls[0][1]["method"], "POST")
        self.assertEqual(client.calls[0][1]["request_body"], {"q": "weth"})

    def test_a_counted_settlement_is_never_counted_again_even_after_a_restart(self):
        first, _ = self.pay(5_000)
        restarted = aa.AllowanceService(
            store=aa.AllowanceStore(Path(self.tmp.name) / "allowance.db"), evm=self.evm,
            fernet=self.service.fernet, owners={USER: self.owner.address},
            guardian_key=lambda: Account.create().key.to_0x_hex(),
            gas_funder_key=funded(self.evm), artifact=self.artifact,
            max_daily=100_000_000, max_per_purchase=25_000_000, max_days=90,
            float_target=200_000, float_low=50_000, exact_above=50_000, now=lambda: self.clock[0],
        )
        # The seller does not settle this time: the old transfer must not stand in for it.
        second = restarted.pay_x402(USER, "https://seller.example/r", self.requirements(5_000),
                                    FakeX402(self.evm, settle=False))
        self.assertIsNone(second["settlementTx"])
        self.assertFalse(second["ok"])
        self.assertIsNotNone(first["settlementTx"])
