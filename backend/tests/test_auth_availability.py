"""P01 availability checks use isolated SQLite and in-process ASGI only."""

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app import main
from app.api.v1.auth import router as auth_router
from app.core import auth, database
from app.license import verify as license_verify
from app.models.access_control import AuthSession
from app.models.user import User
from app.services import access_control


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = []

    class OwnedSession(Session):
        def __init__(self, *args, **kwargs):
            self.owner = threading.get_ident()
            super().__init__(*args, **kwargs)
            self.record("create")

        def record(self, operation):
            assert threading.get_ident() == self.owner, "Session crossed threads"
            calls.append((id(self), self.owner, operation))

        def query(self, *args, **kwargs):
            self.record("query")
            return super().query(*args, **kwargs)

        def commit(self):
            self.record("commit")
            return super().commit()

        def rollback(self):
            self.record("rollback")
            return super().rollback()

        def close(self):
            self.record("close")
            return super().close()

    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}", poolclass=NullPool)
    User.__table__.create(engine)
    AuthSession.__table__.create(engine)
    factory = sessionmaker(bind=engine, class_=OwnedSession, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "signing.pem"))
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    with factory() as db:
        user = User(username="availability", role="user", status="active",
                    password_hash="not-a-login-credential", password_changed_at=datetime.now())
        db.add(user)
        db.commit()
        token = auth.issue_user_token(user, db=db)["token"]
    calls.clear()

    def allow(db, user_id, action):
        db.query(User).first()
        return True, True

    def resolve(db, user_id, **kwargs):
        db.query(User).first()
        return access_control.ScopeSet(
            restricted=True, native_instance_ids=frozenset({42}),
            normalized_instance_ids=frozenset(), host_ids=frozenset(), cluster_ids=frozenset(),
        )

    def licensed():
        with factory() as db:
            db.query(User).first()
        return {"valid": True, "features": []}

    monkeypatch.setattr(access_control, "is_action_allowed", allow)
    monkeypatch.setattr(access_control, "resolve_scope", resolve)
    monkeypatch.setattr(license_verify, "get_license_info", licensed)
    app = FastAPI()
    app.add_exception_handler(auth.AuthenticationUnavailable, main.authentication_unavailable_response)
    app.add_middleware(main.AuthMiddleware)
    app.add_middleware(main.LicenseMiddleware)
    executed = []

    @app.get("/health/live")
    async def live():
        return {"alive": True}

    @app.api_route("/api/v1/instances/{instance_id}", methods=["GET", "PUT"])
    async def endpoint(request: Request):
        executed.append(request.url.path)
        return {"body": (await request.body()).decode(), "auth": request.state.auth["role"]}

    app.include_router(auth_router, prefix="/api/v1")
    yield SimpleNamespace(app=app, factory=factory, token=token, calls=calls,
                          executed=executed, allow=allow, resolve=resolve, licensed=licensed)
    engine.dispose()


def request(env, path="/api/v1/instances/42", method="GET", token=None, **kwargs):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
            return await client.request(method, path, headers={
                "Authorization": f"Bearer {env.token if token is None else token}",
            }, **kwargs)
    return asyncio.run(send())


@pytest.mark.parametrize("stage", ["token", "rbac", "scope", "license"])
def test_blocked_checks_leave_event_loop_and_liveness_available(env, monkeypatch, stage):
    started = threading.Event()
    release = threading.Event()
    worker_ids = []
    target, name = {
        "token": (auth, "_persistent_session_info"),
        "rbac": (access_control, "is_action_allowed"),
        "scope": (access_control, "resolve_scope"),
        "license": (license_verify, "get_license_info"),
    }[stage]
    original = getattr(target, name)

    def blocked(*args, **kwargs):
        worker_ids.append(threading.get_ident())
        started.set()
        assert release.wait(3), "Blocking check prevented event-loop progress"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, blocked)

    async def check():
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
            task = asyncio.create_task(client.get("/api/v1/instances/42", headers={
                "Authorization": f"Bearer {env.token}",
            }))
            try:
                async def entered():
                    while not started.is_set():
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(entered(), 2)
                live = await asyncio.wait_for(client.get("/health/live"), 0.5)
                assert live.status_code == 200
                assert not task.done()
                assert env.executed == []
                assert worker_ids and all(tid != loop_thread for tid in worker_ids)
            finally:
                release.set()
                response = await asyncio.wait_for(task, 3)
            assert response.status_code == 200
        assert all(tid != loop_thread for _, tid, _ in env.calls)
        for session_id in {sid for sid, _, _ in env.calls}:
            operations = [op for sid, _, op in env.calls if sid == session_id]
            assert operations[0] == "create"
            assert operations[-1] == "close"

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["get_token_info", "verify_token"])
@pytest.mark.parametrize("failure", ["factory", "read", "heartbeat", "rollback", "close"])
def test_storage_failure_is_distinct_and_never_trusts_cache(env, monkeypatch, operation, failure):
    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("private connection details must not escape")

    if failure == "factory":
        monkeypatch.setattr(database, "SessionLocal", unavailable)
    else:
        original = env.factory

        def failing_session():
            db = original()
            if failure == "rollback":
                monkeypatch.setattr(db, "query", unavailable)
            if failure == "close":
                close = db.close

                def failed_close():
                    close()
                    unavailable()

                monkeypatch.setattr(db, "close", failed_close)
            else:
                monkeypatch.setattr(db, {"read": "query", "heartbeat": "commit"}.get(failure, failure), unavailable)
            return db

        monkeypatch.setattr(database, "SessionLocal", failing_session)
    with pytest.raises(auth.AuthenticationUnavailable):
        getattr(auth, operation)(env.token)
    assert env.token not in auth.AUTH_TOKENS
    response = request(env, path="/api/v1/auth/current-user")
    assert response.status_code == 503
    assert "private connection" not in response.text
    assert env.executed == []


@pytest.mark.parametrize("stage", ["factory", "close", "rbac", "scope", "license"])
def test_authorization_and_license_fail_closed_with_503(env, monkeypatch, stage):
    def unavailable(*args, **kwargs):
        raise SQLAlchemyError("private connection details")

    if stage in {"factory", "close"}:
        monkeypatch.setattr(main, "get_token_info", lambda token: {
            "user_id": 1, "source": "db", "role": "user",
        })
        monkeypatch.setattr(license_verify, "get_license_info", lambda: {"valid": True})
        def failed_close_session():
            db = env.factory()
            close = db.close

            def failed_close():
                close()
                unavailable()

            monkeypatch.setattr(db, "close", failed_close)
            return db

        monkeypatch.setattr(database, "SessionLocal", unavailable if stage == "factory" else failed_close_session)
    else:
        target, name = {"rbac": (access_control, "is_action_allowed"),
                        "scope": (access_control, "resolve_scope"),
                        "license": (license_verify, "get_license_info")}[stage]
        monkeypatch.setattr(target, name, unavailable)
    response = request(env)
    assert response.status_code == 503
    assert "private connection" not in response.text
    assert env.executed == []


@pytest.mark.parametrize("token", ["", "invalid.jwt"])
def test_invalid_or_missing_token_still_returns_401_without_storage(env, monkeypatch, token):
    def forbidden():
        pytest.fail("Invalid token must not open a Session")
    monkeypatch.setattr(database, "SessionLocal", forbidden)
    assert request(env, path="/api/v1/auth/current-user", token=token).status_code == 401


def test_current_user_second_validation_storage_failure_is_503(env, monkeypatch):
    assert main.app.exception_handlers[auth.AuthenticationUnavailable] is main.authentication_unavailable_response
    original = auth._persistent_session_info
    reads = []

    def fail_second(claims, db):
        reads.append(1)
        if len(reads) == 2:
            raise SQLAlchemyError("second validation unavailable")
        return original(claims, db)

    # The endpoint owns its existing Depends(get_db) session, not middleware's.
    def get_db():
        with env.factory() as db:
            yield db

    env.app.dependency_overrides[database.get_db] = get_db
    monkeypatch.setattr(auth, "_persistent_session_info", fail_second)
    assert request(env, path="/api/v1/auth/current-user").status_code == 503
    assert len(reads) == 2


@pytest.mark.parametrize("operation", ["get_token_info", "verify_token"])
def test_caller_owned_session_failure_propagates_without_closing_it(env, monkeypatch, operation):
    with env.factory() as db:
        def unavailable(*args, **kwargs):
            raise SQLAlchemyError("storage read unavailable")

        monkeypatch.setattr(db, "query", unavailable)
        env.calls.clear()
        with pytest.raises(auth.AuthenticationUnavailable):
            getattr(auth, operation)(env.token, db=db)
        assert [op for _, _, op in env.calls] == ["rollback"]
        assert env.token not in auth.AUTH_TOKENS


@pytest.mark.parametrize("mutation", ["revoked", "disabled", "password", "role"])
def test_revalidation_and_admin_demotion_remain_effective(env, mutation):
    with env.factory() as db:
        user = db.query(User).one()
        if mutation == "revoked":
            db.query(AuthSession).one().revoked_at = auth._utc_now_naive()
        elif mutation == "disabled":
            user.status = "disabled"
        elif mutation == "password":
            user.password_changed_at = datetime(2000, 1, 1)
        else:
            user.role = "admin"
            db.commit()
            env.token = auth.issue_user_token(user, db=db)["token"]
            user.role = "user"
        db.commit()
    assert request(env, path="/api/v1/instances/99").status_code == (403 if mutation == "role" else 401)
    assert env.executed == []


@pytest.mark.parametrize("body,status", [
    (b'{"remark":"unchanged"}', 200),
    (b'{"instance_id":99}', 403),
    (b'{"instance_id":42,"instance_id":99}', 403),
    (b'{"host":"foreign"}', 403),
    (b'invalid-json', 400),
])
def test_scoped_body_replay_and_retarget_denials(env, body, status):
    response = request(env, method="PUT", content=body)
    assert response.status_code == status
    if status == 200:
        assert response.json()["body"] == body.decode()
    else:
        assert env.executed == []


def test_credential_action_denial_survives_offload(env, monkeypatch):
    monkeypatch.setattr(access_control, "additional_actions_for_request", lambda *args: ["credential.manage"])
    monkeypatch.setattr(access_control, "is_action_allowed", lambda db, user, action: (
        action != "credential.manage", True,
    ))
    response = request(env, method="PUT", json={"remark": "change"})
    assert response.status_code == 403
    assert response.json()["required_action"] == "credential.manage"
    assert env.executed == []


@pytest.mark.parametrize("info", [{"valid": False}, {"valid": True, "features": []}])
def test_license_validity_and_feature_denials_remain(env, monkeypatch, info):
    monkeypatch.setattr(license_verify, "get_license_info", lambda: info)
    monkeypatch.setattr(main, "_required_license_features", lambda path: {"missing-feature"})
    assert request(env).status_code == 403
    assert env.executed == []
