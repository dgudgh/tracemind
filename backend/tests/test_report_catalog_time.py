"""Catalog clock regression using real SQLite writes with a deliberately wrong clock."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.api.v1 import report_center as api
from app.core.database import Base, get_db
from app.models.report_center import ReportArtifact
from app.services import report_center as service


APP_UTC = datetime(2026, 9, 10, 2, 34, 17)
SERVER_WALL = datetime(2026, 9, 10, 18, 34, 17)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def db(tmp_path, monkeypatch, signing_key):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def wrong_clock(connection, _record):
        connection.create_function("current_timestamp", 0, lambda: str(SERVER_WALL))

    Base.metadata.create_all(engine)
    monkeypatch.setattr(service, "utcnow", lambda: APP_UTC)
    monkeypatch.setattr(service, "signing_key", lambda: signing_key)
    monkeypatch.setattr(service, "key_id", lambda: "catalog-clock-test")
    with sessionmaker(bind=engine)() as session:
        assert session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar_one() == str(SERVER_WALL)
        yield session
    engine.dispose()


def source():
    return {"id": "slow_sql:clock", "source_id": "clock", "report_type": "slow_sql",
            "title": "clock fixture", "create_time": str(SERVER_WALL),
            "slow_sql_detail": {"capture_time": "2026-09-10 09:30:00", "create_time": str(SERVER_WALL)}}


def create(db, kind):
    if kind == "source":
        return service.sync_source_artifacts(db, [source()])[0]
    return service.create_snapshot_artifact(db, [source()], source_key="manual:clock", title="clock fixture")


def legacy(db):
    row = ReportArtifact(artifact_uid="legacy-clock", source_key="source:slow_sql:clock",
                         report_type="slow_sql", source_type="slow_sql", source_id="clock", title="legacy",
                         extra_json={"legacy_note": "must remain unchanged"})
    db.add(row)
    db.commit()
    db.refresh(row)
    assert row.create_time == SERVER_WALL
    return row


@pytest.mark.parametrize("kind", ["source", "snapshot"])
def test_new_artifact_bypasses_wrong_database_default_and_preserves_source_time(db, kind):
    row = create(db, kind)
    db.expire_all()
    stored = db.get(ReportArtifact, row.id)
    assert stored.create_time == APP_UTC
    assert stored.update_time == APP_UTC
    assert stored.signed_at == APP_UTC
    result = service.serialize_artifact(stored, include_content=True)
    assert result["create_time"] == "2026-09-10 10:34:17+08:00"
    assert result["update_time"] == result["signed_at"] == result["create_time"]
    assert datetime.fromisoformat(result["create_time"]).astimezone(timezone.utc).replace(tzinfo=None) == APP_UTC
    assert service.verify_artifact(stored)["valid"]
    payload = result["content_json"] if kind == "source" else result["content_json"]["items"][0]
    assert payload == source()
    if kind == "snapshot":
        assert result["content_json"]["generated_at"] == "2026-09-10 02:34:17+00:00"
        assert "2026-09-10 02:34:17 UTC" in result["content_markdown"]


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    @app.middleware("http")
    async def test_identity(request, call_next):
        request.state.auth = {"user_id": 1, "username": "isolated-admin", "role": "admin"}
        return await call_next(request)

    def isolated_db():
        with sessionmaker(bind=db.get_bind())() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app) as client:
        yield client


def test_catalog_api_new_time_and_unmodified_legacy_time(db, client):
    old = legacy(db)
    old_payload = service.serialize_artifact(old, include_content=True)
    new = create(db, "snapshot")
    response = client.get("/api/v1/reports/catalog", params={"refresh_sources": "false"})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [new.id, old.id]
    rows = {row["id"]: row for row in response.json()["items"]}
    # Matches the current UI's verbatim first-19-character date rendering.
    assert rows[new.id]["create_time"].replace("T", " ")[:19] == "2026-09-10 10:34:17"
    assert rows[old.id]["create_time"] == "2026-09-10 18:34:17"
    detail = client.get(f"/api/v1/reports/catalog/{new.id}").json()
    assert detail["verification"]["valid"]
    assert detail["verification"]["signed_at"] == rows[new.id]["signed_at"]
    db.expire_all()
    assert service.serialize_artifact(old, include_content=True) == old_payload


def test_catalog_get_is_read_only_by_default(db, client, monkeypatch):
    calls = []

    def unexpected_sync(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("plain catalog GET must not synchronize source artifacts")

    monkeypatch.setattr(api, "_collect_and_sync", unexpected_sync)
    response = client.get("/api/v1/reports/catalog")

    assert response.status_code == 200, response.text
    assert calls == []


def test_resync_legacy_preserves_creation_and_only_timestamps_actual_update(db):
    old = legacy(db)
    result = create(db, "source")
    assert result.id == old.id
    assert result.create_time == SERVER_WALL
    assert result.update_time == APP_UTC
    assert result.extra_json["legacy_note"] == "must remain unchanged"
    assert "create_time" not in result.extra_json["application_utc_fields"]
    view = service.serialize_artifact(result)
    assert view["create_time"] == "2026-09-10 18:34:17"
    assert view["update_time"] == "2026-09-10 10:34:17+08:00"


def test_repeat_sync_at_same_application_time_cannot_fall_back_to_sql_now(db):
    row = create(db, "source")
    changed = source()
    changed["summary"] = "revised without changing the clock"
    again = service.sync_source_artifacts(db, [changed])[0]
    assert again.id == row.id
    assert again.create_time == again.update_time == APP_UTC
    assert "create_time" in again.extra_json["application_utc_fields"]


def test_snapshot_retry_does_not_rewrite_old_payload_or_timestamps(db):
    row = legacy(db)
    before = service.serialize_artifact(row, include_content=True)
    extra = deepcopy(row.extra_json)
    retried = service.create_snapshot_artifact(db, [], source_key=row.source_key, title="ignored")
    assert retried.id == row.id
    db.commit()
    db.expire_all()
    assert service.serialize_artifact(retried, include_content=True) == before
    assert retried.extra_json == extra


def test_aware_generated_at_is_normalized_before_naive_db_storage(db):
    local = APP_UTC.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    row = service.create_snapshot_artifact(db, [], source_key="manual:aware", title="aware", generated_at=local)
    assert row.create_time == row.signed_at == APP_UTC
    assert row.content_json["generated_at"] == "2026-09-10 02:34:17+00:00"
    assert service.serialize_artifact(row)["create_time"] == "2026-09-10 10:34:17+08:00"


@pytest.mark.parametrize("operation", ["archive", "legal_hold", "retention"])
def test_lifecycle_update_stays_on_application_clock(db, monkeypatch, operation):
    row = create(db, "snapshot")
    now = APP_UTC + timedelta(days=366)
    monkeypatch.setattr(service, "utcnow", lambda: now)
    if operation == "archive":
        service.archive_artifact(db, row, actor="test")
    elif operation == "legal_hold":
        service.set_legal_hold(db, row, enabled=True, actor="test")
    else:
        assert service.apply_retention(db, now=now) == 1
    db.expire_all()
    assert row.create_time == APP_UTC
    assert row.update_time == now
    assert service.verify_artifact(row)["valid"]


def test_default_generation_uses_real_local_utc_not_future_server_time(db, monkeypatch):
    monkeypatch.setattr(service, "utcnow", lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    before = datetime.now(timezone.utc)
    row = create(db, "snapshot")
    after = datetime.now(timezone.utc)
    assert before <= row.create_time.replace(tzinfo=timezone.utc) <= after
    assert abs((datetime.fromisoformat(service.serialize_artifact(row)["create_time"]) - before).total_seconds()) < 2


def clock_record(db, key, stored_time, *, known_utc=True, scope=(7,)):
    extra = {"scope_instance_ids": list(scope)}
    if known_utc:
        extra["application_utc_fields"] = ["create_time"]
    row = ReportArtifact(artifact_uid=key, source_key=key, source_type="test", report_type="all",
                         title=key, create_time=stored_time, extra_json=extra)
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("allowed", [None, {7}])
def test_catalog_orders_by_ingestion_id_even_when_source_or_legacy_time_is_later(db, allowed):
    old = clock_record(db, "old-future", SERVER_WALL, known_utc=False)
    newer = clock_record(db, "new-utc", APP_UTC)
    newest = clock_record(db, "newest-old-sample", APP_UTC - timedelta(days=5))
    rows = service.list_artifacts(db, allowed_instance_ids=allowed, limit=2)
    assert [row.id for row in rows] == [newest.id, newer.id]
    db.expire_all()
    assert old.create_time == SERVER_WALL


@pytest.mark.parametrize("lower,upper", [
    ("2026-09-10 10:00:00", "2026-09-10 11:00:00"),
    ("2026-09-10T10:00:00+08:00", "2026-09-10T11:00:00+08:00"),
    ("2026-09-10T02:00:00Z", "2026-09-10T03:00:00Z"),
    ("2026-09-09T19:00:00-07:00", "2026-09-09T20:00:00-07:00"),
])
def test_catalog_api_date_bounds_use_display_time_not_utc_wall_time(db, client, lower, upper):
    utc_row = clock_record(db, "new-utc", APP_UTC)
    old_in = clock_record(db, "old-local", datetime(2026, 9, 10, 10, 34, 17), known_utc=False)
    old_out = clock_record(db, "old-future", SERVER_WALL, known_utc=False)
    clock_record(db, "new-at-18-local", datetime(2026, 9, 10, 10, 34, 17))
    response = client.get("/api/v1/reports/catalog", params={
        "refresh_sources": "false", "date_from": lower, "date_to": upper,
    })
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [old_in.id, utc_row.id]
    db.expire_all()
    assert old_out.create_time == SERVER_WALL


@pytest.mark.parametrize("known_utc", [True, False])
def test_catalog_api_date_only_boundaries_are_inclusive_shanghai_days(db, client, known_utc):
    start = datetime(2026, 9, 10)
    if known_utc:
        start -= timedelta(hours=8)
    clock_record(db, "before", start - timedelta(microseconds=1), known_utc=known_utc)
    first = clock_record(db, "first", start, known_utc=known_utc)
    last = clock_record(db, "last", start + timedelta(days=1, microseconds=-1), known_utc=known_utc)
    clock_record(db, "after", start + timedelta(days=1), known_utc=known_utc)
    response = client.get("/api/v1/reports/catalog", params={
        "refresh_sources": "false", "date_from": "2026-09-10", "date_to": "2026-09-10",
    })
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [last.id, first.id]


@pytest.mark.parametrize("allowed", [None, {7}])
def test_exact_date_and_scope_filtering_precede_result_limit(db, allowed):
    valid = clock_record(db, "valid", APP_UTC)
    clock_record(db, "out-of-scope", APP_UTC, scope=(99,))
    # More than one fetch batch of coarse matches must not consume the result limit.
    db.add_all([
        ReportArtifact(artifact_uid=f"false-{i}", source_key=f"false-{i}", source_type="test",
                       report_type="all", title="coarse-only", create_time=datetime(2026, 9, 10, 10),
                       extra_json={"application_utc_fields": ["create_time"], "scope_instance_ids": [7]})
        for i in range(205)
    ])
    db.commit()
    rows = service.list_artifacts(db, date_from=datetime(2026, 9, 10, 10), date_to=datetime(2026, 9, 10, 11),
                                  allowed_instance_ids=allowed, limit=1)
    assert len(rows) == 1
    assert rows[0].artifact_uid == ("valid" if allowed else "out-of-scope")
    assert service.list_artifacts(db, allowed_instance_ids=set(), limit=1) == []


def test_catalog_api_exact_and_one_sided_dates_and_invalid_input(db, client):
    row = clock_record(db, "exact", APP_UTC)
    common = {"refresh_sources": "false"}
    exact = service.serialize_artifact(row)["create_time"]
    for bounds in ({"date_from": exact}, {"date_to": exact}, {"date_from": exact, "date_to": exact}):
        response = client.get("/api/v1/reports/catalog", params={**common, **bounds})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [row.id]
    response = client.get("/api/v1/reports/catalog", params={**common, "date_from": "2026-09-11", "date_to": "2026-09-10"})
    assert response.json()["items"] == []
    assert client.get("/api/v1/reports/catalog", params={**common, "date_from": "invalid"}).status_code == 422


@pytest.mark.parametrize("exit_mode", ["early_break", "exhausted", "error"])
def test_stream_cursor_is_explicitly_closed_before_reusing_session(db, monkeypatch, exit_mode):
    db.add_all([
        ReportArtifact(artifact_uid=f"stream-{i}", source_key=f"stream-{i}", source_type="test",
                       report_type="all", title="stream", create_time=APP_UTC,
                       extra_json={"scope_instance_ids": [7]})
        for i in range(205)
    ])
    db.commit()
    results, cursors = [], []
    original_scalars = db.scalars

    def keep_result(*args, **kwargs):
        result = original_scalars(*args, **kwargs)
        results.append(result)  # Keep alive so garbage collection cannot close it for us.
        return result

    def keep_cursor(_conn, cursor, _statement, _parameters, context, _executemany):
        if context.execution_options.get("yield_per") == 200:
            cursors.append(cursor)

    monkeypatch.setattr(db, "scalars", keep_result)
    event.listen(db.get_bind(), "after_cursor_execute", keep_cursor)
    try:
        if exit_mode == "error":
            def failed_scope(*args):
                raise RuntimeError("isolated filtering failure")

            monkeypatch.setattr(service, "_artifact_scope_allowed", failed_scope)
            with pytest.raises(RuntimeError, match="isolated filtering failure"):
                service.list_artifacts(db, allowed_instance_ids={7}, limit=1)
        else:
            limit = 1 if exit_mode == "early_break" else 1000
            rows = service.list_artifacts(db, allowed_instance_ids={7}, limit=limit)
            assert len(rows) == (1 if exit_mode == "early_break" else 205)
        assert len(results) == len(cursors) == 1
        assert results[0].closed
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursors[0].execute("SELECT 1")
        assert db.query(ReportArtifact).count() == 205
    finally:
        event.remove(db.get_bind(), "after_cursor_execute", keep_cursor)
