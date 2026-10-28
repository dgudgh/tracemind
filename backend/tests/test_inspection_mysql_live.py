"""C05 sample against a live MySQL target: the real queue, results and read-back.

This is one engine, not four. It exists because the queue path had only ever been
exercised with collectors replaced by fixtures; a fixture cannot prove that the
template actually queries MySQL, that findings are written from real rows, or
that a refused connection is reported as failed instead of as a quiet success.

    MESDBTOOLS_MYSQL_TARGET_PASSWORD=... \
    MESDBTOOLS_MYSQL_TARGET_PORT=33306 \
    python -m pytest backend/tests/test_inspection_mysql_live.py
"""
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

PASSWORD = os.environ.get("MESDBTOOLS_MYSQL_TARGET_PASSWORD")
HOST = os.environ.get("MESDBTOOLS_MYSQL_TARGET_HOST", "127.0.0.1")
PORT = int(os.environ.get("MESDBTOOLS_MYSQL_TARGET_PORT", "33306"))
USER = os.environ.get("MESDBTOOLS_MYSQL_TARGET_USER", "inspector")

pytestmark = pytest.mark.skipif(not PASSWORD, reason="set MESDBTOOLS_MYSQL_TARGET_PASSWORD to a disposable MySQL")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MESDBTOOLS_TARGET_MYSQL_PASSWORD", PASSWORD)
    engine = create_engine(f"sqlite:///{tmp_path / 'inspection-live.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add(AssetInstance(id=1, native_instance_id=1, stable_key="live:mysql", name="live-mysql",
                                  db_type="mysql", ip=HOST, port=PORT, host_id=1, region_id=1, site_id=1,
                                  zone_id=1, env_id=1, status="active"))
        session.add(DBInstance(id=1, instance_name="live-mysql-8", db_type="mysql", host=HOST, port=PORT,
                               username=USER, password_cipher="env://MESDBTOOLS_TARGET_MYSQL_PASSWORD", enabled=1))
        session.commit()
        policy_service.ensure_default_policies(session)
        yield session
    engine.dispose()


def test_live_mysql_inspection_runs_through_the_queue_and_reads_back(db):
    instance = db.get(DBInstance, 1)
    policy = jobs.default_policy_for(db, instance, "mysql")
    assert policy is not None and policy.policy_key == "mysql_standard"

    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    job = payload["job"]
    assert job["status"] == "success", job
    assert payload["task"]["task_id"] == job["id"]
    assert payload["modules"], "a live MySQL run produced no modules"
    assert payload["findings"], "a live MySQL run produced no findings"
    assert payload["report"]["analysis_source"] == "rule_engine"
    assert payload["report"]["overall_conclusion"]

    # Reopen in a fresh session: the job, the rows and the derived run envelope
    # must all come from persisted state, not from the response we just built.
    with Session(db.get_bind(), autoflush=False) as verify:
        stored = verify.get(DBInspectionJob, job["id"])
        assert stored is not None and stored.status == "success"
        assert stored.policy_id == policy.id
        rows = result_writer.get_task_rows(verify, job["id"], instance.id)
        assert len(rows) == len(payload["findings"])
        view = run_readback.build_run_read(verify, job["id"], instance_id=instance.id)
        assert view["run"]["status"] == "success"
        assert view["results"]["total_count"] == len(rows)
        assert view["results"]["task_status"] == "success"
        assert view["evidence"]["state"] in {"recorded", "complete", "partial"}
        assert view["report"]["report"]["overall_conclusion"] == payload["report"]["overall_conclusion"]
        assert view["plan"]["policy_key"] == policy.policy_key
        assert view["reports"]["state"] == "synced"
        artifact = verify.query(ReportArtifact).filter_by(source_key=f"source:inspection:{job['id']}").one()
        assert artifact.source_id == job["id"] and artifact.status == "success"


def test_a_refused_connection_is_failed_not_success(db):
    instance = db.get(DBInstance, 1)
    instance.port = 1  # nothing listens here; connection is refused immediately
    db.commit()
    policy = jobs.default_policy_for(db, instance, "mysql")
    payload = dispatch.run_through_queue(db, instance, policy, trigger_type="manual")
    assert payload["job"]["status"] == "failed", payload["job"]
    assert payload["job"]["failure_reason"]
    assert payload["job"]["published"] is False
    # The only rows are explicit failure evidence, never scored check results.
    assert payload["task"]["overall_score"] is None
    assert payload["task"]["task_status"] == "failed"
    assert payload["findings"] and all("失败" in (row["finding"] or "") for row in payload["findings"])
    assert "失败" in payload["report"]["overall_conclusion"]
    with Session(db.get_bind(), autoflush=False) as verify:
        view = run_readback.build_run_read(verify, payload["job"]["id"], instance_id=instance.id)
        assert view["run"]["status"] == "failed"
        assert view["results"]["task_status"] == "failed"
        assert view["results"]["counts"] == {"critical": 0, "warning": len(payload["findings"]), "info": 0, "normal": 0}
        assert view["evidence"]["state"] == "recorded"
        assert "失败" in view["report"]["report"]["overall_conclusion"]
