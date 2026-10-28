"""Regression checks for identity, immutable inspection targets and audit ordering."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models
from app.analyzers.rules import Finding
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.core.database import get_db
from app.migrations.versions import v20260911_01_sql_history_identity as history_migration
from app.models.access_control import SecurityAuditEvent, UserAssetScope
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.sql_plan import DBSqlPlanHistory
from app.models.sql_profile import DBSqlProfile
from app.models.slow_sql import DBSlowSQL
from app.schemas.db_instance import DBInstanceCreate, DBInstanceUpdate
from app.services import access_control, instances, perf_profile_collector
from app.services.inspection import report_builder, result_writer
from app.services.sql_analyzer.sql_history import build_sql_history


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def instance(db):
    row = DBInstance(instance_name="before", host="before.invalid", port=3306, db_type="mysql",
                     username="synthetic", password_cipher="synthetic")
    db.add(row)
    db.commit()
    return row


def finding():
    return Finding("synthetic", "check", "normal", "one", "1", "", "ok", "",
                   {"sql": "SELECT 1", "execution_status": "success", "rows": [{"one": 1}]})


def test_deleted_instance_identity_and_old_scope_are_not_reused(db, monkeypatch):
    row = instance(db)
    old_id = row.id
    asset = AssetInstance(stable_key="old-asset", name="before", ip="before.invalid", port=3306,
                          db_type="mysql", host_id=991, region_id=991, site_id=991, zone_id=991,
                          env_id=991, native_instance_id=old_id, status="healthy")
    db.add(asset)
    db.flush()
    db.add(UserAssetScope(user_id=991, scope_type="instance", scope_id=asset.id, access_level="read"))
    db.commit()
    result_writer.save_findings(db, row, "old-task", [finding()])
    instances.delete(db, row)
    monkeypatch.setattr(instances, "store_secret", lambda **kwargs: "synthetic")
    new = instances.create(db, DBInstanceCreate(instance_name="after", db_type="mysql", host="after.invalid",
                                               port=3306, username="synthetic", password="synthetic"))
    assert new.id > old_id
    assert asset.native_instance_id is None
    assert not access_control.scope_allows_entity(db, 991, "native_instance", new.id)
    assert result_writer.list_tasks(db, instance_id=new.id) == []
    assert report_builder.build_report_payload(db, "old-task")["report"]["instance"]["host"] == "before.invalid"


@pytest.mark.parametrize("status", ["inactive", "merged", "deleted"])
def test_retired_legacy_asset_does_not_grant_native_scope(db, status):
    row = instance(db)
    asset = AssetInstance(stable_key="retired", name="retired", ip="before.invalid", port=3306,
                          db_type="mysql", host_id=991, region_id=991, site_id=991, zone_id=991,
                          env_id=991, native_instance_id=row.id, status=status)
    db.add(asset)
    db.flush()
    db.add(UserAssetScope(user_id=991, scope_type="instance", scope_id=asset.id, access_level="read"))
    db.commit()
    assert not access_control.scope_allows_entity(db, 991, "native_instance", row.id)


def test_report_target_is_immutable_and_legacy_target_is_unknown(db):
    row = instance(db)
    result_writer.save_findings(db, row, "recorded", [finding()])
    db.add(DBCheckResult(task_id="legacy", instance_id=row.id, db_type="mysql", check_status="normal"))
    db.commit()
    instances.update(db, row, DBInstanceUpdate(instance_name="after", host="after.invalid"))
    target = report_builder.build_report_payload(db, "recorded")["report"]["instance"]
    assert target["name"] == "before" and target["host"] == "before.invalid"
    summaries = {item["task_id"]: item for item in result_writer.list_tasks(db)}
    assert summaries["recorded"]["instance_name"] == "before"
    legacy = report_builder.build_report_payload(db, "legacy")["report"]["instance"]
    assert legacy["status"] == "historical_context_missing" and legacy["host"] == ""


def test_inspection_http_list_and_detail_keep_recorded_target():
    from app.api.v1.inspection import router

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router)

    def isolated_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = isolated_session
    try:
        with Session(engine) as session:
            row = instance(session)
            result_writer.save_findings(session, row, "http-task", [finding()])
            instances.update(session, row, DBInstanceUpdate(instance_name="after", host="after.invalid"))
        with TestClient(app) as client:
            detail = client.get("/inspection/tasks/http-task")
            listing = client.get("/inspection/tasks")
            assert detail.status_code == listing.status_code == 200
            assert detail.json()["report"]["instance"]["host"] == "before.invalid"
            assert listing.json()["items"][0]["instance_name"] == "before"
            assert client.get("/inspection/tasks/missing").status_code == 404
    finally:
        engine.dispose()


def test_concurrent_audit_appends_keep_single_chain(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}", connect_args={"timeout": 15})
    Base.metadata.create_all(engine)
    barrier = Barrier(6, timeout=15)

    def append(number):
        with Session(engine) as session:
            barrier.wait()
            access_control.append_security_audit(session, category="test", action=f"append-{number}", actor="synthetic")
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(append, range(6)))
        with Session(engine) as session:
            rows = session.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id).all()
            assert len(rows) == 6 and access_control.verify_audit_chain(rows)["valid"]
            rows[2].action = "tampered"
            assert not access_control.verify_audit_chain(rows)["valid"]
    finally:
        engine.dispose()


def test_audit_rollback_releases_lock_and_does_not_publish_head(db):
    access_control.append_security_audit(db, category="test", action="rolled-back", actor="synthetic")
    db.rollback()
    access_control.append_security_audit(db, category="test", action="committed", actor="synthetic")
    db.commit()
    rows = db.query(SecurityAuditEvent).all()
    assert len(rows) == 1 and rows[0].action == "committed"
    assert access_control.verify_audit_chain(rows)["valid"]


def test_sql_history_keeps_samples_and_schema_boundaries(db, monkeypatch):
    row = instance(db)
    executions = iter((10, 30))

    def collect(instance, profiles, sources):
        value = next(executions)
        for schema in ("APP", "OTHER"):
            profiles[schema] = {"sql_id": "same", "schema_name": schema, "plan_hash": "plan",
                                "sql_text": "SELECT 1", "executions": value if schema == "APP" else 99,
                                "elapsed_ms": value * 10, "source": "live"}
        sources.add("live")
        return []

    monkeypatch.setattr(perf_profile_collector, "_collect_mysql_sql_live", collect)
    assert perf_profile_collector.collect_sql_profile(db, row)["status"] == "success"
    assert perf_profile_collector.collect_sql_profile(db, row)["status"] == "success"
    profile = db.query(DBSqlProfile).filter_by(schema_name="APP").one()
    history = build_sql_history(db, profile)
    assert [point["executions"] for point in history] == [30, 10]
    assert db.query(DBSqlPlanHistory).count() == 4
    assert profile.executions == 30
    monkeypatch.setattr(perf_profile_collector, "_collect_mysql_sql_live",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic connection failure")))
    assert perf_profile_collector.collect_sql_profile(db, row)["status"] == "failed"
    assert db.query(DBSqlPlanHistory).count() == 4


def test_sql_missing_history_does_not_invent_capture_time(db):
    history = build_sql_history(db, {"instance_id": 123, "sql_id": "missing"})
    assert history[0]["sample_time"] is None and history[0]["history_status"] == "not_collected"


def test_persisted_history_without_sample_time_stays_unknown(db):
    row = instance(db)
    profile = DBSqlProfile(instance_id=row.id, db_type="mysql", sql_id="old", source="live")
    db.add_all([profile, DBSqlPlanHistory(instance_id=row.id, db_type="mysql", sql_id="old",
                                         source="live", create_time=datetime(2020, 1, 1))])
    db.commit()
    point = build_sql_history(db, profile)[0]
    assert point["sample_time"] is None and point["history_status"] == "sample_time_missing"
    assert point["target_status"] == "historical_context_missing"


def test_offline_import_does_not_substitute_ingestion_time(db, monkeypatch):
    row = instance(db)
    db.add(DBSlowSQL(instance_id=row.id, db_type="mysql", sql_id="old", capture_time=None,
                    create_time=datetime(2020, 1, 1), executions=10))
    db.commit()
    monkeypatch.setattr(perf_profile_collector, "_collect_mysql_sql_live", lambda *args: [])
    perf_profile_collector.collect_sql_profile(db, row)
    profile = db.query(DBSqlProfile).one()
    assert profile.capture_time is None
    assert build_sql_history(db, profile)[0]["history_status"] == "not_collected"
    assert db.query(DBSqlPlanHistory).count() == 0


def test_sql_history_does_not_mix_retargeted_connection(db, monkeypatch):
    row = instance(db)

    def collect(target, profiles, sources):
        profiles["sample"] = {"sql_id": "same", "schema_name": "APP", "source": "live",
                              "executions": 10 if target.host == "before.invalid" else 900}
        sources.add("live")
        return []

    monkeypatch.setattr(perf_profile_collector, "_collect_mysql_sql_live", collect)
    perf_profile_collector.collect_sql_profile(db, row)
    profile = db.query(DBSqlProfile).one()
    first_identity = profile.target_fingerprint
    instances.update(db, row, DBInstanceUpdate(host="after.invalid"))
    assert build_sql_history(db, profile)[0]["target_snapshot"]["host"] == "before.invalid"
    perf_profile_collector.collect_sql_profile(db, row)
    assert profile.target_fingerprint != first_identity
    history = build_sql_history(db, profile)
    assert [point["executions"] for point in history] == [900]
    assert history[0]["target_snapshot"]["host"] == "after.invalid"
    assert db.query(DBSqlPlanHistory).count() == 2


def test_concurrent_offline_import_is_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}", connect_args={"timeout": 15})
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        identity = instance(session).id
    barrier = Barrier(2, timeout=15)
    sampled_at = datetime(2026, 9, 1)

    def collect(target, profiles, sources):
        profiles["sample"] = {"sql_id": "same", "schema_name": "APP", "source": "awr",
                              "executions": 10, "capture_time": sampled_at, "elapsed_ms": 0.1234}
        sources.add("awr")
        barrier.wait()
        return []

    monkeypatch.setattr(perf_profile_collector, "_collect_mysql_sql_live", collect)
    monkeypatch.setattr(perf_profile_collector, "_collect_awr_sql", lambda *args: None)
    monkeypatch.setattr(perf_profile_collector, "_collect_slowsql_table", lambda *args: None)

    def refresh(_):
        with Session(engine, autoflush=False) as session:
            return perf_profile_collector.collect_sql_profile(session, session.get(DBInstance, identity))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(refresh, range(2)))
        assert all(result["count"] == 1 for result in results)
        with Session(engine) as session:
            assert session.query(DBSqlProfile).count() == session.query(DBSqlPlanHistory).count() == 1
            point = session.query(DBSqlPlanHistory).one()
            assert point.sample_time == sampled_at and point.executions == 10
            assert float(point.elapsed_ms) == 0.12
            assert point.target_snapshot is None  # Historical input cannot prove today's target.
    finally:
        engine.dispose()


def test_history_additive_migration_retains_legacy_rows_and_is_repeatable():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE db_sql_plan_history (id INTEGER PRIMARY KEY, sql_id VARCHAR(120))")
            connection.exec_driver_sql("INSERT INTO db_sql_plan_history VALUES (1, 'old')")
            connection.exec_driver_sql("CREATE TABLE db_sql_profile (id INTEGER PRIMARY KEY)")
            history_migration.upgrade(connection)
            history_migration.upgrade(connection)
            history_migration.downgrade(connection)
            assert "schema_name" in {item["name"] for item in inspect(connection).get_columns("db_sql_plan_history")}
            assert "target_fingerprint" in {item["name"] for item in inspect(connection).get_columns("db_sql_profile")}
            assert {"target_fingerprint", "target_snapshot"}.issubset(
                {item["name"] for item in inspect(connection).get_columns("db_sql_plan_history")}
            )
            assert connection.execute(text("SELECT sql_id, schema_name FROM db_sql_plan_history")).one() == ("old", None)
    finally:
        engine.dispose()


@pytest.mark.parametrize("definition", ["INTEGER", "VARCHAR(8)", "VARCHAR(128) NOT NULL"])
def test_history_migration_rejects_incompatible_existing_column(definition):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE TABLE db_sql_plan_history (id INTEGER PRIMARY KEY, schema_name {definition})")
            with pytest.raises(RuntimeError, match="Unexpected"):
                history_migration.upgrade(connection)
    finally:
        engine.dispose()
