import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.security import hash_password
from app.identity.models import ExternalIdentity, GroupMapping, IdentityProvider, LoginAudit
from app.identity.providers.oidc import OIDCIdentityProvider, OidcValidationError
from app.identity.service import (
    AuthenticatedIdentity,
    ProviderAuthenticationError,
    authenticate_via_provider_chain,
    disable_missing_external_users,
    map_identity,
)
from app.models.user import User, UserModulePermission


class StubProvider:
    def __init__(self, outcomes, calls):
        self.outcomes = outcomes
        self.calls = calls

    def authenticate(self, provider, credentials):
        self.calls.append(provider.id)
        outcome = self.outcomes[provider.id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class EnterpriseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def provider(self, name, *, provider_type="ldap", priority=100, enabled=1, configuration=None):
        row = IdentityProvider(
            name=name,
            provider_type=provider_type,
            priority=priority,
            enabled=enabled,
            configuration=configuration or {},
        )
        self.db.add(row)
        self.db.commit()
        return row

    def identity(self, provider, subject="u-1", username="external.user", groups=None):
        return AuthenticatedIdentity(
            provider_id=provider.id,
            subject=subject,
            username=username,
            display_name="External User",
            groups=groups or [],
        )

    def test_disabled_provider_is_skipped_and_enabled_providers_follow_priority(self):
        disabled = self.provider("disabled", priority=1, enabled=0)
        first = self.provider("first", priority=10)
        second = self.provider("second", priority=20)
        calls = []
        clients = {
            "ldap": StubProvider(
                {
                    first.id: ProviderAuthenticationError("invalid credentials"),
                    second.id: self.identity(second),
                },
                calls,
            )
        }

        identity = authenticate_via_provider_chain(
            {"username": "external.user", "password": "not-for-audit"},
            db=self.db,
            provider_clients=clients,
        )

        self.assertEqual(identity.provider_id, second.id)
        self.assertEqual(calls, [first.id, second.id])
        self.assertNotIn(disabled.id, calls)

    def test_external_group_mapping_grants_only_explicit_role_and_modules(self):
        provider = self.provider("corporate-directory")
        self.db.add(
            GroupMapping(
                provider_id=provider.id,
                external_group="db-operators",
                role="operator",
                module_keys=["operations.mysql"],
                enabled=1,
            )
        )
        self.db.commit()

        user = map_identity(
            self.identity(provider, groups=["db-operators", "unmapped-admins"]),
            db=self.db,
        )
        modules = {
            row.module_key
            for row in self.db.query(UserModulePermission)
            .filter(UserModulePermission.user_id == user.id)
            .all()
        }

        self.assertEqual(user.role, "operator")
        self.assertEqual(modules, {"operations.mysql"})
        self.assertNotIn("unmapped-admins", modules)

    def test_confirmed_sync_disables_missing_external_user_but_never_builtin_admin(self):
        provider = self.provider("directory")
        admin = User(username="admin", password_hash=hash_password("local-password"), role="admin", status="active")
        worker = User(username="worker", password_hash=hash_password("local-password"), role="operator", status="active")
        self.db.add_all([admin, worker])
        self.db.flush()
        self.db.add_all(
            [
                ExternalIdentity(provider_id=provider.id, subject="admin-subject", user_id=admin.id, username="admin"),
                ExternalIdentity(provider_id=provider.id, subject="worker-subject", user_id=worker.id, username="worker"),
            ]
        )
        self.db.commit()

        self.assertEqual(
            disable_missing_external_users(provider.id, seen_subjects=set(), confirmed=False, db=self.db),
            [],
        )
        self.assertEqual(worker.status, "active")

        disabled_user_ids = disable_missing_external_users(provider.id, seen_subjects=set(), confirmed=True, db=self.db)
        self.db.refresh(admin)
        self.db.refresh(worker)

        self.assertEqual(disabled_user_ids, [worker.id])
        self.assertEqual(worker.status, "disabled")
        self.assertEqual(admin.status, "active")

    def test_confirmed_sync_never_disables_an_administrator_under_another_username(self):
        """Protection from a directory sync cannot be keyed on the literal name ``admin``.

        ``/setup`` forwards its own ``admin_username`` to ``_init_admin_user``, so the
        superuser is routinely called something else. The siblings below already show the
        name-keyed case passing; this one is the case that used to slip through.
        """
        provider = self.provider("directory-renamed")
        superuser = User(username="ops.super", password_hash=hash_password("local-password"),
                         role="admin", status="active")
        worker = User(username="worker.two", password_hash=hash_password("local-password"),
                      role="operator", status="active")
        self.db.add_all([superuser, worker])
        self.db.flush()
        self.db.add_all(
            [
                ExternalIdentity(provider_id=provider.id, subject="super-subject", user_id=superuser.id, username="ops.super"),
                ExternalIdentity(provider_id=provider.id, subject="worker-subject", user_id=worker.id, username="worker.two"),
            ]
        )
        self.db.commit()

        disabled = disable_missing_external_users(provider.id, seen_subjects=set(), confirmed=True, db=self.db)
        self.db.refresh(superuser)
        self.db.refresh(worker)

        self.assertEqual(disabled, [worker.id])
        self.assertEqual(worker.status, "disabled")
        self.assertEqual(superuser.status, "active")

    def test_is_builtin_admin_is_decided_by_role_not_by_username(self):
        from app.identity.service import is_builtin_admin

        self.assertTrue(is_builtin_admin(User(username="ops.super", role="admin")))
        self.assertFalse(is_builtin_admin(User(username="ops.super", role="user")))
        # Legacy clause, kept on purpose: a name match stays protected.
        self.assertTrue(is_builtin_admin(User(username="Admin", role="user")))

    def test_failed_external_auth_keeps_local_admin_active_and_masks_every_audit_attempt(self):
        provider = self.provider("failing-directory")
        admin = User(username="admin", password_hash=hash_password("local-password"), role="admin", status="active")
        self.db.add(admin)
        self.db.commit()
        clients = {"ldap": StubProvider({provider.id: ProviderAuthenticationError("bind failed")}, [])}

        identity = authenticate_via_provider_chain(
            {"username": "administrator", "password": "super-secret", "access_token": "token-value"},
            db=self.db,
            provider_clients=clients,
        )
        self.db.refresh(admin)
        audits = self.db.query(LoginAudit).order_by(LoginAudit.id).all()

        self.assertIsNone(identity)
        self.assertEqual(admin.status, "active")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].outcome, "failure")
        self.assertNotIn("super-secret", str(audits[0].details))
        self.assertNotIn("token-value", str(audits[0].details))
        self.assertNotIn("administrator", str(audits[0].details))
        self.assertNotEqual(audits[0].masked_username, "administrator")

    def test_audit_redacts_nested_secret_aliases(self):
        provider = self.provider("failing-directory")
        clients = {"ldap": StubProvider({provider.id: ProviderAuthenticationError("bind failed")}, [])}

        authenticate_via_provider_chain(
            {
                "username": "external.user",
                "metadata": {"api_key": "api-key-value", "pwd": "password-value"},
            },
            db=self.db,
            provider_clients=clients,
        )

        audit = self.db.query(LoginAudit).one()

        self.assertNotIn("external.user", str(audit.details))
        self.assertNotIn("api-key-value", str(audit.details))
        self.assertNotIn("password-value", str(audit.details))

    def test_empty_provider_chain_writes_a_masked_failure_audit(self):
        identity = authenticate_via_provider_chain(
            {"username": "external.user", "password": "not-for-audit"},
            db=self.db,
        )

        audits = self.db.query(LoginAudit).order_by(LoginAudit.id).all()

        self.assertIsNone(identity)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].outcome, "failure")
        self.assertEqual(audits[0].reason, "no_enabled_provider")
        self.assertNotEqual(audits[0].masked_username, "external.user")
        self.assertNotIn("not-for-audit", str(audits[0].details))

    def test_unexpected_provider_error_fails_closed_without_disabling_local_admin(self):
        provider = self.provider("unstable-directory")
        admin = User(username="admin", password_hash=hash_password("local-password"), role="admin", status="active")
        self.db.add(admin)
        self.db.commit()
        clients = {"ldap": StubProvider({provider.id: RuntimeError("upstream unavailable")}, [])}

        identity = authenticate_via_provider_chain(
            {"username": "administrator", "password": "local-password"},
            db=self.db,
            provider_clients=clients,
        )

        self.db.refresh(admin)
        audit = self.db.query(LoginAudit).one()
        self.assertIsNone(identity)
        self.assertEqual(admin.status, "active")
        self.assertEqual(audit.outcome, "failure")
        self.assertNotIn("local-password", str(audit.details))

    def test_oidc_callback_validates_state_nonce_issuer_and_audience(self):
        provider = self.provider(
            "oidc",
            provider_type="oidc",
            configuration={
                "issuer": "https://issuer.example",
                "client_id": "mesdbtools",
                "authorization_endpoint": "https://issuer.example/authorize",
            },
        )
        oidc = OIDCIdentityProvider()
        pending = {}
        request = oidc.begin_authorization(provider, "https://app.example/callback", pending)
        claims = {
            "sub": "oidc-user",
            "preferred_username": "oidc.user",
            "name": "OIDC User",
            "groups": ["db-operators"],
            "iss": "https://issuer.example",
            "aud": ["mesdbtools"],
            "nonce": request.nonce,
        }

        identity = oidc.validate_callback(provider, state=request.state, claims=claims, pending_states=pending)

        self.assertEqual(identity.subject, "oidc-user")
        self.assertEqual(identity.groups, ["db-operators"])
        with self.assertRaises(OidcValidationError):
            oidc.validate_callback(provider, state=request.state, claims=claims, pending_states=pending)

        for field, value in (("nonce", "wrong"), ("iss", "https://other.example"), ("aud", ["other-client"])):
            pending = {}
            request = oidc.begin_authorization(provider, "https://app.example/callback", pending)
            invalid_claims = dict(claims)
            invalid_claims["nonce"] = request.nonce
            invalid_claims[field] = value
            with self.assertRaises(OidcValidationError):
                oidc.validate_callback(provider, state=request.state, claims=invalid_claims, pending_states=pending)


if __name__ == "__main__":
    unittest.main()
