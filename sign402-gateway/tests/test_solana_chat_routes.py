import unittest
from unittest.mock import Mock
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
        for operation in ('network', 'quote', 'pay', 'payment'):
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
