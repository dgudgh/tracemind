"""C05 sample on live Oracle 19c: real SYSDBA connection, real SQL, honest partial on skipped checks."""
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

PASSWORD = os.environ.get("MESDBTOOLS_ORACLE_PASSWORD")
HOST = os.environ.get("MESDBTOOLS_ORACLE_HOST", "192.168.102.200")
PORT = int(os.environ.get("MESDBTOOLS_ORACLE_PORT", "1521"))
SERVICE = os.environ.get("MESDBTOOLS_ORACLE_SERVICE", "mesdb")

pytestmark = pytest.mark.skipif(not PASSWORD, reason="set MESDBTOOLS_ORACLE_PASSWORD for a disposable Oracle")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MESDBTOOLS_ORACLE_PASSWORD", PASSWORD)
    engine = create_engine(f"sqlite:///{tmp_path / 'oracle-live.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add(AssetInstance(id=3, native_instance_id=3, stable_key="ora:3", name="mesdb",
                                  db_type="oracle", ip=HOST, port=PORT, host_id=3, region_id=1, site_id=1,
                                  zone_id=1, env_id=1, status="active"))
        session.add(DBInstance(id=3, instance_name="mesdb-19c", db_type="oracle", host=HOST, port=PORT,
                               service_name=SERVICE, oracle_connect_type="service", username="sys",
                               password_cipher="env://MESDBTOOLS_ORACLE_PASSWORD", account_type="sysdba",
                               enabled=1))
        session.commit()
        policy_service.ensure_default_policies(session)
        yield session
    engine.dispose()


def test_live_oracle_inspection_collects_and_reads_back(db):
    instance = db.get(DBInstance, 3)
    policy = jobs.default_policy_for(db, instance, "oracle")
    assert policy is not None and policy.policy_key == "oracle_standard"
    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    job = payload["job"]
    assert job["status"] in {"success", "partial"}, job
    reason = job.get("failure_reason") or ""
    assert "dbcheck_noncdb.sql" in reason
    assert payload["modules"] and payload["findings"]
    with Session(db.get_bind(), autoflush=False) as verify:
        stored = verify.get(DBInspectionJob, job["id"])
        rows = result_writer.get_task_rows(verify, stored.id, instance.id)
        assert rows and len(rows) == len(payload["findings"])
        assert verify.query(ReportArtifact).filter_by(source_key=f"source:inspection:{stored.id}").count() == 1
        view = run_readback.build_run_read(verify, stored.id, instance_id=instance.id)
        assert view["run"]["status"] in {"success", "partial"}
        # A collection with skipped or failed SQL never reports a health score. On this
        # instance the AWR-dependent Load Profile sections fail because DBA_HIST has 0
        # snapshots; the failure is surfaced, not hidden.
        if job["status"] == "partial":
            assert view["report"]["task"]["overall_score"] is None
            assert "跳过" in reason or "失败" in reason


def test_a_refused_oracle_port_is_failed_and_unscored(db):
    instance = db.get(DBInstance, 3)
    instance.host = "127.0.0.1"
    instance.port = 1
    db.commit()
    policy = jobs.default_policy_for(db, instance, "oracle")
    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    assert payload["job"]["status"] == "failed"
    assert payload["job"]["published"] is False
    assert payload["task"]["overall_score"] is None
