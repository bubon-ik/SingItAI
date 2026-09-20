"""Solana consent cards and routing, with no external calls."""
import unittest
import test_conversation_ui as conversation


class SolanaChatUITests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    press = conversation.ConversationTests.press
    operations = conversation.ConversationTests.operations
    review = conversation.ConversationTests.review

    def execute_chat(self, operation, identity, *, payload=None, user_access_token):
        if operation in ('network', 'quote', 'pay', 'payment'):
            self.calls.append((operation, str(identity.user_id), payload))
            if operation == 'network':
                self.status['chain'] = payload['chain']
                self.status['availableChains'] = ['base', 'solana']
                return dict(self.status)
            if operation == 'quote':
                return {'ok': True, 'quote': {'quoteId': 'quote-id', 'approvalHash': 'a'*64,
                    'amountUsdc': '5.000000', 'payer': 'MySolanaWallet', 'recipient': 'VeniceReceiver',
                    'asset': 'USDCmint', 'expiresAt': '2026-09-20T16:05:00Z'}}
            if operation == 'pay':
                self.on_answer()
                return {'ok': True, 'telegramText': 'Solana paid receipt with signature'}
            return {'ok': True, 'telegramText': 'Solana payment status'}
        return conversation.ConversationTests.execute_chat(self, operation, identity, payload=payload, user_access_token=user_access_token)

    def select(self):
        self.press('/chat_network solana')

    def test_network_switch_is_free_and_settings_identify_solana(self):
        self.assertIn('USDC on Solana', self.press('/chat_network solana'))
        self.assertEqual(self.operations(), ['network'])
        self.assertEqual(self.plugin._CHAT_NETWORKS['1045618308'], 'solana')

    def test_ordinary_message_is_pinned_to_chosen_network(self):
        self.select()
        self.status.update(hasPolicy=True, policyExpiresAt=4102444800)
        self.press('hello')
        self.assertEqual(self.calls[-1][2], {'text': 'hello', 'chain': 'solana'})

    def test_budget_review_and_approval_are_bound_to_solana(self):
        self.select()
        text = self.review()
        self.assertIn('USDC on Solana', text)
        self.assertIn('separate approval of its exact quote', text)
        self.press('Request budget approval')
        self.assertEqual(self.calls[-2][2]['chain'], 'solana')
        self.assertEqual(self.calls[-1][2]['chain'], 'solana')

    def test_quote_shows_full_terms_without_starting_payment(self):
        self.select()
        text = self.press('/chat_topup')
        for term in ('5.000000', 'MySolanaWallet', 'VeniceReceiver', 'USDCmint', 'Expires:', 'x402'):
            self.assertIn(term, text)
        self.assertEqual(self.operations(), ['network', 'quote'])

    def test_pay_requests_exact_displayed_quote(self):
        self.select()
        self.press('/chat_pay quote-id ' + 'a'*64)
        self.assertEqual(self.calls[-1][2], {'chain': 'solana', 'quoteId': 'quote-id', 'approvalHash': 'a'*64})

    def test_group_cannot_request_quote_or_payment(self):
        for command in ('/chat_network solana', '/chat_topup', '/chat_pay q hash', '/chat_payment'):
            self.assertIn('private chat', self.press(command, kind='group'))
        self.assertFalse(self.calls)

    def test_navigation_during_payment_preserves_receipt(self):
        self.select()
        self.on_answer = lambda: self.press('/wallet')
        self.assertIn('paid receipt', self.press('/chat_pay quote-id ' + 'a'*64))

    def test_cancel_before_payment_worker_starts_does_not_request_phone_approval(self):
        self.select()
        queued = []
        self.plugin._background_runner = queued.append
        self.press('/chat_pay quote-id ' + 'a'*64)
        self.press('/cancel')
        queued.pop()()
        self.assertNotIn('pay', self.operations())

    def test_unknown_message_cost_is_not_displayed_as_zero(self):
        text = self.plugin._chat_answer_text('1', {'chain': 'solana', 'text': 'Answer', 'costAtomic': None, 'outstandingAtomic': 4_999_000})
        self.assertIn('Solana', text)
        self.assertNotIn('$0.00', text)

    def test_pending_payment_offers_recovery_instead_of_new_question(self):
        self.select()
        self.status.update(paused=True, pauseReason='Check pending payment')
        self.assertIn('Check pending payment', self.press('hello'))
        self.assertNotIn('message', self.operations())

    def test_missing_wallet_offers_setup_before_budget(self):
        self.select()
        self.status.update(walletRequired=True)
        self.assertIn('personal Solana wallet', self.press('hello'))
        self.assertNotIn('1045618308', self.plugin._CHAT_SETUP)
