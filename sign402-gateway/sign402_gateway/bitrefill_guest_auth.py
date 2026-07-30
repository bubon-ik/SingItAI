"""Anonymous MCP sessions for Bitrefill guest checkout.

Bitrefill answers an unauthenticated MCP request with an OAuth challenge rather
than a refusal: the endpoint supports anonymous dynamic client registration, so
a session that belongs to nobody's account is obtained by registering a
throwaway client and asking for a `client_credentials` token. That is what makes
a purchase attributable to the affiliate code — a purchase signed with the
account that owns the code never is.

Every value handled here is a bearer secret. Nothing in this module logs one,
returns one in an error, or writes one to disk: the client registration lives
only in the process that made it, and a restart simply registers again.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Any, Callable


DEFAULT_CLIENT_NAME = "sign402-gateway"
# Bitrefill's registration endpoint requires the field even for a client that
# never completes an interactive redirect.
DEFAULT_REDIRECT_URI = "http://127.0.0.1/sign402-guest-checkout"
REFRESH_MARGIN_SECONDS = 120.0


class GuestAuthorizationError(RuntimeError):
    """A guest session could not be opened, described without any secret."""


class GuestMcpAuthorizer:
    """Mint short-lived anonymous bearer tokens for guest purchases."""

    def __init__(
        self,
        *,
        mcp_url: str,
        request: Callable[..., tuple[int, Any]],
        client_name: str = DEFAULT_CLIENT_NAME,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        now_provider: Callable[[], float] = time.time,
        refresh_margin_seconds: float = REFRESH_MARGIN_SECONDS,
    ):
        url = str(mcp_url).strip()
        if not url.startswith("https://"):
            raise ValueError("Bitrefill MCP URL must use https")
        parts = urllib.parse.urlsplit(url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._request = request
        self._client_name = str(client_name)
        self._redirect_uri = str(redirect_uri)
        self._now = now_provider
        self._refresh_margin_seconds = float(refresh_margin_seconds)
        self._lock = threading.Lock()
        self._registration: dict[str, str] | None = None
        self._token = ""
        self._token_expires_at = 0.0

    def token(self) -> str:
        """Return a valid anonymous bearer token, registering if needed."""
        with self._lock:
            if self._token and self._now() < (
                self._token_expires_at - self._refresh_margin_seconds
            ):
                return self._token
            resource, authorization_server = self._protected_resource()
            metadata = self._authorization_server_metadata(authorization_server)
            if self._registration is None:
                self._registration = self._register(
                    str(metadata.get("registration_endpoint") or "")
                )
            self._token, self._token_expires_at = self._request_token(
                str(metadata.get("token_endpoint") or ""),
                resource=resource,
            )
            return self._token

    def forget(self) -> None:
        """Drop the cached client and token, so the next call registers again."""
        with self._lock:
            self._registration = None
            self._token = ""
            self._token_expires_at = 0.0

    def _protected_resource(self) -> tuple[str, str]:
        payload = self._json(
            "GET",
            f"{self._origin}/.well-known/oauth-protected-resource",
            stage="protected resource metadata",
        )
        servers = payload.get("authorization_servers")
        authorization_server = (
            str(servers[0]).strip()
            if isinstance(servers, list) and servers
            else ""
        )
        resource = str(payload.get("resource") or "").strip()
        if not authorization_server or not resource:
            raise GuestAuthorizationError(
                "Bitrefill did not advertise a guest authorization server"
            )
        return resource, authorization_server

    def _authorization_server_metadata(
        self,
        authorization_server: str,
    ) -> dict[str, Any]:
        # RFC 8414 inserts the well-known segment between host and issuer path,
        # which is where Bitrefill serves it.
        parts = urllib.parse.urlsplit(authorization_server)
        path = parts.path.strip("/")
        url = (
            f"{parts.scheme}://{parts.netloc}"
            f"/.well-known/oauth-authorization-server"
            f"{'/' + path if path else ''}"
        )
        metadata = self._json(
            "GET",
            url,
            stage="authorization server metadata",
        )
        for field in ("registration_endpoint", "token_endpoint"):
            if not str(metadata.get(field) or "").strip():
                raise GuestAuthorizationError(
                    f"Bitrefill guest authorization is missing {field}"
                )
        return metadata

    def _register(self, registration_endpoint: str) -> dict[str, str]:
        payload = self._json(
            "POST",
            registration_endpoint,
            json={
                "client_name": self._client_name,
                "redirect_uris": [self._redirect_uri],
                "grant_types": ["client_credentials"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "mcp",
            },
            stage="client registration",
        )
        client_id = str(payload.get("client_id") or "").strip()
        client_secret = str(payload.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise GuestAuthorizationError(
                "Bitrefill guest client registration returned no credentials"
            )
        return {"client_id": client_id, "client_secret": client_secret}

    def _request_token(
        self,
        token_endpoint: str,
        *,
        resource: str,
    ) -> tuple[str, float]:
        registration = self._registration or {}
        payload = self._json(
            "POST",
            token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": registration.get("client_id", ""),
                "client_secret": registration.get("client_secret", ""),
                "scope": "mcp",
                # Required: the token is bound to the resource it may be spent
                # on, and Bitrefill rejects the request without it.
                "resource": resource,
            },
            stage="token request",
        )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise GuestAuthorizationError(
                "Bitrefill guest token request returned no token"
            )
        try:
            lifetime = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        if lifetime <= 0:
            lifetime = 300.0
        return token, self._now() + lifetime

    def _json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            status, payload = self._request(method, url, json=json, data=data)
        except Exception as exc:
            # The body of a failed OAuth call can echo the client secret back,
            # so only the stage travels.
            raise GuestAuthorizationError(
                f"Bitrefill guest {stage} failed"
            ) from exc
        if int(status) >= 400 or not isinstance(payload, dict):
            raise GuestAuthorizationError(
                f"Bitrefill guest {stage} failed (HTTP {int(status)})"
            )
        return payload


def build_http_request(timeout_seconds: float = 30.0) -> Callable[..., tuple[int, Any]]:
    """Return an HTTP caller for the authorizer, isolated for testing."""
    import httpx

    def request(
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        ) as client:
            response = client.request(method, url, json=json, data=data)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return response.status_code, payload

    return request
