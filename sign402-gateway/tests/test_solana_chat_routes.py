import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from sign402_gateway.server import _require_authenticated_user
import test_chat_endpoints as endpoints


class SolanaChatRouteTests(unittest.TestCase):
    setUp = endpoints.ChatEndpointTestCase.setUp
    enable_flag = endpoints.ChatEndpointTestCase.enable_flag
    make_handler = endpoints.ChatEndpointTestCase.make_handler
    response_text = endpoints.ChatEndpointTestCase.response_text
    response_json = endpoints.ChatEndpointTestCase.response_json
    status_of = endpoints.ChatEndpointTestCase.status_of
    authenticated = endpoints.ChatEndpointTestCase.authenticated

    def server(self, chain='solana'):
        server = endpoints.ChatDummyServer()
        server.solana_chat_service = Mock()
        server.solana_chat_service.store.chain.return_value = chain
        server.solana_chat_service.handle.return_value = {'ok': True, 'chain': 'solana'}
        server.chat_service.start.return_value = {'ok': True}
        return server

    def test_solana_message_never_calls_base_service(self):
        server = self.server()
        self.authenticated(server)
        response = self.make_handler('/agent/chat/message', {'text': 'hello', 'chain': 'solana'}, server=server)
        self.assertEqual(self.response_json(response)['chain'], 'solana')
        server.chat_service.send.assert_not_called()
        server.solana_chat_service.handle.assert_called_once_with('/agent/chat/message', endpoints.USER_ID, {'text': 'hello', 'chain': 'solana'})

    def test_every_payment_route_requires_authentication(self):
        for operation in ('network', 'quote', 'pay', 'payment', 'search', 'search-prepare', 'search-approve', 'search-disable', 'search-payment'):
            with self.subTest(operation=operation):
                server = self.server()
                response = self.make_handler('/agent/chat/' + operation, {}, server=server, headers={})
                self.assertEqual(self.status_of(response), 401)
                server.solana_chat_service.handle.assert_not_called()

    def test_stale_network_is_rejected_before_base_payment(self):
        server = self.server(chain='base')
        self.authenticated(server)
        response = self.make_handler('/agent/chat/message', {'chain': 'solana', 'text': 'hello'}, server=server)
        self.assertEqual(self.response_json(response)['state'], 'NETWORK_CHANGED')
        server.chat_service.send.assert_not_called()
        server.solana_chat_service.handle.assert_not_called()

    def test_network_selection_returns_current_network_settings(self):
        server = self.server(chain='base')
        self.authenticated(server)
        response = self.make_handler('/agent/chat/network', {'chain': 'base'}, server=server)
        self.assertEqual(self.response_json(response)['availableChains'], ['base', 'solana'])
        server.solana_chat_service.store.chain.assert_any_call(endpoints.USER_ID, 'base')

    def test_disabled_solana_never_falls_back_to_base(self):
        server = endpoints.ChatDummyServer()
        self.authenticated(server)
        response = self.make_handler('/agent/chat/message', {'chain': 'solana', 'text': 'hello'}, server=server)
        self.assertFalse(self.response_json(response)['ok'])
        server.chat_service.send.assert_not_called()

    def test_payment_recovery_keeps_explicit_authenticated_identity(self):
        server = self.server()
        self.authenticated(server)
        self.make_handler('/agent/chat/payment', {'chain': 'solana', 'quoteId': 'q', 'telegramUserId': 'attacker'}, server=server)
        self.assertEqual(server.solana_chat_service.handle.call_args.args[1], endpoints.USER_ID)

    def test_disabling_solana_preserves_choice_and_blocks_base_fallback(self):
        server = self.server(chain='solana')
        server.solana_chat_service.enabled = False
        self.authenticated(server)
        response = self.make_handler('/agent/chat/message', {'text': 'hello'}, server=server)
        self.assertEqual(self.response_json(response)['state'], 'NETWORK_UNAVAILABLE')
        server.chat_service.send.assert_not_called()
        server.solana_chat_service.handle.assert_not_called()

    @patch('sign402_gateway.server._enforce_user_request_rate')
    def test_real_auth_guard_allows_only_integrated_solana_chat_routes(self, rate):
        for operation in ('start', 'end', 'models', 'network', 'approve-policy', 'message', 'quote', 'pay', 'payment', 'search', 'search-prepare', 'search-approve', 'search-disable', 'search-payment'):
            with self.subTest(operation=operation):
                server = self.server()
                server.user_wallet_service.resolve_telegram_user_id.return_value = endpoints.USER_ID
                response = self.make_handler('/agent/chat/' + operation,
                    {'telegramUserId': endpoints.USER_ID, 'chain': 'solana'}, server=server)
                self.assertTrue(self.response_json(response).get('ok'), self.response_json(response))
                server.solana_chat_service.handle.assert_called_once()

    @patch('sign402_gateway.server._enforce_user_request_rate')
    def test_real_auth_guard_rejects_user_substitution(self, rate):
        server = self.server()
        server.user_wallet_service.resolve_telegram_user_id.return_value = endpoints.USER_ID
        response = self.make_handler('/agent/chat/pay', {'telegramUserId': 'other', 'chain': 'solana'}, server=server)
        self.assertEqual(self.status_of(response), 401)
        server.solana_chat_service.handle.assert_not_called()

    def test_real_auth_guard_still_blocks_solana_legacy_shop_calls(self):
        server = self.server()
        server.user_wallet_service.resolve_telegram_user_id.return_value = endpoints.USER_ID
        handler = SimpleNamespace(server=server, path='/agent/buy-bitrefill', headers={
            'Authorization': 'Bearer ' + endpoints.WALLET_TOKEN,
            'X-Sign402-User-Token': endpoints.USER_TOKEN,
        })
        with self.assertRaisesRegex(ValueError, 'not enabled on Solana'):
            _require_authenticated_user(handler, {'chain': 'solana', 'telegramUserId': endpoints.USER_ID})
