from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core import auth, database
from app.models.access_control import AuthSession
from app.models.user import User


@pytest.fixture(scope="module")
def signing_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("auth-signing") / "test-key.pem"


@pytest.fixture
def auth_db(tmp_path, monkeypatch, signing_key_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    User.__table__.create(engine)
    AuthSession.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(signing_key_path))
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as db:
        user = User(
            username="session.operator",
            password_hash="unused-by-token-verification",
            role="admin",
            status="active",
            password_changed_at=datetime.now(),
        )
        db.add(user)
        db.commit()
        yield SimpleNamespace(db=db, factory=factory, user=user, engine=engine)
    engine.dispose()


@pytest.fixture(params=["get_token_info", "verify_token"])
def validate(request):
    return getattr(auth, request.param)


@pytest.fixture(params=["db", "identity"])
def token(auth_db, request):
    return auth.issue_user_token(auth_db.user, source=request.param, db=auth_db.db)["token"]


def test_cached_logout_from_another_worker(auth_db, token, validate, monkeypatch):
    worker_cache = auth.AUTH_TOKENS
    assert validate(token, db=auth_db.db)
    with monkeypatch.context() as worker, auth_db.factory() as other_db:
        worker.setattr(auth, "AUTH_TOKENS", {})
        auth.logout(token, db=other_db)
    assert token in worker_cache
    assert not validate(token, db=auth_db.db)
    assert token not in worker_cache
    with auth_db.factory() as db:
        row = db.query(AuthSession).one()
        assert row.revoked_at is not None
        assert row.revoke_reason == "logout"


@pytest.mark.parametrize("mutation", ["revoked", "expired", "idle", "deleted"])
def test_cached_session_rechecks_persistent_state(auth_db, token, validate, mutation):
    # Keep a stale ORM instance alive as well as the cached token metadata.
    stale = auth_db.db.query(AuthSession).one()
    with auth_db.factory() as other_db:
        row = other_db.query(AuthSession).one()
        if mutation == "revoked":
            row.revoked_at = auth._utc_now_naive()
        elif mutation == "expired":
            row.expires_at = auth._utc_now_naive() - timedelta(seconds=1)
        elif mutation == "idle":
            row.last_seen_at = auth._utc_now_naive() - timedelta(seconds=auth.settings.AUTH_SESSION_IDLE_SECONDS + 1)
        else:
            other_db.delete(row)
        other_db.commit()
    assert stale.revoked_at is None
    assert token in auth.AUTH_TOKENS
    assert not validate(token, db=auth_db.db)
    assert token not in auth.AUTH_TOKENS
    if mutation == "idle":
        with auth_db.factory() as db:
            assert db.query(AuthSession).one().revoke_reason == "idle_timeout"


@pytest.mark.parametrize("mutation", ["disabled", "locked", "future_lock", "password", "deleted"])
def test_cached_session_rechecks_current_user(auth_db, token, validate, mutation):
    with auth_db.factory() as other_db:
        user = other_db.get(User, auth_db.user.id)
        if mutation in {"disabled", "locked"}:
            user.status = mutation
        elif mutation == "future_lock":
            user.locked_until = datetime.now() + timedelta(minutes=10)
        elif mutation == "password":
            user.password_changed_at += timedelta(seconds=1)
        else:
            other_db.delete(user)
        other_db.commit()
    assert auth_db.user.status == "active"
    assert not validate(token, db=auth_db.db)
    assert token not in auth.AUTH_TOKENS


def test_cached_role_and_username_are_refreshed(auth_db, token, validate):
    assert auth.AUTH_TOKENS[token]["role"] == "admin"
    with auth_db.factory() as other_db:
        user = other_db.get(User, auth_db.user.id)
        user.role = "user"
        user.username = "renamed.operator"
        other_db.commit()
    assert validate(token, db=auth_db.db)
    assert auth.AUTH_TOKENS[token]["role"] == "user"
    assert auth.AUTH_TOKENS[token]["username"] == "renamed.operator"


def test_cached_validation_without_db_uses_persistence(auth_db, token, validate):
    assert validate(token)
    with auth_db.factory() as other_db:
        other_db.query(AuthSession).update({"revoked_at": auth._utc_now_naive()})
        other_db.commit()
    assert not validate(token)
    assert token not in auth.AUTH_TOKENS


def test_active_cached_session_updates_heartbeat(auth_db, token, validate):
    old_seen = auth._utc_now_naive() - timedelta(seconds=30)
    with auth_db.factory() as other_db:
        other_db.query(AuthSession).update({"last_seen_at": old_seen})
        other_db.commit()
    before = auth._utc_now_naive()
    assert validate(token, db=auth_db.db)
    with auth_db.factory() as db:
        assert db.query(AuthSession).one().last_seen_at >= before


def test_persistent_session_survives_cache_loss(auth_db, token, validate):
    auth.AUTH_TOKENS.clear()
    assert validate(token)
    assert auth.AUTH_TOKENS[token]["user_id"] == auth_db.user.id
    with auth_db.factory() as db:
        expected_expiry = db.query(AuthSession).one().expires_at
    assert auth.AUTH_TOKENS[token]["expires_at"] == expected_expiry.replace(tzinfo=auth.timezone.utc).timestamp()


@pytest.mark.parametrize("failure", ["session_read", "user_read", "heartbeat"])
def test_persistence_failure_never_falls_back_to_cache(auth_db, token, validate, monkeypatch, failure):
    if failure == "heartbeat":
        def fail_commit():
            raise SQLAlchemyError("injected heartbeat failure")

        monkeypatch.setattr(auth_db.db, "commit", fail_commit)
    else:
        query = auth_db.db.query

        def fail_read(model, *args, **kwargs):
            if model is (AuthSession if failure == "session_read" else User):
                raise SQLAlchemyError("injected persistence read failure")
            return query(model, *args, **kwargs)

        monkeypatch.setattr(auth_db.db, "query", fail_read)
    with pytest.raises(auth.AuthenticationUnavailable):
        validate(token, db=auth_db.db)
    assert token not in auth.AUTH_TOKENS


def test_session_factory_failure_evicts_cached_token(auth_db, token, validate, monkeypatch):
    def unavailable():
        raise SQLAlchemyError("injected session factory failure")

    monkeypatch.setattr(database, "SessionLocal", unavailable)
    with pytest.raises(auth.AuthenticationUnavailable):
        validate(token)
    assert token not in auth.AUTH_TOKENS


@pytest.mark.parametrize("source", ["db", "identity"])
def test_local_identity_issuance_requires_persistence(auth_db, source):
    with pytest.raises(ValueError, match="database session"):
        auth.issue_user_token(auth_db.user, source=source)
    assert auth.AUTH_TOKENS == {}


@pytest.mark.parametrize("source", ["db", "identity"])
@pytest.mark.parametrize("failure", ["missing_table", "commit"])
def test_failed_issuance_does_not_cache_or_return_token(auth_db, source, failure, monkeypatch):
    if failure == "missing_table":
        AuthSession.__table__.drop(auth_db.engine)
    else:
        def fail_commit():
            raise SQLAlchemyError("injected issuance failure")

        monkeypatch.setattr(auth_db.db, "commit", fail_commit)
    with pytest.raises(SQLAlchemyError):
        auth.issue_user_token(auth_db.user, source=source, db=auth_db.db)
    assert auth.AUTH_TOKENS == {}


@pytest.mark.parametrize("operation", ["logout", "invalidate"])
def test_revocation_without_db_is_persisted(auth_db, token, operation, monkeypatch):
    with monkeypatch.context() as other_worker:
        other_worker.setattr(auth, "AUTH_TOKENS", {})
        if operation == "logout":
            auth.logout(token)
        else:
            auth.invalidate_user_tokens(auth_db.user.id)
    assert token in auth.AUTH_TOKENS
    assert not auth.verify_token(token, db=auth_db.db)


@pytest.mark.parametrize("operation", ["logout", "invalidate"])
def test_revocation_failure_is_not_silently_successful(auth_db, token, monkeypatch, operation):
    def fail_commit():
        raise SQLAlchemyError("injected revocation failure")

    monkeypatch.setattr(auth_db.db, "commit", fail_commit)
    with pytest.raises(SQLAlchemyError):
        if operation == "logout":
            auth.logout(token, db=auth_db.db)
        else:
            auth.invalidate_user_tokens(auth_db.user.id, db=auth_db.db)
    assert token not in auth.AUTH_TOKENS


def test_mes_compatibility_is_explicitly_ephemeral(auth_db, monkeypatch):
    monkeypatch.setenv("MES_AUTH_TOKEN", "configured-mes-test-secret")
    monkeypatch.setenv("MES_AUTH_USERNAME", "mes.integration")

    def no_database():
        pytest.fail("Ephemeral MES login must not require database persistence")

    monkeypatch.setattr(database, "SessionLocal", no_database)
    result = auth.authenticate_by_mes("configured-mes-test-secret")
    assert set(result) == {"token", "username", "expires_in"}
    token = result["token"]
    info = auth.get_token_info(token)
    assert (info["source"], info["user_id"], info["role"]) == ("mes", 0, "user")
    assert info["ephemeral"] is True
    assert auth.verify_token(token)
    auth.logout(token)
    assert not auth.verify_token(token)


def test_mes_cache_loss_does_not_reconstitute_session(auth_db, monkeypatch):
    monkeypatch.setenv("MES_AUTH_TOKEN", "configured-mes-test-secret")
    token = auth.authenticate_by_mes("configured-mes-test-secret")["token"]
    auth.AUTH_TOKENS.clear()
    assert not auth.verify_token(token)


def test_db_token_cannot_use_mes_cache_exception(auth_db, token):
    auth.AUTH_TOKENS[token].update(source="mes", user_id=0, ephemeral=True)
    with auth_db.factory() as other_db:
        other_db.query(AuthSession).delete()
        other_db.commit()
    assert not auth.verify_token(token, db=auth_db.db)


def test_persistent_session_must_match_signed_identity(auth_db, token):
    with auth_db.factory() as other_db:
        other_db.query(AuthSession).update({"user_id": auth_db.user.id + 1})
        other_db.commit()
    assert not auth.verify_token(token, db=auth_db.db)


def test_validation_locks_user_before_session_like_credential_changes(auth_db, token):
    from sqlalchemy import event

    locked_tables = []

    def record_lock_order(execution):
        statement = execution.statement
        if execution.is_select and getattr(statement, "_for_update_arg", None) is not None:
            locked_tables.extend(table.name for table in statement.get_final_froms())

    event.listen(auth_db.db, "do_orm_execute", record_lock_order)
    try:
        assert auth.verify_token(token, db=auth_db.db)
    finally:
        event.remove(auth_db.db, "do_orm_execute", record_lock_order)
    assert locked_tables[:2] == [User.__tablename__, AuthSession.__tablename__]
