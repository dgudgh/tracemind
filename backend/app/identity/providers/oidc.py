import secrets
import time
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any
from urllib.parse import urlencode

from app.identity.contracts import AuthenticatedIdentity, ProviderAuthenticationError
from app.core.http_security import require_http_url


class OidcValidationError(ProviderAuthenticationError):
    """The callback did not match the authorization request or OIDC client."""


@dataclass(frozen=True)
class OidcAuthorizationRequest:
    authorization_url: str
    state: str
    nonce: str


class OidcHttpClient:
    def exchange_and_verify(self, provider, *, code: str, redirect_uri: str, expected_nonce: str) -> dict[str, Any]:
        configuration = provider.configuration or {}
        token_endpoint = require_http_url(str(configuration.get("token_endpoint") or ""))
        jwks_uri = require_http_url(str(configuration.get("jwks_uri") or ""))
        client_id = str(configuration.get("client_id") or "")
        client_secret = str(configuration.get("client_secret") or "")
        issuer = str(configuration.get("issuer") or "")
        if not client_id or not client_secret or not issuer:
            raise OidcValidationError("OIDC client_id, client_secret, and issuer are required")
        timeout = max(2, min(int(configuration.get("timeout_seconds") or 10), 30))
        try:
            import jwt
            import requests

            response = requests.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
            token_payload = response.json()
            id_token = token_payload.get("id_token")
            if not id_token:
                raise OidcValidationError("OIDC token response did not include id_token")
            signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
            algorithms = configuration.get("algorithms") or ["RS256"]
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=[str(item) for item in algorithms],
                audience=client_id,
                issuer=issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except OidcValidationError:
            raise
        except Exception as exc:
            raise OidcValidationError("OIDC token exchange or signature validation failed") from exc
        if not compare_digest(str(claims.get("nonce", "")), expected_nonce):
            raise OidcValidationError("OIDC nonce validation failed")
        return dict(claims)


class OIDCIdentityProvider:
    """Validates the OIDC callback claims supplied by a token-verifying client."""

    def __init__(self, http_client: OidcHttpClient | None = None):
        self.http_client = http_client or OidcHttpClient()

    def begin_authorization(self, provider, redirect_uri: str, pending_states: dict[str, dict[str, Any]]) -> OidcAuthorizationRequest:
        configuration = provider.configuration or {}
        endpoint = require_http_url(str(configuration.get("authorization_endpoint") or ""))
        client_id = configuration.get("client_id")
        if not endpoint or not client_id:
            raise OidcValidationError("OIDC authorization endpoint and client_id are required")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        pending_states[state] = {
            "provider_id": provider.id,
            "nonce": nonce,
            "issuer": configuration.get("issuer", ""),
            "audience": client_id,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": configuration.get("scope", "openid profile email groups"),
                "state": state,
                "nonce": nonce,
            }
        )
        return OidcAuthorizationRequest(f"{endpoint}?{query}", state, nonce)

    def complete_authorization(self, provider, *, state: str, code: str, pending_states: dict[str, dict[str, Any]]) -> AuthenticatedIdentity:
        expected = pending_states.pop(state, None)
        if not expected or expected.get("provider_id") != provider.id:
            raise OidcValidationError("OIDC state is missing, expired, or belongs to another provider")
        if time.time() - float(expected.get("created_at") or 0) > 300:
            raise OidcValidationError("OIDC state has expired")
        claims = self.http_client.exchange_and_verify(
            provider,
            code=code,
            redirect_uri=str(expected.get("redirect_uri") or ""),
            expected_nonce=str(expected.get("nonce") or ""),
        )
        return self._identity_from_claims(provider, claims=claims, expected=expected)

    def validate_callback(self, provider, *, state: str, claims: dict[str, Any], pending_states: dict[str, dict[str, Any]]) -> AuthenticatedIdentity:
        expected = pending_states.pop(state, None)
        if not expected or expected.get("provider_id") != provider.id:
            raise OidcValidationError("OIDC state is missing, expired, or belongs to another provider")
        return self._identity_from_claims(provider, claims=claims, expected=expected)

    def _identity_from_claims(self, provider, *, claims: dict[str, Any], expected: dict[str, Any]) -> AuthenticatedIdentity:
        if not compare_digest(str(claims.get("nonce", "")), str(expected["nonce"])):
            raise OidcValidationError("OIDC nonce validation failed")
        if not compare_digest(str(claims.get("iss", "")), str(expected.get("issuer", ""))):
            raise OidcValidationError("OIDC issuer validation failed")
        audience = claims.get("aud", [])
        audiences = audience if isinstance(audience, list) else [audience]
        if expected.get("audience") not in audiences:
            raise OidcValidationError("OIDC audience validation failed")
        subject = claims.get("sub")
        username = claims.get("preferred_username") or claims.get("email")
        if not subject or not username:
            raise OidcValidationError("OIDC claims did not include sub and username")
        groups = claims.get("groups", [])
        if not isinstance(groups, list):
            raise OidcValidationError("OIDC groups claim must be a list")
        return AuthenticatedIdentity(
            provider_id=provider.id,
            subject=str(subject),
            username=str(username),
            display_name=str(claims.get("name") or ""),
            groups=[str(group) for group in groups],
            claims=dict(claims),
        )

    def authenticate(self, provider, credentials: dict[str, Any]) -> AuthenticatedIdentity:
        try:
            return self.validate_callback(
                provider,
                state=str(credentials["state"]),
                claims=dict(credentials["claims"]),
                pending_states=credentials["pending_states"],
            )
        except KeyError as exc:
            raise OidcValidationError("OIDC callback is incomplete") from exc
