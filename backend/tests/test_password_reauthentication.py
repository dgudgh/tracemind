from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.api.v1 import users
from app.api.v1 import auth as auth_api
from app.core import auth, database
from app.core.security import hash_password, verify_password
from app.main import AuthMiddleware
from app.models.access_control import AccessRole, AuthSession, RoleActionPermission, SecurityAuditEvent, UserAccessRole, UserMfaCredential
from app.models.user import User
from app.services import access_control


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'passwords.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "signing.pem"))
    with factory() as db:
        admin = User(username="admin.test", role="admin", status="active", password_hash=hash_password("AdminCurrent1"), password_changed_at=datetime.now())
        target = User(username="target.test", role="user", status="active", password_hash=hash_password("TargetCurrent2"), password_changed_at=datetime.now())
        db.add_all([admin, target])
        db.commit()
        admin_id, target_id = admin.id, target.id
        admin_token = auth.issue_user_token(admin, db=db, mfa_verified=True)["token"]
        target_token = auth.issue_user_token(target, db=db)["token"]
    app = FastAPI()
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(auth_api.router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)

    def get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_db
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, admin_id=admin_id, target_id=target_id, admin_token=admin_token, target_token=target_token)
    engine.dispose()


def reset(env, *, token=None, target=None, current="AdminCurrent1", new="NewSecure3", payload=None):
    return env.client.put(
        f"/api/v1/users/{target or env.target_id}/reset-password",
        headers={"Authorization": f"Bearer {token or env.admin_token}"},
        json=payload if payload is not None else {"current_password": current, "new_password": new},
    )


def assert_unchanged(env):
    with env.factory() as db:
        assert verify_password("AdminCurrent1", db.get(User, env.admin_id).password_hash)
        assert verify_password("TargetCurrent2", db.get(User, env.target_id).password_hash)


@pytest.mark.parametrize("payload", [
    {"new_password": "NewSecure3"},
    {"current_password": "", "new_password": "NewSecure3"},
    {"current_password": None, "new_password": "NewSecure3"},
    {"current_password": "x" * 1025, "new_password": "NewSecure3"},
])
def test_current_password_is_required_by_http_api(env, payload):
    assert reset(env, payload=payload).status_code == 422
    assert_unchanged(env)


def test_anonymous_cannot_reset_with_known_password(env):
    response = env.client.put(f"/api/v1/users/{env.admin_id}/reset-password", json={"current_password": "AdminCurrent1", "new_password": "NewSecure3"})
    assert response.status_code == 401
    assert_unchanged(env)


def test_target_password_is_not_administrator_reauthentication(env):
    response = reset(env, current="TargetCurrent2")
    assert response.status_code == 400
    assert "操作管理员" in response.json()["detail"]
    assert_unchanged(env)
    with env.factory() as db:
        assert db.get(User, env.admin_id).login_fail_count == 1
        assert db.get(User, env.target_id).login_fail_count == 0
        audit = db.query(SecurityAuditEvent).one()
        assert (audit.actor_user_id, audit.target_id, audit.outcome) == (env.admin_id, str(env.target_id), "failure")


def test_admin_reset_revokes_only_target_sessions_and_audits_without_secrets(env):
    response = reset(env)
    assert response.status_code == 200
    assert response.json()["reauthenticate"] is False
    with env.factory() as db:
        assert verify_password("NewSecure3", db.get(User, env.target_id).password_hash)
        assert auth.verify_token(env.admin_token, db=db)
        assert not auth.verify_token(env.target_token, db=db)
        event = db.query(SecurityAuditEvent).one()
        assert (event.actor_user_id, event.target_id, event.action) == (env.admin_id, str(env.target_id), "password_reset")
        assert event.details == {"reason": "current_credential_verified"}
        assert access_control.verify_audit_chain([event])["valid"]
        assert db.query(AuthSession).filter_by(user_id=env.target_id).one().revoke_reason == "password_changed"


def test_self_change_requires_own_password_and_signs_out_all_sessions(env):
    with env.factory() as db:
        second_token = auth.issue_user_token(db.get(User, env.admin_id), db=db)["token"]
    assert reset(env, target=env.admin_id, current="WrongCurrent0").status_code == 400
    assert_unchanged(env)
    response = reset(env, target=env.admin_id)
    assert response.status_code == 200
    assert response.json()["reauthenticate"] is True
    with env.factory() as db:
        assert not auth.verify_token(env.admin_token, db=db)
        assert not auth.verify_token(second_token, db=db)
        assert auth.verify_token(env.target_token, db=db)
        assert db.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id.desc()).first().action == "password_change"


def test_reauthentication_attempt_limit_locks_operator_not_target(env, monkeypatch):
    monkeypatch.setattr(users, "load_password_policy", lambda db: users.PasswordPolicyConfig(max_login_fail=2))
    assert reset(env, current="Wrong1").status_code == 400
    assert reset(env, current="Wrong2").status_code == 401
    assert reset(env).status_code == 401
    with env.factory() as db:
        admin = db.get(User, env.admin_id)
        assert admin.status == "locked"
        assert admin.login_fail_count == 2
        assert admin.locked_until > datetime.now()
        assert db.get(User, env.target_id).status == "active"
        assert db.query(AuthSession).filter_by(user_id=env.admin_id).one().revoked_at
    assert_unchanged(env)


def test_client_actor_fields_do_not_grant_authority(env):
    response = reset(env, token=env.target_token, target=env.admin_id, payload={
        "current_password": "TargetCurrent2", "new_password": "NewSecure3",
        "actor_id": env.admin_id, "role": "admin", "user_id": env.admin_id,
    })
    assert response.status_code == 403
    assert_unchanged(env)


def test_self_unlock_cannot_clear_reauthentication_failures(env):
    assert reset(env, current="Wrong1").status_code == 400
    response = env.client.post(f"/api/v1/users/{env.admin_id}/unlock", headers={"Authorization": f"Bearer {env.admin_token}"})
    assert response.status_code == 403
    with env.factory() as db:
        assert db.get(User, env.admin_id).login_fail_count == 1
    assert_unchanged(env)


def test_unlock_cannot_clear_active_account_failure_counter(env):
    with env.factory() as db:
        db.get(User, env.target_id).login_fail_count = 2
        db.commit()
    response = env.client.post(f"/api/v1/users/{env.target_id}/unlock", headers={"Authorization": f"Bearer {env.admin_token}"})
    assert response.status_code == 400
    with env.factory() as db:
        assert db.get(User, env.target_id).login_fail_count == 2


def test_explicit_security_role_can_reset_using_its_own_password(env):
    with env.factory() as db:
        role = AccessRole(role_key="credential_admin", name="Credential Admin", status="active")
        db.add(role)
        db.flush()
        db.add_all([UserAccessRole(user_id=env.target_id, role_id=role.id), RoleActionPermission(role_id=role.id, action_key="platform.security")])
        db.commit()
    response = reset(env, token=env.target_token, target=env.admin_id, current="TargetCurrent2")
    assert response.status_code == 200
    with env.factory() as db:
        assert not auth.verify_token(env.admin_token, db=db)
        assert auth.verify_token(env.target_token, db=db)


def test_password_reset_preserves_mfa_requirement(env):
    with env.factory() as db:
        db.add(UserMfaCredential(user_id=env.target_id, enabled=1, secret_cipher="test-unused-secret"))
        db.commit()
    assert reset(env).status_code == 200
    response = env.client.post("/api/v1/auth/login", json={"username": "target.test", "password": "NewSecure3"})
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert "token" not in response.json()
    auth_api.MFA_LOGIN_CHALLENGES.pop(response.json()["challenge_token"], None)


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_pre_reset_mfa_challenge_cannot_mint_post_reset_session(env, monkeypatch, same_timestamp):
    with env.factory() as db:
        db.add(UserMfaCredential(user_id=env.target_id, enabled=1, secret_cipher="test-unused-secret"))
        db.commit()
    old = env.client.post("/api/v1/auth/login", json={"username": "target.test", "password": "TargetCurrent2"})
    assert old.status_code == 200
    old_challenge = old.json()["challenge_token"]
    old_version = auth_api.MFA_LOGIN_CHALLENGES[old_challenge]["password_version"]
    checked = []

    def valid_code(*args, **kwargs):
        checked.append(True)
        assert kwargs["commit"] is False
        return True

    monkeypatch.setattr(auth_api, "_verify_mfa_code", valid_code)
    assert reset(env).status_code == 200
    if same_timestamp:
        # Model a second credential change in the same MySQL DATETIME second.
        with env.factory() as db:
            db.get(User, env.target_id).password_changed_at = datetime.fromisoformat(old_version)
            db.commit()
    rejected = env.client.post("/api/v1/auth/mfa/verify", json={"challenge_token": old_challenge, "code": "123456"})
    assert rejected.status_code == 401
    assert "token" not in rejected.json()
    assert checked == []
    fresh = env.client.post("/api/v1/auth/login", json={"username": "target.test", "password": "NewSecure3"})
    verified = env.client.post("/api/v1/auth/mfa/verify", json={"challenge_token": fresh.json()["challenge_token"], "code": "123456"})
    assert verified.status_code == 200
    assert checked == [True]
    with env.factory() as db:
        assert auth.verify_token(verified.json()["token"], db=db)


def test_disabled_user_cannot_start_new_password_login(env):
    with env.factory() as db:
        db.get(User, env.target_id).status = "disabled"
        db.commit()
    response = env.client.post("/api/v1/auth/login", json={"username": "target.test", "password": "TargetCurrent2"})
    assert response.status_code == 401


@pytest.mark.parametrize("state", ["disabled", "locked", "revoked", "demoted"])
def test_stale_admin_session_is_rejected(env, state):
    with env.factory() as db:
        admin = db.get(User, env.admin_id)
        if state == "revoked":
            auth.invalidate_user_tokens(admin.id, db=db)
        elif state == "demoted":
            admin.role = "user"
        else:
            admin.status = state
        db.commit()
    assert reset(env).status_code in {401, 403}
    assert_unchanged(env)


def test_external_identity_cannot_use_local_reset(env):
    with env.factory() as db:
        external_token = auth.issue_user_token(db.get(User, env.admin_id), source="identity", db=db)["token"]
    assert reset(env, token=external_token).status_code == 403
    assert_unchanged(env)


@pytest.mark.parametrize("state", ["disabled", "locked"])
def test_password_reset_does_not_reenable_disabled_target(env, state):
    with env.factory() as db:
        user = db.get(User, env.target_id)
        user.status = state
        user.locked_until = datetime.now() + timedelta(minutes=5)
        db.commit()
    assert reset(env).status_code == 200
    with env.factory() as db:
        user = db.get(User, env.target_id)
        assert user.status == ("active" if state == "locked" else "disabled")
        assert user.locked_until is None


@pytest.mark.parametrize("password", ["weak", "TargetCurrent2"])
def test_password_policy_and_no_reuse_are_enforced(env, password):
    assert reset(env, new=password).status_code == 400
    assert_unchanged(env)


def test_missing_audit_prevents_any_password_or_revocation_commit(env, monkeypatch):
    monkeypatch.setattr(access_control, "append_security_audit", lambda *args, **kwargs: None)
    assert reset(env).status_code == 503
    assert_unchanged(env)
    with env.factory() as db:
        assert auth.verify_token(env.target_token, db=db)
        assert db.query(SecurityAuditEvent).count() == 0


def test_failed_commit_rolls_back_password_audit_and_sessions(env, monkeypatch):
    with env.factory() as db:
        request = Request({"type": "http", "headers": []})
        request.state.auth = auth.get_token_info(env.admin_token, db=db)

        def unavailable():
            raise SQLAlchemyError("injected transaction failure")

        monkeypatch.setattr(db, "commit", unavailable)
        with pytest.raises(HTTPException) as error:
            users.reset_password(env.target_id, users.ChangePasswordRequest(current_password="AdminCurrent1", new_password="NewSecure3"), request, db)
        assert error.value.status_code == 503
    assert_unchanged(env)
    with env.factory() as db:
        assert db.query(SecurityAuditEvent).count() == 0
        assert auth.verify_token(env.target_token, db=db)


def test_credential_changed_after_authentication_is_rejected(env):
    with env.factory() as db:
        request = Request({"type": "http", "headers": []})
        request.state.auth = auth.get_token_info(env.admin_token, db=db)
        with env.factory() as other:
            actor = other.get(User, env.admin_id)
            actor.password_changed_at += timedelta(seconds=1)
            other.commit()
        with pytest.raises(HTTPException) as error:
            users.reset_password(env.target_id, users.ChangePasswordRequest(current_password="AdminCurrent1", new_password="NewSecure3"), request, db)
        assert error.value.status_code == 401
    assert_unchanged(env)
