"""Regression coverage for location follow-ups and selected-model failures; no network/payments."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from sign402_gateway import web_agent as wg, web_internal, web_ask
from sign402_gateway.agent_allowance import AllowanceError

ACCOUNT = 'wallet:0x1111111111111111111111111111111111111111'

class ContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.calls = []
        self.intent = 'chat'
        lane = Mock(); lane.status.return_value = {'configured': True, 'state': 'granted'}
        lane.stale_allowances.return_value = []
        self.reply = {'ok': True, 'text': 'Answer', 'costAtomic': 1000}
        def shop(action, account, body):
            self.calls.append((action, body))
            if action == 'venice-chat': return 200, self.reply
            if action == 'data-buy': return 200, {'ok': True, 'name': 'Web search', 'costUsd': '0.007', 'digest': '{}'}
            raise AssertionError('Unexpected action: '+action)
        self.agent = wg.WebAgent(allowance=lane, shop=shop, store=wg.ChatStore(Path(tmp.name)/'web.db'),
                                classify=lambda text: {'intent': self.intent})

    def test_prague_survives_planner_failure_and_wrong_assistant_city(self):
        chat = self.agent.message(ACCOUNT, None, "Hey, I'm hungry in now in Prague")['chatId']
        self.agent.store.add(chat, 'assistant', 'Coffee in The Hague', [], 1)
        self.intent = 'live_data'
        self.agent.message(ACCOUNT, chat, 'And what about good coffee near centrum?')
        query = next(body['params']['query'] for action, body in self.calls if action=='data-buy')
        self.assertIn('Prague', query); self.assertIn('coffee', query)
        self.assertNotIn('Hague', query)

    def test_location_correction_continues_coffee_even_if_classifier_would_pick_esim(self):
        self.intent = 'live_data'
        chat = self.agent.message(ACCOUNT, None, 'Good coffee near centrum?')['chatId']
        self.calls.clear(); self.intent = 'esim'
        self.agent.message(ACCOUNT, chat, 'I need in Prague')
        query = self.calls[0][1]['params']['query']
        self.assertEqual(self.calls[0][0], 'data-buy')
        self.assertIn('coffee', query); self.assertIn('Prague', query)
        self.assertFalse(any(action.startswith('bitrefill') for action, _ in self.calls))

    def test_new_explicit_request_is_not_replaced_by_previous_lookup(self):
        self.intent = 'live_data'
        chat = self.agent.message(ACCOUNT, None, 'Coffee in Prague')['chatId']
        with patch.object(self.agent, '_on_catalog', return_value=('eSIM plans', [])) as catalog:
            self.intent = 'esim'
            self.agent.message(ACCOUNT, chat, 'Find an eSIM for Germany')
            self.assertEqual(catalog.call_args.args[2:], ('Find an eSIM for Germany', 'esim'))

    def test_other_chat_does_not_inherit_prague(self):
        self.intent = 'live_data'
        self.agent.message(ACCOUNT, None, 'Coffee in Prague')
        self.calls.clear()
        self.agent.message(ACCOUNT, None, 'Coffee in Lisbon')
        query = self.calls[0][1]['params']['query']
        self.assertIn('Lisbon', query); self.assertNotIn('Prague', query)

    def test_data_planner_sees_only_user_context(self):
        model = Mock(return_value='{"tool":"places","query":"coffee near centrum"}')
        result = wg.plan_data('Coffee near centrum?', model, '2026-10-03', [
            {'role':'user','content':'I am in Prague'},
            {'role':'assistant','content':'The Hague'}])
        self.assertIn('Prague', result[1]['query'])
        self.assertNotIn('Hague', json.dumps(model.call_args.args[0]))

    def test_ask_failure_is_shown_without_claiming_venice_or_calling_fallback(self):
        self.intent = 'live_data'
        self.reply = {'ok':False, 'error':'ask_refused', 'provider':'SingIt Ask',
                      'text':'A previous Ask payment needs settlement review. No new payment was sent.'}
        model=Mock(side_effect=wg.AgentUnavailable('offline'))
        self.agent.model=model
        answer=self.agent.message(ACCOUNT,None,'Coffee in Prague')['messages'][1]
        self.assertEqual(answer['text'],self.reply['text'])
        self.assertEqual(model.call_count,1) # planner only; never substitute another answer model
        self.assertNotIn('Venice',answer['text'])

    def test_blocked_ask_does_not_buy_more_search_data(self):
        with patch.object(web_internal, '_account', return_value=ACCOUNT), \
             patch.object(web_ask, 'selected', return_value=True), \
             patch.object(web_ask, 'require_payment_ready', side_effect=AllowanceError('Settlement review required.')), \
             patch('sign402_gateway.web_data.buy') as buy:
            with self.assertRaises(AllowanceError):
                web_internal.handle(Mock(), 'data-buy', {'account': ACCOUNT, 'tool': 'places'})
            buy.assert_not_called()

    def test_dispatch_preserves_ask_identity_on_error(self):
        with patch.object(web_internal, '_account', return_value=ACCOUNT), \
             patch.object(web_ask, 'selected', return_value=True), \
             patch.object(web_ask, 'chat', side_effect=AllowanceError('Settlement review required.')):
            status, reply = web_internal.handle(Mock(), 'venice-chat', {'account':ACCOUNT})
        self.assertEqual(status,409); self.assertEqual(reply['provider'],'SingIt Ask')

if __name__=='__main__': unittest.main()
