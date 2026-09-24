"""Per-user Solana chat, exact consent and crash recovery. No network or funds."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from sign402_gateway.solana_chat import SolanaChatService, SolanaChatError, SolanaBridge, USDC
from sign402_gateway.solana_chat_store import SolanaChatStore
from sign402_gateway.solana_wallets import SOLANA_NETWORK


class SolanaChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [1789900000]
        self.now = lambda: self.clock[0]
        self.store = SolanaChatStore(Path(self.tmp.name) / 'chat.db', now=self.now)
        self.wallets = Mock()
        self.wallets.wallet_status.side_effect = lambda user, chain: {'ok': True, 'wallet': {'address': 'SolAnaWallet' + str(user)}}
        self.approvals = Mock()
        self.approvals.request_hash_approval.side_effect = lambda **kw: {'ok': True, 'approved': True, 'approvedHash': kw['commitment_hash']}
        self.calls, self.attempted, self.can_consume = [], False, False
        self.counter = 0
        self.bridge_error = None
        self.state = 'confirmed'
        self.service = SolanaChatService(store=self.store, wallets=self.wallets, bridge=self.bridge,
            approvals=self.approvals, now=self.now)

    def bridge(self, user, payer, operation, **payload):
        self.calls.append((user, payer, operation, payload))
        if operation == 'balance':
            return {'balanceUsd': 5 if self.can_consume else 0, 'canConsume': self.can_consume}
        if operation == 'quote':
            self.counter += 1
            return {'quoteId': f'q-{self.counter}', 'payer': payer, 'recipient': 'MerchantCaseSensitive',
                    'network': SOLANA_NETWORK, 'asset': USDC, 'amountUsdc': '5.000000', 'feePayer': 'Sponsor',
                    'expiresAt': datetime.fromtimestamp(self.now()+300, timezone.utc).isoformat(), 'approvalHash': 'a'*64}
        if operation == 'pay':
            if self.bridge_error:
                raise self.bridge_error
            self.attempted = True
            return self.receipt()
        if operation == 'status':
            return {'attempted': self.attempted, 'transaction': 'signature' if self.attempted else None}
        if operation == 'reconcile':
            return self.receipt()
        if operation == 'chat':
            if not self.can_consume:
                raise SolanaChatError('TOP_UP_REQUIRED', 'Top up first')
            return {'text': 'Selected-model answer', 'balanceRemaining': '4.998', 'model': payload['model']}
        raise AssertionError(operation)

    def receipt(self):
        return {'state': self.state, 'transaction': 'signature',
                'veniceBalance': {'balanceUsd': 5 if self.can_consume else 0, 'canConsume': self.can_consume}}

    def call(self, operation, user='1', **payload):
        return self.service.handle('/agent/chat/' + operation, user, payload)

    def budget(self, user='1', cap=5_000_000):
        return self.call('approve-policy', user, dailyCapAtomic=cap, days=30)

    def quote(self, user='1'):
        self.budget(user)
        return self.call('quote', user)['quote']

    def pay(self, quote, user='1'):
        return self.call('pay', user, quoteId=quote['quoteId'], approvalHash=quote['approvalHash'])

    def paid_calls(self):
        return [c for c in self.calls if c[2] == 'pay']

    def test_default_is_base_and_preferences_survive_restart(self):
        self.assertEqual(self.store.chain('1'), 'base')
        self.store.chain('1', 'solana')
        other = SolanaChatStore(self.store.path)
        self.assertEqual(other.chain('1'), 'solana')
        self.assertEqual(other.chain('2'), 'base')
        with self.assertRaises(ValueError):
            other.chain('1', 'ethereum')

    def test_quote_is_free_and_case_sensitive_wallet_is_preserved(self):
        q = self.quote()
        self.assertEqual(self.paid_calls(), [])
        self.assertEqual(q['payer'], 'SolAnaWallet1')
        self.assertEqual(self.store.user('1')['payer'], 'SolAnaWallet1')
        self.assertEqual(self.approvals.request_hash_approval.call_args.kwargs['wallet_chain'], 'solana')

    def test_full_flow_requires_two_approvals_then_uses_selected_model(self):
        q = self.quote()
        self.can_consume = True
        result = self.pay(q)
        self.assertTrue(result['creditReady'])
        self.assertIn('signature', result['telegramText'])
        self.assertEqual(self.approvals.request_hash_approval.call_count, 2)
        args = self.approvals.request_hash_approval.call_args.kwargs
        self.assertEqual(args['commitment_hash'], q['approvalHash'])
        self.assertIn(q['recipient'], '\n'.join(args['context_lines']))
        self.store.set_model('1', 'selected-model')
        answer = self.call('message', text='hello')
        self.assertEqual(answer['model'], 'selected-model')
        self.assertEqual(answer['outstandingAtomic'], 4_998_000)
        self.assertEqual(self.store.spent('1'), 5_000_000)
        self.assertIsNone(answer['costAtomic'])

    def test_user_cannot_pay_another_users_quote(self):
        q = self.quote()
        result = self.pay(q, '2')
        self.assertEqual(result['state'], 'APPROVAL_REQUIRED')
        self.assertFalse(self.paid_calls())

    def test_phone_approval_must_confirm_the_same_hash(self):
        q = self.quote()
        self.approvals.request_hash_approval.side_effect = None
        self.approvals.request_hash_approval.return_value = {'ok': True, 'approved': True, 'approvedHash': 'b'*64}
        self.assertEqual(self.pay(q)['state'], 'APPROVAL_MISMATCH')
        self.assertFalse(self.paid_calls())
        self.assertEqual(self.store.spent('1'), 0)

    def test_wrong_hash_never_requests_payment_approval(self):
        q = self.quote()
        q['approvalHash'] = 'b'*64
        self.assertEqual(self.pay(q)['state'], 'APPROVAL_REQUIRED')
        self.assertEqual(self.approvals.request_hash_approval.call_count, 1)

    def test_declined_topup_frees_hold_without_payment(self):
        q = self.quote()
        self.approvals.request_hash_approval.side_effect = None
        self.approvals.request_hash_approval.return_value = {'ok': True, 'approved': False}
        self.assertFalse(self.pay(q)['ok'])
        self.assertEqual(self.store.spent('1'), 0)
        self.assertFalse(self.paid_calls())

    def test_budget_without_explicit_approval_is_not_saved(self):
        self.approvals.request_hash_approval.side_effect = None
        self.approvals.request_hash_approval.return_value = {'ok': True}
        self.assertFalse(self.budget()['ok'])
        self.assertEqual(self.call('message', text='hello')['state'], 'POLICY_MISSING')

    def test_expired_quote_never_reaches_phone_or_node(self):
        q = self.quote()
        self.clock[0] += 301
        self.assertEqual(self.pay(q)['state'], 'QUOTE_EXPIRED')
        self.assertFalse(self.paid_calls())
        self.assertEqual(self.approvals.request_hash_approval.call_count, 1)

    def test_quote_expiring_during_phone_approval_is_not_paid(self):
        q = self.quote()
        def approve(**kwargs):
            self.clock[0] += 301
            return {'ok': True, 'approved': True, 'approvedHash': kwargs['commitment_hash']}
        self.approvals.request_hash_approval.side_effect = approve
        self.assertEqual(self.pay(q)['state'], 'QUOTE_EXPIRED')
        self.assertEqual(self.store.spent('1'), 0)
        self.assertFalse(self.paid_calls())

    def test_duplicate_payment_cannot_double_charge(self):
        q = self.quote()
        self.can_consume = True
        self.pay(q)
        self.assertFalse(self.pay(q)['ok'])
        self.assertEqual(len(self.paid_calls()), 1)
        self.call('payment')
        self.call('payment')
        self.assertEqual(self.store.spent('1'), 5_000_000)

    def test_old_receipt_after_credit_consumption_does_not_block_next_topup(self):
        q = self.quote()
        self.can_consume = True
        self.pay(q)
        self.can_consume = False
        self.call('payment')
        self.assertIsNone(self.store.latest('1', active=True))
        self.clock[0] += 86400
        self.assertIn('quote', self.call('quote'))

    def test_repeated_quote_request_reuses_unexpired_terms_without_spending(self):
        first = self.quote()
        second = self.call('quote')['quote']
        self.assertEqual(first, second)
        self.assertEqual([c[2] for c in self.calls].count('quote'), 1)
        self.assertFalse(self.paid_calls())

    def test_chain_confirmation_is_not_usable_credit(self):
        q = self.quote()
        result = self.pay(q)
        self.assertEqual(result['paymentState'], 'confirmed')
        self.assertFalse(result['creditReady'])
        self.assertEqual(self.call('message', text='hello')['state'], 'PAYMENT_PENDING')
        self.assertEqual(self.call('quote')['state'], 'PAYMENT_PENDING')
        self.can_consume = True
        self.assertTrue(self.call('payment')['creditReady'])
        self.assertEqual(len(self.paid_calls()), 1)

    def test_uncertain_payment_holds_budget_after_restart_and_next_day(self):
        q = self.quote()
        self.attempted = True
        self.bridge_error = SolanaChatError('PAYMENT_UNCERTAIN', 'Uncertain')
        self.assertEqual(self.pay(q)['state'], 'PAYMENT_UNCERTAIN')
        self.service.store = SolanaChatStore(self.store.path, now=self.now)
        self.clock[0] += 86400
        self.assertEqual(self.call('quote')['state'], 'PAYMENT_PENDING')
        self.can_consume = True
        self.assertTrue(self.call('payment')['creditReady'])
        self.assertEqual(len(self.paid_calls()), 1)

    def test_pre_submission_failure_releases_reservation(self):
        q = self.quote()
        self.bridge_error = SolanaChatError('INSUFFICIENT_USDC', 'Insufficient')
        self.assertEqual(self.pay(q)['state'], 'INSUFFICIENT_USDC')
        self.assertEqual(self.store.spent('1'), 0)
        self.assertIsNone(self.store.latest('1', active=True))

    def test_unreadable_journal_never_assumes_payment_failed(self):
        q = self.quote()
        original = self.service.bridge
        def broken(user, payer, operation, **payload):
            if operation in ('pay', 'status'):
                raise SolanaChatError('BRIDGE_UNAVAILABLE', 'Unavailable')
            return original(user, payer, operation, **payload)
        self.service.bridge = broken
        self.pay(q)
        self.assertEqual(self.store.latest('1', active=True)['state'], 'uncertain')
        self.assertEqual(self.store.spent('1'), 5_000_000)

    def test_failed_chain_payment_releases_budget(self):
        q = self.quote()
        self.state = 'failed'
        self.pay(q)
        self.assertEqual(self.store.spent('1'), 0)
        self.assertIsNone(self.store.latest('1', active=True))

    def test_spending_existing_credit_does_not_trigger_another_topup(self):
        self.budget()
        self.can_consume = True
        self.assertTrue(self.call('message', text='hello')['ok'])
        self.assertFalse(self.paid_calls())
        self.assertNotIn('quote', [c[2] for c in self.calls])

    def test_empty_credit_never_automatically_pays(self):
        self.budget()
        self.assertEqual(self.call('message', text='hello')['state'], 'TOP_UP_REQUIRED')
        self.assertFalse(self.paid_calls())

    def test_no_second_topup_above_daily_cap(self):
        q = self.quote()
        self.can_consume = True
        self.pay(q)
        self.can_consume = False
        self.assertEqual(self.call('quote')['state'], 'WINDOW_EXHAUSTED')

    def test_policy_expiry_blocks_messages_and_topups(self):
        self.budget()
        self.clock[0] += 31*86400
        self.assertTrue(self.call('start')['policyExpired'])
        self.assertEqual(self.call('message', text='hello')['state'], 'POLICY_MISSING')

    def test_wallet_is_required_and_never_falls_back_to_shared_key(self):
        self.wallets.wallet_status.return_value = {'ok': False}
        self.wallets.wallet_status.side_effect = None
        self.assertTrue(self.call('start')['walletRequired'])
        self.assertEqual(self.call('quote')['state'], 'WALLET_REQUIRED')
        self.assertFalse(self.calls)

    def test_global_pause_blocks_approval_quote_payment_and_chat(self):
        self.service.purchases_paused = lambda: True
        for op in ('quote', 'pay', 'approve-policy', 'message'):
            self.assertEqual(self.call(op)['state'], 'PURCHASES_PAUSED')
        self.assertTrue(self.call('payment')['ok'])

    def test_pause_during_phone_approval_cancels_payment(self):
        q = self.quote()
        def approve(**kwargs):
            self.service.purchases_paused = lambda: True
            return {'ok': True, 'approved': True, 'approvedHash': kwargs['commitment_hash']}
        self.approvals.request_hash_approval.side_effect = approve
        self.assertEqual(self.pay(q)['state'], 'PURCHASES_PAUSED')
        self.assertFalse(self.paid_calls())

    def test_concurrent_request_is_rejected(self):
        with self.service.guard('1'):
            self.assertEqual(self.call('quote')['state'], 'BUSY')

    def test_midnight_approval_is_charged_to_submission_day(self):
        self.clock[0] = 86400 * 20000 + 86395
        q = self.quote()
        def approve(**kwargs):
            self.clock[0] += 10
            return {'ok': True, 'approved': True, 'approvedHash': kwargs['commitment_hash']}
        self.approvals.request_hash_approval.side_effect = approve
        self.can_consume = True
        self.pay(q)
        self.assertEqual(self.store.spent('1'), 5_000_000)

    def test_recovery_does_not_cancel_unlocated_payment_after_gateway_restart(self):
        q = self.quote()
        self.store.claim('1', q['quoteId'])
        self.store.begin_payment('1', q['quoteId'])
        self.assertIn('unresolved', self.call('payment')['telegramText'])
        self.assertEqual(self.store.spent('1'), 5_000_000)

    def test_prompt_and_answer_are_not_in_journal(self):
        self.budget()
        self.can_consume = True
        self.call('message', text='private question marker')
        data = self.store.path.read_bytes()
        self.assertNotIn(b'private question marker', data)
        self.assertNotIn(b'Selected-model answer', data)

    def test_balance_failure_is_not_reported_as_zero(self):
        self.service.bridge = Mock(side_effect=SolanaChatError('BALANCE_FAILED', 'Unavailable'))
        status = self.call('start')
        self.assertFalse(status['creditFresh'])
        self.assertEqual(status['outstandingUsdc'], 'unavailable')


class BridgeTests(unittest.TestCase):
    @patch('sign402_gateway.solana_chat.subprocess.run')
    def test_key_only_in_stdin_and_errors_are_redacted(self, run):
        wallets = Mock()
        wallets.decrypt_private_key_for_future_signing.return_value = 'secret-private-key'
        bridge = SolanaBridge(wallets, '/tmp/example', node='/usr/local/bin/node')
        run.return_value = subprocess.CompletedProcess([], 1, '{"ok":false,"code":"BUILD_FAILED"}', 'secret provider response')
        with self.assertRaises(SolanaChatError) as caught:
            bridge('1', 'payer', 'quote')
        self.assertNotIn('secret', str(caught.exception))
        args, kwargs = run.call_args
        self.assertNotIn('secret', str(args))
        self.assertNotIn('secret', str(kwargs['env']))
        self.assertEqual(json.loads(kwargs['input'])['privateKey'], 'secret-private-key')
        wallets.decrypt_private_key_for_future_signing.assert_called_once_with('1', chain='solana')

    @patch('sign402_gateway.solana_chat.subprocess.run', side_effect=subprocess.TimeoutExpired('node', 240))
    def test_timeout_is_uncertain_not_a_retry(self, run):
        wallets = Mock()
        wallets.decrypt_private_key_for_future_signing.return_value = 'private-key'
        bridge = SolanaBridge(wallets, '/tmp/example')
        with self.assertRaises(SolanaChatError) as error:
            bridge('1', 'payer', 'pay')
        self.assertEqual(error.exception.code, 'BRIDGE_UNAVAILABLE')
        self.assertEqual(run.call_count, 1)
