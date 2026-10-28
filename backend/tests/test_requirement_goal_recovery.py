"""Negative invariants for natural-language goal dispatch and crash recovery."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.access_control import SecurityAuditEvent
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch as dispatch
from app.services import capability_registry as registry
from app.services import plan_governance as governance, plan_queue, plan_service as plans
from app.api.v1 import assistant


ACTOR = {"user_id": 1}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="goal-recovery-admin", password_hash="unused", role="admin", status="active"),
            User(id=4, username="goal-recovery-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", password_cipher="env://GOAL_RECOVERY_UNUSED", enabled=1),
            DBInstance(id=11, instance_name="oracle-11", db_type="oracle", host="127.0.0.1", port=1521,
                       service_name="orcl", oracle_connect_type="service_name", username="unused",
                       password_cipher="env://GOAL_RECOVERY_UNUSED", enabled=1),
            AIProviderConfig(provider_name="fake", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="fake", enabled=1, is_default=1),
        ])
        session.commit()
        registry.sync_definitions(session, actor=ACTOR)
        yield session
    engine.dispose()


def _model(monkeypatch, **overrides):
    value = {
        "decision": "execute",
        "capability_id": "report.catalog.read",
        "params": {},
        "steps": [],
        "questions": [],
        "target_key": None,
        "goal": "test goal",
        "confidence": 0.99,
        "reason": "test",
        **overrides,
    }
    monkeypatch.setattr(dispatch, "call_ai_provider", lambda *_a, **_k: json.dumps(value))


def _input_hash(text, instance_id=None, answers=None):
    identity = {"text": text, "instance_id": instance_id}
    if answers:
        identity["answers"] = answers
    return hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _claim(db, request_id, text, *, instance_id=None):
    assert dispatch._claim_dispatch(db, ACTOR, request_id, _input_hash(text, instance_id)) is True
    return db.query(SecurityAuditEvent).filter_by(
        action="assistant.dispatch_claim", actor_user_id=1, target_id=request_id).one()


def test_explicit_rca_goal_cannot_be_downgraded_to_navigation(db, monkeypatch):
    _model(monkeypatch, decision="navigate", capability_id=dispatch.RCA_CAPABILITY,
           target_key="diagnose.smart", goal="端到端根因分析")

    result = dispatch.dispatch(
        db, actor=ACTOR, text="请对客户现场订单超时做端到端根因分析",
        instance=db.get(DBInstance, 11), request_id="recover-rca-nav-001")

    assert result["action"] == "clarify"
    assert {question["key"] for question in result["questions"]} == {
        "symptom", "time_window", "impact_scope"}
    assert db.query(DBCapabilityPlan).count() == 0


def test_navigation_target_is_bound_to_the_selected_capability(db, monkeypatch):
    _model(monkeypatch, decision="navigate", capability_id="report.catalog.read",
           target_key="perf.slowsql", goal="打开统一报告页面")
    crossed = dispatch.dispatch(
        db, actor=ACTOR, text="打开统一报告页面", request_id="recover-nav-cross-001")
    assert crossed["action"] == "navigate" and crossed["target_key"] == "report.unified"

    _model(monkeypatch, decision="navigate", capability_id="report.catalog.read",
           target_key="report.unified", goal="打开统一报告页面")
    current = dispatch.dispatch(
        db, actor=ACTOR, text="打开统一报告页面", request_id="recover-nav-current-001")
    assert current["action"] == "navigate" and current["target_key"] == "report.unified"
    assert db.query(DBCapabilityPlan).count() == 0


def test_coordinated_engine_exclusions_create_no_batch_or_plan(db, monkeypatch):
    _model(monkeypatch, capability_id="performance.slow_sql.read",
           params={"page": 1, "page_size": 10}, goal="检查慢 SQL")

    result = dispatch.dispatch(
        db, actor=ACTOR, text="检查所有数据库的慢SQL，但不要检查Oracle和MySQL",
        request_id="recover-engine-negation-001")

    assert dispatch._excluded_engines("不要检查Oracle和MySQL") == {"oracle", "mysql"}
    assert result["action"] == "navigate" and result["reason_code"] == "batch_scope_required"
    assert db.query(DBCapabilityPlan).count() == 0
    assert db.query(DBCapabilityBatch).count() == 0


def test_expired_preplan_claim_fails_without_replaying_the_model(db, monkeypatch):
    text, request_id = "列出报告", "recover-expired-claim-001"
    row = _claim(db, request_id, text)
    row.details = {
        **row.details,
        "claimed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "lease_seconds": 1,
    }
    db.commit()
    monkeypatch.setattr(dispatch, "call_ai_provider",
                        lambda *_a, **_k: pytest.fail("an expired claim must not replay the model"))

    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id=request_id)
    replay = dispatch.dispatch(db, actor=ACTOR, text=text, request_id=request_id)

    assert receipt["action"] == replay["action"] == "failed"
    assert receipt["reason_code"] == replay["reason_code"] == "dispatch_interrupted"
    assert db.query(DBCapabilityPlan).count() == 0


def test_batch_is_recovered_by_request_key_when_receipt_was_not_stored(db, monkeypatch):
    text, request_id = "检查所有数据库的慢SQL", "recover-batch-window-001"
    _claim(db, request_id, text)
    item = next(entry for entry in dispatch._candidates(
        registry.list_capabilities(db, actor=ACTOR))
        if entry["capability_id"] == "performance.slow_sql.read")
    created = dispatch._batch_dispatch(
        db, ACTOR, item, {"page": 1, "page_size": 10}, request_id,
        requested_engines={"oracle", "mysql"}, excluded_engines=set())
    assert created["action"] == "batch"
    assert db.query(SecurityAuditEvent).filter_by(
        action="assistant.dispatch", target_id=request_id).count() == 0
    monkeypatch.setattr(dispatch, "call_ai_provider",
                        lambda *_a, **_k: pytest.fail("durable batch recovery must not replay the model"))

    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id=request_id)
    replay = dispatch.dispatch(db, actor=ACTOR, text=text, request_id=request_id)

    assert receipt["action"] == replay["action"] == "batch"
    assert receipt["batch"]["batch_id"] == replay["batch"]["batch_id"] == created["batch"]["batch_id"]
    assert db.query(DBCapabilityBatch).count() == 1


def test_workflow_plan_is_recovered_when_prepare_receipt_was_not_stored(db, monkeypatch):
    text, request_id = "列出巡检任务", "recover-workflow-window-001"
    _claim(db, request_id, text)
    created = dispatch._workflow_dispatch(db, ACTOR, text, None, request_id)
    assert created and created["action"] == "workflow"
    assert db.query(SecurityAuditEvent).filter_by(
        action="assistant.dispatch", target_id=request_id).count() == 0
    monkeypatch.setattr(dispatch, "call_ai_provider",
                        lambda *_a, **_k: pytest.fail("durable workflow recovery must not replay the model"))

    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id=request_id)
    replay = dispatch.dispatch(db, actor=ACTOR, text=text, request_id=request_id)

    assert receipt["action"] == replay["action"] == "confirm"
    assert receipt["plan"]["plan_id"] == replay["plan"]["plan_id"] == created["plan"]["plan_id"]
    assert db.query(DBCapabilityPlan).count() == len(created["plans"])


@pytest.mark.parametrize("text", [
    "打开根因分析页面后执行诊断",
    "查看历史事件后，对当前订单超时做根因分析",
])
def test_navigation_or_history_prefix_does_not_hide_later_execution_goal(text):
    assert dispatch._page_only_goal(text) is False
    assert dispatch._history_goal(text) is False
    assert dispatch._root_cause_goal(text) is True


def test_navigation_followed_by_history_read_does_not_become_new_root_cause_execution():
    text = "打开根因分析页面后查询历史"
    assert dispatch._root_cause_goal(text) is False


def test_rca_execute_binds_revision_under_lock_and_retry_reuses_delivery(db, monkeypatch):
    params = {
        "symptom": "订单提交持续超时",
        "time_window": "2026-10-23 08:00-08:30 UTC",
        "impact_scope": "实例 11 的订单提交接口",
    }
    plan = plans.create_plan(
        db, actor=ACTOR, capability_id=dispatch.RCA_CAPABILITY, definition_version=1,
        instance_id=11, params=params,
    )
    governance.request_approval(db, plan["plan_id"], actor=ACTOR, expected_revision=1)
    row = plans._load(db, plan["plan_id"], ACTOR, owner_only=True)
    governance.approve_plan(
        db, plan["plan_id"], actor={"user_id": 4}, expected_revision=1,
        expected_binding_hash=row.approval_binding_hash,
    )

    original_lock = assistant.lock_transaction
    original_enqueue = plan_queue.enqueue_batch
    events = []

    def observed_lock(session, key):
        events.append(("lock", key))
        return original_lock(session, key)

    def observed_enqueue(session, plan_ids, *, actor, idempotency_key, **kwargs):
        events.append(("enqueue", idempotency_key))
        return original_enqueue(
            session, plan_ids, actor=actor, idempotency_key=idempotency_key, **kwargs)

    monkeypatch.setattr(assistant, "lock_transaction", observed_lock)
    monkeypatch.setattr(plan_queue, "enqueue_batch", observed_enqueue)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    first = assistant.execute_plan(plan["plan_id"], request, actor=ACTOR, _empty={}, db=db)
    assert first["revision"] == 1
    assert first["execution_queue_state"] == "queued"
    member = db.query(DBCapabilityExecution).one()
    batch = db.query(DBCapabilityBatch).one()
    assert events[:2] == [
        ("lock", "capability_plan_state"),
        ("enqueue", f"plan-execute:{plan['plan_id']}:1"),
    ]
    assert member.plan_revision == 1
    assert batch.idempotency_key.endswith(":1")

    replay = assistant.execute_plan(plan["plan_id"], request, actor=ACTOR, _empty={}, db=db)
    assert replay["execution_batch_id"] == first["execution_batch_id"]
    assert db.query(DBCapabilityBatch).count() == 1


def test_rca_execute_recovers_current_delivery_created_with_another_admission_key(db, monkeypatch):
    params = {
        "symptom": "订单提交持续超时",
        "time_window": "2026-10-23 08:00-08:30 UTC",
        "impact_scope": "实例 11 的订单提交接口",
    }
    plan = plans.create_plan(
        db, actor=ACTOR, capability_id=dispatch.RCA_CAPABILITY, definition_version=1,
        instance_id=11, params=params,
    )
    governance.request_approval(db, plan["plan_id"], actor=ACTOR, expected_revision=1)
    row = plans._load(db, plan["plan_id"], ACTOR, owner_only=True)
    governance.approve_plan(
        db, plan["plan_id"], actor={"user_id": 4}, expected_revision=1,
        expected_binding_hash=row.approval_binding_hash,
    )
    accepted = plan_queue.enqueue_batch(
        db, [plan["plan_id"]], actor=ACTOR,
        idempotency_key="independent-admission-before-http-response", schema_version=2,
    )
    monkeypatch.setattr(
        plan_queue, "enqueue_batch",
        lambda *_a, **_k: pytest.fail("a current durable delivery must be returned without a second enqueue"),
    )

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    recovered = assistant.execute_plan(plan["plan_id"], request, actor=ACTOR, _empty={}, db=db)

    assert recovered["execution_batch_id"] == accepted["batch_id"]
    assert recovered["execution_queue_state"] == "queued"
    assert db.query(DBCapabilityBatch).count() == 1
