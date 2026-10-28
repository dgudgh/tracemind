"""C14 second effect: the metric collection is judged by the rows it wrote, not its envelope."""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole
from app.models.db_instance import DBInstance
from app.models.perf_metric import DBPerfMetric
from app.models.user import User
from app.services import access_control, capability_registry as registry, perf_baseline
from app.services import plan_governance as governance, plan_service as plans

REQUESTER, APPROVER, VIEWER = {"user_id": 1}, {"user_id": 4}, {"user_id": 3}
CAPABILITY = "performance.baseline.collect"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c14b-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14b-approver", password_hash="unused", role="admin", status="active"),
            User(id=3, username="c14b-viewer", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=3, role_id=roles["viewer"].id),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3310,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_BASELINE_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="127.0.0.1", port=3310, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _write(db, names=("qps", "connections"), when=None):
    for name in names:
        db.add(DBPerfMetric(instance_id=10, db_type="mysql", metric_name=name, value=1,
                            capture_time=when or datetime.datetime.now()))
    db.commit()


def _approved(db):
    created = plans.create_plan(db, actor=REQUESTER, capability_id=CAPABILITY, definition_version=3, instance_id=10)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return created


def test_a_plan_for_metric_collection_waits_for_approval(db, monkeypatch):
    monkeypatch.setattr(perf_baseline, "collect_perf_metrics", lambda *_: pytest.fail("must not run"))
    created = plans.create_plan(db, actor=REQUESTER, capability_id=CAPABILITY, definition_version=3, instance_id=10)
    assert created["approval_state"] == "pending"
    with pytest.raises(plans.PlanError, match="approval_required"):
        plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_real_rows_make_it_succeed_and_reopen(db, monkeypatch):
    def run(_db, instance):
        _write(db)
        return {"count": 2, "metrics": {"qps": 1, "connections": 1}, "errors": [], "warnings": []}
    monkeypatch.setattr(perf_baseline, "collect_perf_metrics", run)
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "succeeded", finished
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["count"] == 2 and inline["metric_names"] == ["connections", "qps"]
    assert plans.read_plan(db, created["plan_id"], actor=REQUESTER)["result_ref"] == finished["result_ref"]


def test_zero_rows_with_a_warning_is_partial_not_success(db, monkeypatch):
    monkeypatch.setattr(perf_baseline, "collect_perf_metrics",
                        lambda *_: {"count": 0, "metrics": {}, "errors": [], "warnings": ["unsupported engine"]})
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "partial" and finished["steps"][0]["status"] == "partial"


def test_a_collector_error_is_failed_with_no_rows(db, monkeypatch):
    monkeypatch.setattr(perf_baseline, "collect_perf_metrics",
                        lambda *_: {"count": 0, "metrics": {}, "errors": ["access denied"], "warnings": []})
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "failed"
    assert finished["outcome_kind"] == "collector_error"


def test_rows_older_than_the_step_cannot_stand_in_for_this_run(db, monkeypatch):
    def stale(_db, _instance):
        _write(db, when=datetime.datetime.now() - datetime.timedelta(days=2))
        return {"count": 2, "metrics": {"qps": 1, "connections": 1}, "errors": [], "warnings": []}
    monkeypatch.setattr(perf_baseline, "collect_perf_metrics", stale)
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] != "succeeded"
    assert finished["outcome_kind"] == "result_unknown"
