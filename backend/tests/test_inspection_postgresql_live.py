"""C05 sample on live PostgreSQL 16: real queue, real SQL, results and report read-back."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.services.inspection import dispatch, jobs, policy_service, result_writer, run_readback

PASSWORD = os.environ.get("MESDBTOOLS_PG_TARGET_PASSWORD")
SOCKET_DIR = os.environ.get("MESDBTOOLS_PG_TARGET_SOCKET", "/tmp/mesdbtools-pgdata2")
PORT = int(os.environ.get("MESDBTOOLS_PG_TARGET_PORT", "5432"))

pytestmark = pytest.mark.skipif(not PASSWORD, reason="set MESDBTOOLS_PG_TARGET_PASSWORD to a disposable PostgreSQL")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MESDBTOOLS_TARGET_PG_PASSWORD", PASSWORD)
    engine = create_engine(f"sqlite:///{tmp_path / 'pg-live.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add(AssetInstance(id=2, native_instance_id=2, stable_key="pg:2", name="live-pg",
                                  db_type="postgresql", ip="127.0.0.1", port=PORT, host_id=2, region_id=1,
                                  site_id=1, zone_id=1, env_id=1, status="active"))
        session.add(DBInstance(id=2, instance_name="live-pg16", db_type="postgresql", host=SOCKET_DIR,
                               port=PORT, database_name="postgres", username="postgres",
                               password_cipher="env://MESDBTOOLS_TARGET_PG_PASSWORD", enabled=1))
        session.commit()
        policy_service.ensure_default_policies(session)
        yield session
    engine.dispose()


def test_live_postgresql_inspection_runs_and_reads_back(db):
    instance = db.get(DBInstance, 2)
    policy = jobs.default_policy_for(db, instance, "postgresql")
    assert policy is not None and policy.policy_key == "postgresql_standard"
    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    assert payload["job"]["status"] == "success", payload["job"]
    assert payload["modules"] and payload["findings"]
    assert payload["report"]["analysis_source"] == "rule_engine"
    with Session(db.get_bind(), autoflush=False) as verify:
        stored = verify.get(DBInspectionJob, payload["job"]["id"])
        assert stored is not None and stored.status == "success"
        rows = result_writer.get_task_rows(verify, stored.id, instance.id)
        assert len(rows) == len(payload["findings"])
        view = run_readback.build_run_read(verify, stored.id, instance_id=instance.id)
        assert view["run"]["status"] == "success"
        assert view["reports"]["state"] == "synced"
        assert verify.query(ReportArtifact).filter_by(source_key=f"source:inspection:{stored.id}").count() == 1


def test_a_refused_postgresql_connection_is_failed(db):
    instance = db.get(DBInstance, 2)
    instance.host = "/tmp/mesdbtools-no-such-pg-socket"
    db.commit()
    policy = jobs.default_policy_for(db, instance, "postgresql")
    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    assert payload["job"]["status"] == "failed"
    assert payload["job"]["published"] is False
    assert payload["task"]["overall_score"] is None
