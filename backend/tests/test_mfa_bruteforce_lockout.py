"""Second-factor brute force must consume the same lockout budget as a password.

``/login`` verifies the password and then hands out a fresh MFA challenge. Because
``/mfa/verify`` only wrote an audit row on failure, an attacker who already knew the
password could re-request a challenge and grind the 6-digit TOTP space without limit.

Every attempt here goes through the real ``/login`` -> ``/mfa/verify`` pair. Bypassing
``/login`` by injecting a challenge directly would hide the fact that a successful
password check used to clear ``login_fail_count`` on every round.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth as auth_api
from app.core import auth, database
from app.core.security import hash_password
from app.models.access_control import AuthSession, SecurityAuditEvent, UserMfaCredential
from app.models.system_setting import DBSystemSetting
from app.models.user import User
from app.services import system_settings

MAX_LOGIN_FAIL = 3
LOCK_MINUTES = 30
USERNAME = "mfa.user"
PASSWORD = "MfaPassword9"


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    for model in (User, UserMfaCredential, AuthSession, SecurityAuditEvent, DBSystemSetting):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth_api, "MFA_LOGIN_CHALLENGES", {})

    with factory() as db:
        system_settings.update_settings(db, {
            "max_login_fail": MAX_LOGIN_FAIL,
            "lock_duration_minutes": LOCK_MINUTES,
        })
        protected = User(username=USERNAME, password_hash=hash_password(PASSWORD),
                         role="user", status="active", password_changed_at=datetime.now())
        plain = User(username="plain.user", password_hash=hash_password(PASSWORD),
                     role="user", status="active", password_changed_at=datetime.now())
        db.add_all([protected, plain])
        db.flush()
        db.add(UserMfaCredential(user_id=protected.id, enabled=1, secret_cipher="cipher",
                                 recovery_code_hashes=[], last_totp_counter=0))
        db.commit()
        protected_id, plain_id = protected.id, plain.id

    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api/v1")

    def get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield type("Env", (), {"client": client, "factory": factory, "engine": engine,
                               "protected_id": protected_id, "plain_id": plain_id})()
    engine.dispose()


def _login(env, monkeypatch, *, code_ok=False, username=USERNAME, password=PASSWORD):
    """One real /login -> /mfa/verify round trip; returns (login_response, verify_response)."""
    monkeypatch.setattr(auth_api, "_verify_mfa_code", lambda *a, **k: code_ok)
    login = env.client.post("/api/v1/auth/login",
                            json={"username": username, "password": password})
    if login.status_code != 200 or not login.json().get("mfa_required"):
        return login, None
    verify = env.client.post("/api/v1/auth/mfa/verify",
                             json={"challenge_token": login.json()["challenge_token"],
                                   "code": "000000"})
    return login, verify


def _account(env, user_id=None):
    with env.factory() as db:
        user = db.get(User, user_id if user_id is not None else env.protected_id)
        return {"status": user.status, "login_fail_count": user.login_fail_count,
                "locked_until": user.locked_until}


def _mfa_audits(env):
    with env.factory() as db:
        rows = db.query(SecurityAuditEvent).filter(
            SecurityAuditEvent.action == "mfa_login").order_by(SecurityAuditEvent.id).all()
        return [dict(row.details or {}) for row in rows]


def test_wrong_codes_through_the_real_login_flow_exhaust_the_budget(env, monkeypatch):
    for expected_count in range(1, MAX_LOGIN_FAIL + 1):
        login, verify = _login(env, monkeypatch)
        assert login.status_code == 200
        assert login.json()["mfa_required"] is True
        assert verify.status_code == 401
        assert _account(env)["login_fail_count"] == expected_count

    account = _account(env)
    assert account["status"] == "locked"
    assert account["locked_until"] > datetime.now() + timedelta(minutes=LOCK_MINUTES - 1)


def test_re_requesting_a_challenge_does_not_refill_the_budget(env, monkeypatch):
    """The regression that made the first version of this fix a no-op."""
    for _ in range(MAX_LOGIN_FAIL - 1):
        _login(env, monkeypatch)
    assert _account(env)["login_fail_count"] == MAX_LOGIN_FAIL - 1

    _login(env, monkeypatch)
    assert _account(env)["status"] == "locked"


def test_a_locked_account_can_no_longer_obtain_a_challenge(env, monkeypatch):
    for _ in range(MAX_LOGIN_FAIL):
        _login(env, monkeypatch)
    assert _account(env)["status"] == "locked"

    login, verify = _login(env, monkeypatch)
    assert login.status_code == 401
    assert verify is None


def test_lockout_is_recorded_in_the_audit_trail(env, monkeypatch):
    for _ in range(MAX_LOGIN_FAIL):
        _login(env, monkeypatch)

    audits = _mfa_audits(env)
    assert len(audits) == MAX_LOGIN_FAIL
    assert all(item["reason"] == "invalid_mfa_code" for item in audits)
    assert [item["login_fail_count"] for item in audits] == [1, 2, 3]
    assert [item["account_locked"] for item in audits] == [False, False, True]


def test_a_correct_second_factor_still_works_and_clears_the_budget(env, monkeypatch):
    for _ in range(MAX_LOGIN_FAIL - 1):
        _login(env, monkeypatch)
    assert _account(env)["login_fail_count"] == MAX_LOGIN_FAIL - 1

    login, verify = _login(env, monkeypatch, code_ok=True)
    assert verify.status_code == 200
    assert verify.json()["token"]
    account = _account(env)
    assert account["status"] == "active"
    assert account["login_fail_count"] == 0


def test_password_only_login_still_clears_the_counter(env, monkeypatch):
    """``reset_fail_count`` defaults to True, so ordinary logins are unchanged."""
    with env.factory() as db:
        user = db.get(User, env.plain_id)
        user.login_fail_count = 2
        db.commit()

    response = env.client.post("/api/v1/auth/login",
                               json={"username": "plain.user", "password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["token"]
    assert _account(env, env.plain_id)["login_fail_count"] == 0


def test_wrong_password_still_locks_and_records_the_count(env, monkeypatch):
    for _ in range(MAX_LOGIN_FAIL):
        response = env.client.post("/api/v1/auth/login",
                                   json={"username": "plain.user", "password": "WrongPass1"})
        assert response.status_code == 401
    assert _account(env, env.plain_id)["status"] == "locked"
