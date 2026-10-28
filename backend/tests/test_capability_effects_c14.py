"""C14: an approval-gated capacity collection, verified by the row it writes.

The first effect beyond inspection must prove four separate things: it needs an approved plan,
its handler refuses to run without a durable execution identity, the fence is consulted around
the write, and the stored evidence — not the handler's own return value — decides success.
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.capacity_policy import DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import (
    access_control,
    capability_handler,
    capability_registry as registry,
    capacity_monitor,
    plan_governance as governance,
    plan_service as plans,
)
from app.services.capability_declarations import CAPABILITY_DECLARATIONS
from app.services.plan_validation import APPROVAL_KINDS


BY_ID = {item["capability_id"]: item for item in CAPABILITY_DECLARATIONS}
REQUESTER = {"user_id": 1}     # platform administrator: the only role with monitoring.manage
APPROVER = {"user_id": 4}      # a different administrator, so self-approval stays impossible


class _Spy:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c14-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14-approver", password_hash="unused", role="admin", status="active"),
            User(id=3, username="c14-viewer", password_hash="unused", role="user", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1",
                       port=3310, username="unused", enabled=1,
                       password_cipher="env://SYNTHETIC_CAPABILITY_TEST_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10",
                          db_type="mysql", ip="127.0.0.1", port=3310, host_id=10, region_id=1,
                          site_id=1, zone_id=1, env_id=1, status="active"),
        ])
        session.flush()
        session.add(UserAccessRole(user_id=3, role_id=roles["viewer"].id))
        session.add(UserAssetScope(user_id=3, scope_type="instance", scope_id=10, access_level="read"))
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _stored(db, *, samples=3, status="success", when=None):
    row = db.query(DBCapacityPolicy).filter_by(instance_id=10).first()
    if row is None:
        row = DBCapacityPolicy(instance_id=10)
        db.add(row)
    row.last_run_time = when or datetime.datetime.now()
    row.last_status = status
    row.last_sample_count = samples
    db.commit()
    return row


def _collector(db, *, status=None, samples=3):
    def run(_db, instance):
        _stored(db, samples=samples, status=status or "success")
        value = {"snapshot": {"objects": [{}] * samples}, "analysis": {}, "alerts": [],
                 "policy": {"last_sample_count": samples}}
        if status:
            value["status"] = status
        return value
    return run


def _context(db, *, execution_id="pla:1:1", guard=None):
    handler, context = registry.resolve_handler(
        db, "monitor.capacity.execute", 3, actor=REQUESTER, instance_id=10, params={},
        _allow_effects=True)
    return handler, context.__class__(**{**context.__dict__, "execution_id": execution_id,
                                         "execution_guard": guard})


def test_the_effect_is_declared_and_registered_as_an_approval_kind():
    entry = BY_ID["monitor.capacity.execute"]
    assert entry["version"] == 3
    assert entry["handler"] == "monitor.capacity_collect"
    assert entry["handler_status"] == "wired"
    assert entry["kind"] in APPROVAL_KINDS
    assert entry["output_schema"] == "capability.effect_result/v1"
    assert registry.EFFECT_HANDLERS["monitor.capacity.execute"] == "monitor.capacity_collect"


def test_the_unverifiable_rebuild_stays_explicitly_unwired():
    """A rebuild can legitimately write nothing, so it has no row-level evidence of success."""
    entry = BY_ID["performance.suggestion.refresh"]
    assert entry["execution"] == "client"
    assert entry["handler_status"] == "declared_not_wired"
    assert "evidence" in entry["reason"]


def test_an_effect_without_a_durable_execution_identity_is_refused(db, monkeypatch):
    spy = _Spy(_collector(db)(db, db.get(DBInstance, 10)))
    monkeypatch.setattr(capacity_monitor, "collect_snapshot", spy)
    handler = capability_handler.HANDLERS["monitor.capacity_collect"]
    context = capability_handler.CapabilityContext(db=db, actor_id=1, params={},
                                                   instance=db.get(DBInstance, 10))
    with pytest.raises(ValueError, match="durable_execution_required"):
        handler(context)
    assert spy.calls == []


def test_the_fence_is_consulted_before_and_after_the_write(db, monkeypatch):
    monkeypatch.setattr(capacity_monitor, "collect_snapshot", _collector(db))
    marks = []
    handler, context = _context(db, guard=lambda: marks.append("fence"))
    value = handler(context)
    assert value["outcome"] == "succeeded" and value["sample_count"] == 3
    assert marks == ["fence", "fence"]


def test_a_failing_fence_never_reaches_the_collector(db, monkeypatch):
    spy = _Spy({})
    monkeypatch.setattr(capacity_monitor, "collect_snapshot", spy)

    def stop():
        raise RuntimeError("stopped")

    handler, context = _context(db, guard=stop)
    with pytest.raises(RuntimeError, match="stopped"):
        handler(context)
    assert spy.calls == []


@pytest.mark.parametrize("status", ["skipped", "no_data"])
def test_a_collector_that_stores_nothing_is_never_reported_as_success(db, monkeypatch, status):
    monkeypatch.setattr(capacity_monitor, "collect_snapshot", _collector(db, status=status, samples=0))
    handler, context = _context(db, guard=lambda: None)
    value = handler(context)
    assert value["outcome"] == "partial"
    assert value["reason_code"] == f"capacity_{status}"


def test_a_plan_for_a_write_waits_for_approval(db):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="monitor.capacity.execute",
                                definition_version=3, instance_id=10)
    assert created["status"] == "validated" and created["approval_state"] == "pending"
    with pytest.raises(plans.PlanError, match="approval_required"):
        plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_a_read_only_account_cannot_reach_the_effect(db):
    with pytest.raises(registry.CapabilityError) as excinfo:
        registry.resolve_handler(db, "monitor.capacity.execute", 3, actor={"user_id": 3},
                                 instance_id=10, params={}, _allow_effects=True)
    assert excinfo.value.code in {"permission_denied", "scope_denied", "handler_not_wired"}


def test_an_approved_effect_runs_end_to_end_and_persists_verified_evidence(db, monkeypatch):
    calls = []

    def run(_db, instance):
        calls.append(instance.id)
        _stored(db, samples=2)
        return {"snapshot": {"objects": [{}, {}]}, "analysis": {}, "alerts": [],
                "policy": {"last_sample_count": 2}}

    monkeypatch.setattr(capacity_monitor, "collect_snapshot", run)

    created = plans.create_plan(db, actor=REQUESTER, capability_id="monitor.capacity.execute",
                                definition_version=3, instance_id=10)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    approved = governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                                       expected_revision=created["revision"],
                                       expected_binding_hash=row.approval_binding_hash)
    assert approved["status"] == "approved"
    assert calls == []                     # approval alone executes nothing

    fences = []
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER,
                                  execution_guard=lambda: fences.append("fence"))
    assert finished["outcome"] == "succeeded", finished
    assert finished["steps"][0]["result_ref"]["kind"] == "capability_effect"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["sample_count"] == 2
    assert calls == [10] and fences

    reopened = plans.read_plan(db, created["plan_id"], actor=REQUESTER)
    assert reopened["result_ref"] == finished["result_ref"]


def test_evidence_that_predates_the_run_is_rejected(db, monkeypatch):
    """A stale policy row must not be accepted just because the handler returned success."""
    _stored(db, samples=5, when=datetime.datetime.now() - datetime.timedelta(days=2))
    monkeypatch.setattr(capacity_monitor, "collect_snapshot",
                        lambda _db, instance: {"snapshot": {"objects": [{}] * 5}, "analysis": {},
                                               "alerts": [], "policy": {"last_sample_count": 5}})
    created = plans.create_plan(db, actor=REQUESTER, capability_id="monitor.capacity.execute",
                                definition_version=3, instance_id=10)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"],
                            expected_binding_hash=row.approval_binding_hash)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] != "succeeded"
    assert finished["outcome_kind"] == "result_unknown"
