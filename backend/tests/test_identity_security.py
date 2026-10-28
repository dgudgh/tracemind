import unittest

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.auth import get_token_info
from app.core.database import Base
from app.core.security import hash_password
from app.identity.contracts import AuthenticatedIdentity
from app.identity.models import IdentityProvider
from app.identity.providers.ldap import LDAPIdentityProvider, Ldap3DirectoryClient
from app.identity.providers.oidc import OIDCIdentityProvider, OidcValidationError
from app.identity.schemas import ExternalLoginRequest, IdentityProviderCreate
from app.identity.service import login_external
from app.models.user import User


class StubProvider:
    def authenticate(self, provider, credentials):
        if credentials.get("password") != "directory-password":
            raise ValueError("invalid credentials")
        return AuthenticatedIdentity(
            provider_id=provider.id,
            subject="directory-user-42",
            username="directory.user",
            display_name="Directory User",
            groups=["db-operators"],
        )


class StubOidcHttpClient:
    def exchange_and_verify(self, provider, *, code, redirect_uri, expected_nonce):
        if code != "verified-code":
            raise OidcValidationError("invalid code")
        return {
            "sub": "oidc-42",
            "preferred_username": "oidc.user",
            "name": "OIDC User",
            "groups": ["db-operators"],
            "iss": provider.configuration["issuer"],
            "aud": [provider.configuration["client_id"]],
            "nonce": expected_nonce,
        }


class IdentitySecurityTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_provider_secret_config_is_encrypted_and_never_serialized(self):
        from app.identity.api import create_provider

        payload = IdentityProviderCreate(
            name="corporate-oidc",
            provider_type="oidc",
            configuration={"issuer": "https://issuer.example", "client_id": "tracemind"},
            secret_config={"client_secret": "plain-secret-value"},
        )

        response = create_provider(payload, db=self.db)
        stored = self.db.query(IdentityProvider).one()

        self.assertNotIn("plain-secret-value", str(stored.secret_config))
        self.assertTrue(response["has_secret"])
        self.assertNotIn("secret_config", response)

    def test_public_provider_configuration_rejects_secret_fields(self):
        with self.assertRaises(ValidationError):
            IdentityProviderCreate(
                name="unsafe",
                provider_type="oidc",
                configuration={"client_secret": "must-not-be-public"},
            )

    def test_external_login_maps_identity_and_issues_normal_application_token(self):
        provider = IdentityProvider(
            name="directory",
            provider_type="ldap",
            priority=1,
            enabled=1,
            configuration={},
        )
        self.db.add(provider)
        self.db.commit()

        result = login_external(
            {"username": "directory.user", "password": "directory-password"},
            db=self.db,
            provider_clients={"ldap": StubProvider()},
        )

        self.assertTrue(result["token"])
        info = get_token_info(result["token"], db=self.db)
        self.assertEqual(info["source"], "identity")
        self.assertEqual(info["username"], "directory.user")

    def test_external_username_collision_suffix_is_stable(self):
        from app.identity.service import _external_username

        self.db.add(User(username="directory.user", password_hash=hash_password("local-password"), role="user", status="active"))
        self.db.commit()

        first = _external_username(self.db, "directory.user", 7, "stable-subject")
        second = _external_username(self.db, "directory.user", 7, "stable-subject")

        self.assertEqual(first, second)
        self.assertRegex(first, r"directory\.user-7-[0-9a-f]{10}$")

    def test_default_ldap_provider_has_a_real_directory_client_boundary(self):
        self.assertIsInstance(LDAPIdentityProvider().directory_client, Ldap3DirectoryClient)

    def test_oidc_authorization_code_is_server_verified_and_state_is_one_time(self):
        provider = IdentityProvider(
            id=9,
            name="oidc",
            provider_type="oidc",
            enabled=1,
            configuration={
                "issuer": "https://issuer.example",
                "client_id": "tracemind",
                "authorization_endpoint": "https://issuer.example/authorize",
                "redirect_uri": "https://app.example/api/v1/auth/oidc/callback",
            },
        )
        pending = {}
        oidc = OIDCIdentityProvider(http_client=StubOidcHttpClient())
        request = oidc.begin_authorization(provider, provider.configuration["redirect_uri"], pending)

        identity = oidc.complete_authorization(provider, state=request.state, code="verified-code", pending_states=pending)

        self.assertEqual(identity.subject, "oidc-42")
        with self.assertRaises(OidcValidationError):
            oidc.complete_authorization(provider, state=request.state, code="verified-code", pending_states=pending)

    def test_admin_provider_test_cannot_bypass_oidc_authorization_code_flow(self):
        from app.identity.api import authenticate_provider

        provider = IdentityProvider(
            name="oidc-test",
            provider_type="oidc",
            enabled=1,
            priority=1,
            configuration={},
        )
        self.db.add(provider)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            authenticate_provider(
                provider.id,
                ExternalLoginRequest(credentials={"claims": {"sub": "forged"}}),
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
