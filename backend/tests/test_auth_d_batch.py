"""D-batch regressions: private SQLite, real auth middleware, stub identity providers."""

import time
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.v1 import auth as auth_api, users
from app.core import auth, database
from app.core.mfa import recovery_code_hash, totp_code
from app.core.secrets import store_secret
from app.core.security import hash_password
from app.identity import service as identity_service
from app.identity.contracts import AuthenticatedIdentity, ProviderAuthenticationError
from app.identity.models import ExternalIdentity, GroupMapping, IdentityProvider, LoginAudit
from app.main import AuthMiddleware, authentication_unavailable_response
from app.models.access_control import AuthSession, SecurityAuditEvent, UserMfaCredential
from app.models.system_setting import DBSystemSetting
from app.models.user import User, UserModulePermission


@pytest.fixture
def env(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("D-batch tests must not use external services")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    engine = create_engine(f"sqlite:///{tmp_path / 'auth-d.sqlite'}")
    for model in (User, UserModulePermission, AuthSession, UserMfaCredential,
                  SecurityAuditEvent, DBSystemSetting, IdentityProvider,
                  ExternalIdentity, GroupMapping, LoginAudit):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "jwt.pem"))
    monkeypatch.setattr(auth_api, "MFA_LOGIN_CHALLENGES", {})
    monkeypatch.setattr(auth_api, "OIDC_PENDING_STATES", {})
    monkeypatch.setattr(auth_api, "OIDC_LOGIN_CODES", {})
    with factory() as db:
        admin = User(username="d.admin", password_hash=hash_password("DAdminOnly7"),
                     role="admin", status="active", password_changed_at=datetime.now())
        target = User(username="d.target", display_name="Preserved Name",
                      password_hash=hash_password("DTargetOnly8"), role="user", status="active",
                      password_changed_at=datetime.now(), login_fail_count=2)
        provider = IdentityProvider(name="d.provider", provider_type="ldap", enabled=1,
                                    configuration={"frontend_redirect_uri": "http://test/complete"})
        db.add_all([admin, target, provider])
        db.commit()
        admin_id, target_id, provider_id = admin.id, target.id, provider.id
        admin_token = auth.issue_user_token(admin, db=db)["token"]
        target_token = auth.issue_user_token(target, db=db)["token"]
        db.add(ExternalIdentity(provider_id=provider_id, subject="d-subject", user_id=target_id,
                                username=target.username, display_name=target.display_name))
        db.add(UserModulePermission(user_id=target_id, module_key="preserved.module"))
        db.commit()
    identity = AuthenticatedIdentity(provider_id=provider_id, subject="d-subject",
                                     username="d.target", display_name="Directory Name")

    class Provider:
        def authenticate(self, provider, credentials):
            return identity

    monkeypatch.setattr(identity_service, "_default_provider_clients", lambda: {"ldap": Provider()})
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.add_exception_handler(auth.AuthenticationUnavailable, authentication_unavailable_response)
    app.include_router(auth_api.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    endpoint_sessions = []

    def get_db():
        with factory() as db:
            endpoint_sessions.append(db)
            yield db

    app.dependency_overrides[database.get_db] = get_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, factory=factory, engine=engine, identity=identity,
                              target_id=target_id, admin_id=admin_id, provider_id=provider_id,
                              target_token=target_token, admin_token=admin_token,
                              endpoint_sessions=endpoint_sessions)
    assert engine.pool.checkedout() == 0
    engine.dispose()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def external_request(env, monkeypatch, flow):
    if flow == "ldap":
        return env.client.post("/api/v1/auth/external-login", json={
            "username": "d.target", "password": "private-test-credential",
        })
    with env.factory() as db:
        db.get(IdentityProvider, env.provider_id).provider_type = "oidc"
        db.commit()
    auth_api.OIDC_PENDING_STATES["d-state"] = {"provider_id": env.provider_id, "created_at": time.time()}

    def complete(self, provider, *, state, code, pending_states):
        pending_states.pop(state)
        return env.identity

    monkeypatch.setattr(auth_api.OIDCIdentityProvider, "complete_authorization", complete)
    return env.client.get("/api/v1/auth/oidc/callback", params={"state": "d-state", "code": "stub"},
                          follow_redirects=False)


def snapshot(env):
    with env.factory() as db:
        user = db.get(User, env.target_id)
        fields = {key: getattr(user, key) for key in (
            "username", "display_name", "role", "status", "locked_until", "login_fail_count",
            "password_hash", "password_changed_at", "last_login_at",
        )}
        fields["modules"] = [row.module_key for row in db.query(UserModulePermission).filter_by(user_id=user.id)]
        fields["sessions"] = [(row.id, row.revoked_at, row.revoke_reason) for row in
                              db.query(AuthSession).filter_by(user_id=user.id).order_by(AuthSession.id)]
        return fields


@pytest.mark.parametrize("status", ["disabled", "locked"])
def test_map_preserves_existing_account_status(env, status):
    with env.factory() as db:
        db.get(User, env.target_id).status = status
        db.commit()
        with pytest.raises(ProviderAuthenticationError, match="local_account_unavailable"):
            identity_service.map_identity(env.identity, db=db)
        db.rollback()
        assert db.get(User, env.target_id).status == status


@pytest.mark.parametrize("flow", ["ldap", "oidc"])
@pytest.mark.parametrize("state", ["disabled", "locked", "locked_expired", "future_lock"])
def test_external_login_rejects_inactive_user_without_mutation_or_session(env, monkeypatch, flow, state):
    with env.factory() as db:
        user = db.get(User, env.target_id)
        user.status = "active" if state == "future_lock" else "locked" if state == "locked_expired" else state
        user.locked_until = (datetime.now() - timedelta(minutes=1) if state == "locked_expired" else
                             datetime.now() + timedelta(minutes=10) if state == "future_lock" else None)
        db.commit()
    before = snapshot(env)
    response = external_request(env, monkeypatch, flow)
    assert response.status_code == 401
    assert snapshot(env) == before
    assert auth_api.OIDC_LOGIN_CODES == {}
    assert "private-test-credential" not in response.text
    assert "password_hash" not in response.text


@pytest.mark.parametrize("flow", ["ldap", "oidc"])
def test_active_external_login_keeps_contract(env, monkeypatch, flow):
    response = external_request(env, monkeypatch, flow)
    if flow == "oidc":
        assert response.status_code == 302
        from urllib.parse import parse_qs, urlsplit

        code = parse_qs(urlsplit(response.headers["location"]).query)["external_login_code"][0]
        response = env.client.post("/api/v1/auth/oidc/exchange", json={"code": code})
    assert response.status_code == 200
    assert set(response.json()) == {"token", "username", "expires_in"}
    with env.factory() as db:
        info = auth.get_token_info(response.json()["token"], db=db)
        assert info["user_id"] == env.target_id and info["source"] == "identity"


def mfa_snapshot(env):
    with env.factory() as db:
        row = db.query(UserMfaCredential).filter_by(user_id=env.target_id).one()
        return tuple(getattr(row, key) for key in (
            "secret_cipher", "recovery_code_hashes", "enabled", "last_totp_counter", "verified_at",
        ))


def enabled_mfa(env):
    with env.factory() as db:
        db.add(UserMfaCredential(user_id=env.target_id, enabled=1,
                                secret_cipher=store_secret(plaintext="JBSWY3DPEHPK3PXP"),
                                recovery_code_hashes=[recovery_code_hash("d-recovery-code")],
                                last_totp_counter=123, verified_at=datetime.now()))
        db.commit()


def test_enabled_mfa_enroll_returns_409_and_preserves_all_security_state(env, monkeypatch):
    enabled_mfa(env)
    before, account = mfa_snapshot(env), snapshot(env)
    generated = []
    original = auth_api.generate_secret

    def generate():
        generated.append(True)
        return original()

    monkeypatch.setattr(auth_api, "generate_secret", generate)
    response = env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token))
    assert response.status_code == 409
    assert mfa_snapshot(env) == before
    assert snapshot(env) == account
    assert not generated
    assert "secret" not in response.json() and "recovery_codes" not in response.json()
    result = env.client.post("/api/v1/auth/login", json={"username": "d.target", "password": "DTargetOnly8"})
    assert result.json()["mfa_required"] is True


def test_verified_disable_is_required_before_reenroll(env):
    enabled_mfa(env)
    before = mfa_snapshot(env)
    response = env.client.post("/api/v1/auth/mfa/disable", headers=headers(env.target_token),
                               json={"code": "invalid-code"})
    assert response.status_code == 422
    assert mfa_snapshot(env) == before
    assert env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token)).status_code == 409
    response = env.client.post("/api/v1/auth/mfa/disable", headers=headers(env.target_token),
                               json={"code": "d-recovery-code"})
    assert response.status_code == 200 and response.json() == {"enabled": False}
    response = env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token))
    assert response.status_code == 200
    assert {"secret", "provisioning_uri", "recovery_codes"} <= set(response.json())
    assert mfa_snapshot(env)[2] == 0


def test_first_and_pending_mfa_enroll_remain_supported(env):
    for _ in range(2):
        response = env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token))
        assert response.status_code == 200
        assert mfa_snapshot(env)[2] == 0


@pytest.mark.parametrize("failure", ["revoke_update", "commit"])
def test_user_update_failure_is_503_and_rolls_back_user_and_sessions(env, monkeypatch, failure):
    enabled_mfa(env)
    before, mfa_before = snapshot(env), mfa_snapshot(env)
    triggered = []

    def inject(execution):
        if execution.is_update and execution.statement.table.name == AuthSession.__tablename__:
            triggered.append(True)
            if failure == "revoke_update":
                raise SQLAlchemyError("private-storage-detail")
            def fail_commit():
                raise SQLAlchemyError("private-storage-detail")
            monkeypatch.setattr(execution.session, "commit", fail_commit)

    from sqlalchemy.orm import Session

    event.listen(Session, "do_orm_execute", inject)
    try:
        response = env.client.put(f"/api/v1/users/{env.target_id}", headers=headers(env.admin_token),
                                   json={"role": "admin", "display_name": "Changed", "status": "disabled"})
    finally:
        event.remove(Session, "do_orm_execute", inject)
    assert triggered
    assert response.status_code == 503
    assert "private-storage-detail" not in response.text
    assert snapshot(env) == before
    assert mfa_snapshot(env) == mfa_before
    with env.factory() as db:
        info = auth.get_token_info(env.target_token, db=db)
        assert info and info["role"] == "user"


def test_user_update_success_revokes_only_target_and_preserves_other_fields(env):
    enabled_mfa(env)
    before, mfa_before = snapshot(env), mfa_snapshot(env)
    response = env.client.put(f"/api/v1/users/{env.target_id}", headers=headers(env.admin_token),
                               json={"display_name": "Changed", "role": "admin"})
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    after = snapshot(env)
    for key in before.keys() - {"display_name", "role", "sessions"}:
        assert after[key] == before[key]
    assert mfa_snapshot(env) == mfa_before
    with env.factory() as db:
        assert auth.get_token_info(env.target_token, db=db) is None
        assert auth.get_token_info(env.admin_token, db=db) is not None


def test_user_update_missing_target_remains_404(env):
    response = env.client.put("/api/v1/users/999999", headers=headers(env.admin_token), json={"role": "user"})
    assert response.status_code == 404


def test_external_mapping_rechecks_stale_user_and_does_not_recreate_deleted_user(env):
    with env.factory() as db:
        stale = db.get(User, env.target_id)
        with env.factory() as other:
            other.get(User, env.target_id).status = "disabled"
            other.commit()
        assert stale.status == "active"
        with pytest.raises(ProviderAuthenticationError):
            identity_service.map_identity(env.identity, db=db)
        db.rollback()
        assert db.get(User, env.target_id).status == "disabled"
        db.delete(db.get(User, env.target_id))
        db.commit()
        with pytest.raises(ProviderAuthenticationError):
            identity_service.map_identity(env.identity, db=db)
        db.rollback()
        assert db.get(User, env.target_id) is None
        assert db.query(User).filter_by(username="d.target").count() == 0


def test_pending_mfa_confirmation_still_requires_factor_and_blocks_reenroll(env):
    enrolled = env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token)).json()
    code = totp_code(enrolled["secret"], int(time.time() // 30))
    response = env.client.post("/api/v1/auth/mfa/confirm", headers=headers(env.target_token), json={"code": code})
    assert response.status_code == 200 and response.json() == {"enabled": True}
    before = mfa_snapshot(env)
    assert env.client.post("/api/v1/auth/mfa/enroll", headers=headers(env.target_token)).status_code == 409
    assert mfa_snapshot(env) == before


@pytest.mark.parametrize("operation", ["confirm", "disable"])
def test_mfa_write_failure_does_not_consume_recovery_code_separately(env, monkeypatch, operation):
    enabled_mfa(env)
    if operation == "confirm":
        with env.factory() as db:
            db.query(UserMfaCredential).one().enabled = 0
            db.commit()
    before = mfa_snapshot(env)
    from starlette.requests import Request

    request = Request({"type": "http", "state": {"auth": {"user_id": env.target_id, "username": "d.target"}}})
    with env.factory() as db:
        def fail_commit():
            raise SQLAlchemyError("injected MFA commit failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(SQLAlchemyError):
            getattr(auth_api, f"{operation}_mfa")(auth_api.MfaCodeRequest(code="d-recovery-code"), request, db=db)
        db.rollback()
    assert mfa_snapshot(env) == before


def test_invalid_lock_time_does_not_partially_update_user(env):
    before = snapshot(env)
    response = env.client.put(f"/api/v1/users/{env.target_id}", headers=headers(env.admin_token),
                              json={"role": "admin", "locked_until": "invalid-date"})
    assert response.status_code == 400
    assert snapshot(env) == before


def test_concurrent_pending_enroll_and_confirm_cannot_enable_unconfirmed_secret(env, monkeypatch):
    enabled_mfa(env)
    with env.factory() as db:
        db.query(UserMfaCredential).one().enabled = 0
        db.commit()
    entered, release, confirming = Event(), Event(), Event()
    original = auth_api.generate_secret

    def paused_secret():
        entered.set()
        assert release.wait(5), "enrollment gate timed out"
        return original()

    def confirm():
        confirming.set()
        return env.client.post("/api/v1/auth/mfa/confirm", headers=headers(env.target_token),
                               json={"code": "d-recovery-code"})

    monkeypatch.setattr(auth_api, "generate_secret", paused_secret)
    with ThreadPoolExecutor(max_workers=2) as pool:
        enrollment = pool.submit(env.client.post, "/api/v1/auth/mfa/enroll", headers=headers(env.target_token))
        assert entered.wait(3)
        confirmation = pool.submit(confirm)
        assert confirming.wait(3)
        try:
            # Let a competing real HTTP request reach the database while A is paused.
            time.sleep(0.3)
        finally:
            release.set()
        enrolled, confirmed = enrollment.result(timeout=8), confirmation.result(timeout=8)
    assert enrolled.status_code == 200
    assert confirmed.status_code == 422
    with env.factory() as db:
        assert db.query(UserMfaCredential).one().enabled == 0
