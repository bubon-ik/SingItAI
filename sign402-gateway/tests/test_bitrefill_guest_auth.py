import unittest

from sign402_gateway.bitrefill_guest_auth import (
    GuestAuthorizationError,
    GuestMcpAuthorizer,
)


PROTECTED_RESOURCE = {
    "resource": "https://api.bitrefill.com/",
    "authorization_servers": ["https://api.bitrefill.com/oauth/mcp"],
}
SERVER_METADATA = {
    "issuer": "https://api.bitrefill.com/oauth/mcp",
    "registration_endpoint": "https://api.bitrefill.com/oauth/mcp/register",
    "token_endpoint": "https://api.bitrefill.com/oauth/mcp/token",
    "grant_types_supported": ["client_credentials"],
}


class FakeOAuthServer:
    def __init__(self, *, token="guest_token_1", expires_in=21600):
        self.calls = []
        self.token = token
        self.expires_in = expires_in
        self.registrations = 0
        self.failures = {}

    def __call__(self, method, url, *, json=None, data=None):
        self.calls.append((method, url, json, data))
        if url in self.failures:
            return self.failures[url]
        if url.endswith("/.well-known/oauth-protected-resource"):
            return 200, dict(PROTECTED_RESOURCE)
        if "/.well-known/oauth-authorization-server" in url:
            return 200, dict(SERVER_METADATA)
        if url.endswith("/register"):
            self.registrations += 1
            return 201, {
                "client_id": f"client_{self.registrations}",
                "client_secret": "REGISTERED-SECRET-MARKER",
            }
        if url.endswith("/token"):
            return 200, {
                "access_token": self.token,
                "token_type": "bearer",
                "expires_in": self.expires_in,
            }
        raise AssertionError(f"unexpected OAuth call: {url}")


class GuestMcpAuthorizerTests(unittest.TestCase):
    def _authorizer(self, server, **overrides):
        clock = overrides.pop("clock", lambda: 1_000.0)
        return GuestMcpAuthorizer(
            mcp_url="https://api.bitrefill.com/mcp?ref=nrVGauph",
            request=server,
            now_provider=clock,
            **overrides,
        )

    def test_a_token_is_minted_without_any_account_credential(self):
        server = FakeOAuthServer()
        authorizer = self._authorizer(server)

        self.assertEqual(authorizer.token(), "guest_token_1")
        sent = "\n".join(str(call) for call in server.calls)
        self.assertNotIn("BITREFILL_API_KEY", sent)
        self.assertNotIn("Authorization", sent)

    def test_discovery_reads_the_advertised_endpoints(self):
        server = FakeOAuthServer()

        self._authorizer(server).token()

        urls = [url for _method, url, _json, _data in server.calls]
        self.assertEqual(
            urls,
            [
                "https://api.bitrefill.com/.well-known/oauth-protected-resource",
                "https://api.bitrefill.com/.well-known/"
                "oauth-authorization-server/oauth/mcp",
                "https://api.bitrefill.com/oauth/mcp/register",
                "https://api.bitrefill.com/oauth/mcp/token",
            ],
        )

    def test_the_token_request_is_bound_to_the_advertised_resource(self):
        # Bitrefill rejects a client_credentials request that names no resource.
        server = FakeOAuthServer()

        self._authorizer(server).token()

        _method, _url, _json, data = server.calls[-1]
        self.assertEqual(data["grant_type"], "client_credentials")
        self.assertEqual(data["resource"], "https://api.bitrefill.com/")

    def test_a_live_token_is_reused_instead_of_registering_again(self):
        server = FakeOAuthServer()
        authorizer = self._authorizer(server)

        self.assertEqual(authorizer.token(), authorizer.token())

        self.assertEqual(server.registrations, 1)
        self.assertEqual(
            len([url for _m, url, _j, _d in server.calls if url.endswith("/token")]),
            1,
        )

    def test_an_expiring_token_is_replaced_without_re_registering(self):
        server = FakeOAuthServer(expires_in=300)
        now = [1_000.0]
        authorizer = self._authorizer(server, clock=lambda: now[0])

        authorizer.token()
        now[0] = 1_000.0 + 299
        authorizer.token()

        self.assertEqual(server.registrations, 1)
        self.assertEqual(
            len([url for _m, url, _j, _d in server.calls if url.endswith("/token")]),
            2,
        )

    def test_a_failed_stage_names_no_secret(self):
        server = FakeOAuthServer()
        server.failures["https://api.bitrefill.com/oauth/mcp/token"] = (
            400,
            {"error_description": "client_secret REGISTERED-SECRET-MARKER is bad"},
        )
        authorizer = self._authorizer(server)

        with self.assertRaises(GuestAuthorizationError) as captured:
            authorizer.token()

        self.assertNotIn("REGISTERED-SECRET-MARKER", str(captured.exception))
        self.assertIn("token request", str(captured.exception))

    def test_a_missing_authorization_server_is_a_clear_failure(self):
        server = FakeOAuthServer()
        server.failures[
            "https://api.bitrefill.com/.well-known/oauth-protected-resource"
        ] = (200, {"resource": "https://api.bitrefill.com/"})
        authorizer = self._authorizer(server)

        with self.assertRaisesRegex(
            GuestAuthorizationError,
            "authorization server",
        ):
            authorizer.token()

    def test_forget_makes_the_next_call_register_again(self):
        server = FakeOAuthServer()
        authorizer = self._authorizer(server)

        authorizer.token()
        authorizer.forget()
        authorizer.token()

        self.assertEqual(server.registrations, 2)

    def test_a_plain_http_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "https"):
            GuestMcpAuthorizer(
                mcp_url="http://api.bitrefill.com/mcp",
                request=FakeOAuthServer(),
            )


if __name__ == "__main__":
    unittest.main()
