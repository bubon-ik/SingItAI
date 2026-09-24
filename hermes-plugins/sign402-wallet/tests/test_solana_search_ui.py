"""Search settings, explicit consent and receipts. Fake gateway only."""
import unittest
import test_conversation_ui as conversation
import test_solana_chat_ui as solana


class SearchUITests(unittest.TestCase):
    setUp = conversation.ConversationTests.setUp
    press = conversation.ConversationTests.press
    operations = conversation.ConversationTests.operations

    def execute_chat(self, operation, identity, *, payload=None, user_access_token):
        if operation.startswith('search'):
            self.calls.append((operation, str(identity.user_id), payload))
            if operation == 'search-prepare':
                return {'ok': True, 'approvalHash': 'a'*64, 'telegramText': 'Review Exa budget: up to 0.02 USDC per call and 0.2 USDC per day.'}
            if operation == 'search-approve':
                self.on_approve()
                return {'ok': True, 'telegramText': 'Exa search budget approved'}
            return {'ok': True, 'telegramText': 'Exa search settings'}
        return solana.SolanaChatUITests.execute_chat(self, operation, identity, payload=payload, user_access_token=user_access_token)

    def test_commands_are_solanan_only_and_review_moves_no_money(self):
        for command, operation in [('/chat_search','search'), ('/chat_search_review','search-prepare'),
                ('/chat_search_off','search-disable'), ('/chat_search_payment','search-payment')]:
            self.press(command)
            self.assertEqual(self.calls[-1][0], operation)
            self.assertEqual(self.calls[-1][2], {'chain': 'solana'})
        self.assertNotIn('search-approve', self.operations())
        self.assertNotIn('message', self.operations())

    def test_full_review_hash_is_sent_for_phone_approval(self):
        self.press('/chat_search_approve ' + 'a'*64)
        self.assertEqual(self.calls[-1][2], {'chain': 'solana', 'approvalHash': 'a'*64})

    def test_invalid_hash_does_not_call_gateway(self):
        self.press('/chat_search_approve nope')
        self.assertFalse(self.calls)

    def test_group_cannot_review_approve_or_read_search_receipts(self):
        for command in ('/chat_search', '/chat_search_review', '/chat_search_approve ' + 'a'*64, '/chat_search_off', '/chat_search_payment'):
            self.assertIn('private chat', self.press(command, kind='group'))
        self.assertFalse(self.calls)

    def test_navigation_during_approval_preserves_its_result(self):
        self.on_approve = lambda: self.press('/wallet')
        self.assertIn('search budget approved', self.press('/chat_search_approve ' + 'a'*64))

    def test_cancelling_queued_approval_never_sends_it(self):
        queued = []
        self.plugin._background_runner = queued.append
        self.press('/chat_search_approve ' + 'a'*64)
        self.press('/cancel')
        queued.pop()()
        self.assertFalse(self.calls)

    def test_solana_answer_has_sources_precise_separate_cost_and_receipt(self):
        text = self.plugin._chat_answer_text('1', {'chain': 'solana', 'text': 'Answer [1]',
            'outstandingAtomic': 4000000, 'sources': [{'url': 'https://example.com/source'}],
            'webFooter': 'Exa web search: 0.007 USDC · Solana · x402', 'webTransaction': 'fixture-tx'})
        for part in ('[1] https://example.com/source', '0.007 USDC', '$4.00 Venice credit', 'https://solscan.io/tx/fixture-tx'):
            self.assertIn(part, text)

    def test_recovery_forwards_quote_and_signature_without_paying(self):
        self.press('/chat_search_payment q signature')
        self.assertEqual(self.calls[-1][2], {'chain':'solana', 'quoteId':'q', 'transaction':'signature'})
        self.assertEqual(self.operations(), ['search-payment'])
