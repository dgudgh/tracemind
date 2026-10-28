import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.auth import get_token_info
from app.core.database import Base
from app.identity.models import IdentityProvider
from app.identity.providers.oidc import OIDCIdentityProvider
from app.identity.service import encode_secret_config


class StubOidcHttpClient:
    def exchange_and_verify(self, provider, *, code, redirect_uri, expected_nonce):
        self.last_redirect_uri = redirect_uri
        return {
            "sub": "oidc-login-user",
            "preferred_username": "oidc.login",
            "name": "OIDC Login User",
            "groups": [],
            "iss": provider.configuration["issuer"],
            "aud": [provider.configuration["client_id"]],
            "nonce": expected_nonce,
        }


class OidcLoginFlowTest(unittest.TestCase):
    def setUp(self):
        from app.api.v1 import auth

        auth.OIDC_PENDING_STATES.clear()
        auth.OIDC_LOGIN_CODES.clear()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.provider = IdentityProvider(
            name="corporate-oidc",
            provider_type="oidc",
            enabled=1,
            priority=1,
            configuration={
                "authorization_endpoint": "https://issuer.example/authorize",
                "issuer": "https://issuer.example",
                "client_id": "tracemind",
                "redirect_uri": "https://app.example/api/v1/auth/oidc/callback",
                "frontend_redirect_uri": "https://app.example/",
            },
            secret_config=encode_secret_config({"client_secret": "encrypted-at-rest"}),
        )
        self.db.add(self.provider)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_authorization_callback_uses_one_time_exchange_code(self):
        from app.api.v1 import auth

        oidc = OIDCIdentityProvider(http_client=StubOidcHttpClient())
        with patch.object(auth, "OIDCIdentityProvider", return_value=oidc):
            started = auth.oidc_start(self.provider.id, db=self.db)
            state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
            response = auth.oidc_callback(state=state, code="verified-code", db=self.db)

        location = response.headers["location"]
        login_code = parse_qs(urlsplit(location).query)["external_login_code"][0]
        token_result = auth.oidc_exchange(auth.OidcExchangeRequest(code=login_code))
        info = get_token_info(token_result["token"], db=self.db)

        self.assertEqual(info["source"], "identity")
        self.assertEqual(info["username"], "oidc.login")
        with self.assertRaises(HTTPException) as reused:
            auth.oidc_exchange(auth.OidcExchangeRequest(code=login_code))
        self.assertEqual(reused.exception.status_code, 401)

    def test_anonymous_oidc_cache_prunes_expired_and_oldest_entries(self):
        from app.api.v1 import auth

        cache = {
            "expired": {"created_at": 1.0},
            "oldest": {"created_at": 990.0},
            "newest": {"created_at": 999.0},
        }
        with patch.object(auth, "OIDC_CACHE_MAX_ITEMS", 2):
            auth._prune_oidc_cache(
                cache,
                max_age_seconds=100,
                reserve_slot=True,
                now=1000.0,
            )

        self.assertEqual(cache, {"newest": {"created_at": 999.0}})


if __name__ == "__main__":
    unittest.main()
