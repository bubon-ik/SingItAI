import copy
import io
import json
import unittest
from urllib.error import URLError

from sign402_gateway.solana_balances import SolanaBalanceProvider, SolanaBalanceError, MAINNET_GENESIS_HASH, USDC_MINT, TOKEN_PROGRAM

OWNER = '11111111111111111111111111111111'
ACCOUNT = USDC_MINT  # Syntactically valid address used only as an offline fixture.


def account(amount='1234567'):
    return {'pubkey': ACCOUNT, 'account': {'owner': TOKEN_PROGRAM, 'data': {'parsed': {
        'type': 'account', 'info': {'owner': OWNER, 'mint': USDC_MINT,
                                  'tokenAmount': {'decimals': 6, 'amount': amount}}}}}}


class SolanaBalanceTests(unittest.TestCase):
    def make_provider(self, *, native=1234567890, accounts=None, genesis=MAINNET_GENESIS_HASH):
        calls = []
        results = {'getGenesisHash': genesis, 'getBalance': {'value': native},
                   'getTokenAccountsByOwner': {'value': [account()] if accounts is None else accounts}}
        def opener(request, **kwargs):
            payload = json.loads(request.data)
            calls.append(payload)
            return io.BytesIO(json.dumps({'jsonrpc': '2.0', 'id': payload['id'], 'result': results[payload['method']]}).encode())
        return SolanaBalanceProvider(endpoint_url='https://rpc.example.test', opener=opener), calls

    def test_precise_sol_and_native_usdc_balances(self):
        provider, calls = self.make_provider(native=9007199254740993)
        self.assertEqual(provider(OWNER), {'SOL': '9007199.254740993', 'USDC': '1.234567'})
        self.assertEqual(calls[-1]['params'], [OWNER, {'mint': USDC_MINT}, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}])

    def test_no_token_accounts_means_zero_usdc(self):
        provider, _ = self.make_provider(native=0, accounts=[])
        self.assertEqual(provider(OWNER), {'SOL': '0.000000000', 'USDC': '0.000000'})

    def test_multiple_accounts_are_summed(self):
        second = account('1')
        second['pubkey'] = TOKEN_PROGRAM
        provider, _ = self.make_provider(accounts=[account('999999'), second])
        self.assertEqual(provider(OWNER)['USDC'], '1.000000')

    def test_rejects_wrong_network_before_balance_lookup(self):
        provider, calls = self.make_provider(genesis='devnet')
        with self.assertRaises(SolanaBalanceError):
            provider(OWNER)
        self.assertEqual(len(calls), 1)

    def test_checks_mainnet_again_on_every_balance_request(self):
        provider, calls = self.make_provider()
        provider(OWNER)
        provider(OWNER)
        self.assertEqual(sum(c['method'] == 'getGenesisHash' for c in calls), 2)

    def test_rejects_wrong_mint_owner_program_decimals_and_amounts(self):
        changes = [(['account', 'owner'], OWNER),
                   (['account', 'data', 'parsed', 'info', 'owner'], USDC_MINT),
                   (['account', 'data', 'parsed', 'info', 'mint'], USDC_MINT.lower()),
                   (['account', 'data', 'parsed', 'info', 'tokenAmount', 'decimals'], 9)]
        for amount in ['-1', '1.2', '01', 'NaN', str(2**64), 12, None]:
            changes.append((['account', 'data', 'parsed', 'info', 'tokenAmount', 'amount'], amount))
        for path, value in changes:
            row = copy.deepcopy(account())
            node = row
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            provider, _ = self.make_provider(accounts=[row])
            with self.subTest(path=path, value=value), self.assertRaises(SolanaBalanceError):
                provider(OWNER)

    def test_malformed_or_duplicate_accounts_fail_closed(self):
        for accounts in [None, {}, [account(), account()], [{'account': {}}]]:
            provider, _ = self.make_provider(accounts={} if accounts is None else accounts)
            with self.subTest(accounts=accounts), self.assertRaises(SolanaBalanceError):
                provider(OWNER)

    def test_invalid_native_balance_fails_closed(self):
        for value in [-1, True, '1', 1.1, 2**64]:
            provider, _ = self.make_provider(native=value)
            with self.subTest(value=value), self.assertRaises(SolanaBalanceError):
                provider(OWNER)

    def test_transport_errors_and_rpc_errors_are_redacted(self):
        def fail(*args, **kwargs):
            raise URLError('secret-provider-url')
        cases = [fail]
        for raw in [b'not JSON', b'x' * (256 * 1024 + 1),
                    b'{"jsonrpc":"2.0","id":1,"error":{"message":"secret-provider-url"}}',
                    b'{"jsonrpc":"2.0","id":2,"result":"whatever"}']:
            cases.append(lambda *args, raw=raw, **kwargs: io.BytesIO(raw))
        for opener in cases:
            provider = SolanaBalanceProvider(endpoint_url='https://rpc.example.test', opener=opener)
            with self.assertRaisesRegex(SolanaBalanceError, '^Solana balance lookup failed$'):
                provider(OWNER)

    def test_rejects_unsafe_rpc_urls(self):
        for url in ['http://rpc.test', 'https://user:secret@rpc.test', 'https://rpc.test/#fragment', 'https://']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                SolanaBalanceProvider(endpoint_url=url)
