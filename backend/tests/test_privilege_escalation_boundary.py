"""Privilege ceiling regressions for user/role administration.

``role="admin"`` and the builtin ``platform_admin`` role bypass every policy
check in the request pipeline. A delegated ``platform.security`` operator must
therefore not be able to grant that authority to anybody -- including itself --
while ordinary role delegation keeps working for a superuser.
"""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1 import users
from app.api.v1 import security as security_api
from app.core import auth, database
from app.core.security import hash_password, verify_password
from app.identity import api as identity_api
from app.identity.contracts import AuthenticatedIdentity
from app.identity.models import ExternalIdentity, GroupMapping, IdentityProvider, LoginAudit
from app.identity.service import map_identity
from app.main import AuthMiddleware, authentication_unavailable_response
from app.models.access_control import (
    AccessRole,
    AuthSession,
    RoleActionPermission,
    SecurityAuditEvent,
    UserAccessRole,
    UserAssetScope,
    UserMfaCredential,
    UserOrganization,
)
from app.models.system_setting import DBSystemSetting
from app.models.user import User, UserModulePermission
from app.services.access_control import ensure_builtin_roles


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'privilege.sqlite'}")
    for model in (
        User, UserModulePermission, AuthSession, SecurityAuditEvent,
        AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope,
        UserOrganization, UserMfaCredential,
        DBSystemSetting, IdentityProvider, GroupMapping, ExternalIdentity, LoginAudit,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})

    with factory() as db:
        ensure_builtin_roles(db)
        roles = {row.role_key: row for row in db.query(AccessRole).all()}
        admin = User(username="p.admin", password_hash=hash_password("PAdminOnly7"),
                     role="admin", status="active", password_changed_at=datetime.now())
        delegated = User(username="p.delegated", password_hash=hash_password("PDelegated8"),
                         role="user", status="active", password_changed_at=datetime.now())
        target = User(username="p.target", password_hash=hash_password("PTargetOnly9"),
                      role="user", status="active", password_changed_at=datetime.now())
        db.add_all([admin, delegated, target])
        db.flush()
        # The delegated operator only holds user/role maintenance, nothing else.
        db.add(RoleActionPermission(role_id=roles["viewer"].id, action_key="platform.security"))
        db.add(UserAccessRole(user_id=delegated.id, role_id=roles["viewer"].id))
        # A low-privilege role the delegated operator is allowed to hand out,
        # because every action it grants is inside the operator's own ceiling.
        narrow = AccessRole(role_key="p.narrow", name="窄范围", status="active")
        db.add(narrow)
        db.flush()
        db.add(RoleActionPermission(role_id=narrow.id, action_key="platform.security"))
        provider = IdentityProvider(name="p.ldap", provider_type="ldap", enabled=1,
                                    configuration={"frontend_redirect_uri": "http://test/complete"})
        db.add(provider)
        db.commit()
        admin_id, delegated_id, target_id = admin.id, delegated.id, target.id
        narrow_id = narrow.id
        provider_id = provider.id
        platform_admin_id = roles["platform_admin"].id
        admin_token = auth.issue_user_token(admin, db=db)["token"]
        delegated_token = auth.issue_user_token(delegated, db=db)["token"]

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.add_exception_handler(auth.AuthenticationUnavailable, authentication_unavailable_response)
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(security_api.router, prefix="/api/v1")
    app.include_router(identity_api.router, prefix="/api/v1")

    def get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield type("Env", (), {
            "client": client, "factory": factory, "engine": engine,
            "admin_id": admin_id, "delegated_id": delegated_id, "target_id": target_id,
            "narrow_id": narrow_id, "platform_admin_id": platform_admin_id,
            "provider_id": provider_id,
            "admin_token": admin_token, "delegated_token": delegated_token,
        })()
    engine.dispose()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def _roles_of(env, user_id):
    with env.factory() as db:
        return sorted(row.role_id for row in db.query(UserAccessRole).filter_by(user_id=user_id).all())


def test_delegated_operator_cannot_assign_platform_admin_to_itself_or_anyone(env):
    response = env.client.put(
        f"/api/v1/security/users/{env.delegated_id}/roles",
        headers=headers(env.delegated_token), json={"role_ids": [env.platform_admin_id]},
    )
    assert response.status_code == 403
    assert env.platform_admin_id not in _roles_of(env, env.delegated_id)

    response = env.client.put(
        f"/api/v1/security/users/{env.target_id}/roles",
        headers=headers(env.delegated_token), json={"role_ids": [env.platform_admin_id]},
    )
    assert response.status_code == 403
    assert _roles_of(env, env.target_id) == []


def test_delegated_operator_cannot_self_promote_through_the_users_endpoint(env):
    response = env.client.put(
        f"/api/v1/users/{env.delegated_id}",
        headers=headers(env.delegated_token), json={"role": "admin"},
    )
    assert response.status_code == 403
    with env.factory() as db:
        assert db.get(User, env.delegated_id).role == "user"


def test_delegated_operator_cannot_create_an_administrator_account(env):
    response = env.client.post(
        "/api/v1/users", headers=headers(env.delegated_token),
        json={"username": "p.newadmin", "password": "PNewAdmin7x", "role": "admin"},
    )
    assert response.status_code == 403
    with env.factory() as db:
        assert db.query(User).filter_by(username="p.newadmin").first() is None


def test_delegated_operator_may_still_manage_ordinary_users(env):
    """The boundary is only about acquiring ``role="admin"``, not about user admin."""
    response = env.client.put(
        f"/api/v1/users/{env.target_id}",
        headers=headers(env.delegated_token), json={"display_name": "改过的名字"},
    )
    assert response.status_code == 200
    with env.factory() as db:
        assert db.get(User, env.target_id).display_name == "改过的名字"
        assert db.get(User, env.target_id).role == "user"


def test_delegated_operator_may_still_reset_an_ordinary_user_password(env):
    response = env.client.put(
        f"/api/v1/users/{env.target_id}/reset-password",
        headers=headers(env.delegated_token),
        json={"current_password": "PDelegated8", "new_password": "RotatedPass123"},
    )
    assert response.status_code == 200
    with env.factory() as db:
        assert verify_password("RotatedPass123", db.get(User, env.target_id).password_hash) is True


def test_delegated_credential_admin_keeps_its_documented_power_over_an_admin_account(env):
    """Explicitly preserved business rule, not an oversight.

    ``test_password_reauthentication.test_explicit_security_role_can_reset_using_its_own_password``
    asserts that a delegated ``platform.security`` holder may reset an administrator's
    password with its own re-authentication. This round deliberately did not change
    that; it is recorded here so the boundary cannot drift unnoticed.
    """
    response = env.client.put(
        f"/api/v1/users/{env.admin_id}/reset-password",
        headers=headers(env.delegated_token),
        json={"current_password": "PDelegated8", "new_password": "AdminRotated123"},
    )
    assert response.status_code == 200
    with env.factory() as db:
        assert verify_password("AdminRotated123", db.get(User, env.admin_id).password_hash) is True


def test_delegated_operator_may_still_assign_a_role_inside_its_own_ceiling(env):
    """Delegation keeps working when the granted role adds nothing the actor lacks."""
    response = env.client.put(
        f"/api/v1/security/users/{env.target_id}/roles",
        headers=headers(env.delegated_token), json={"role_ids": [env.narrow_id]},
    )
    assert response.status_code == 200
    assert _roles_of(env, env.target_id) == [env.narrow_id]


def test_group_mapping_to_admin_is_reserved_for_a_superuser(env):
    """An admin group mapping mints a superuser on every SSO login."""
    response = env.client.post(
        f"/api/v1/identity/providers/{env.provider_id}/mappings",
        headers=headers(env.delegated_token),
        json={"external_group": "CN=Domain Admins", "role": "admin"},
    )
    assert response.status_code == 403
    with env.factory() as db:
        assert db.query(GroupMapping).filter_by(provider_id=env.provider_id).count() == 0

    allowed = env.client.post(
        f"/api/v1/identity/providers/{env.provider_id}/mappings",
        headers=headers(env.admin_token),
        json={"external_group": "CN=Domain Admins", "role": "admin"},
    )
    assert allowed.status_code == 200


def test_delegated_operator_may_still_map_non_admin_roles(env):
    response = env.client.post(
        f"/api/v1/identity/providers/{env.provider_id}/mappings",
        headers=headers(env.delegated_token),
        json={"external_group": "CN=Mes Operators", "role": "operator"},
    )
    assert response.status_code == 200


def test_a_mismatched_group_mapping_really_does_grant_admin(env):
    """Why the guard above matters: the mapped role is copied onto the local account."""
    with env.factory() as db:
        db.add(GroupMapping(provider_id=env.provider_id, external_group="CN=Domain Admins",
                            role="admin", module_keys=[], enabled=1))
        db.commit()
        identity = AuthenticatedIdentity(
            provider_id=env.provider_id, subject="ss-subject", username="ss.user",
            display_name="SSO User", groups=["CN=Domain Admins"],
        )
        user = map_identity(identity, db=db)
        db.commit()
        assert user.role == "admin"


def test_superuser_keeps_administrator_role_administration(env):
    response = env.client.put(
        f"/api/v1/security/users/{env.target_id}/roles",
        headers=headers(env.admin_token), json={"role_ids": [env.platform_admin_id]},
    )
    assert response.status_code == 200
    assert _roles_of(env, env.target_id) == [env.platform_admin_id]
    response = env.client.put(
        f"/api/v1/users/{env.target_id}",
        headers=headers(env.admin_token), json={"role": "admin"},
    )
    assert response.status_code == 200
    with env.factory() as db:
        assert db.get(User, env.target_id).role == "admin"


def test_action_ceiling_helper_rejects_star_grants_for_non_superusers(env):
    from app.services.access_control import actions_beyond_actor

    with env.factory() as db:
        beyond = actions_beyond_actor(
            db, env.delegated_id, ["*"], actor_is_superuser=False,
        )
        assert beyond == ["*"]
        assert actions_beyond_actor(
            db, env.delegated_id, ["platform.security"], actor_is_superuser=False,
        ) == []
        assert actions_beyond_actor(
            db, env.delegated_id, ["*"], actor_is_superuser=True,
        ) == []


def test_platform_admin_role_still_grants_the_full_action_catalog(env):
    """Guard against silently weakening the builtin administrator role."""
    with env.factory() as db:
        grants = {
            row.action_key for row in db.query(RoleActionPermission)
            .filter_by(role_id=env.platform_admin_id).all()
        }
    assert grants == {"*"}


def test_a_path_outside_the_policy_table_is_denied_not_allowed(env):
    """Fail-closed default: ``action_for_request`` returns ``None`` for a path the table
    does not know, ``is_action_allowed`` treats ``None`` as denied, and the middleware
    answers 403. Pinned because "no action mapped" is exactly the case a later reader
    might mistake for "no policy required".
    """
    response = env.client.get("/api/v1/not-a-real-area/thing", headers=headers(env.delegated_token))
    assert response.status_code == 403
    assert response.json().get("required_action") is None


def test_the_action_policy_table_is_written_against_the_default_api_prefix():
    """The table hardcodes ``/api/v1`` while the router mounts at ``settings.API_V1_PREFIX``.

    So a non-default prefix routes fine but matches no branch: every non-administrator
    then gets 403 while administrators keep bypassing policy. That is fail-*closed*, not
    fail-open -- the earlier report called it fail-open and was wrong. This test pins the
    coupling so changing the default fails loudly here instead of silently denying
    everyone in production.
    """
    from app.core.config import settings
    from app.services.access_control import action_for_request, prefix_supports_action_policy

    assert settings.API_V1_PREFIX.rstrip("/") == "/api/v1"
    assert action_for_request("GET", "/api/v1/instances") is not None
    assert action_for_request("GET", "/api/instances") is None
    assert action_for_request("POST", "/users") is None

    # The startup diagnostic in main.py keys off this predicate.
    assert prefix_supports_action_policy("/api/v1") is True
    assert prefix_supports_action_policy("/api/v1/") is True
    assert prefix_supports_action_policy("/api") is False
    assert prefix_supports_action_policy("") is False


def test_the_startup_diagnostic_names_the_consequence_of_a_mismatched_prefix():
    """The lifespan hook only logs this; the wording is asserted here.

    A message that says "mismatch" without saying "every non-admin request will 403"
    reads like a cosmetic warning, and an operator changing the prefix during a
    deployment would not connect the two. Both halves are part of the contract.
    """
    from app.services.access_control import api_prefix_mismatch_message

    assert api_prefix_mismatch_message("/api/v1") is None
    assert api_prefix_mismatch_message("/api/v1/") is None

    message = api_prefix_mismatch_message("/x/v9")
    assert message is not None
    assert "/x/v9" in message
    assert "403" in message
    assert "配置错误" in message
    assert "非管理员" in message


def test_an_unmapped_action_is_denied_for_every_non_superuser(env):
    from app.services.access_control import is_action_allowed

    with env.factory() as db:
        assert is_action_allowed(db, env.delegated_id, None) == (False, False)
        assert is_action_allowed(db, env.delegated_id, "") == (False, False)


def test_delegated_operator_cannot_delete_an_administrator_account(env):
    """The pre-existing rule ("不能删除管理员账号") has to be keyed on the role.

    ``/setup`` lets the installer pick the superuser's username, so a delegate able to
    delete any ``role="admin"`` account can destroy the platform administrator -- and
    deletion cascades over the MFA credential, which a password reset does not touch.
    """
    response = env.client.delete(f"/api/v1/users/{env.admin_id}", headers=headers(env.delegated_token))
    assert response.status_code == 403
    with env.factory() as db:
        assert db.get(User, env.admin_id) is not None


def test_delegated_operator_may_still_delete_an_ordinary_user(env):
    """The new guard is about administrator accounts only, not about user administration."""
    response = env.client.delete(f"/api/v1/users/{env.target_id}", headers=headers(env.delegated_token))
    assert response.status_code == 200
    with env.factory() as db:
        assert db.get(User, env.target_id) is None


def test_superuser_may_delete_an_administrator_under_another_username(env):
    with env.factory() as db:
        second = User(username="p.second.admin", password_hash=hash_password("PSecondAdmin7"),
                      role="admin", status="active", password_changed_at=datetime.now())
        db.add(second)
        db.commit()
        second_id = second.id

    response = env.client.delete(f"/api/v1/users/{second_id}", headers=headers(env.admin_token))
    assert response.status_code == 200
    with env.factory() as db:
        assert db.get(User, second_id) is None


def test_the_literal_admin_username_stays_undeletable_even_for_a_superuser(env):
    """Existing business rule, preserved verbatim.

    This round widened *who* the administrator rule recognises; it did not relax
    anything, so the name-based refusal still fires for a superuser too.
    """
    with env.factory() as db:
        builtin = User(username="admin", password_hash=hash_password("PAdminNameOnly1"),
                       role="user", status="active", password_changed_at=datetime.now())
        db.add(builtin)
        db.commit()
        builtin_id = builtin.id

    response = env.client.delete(f"/api/v1/users/{builtin_id}", headers=headers(env.admin_token))
    assert response.status_code == 400
    with env.factory() as db:
        assert db.get(User, builtin_id) is not None
