"""C14: snapshot/profile/workload collections are accepted only via their own rows."""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole
from app.models.capacity_policy import DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.models.index_finding import DBIndexFinding
from app.models.table_profile import DBTableProfile
from app.models.user import User
from app.models.workload_report import DBWorkloadReport
from app.services import access_control, capability_registry as registry, capacity_monitor
from app.services import perf_profile_collector, plan_governance as governance, plan_service as plans
from app.services import workload_report

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c14p-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14p-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3310,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_PROFILE_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="127.0.0.1", port=3310, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _approved(db, capability):
    created = plans.create_plan(db, actor=REQUESTER, capability_id=capability, definition_version=3, instance_id=10)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return created


def _run(db, capability):
    created = _approved(db, capability)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_table_snapshot_rows_are_the_evidence(db, monkeypatch):
    def collect(_db, instance):
        now = datetime.datetime.now()
        _db.add_all([DBTableProfile(instance_id=instance.id, db_type="mysql", table_name="t1",
                                    capture_time=now, issues=[]),
                     DBTableProfile(instance_id=instance.id, db_type="mysql", table_name="t2",
                                    capture_time=now, issues=[])])
        _db.commit()
        return {"status": "success", "count": 2, "published": True, "errors": []}
    monkeypatch.setattr(perf_profile_collector, "collect_table_profile", collect)
    finished = _run(db, "performance.table_profile.collect")
    assert finished["outcome"] == "succeeded", finished
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["count"] == 2


def test_zero_row_complete_snapshot_is_partial_not_success(db, monkeypatch):
    monkeypatch.setattr(perf_profile_collector, "collect_table_profile",
                        lambda _db, instance: {"status": "success", "count": 0, "published": True, "errors": []})
    finished = _run(db, "performance.table_profile.collect")
    assert finished["outcome"] == "partial"


def test_index_findings_use_rows_and_stale_rows_are_rejected(db, monkeypatch):
    def stale(_db, instance):
        _db.add(DBIndexFinding(instance_id=instance.id, db_type="mysql", finding_type="missing",
                               capture_time=datetime.datetime.now() - datetime.timedelta(days=1)))
        _db.commit()
        return {"status": "success", "count": 1, "published": True, "errors": []}
    monkeypatch.setattr(perf_profile_collector, "collect_index_findings", stale)
    finished = _run(db, "performance.index.collect")
    assert finished["outcome_kind"] == "result_unknown"


def test_workload_report_row_must_match_status_and_time(db, monkeypatch):
    def collect(_db, instance, payload, actor="system"):
        row = DBWorkloadReport(report_no="WR-1", instance_id=instance.id, instance_name=instance.instance_name,
                               db_type="mysql", source="mysql", window_start=datetime.datetime.now(),
                               window_end=datetime.datetime.now(), status="success", capability_status="available",
                               license_status="not_required", redaction_mode="fingerprint_only", cost_level="low",
                               row_limit=20, created_by=actor, create_time=datetime.datetime.now(),
                               summary_json={"score": 90})
        _db.add(row)
        _db.commit()
        return row
    monkeypatch.setattr(workload_report, "collect_report", collect)
    finished = _run(db, "performance.workload.collect")
    assert finished["outcome"] == "succeeded"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["report_no"] == "WR-1"


def test_capacity_collect_reuses_the_policy_row_evidence(db, monkeypatch):
    def collect(_db, instance):
        row = _db.query(DBCapacityPolicy).filter_by(instance_id=instance.id).first() or DBCapacityPolicy(instance_id=instance.id)
        row.last_run_time = datetime.datetime.now()
        row.last_status, row.last_sample_count = "success", 2
        _db.add(row)
        _db.commit()
        return {"snapshot": {"objects": [{}, {}]}, "policy": {"last_sample_count": 2}}
    monkeypatch.setattr(capacity_monitor, "collect_snapshot", collect)
    finished = _run(db, "performance.capacity.collect")
    assert finished["outcome"] == "succeeded"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["sample_count"] == 2
