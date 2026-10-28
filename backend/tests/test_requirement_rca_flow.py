"""Isolated requirement flow: clarify -> approve -> durable queue -> persisted RCA."""
from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.api.v1 import assistant, diagnosis_center
from app.cmdb.models import AssetInstance
from app.core.database import Base, get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagEvidence, DiagIncident, DiagRootCause
from app.models.user import User
from app.services import access_control, capability_dispatch, capability_handler
from app.services import capability_registry as registry
from app.services import plan_governance as governance, plan_queue, plan_service as plans
from app.services.diagnosis import orchestrator
from app.services.diagnosis.capability_effect import customer_context_summary


REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}
CAPABILITY = "diagnosis.root_cause.execute"
TEXT = "请对订单服务超时做端到端根因分析"
ANSWERS = {
    "symptom": "订单提交持续超时，连接数同步上涨",
    "time_window": "2026-10-23 08:00-08:30 UTC",
    "impact_scope": "实例 10 的订单提交接口和在线用户",
}


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rca-flow.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    with factory() as db:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
        db.add_all([
            User(id=1, username="rca-flow-requester", password_hash="unused", role="admin", status="active"),
            User(id=2, username="rca-flow-operator", password_hash="unused", role="user", status="active"),
            User(id=4, username="rca-flow-approver", password_hash="unused", role="admin", status="active"),
            User(id=3, username="rca-flow-viewer", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["dba_operator"].id),
            UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="operate"),
            UserAccessRole(user_id=3, role_id=roles["viewer"].id),
            UserAssetScope(user_id=3, scope_type="instance", scope_id=10, access_level="operate"),
            DBInstance(id=10, instance_name="orders-oracle", db_type="oracle", host="127.0.0.1",
                       port=1521, service_name="orcl", oracle_connect_type="service_name",
                       username="unused", password_cipher="env://SYNTHETIC_RCA_FLOW", enabled=1),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10",
                          db_type="oracle", ip="127.0.0.1", port=1521, host_id=10, region_id=1,
                          site_id=1, zone_id=1, env_id=1, status="active"),
            AIProviderConfig(provider_name="synthetic", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="synthetic",
                             enabled=1, is_default=1),
        ])
        db.commit()
        registry.sync_definitions(db, actor=REQUESTER)
    yield SimpleNamespace(engine=engine, factory=factory)
    engine.dispose()


def _model_selects_context(*_args, **_kwargs):
    return json.dumps({
        "decision": "execute",
        "capability_id": "diagnosis.investigation.read",
        "params": {},
        "confidence": 0.95,
        "reason": "synthetic model deliberately chose existing context",
    })


def _persist_rca(db, instance_id, *, execution_status="success", **kwargs):
    incident = DiagIncident(
        incident_no=f"INC-RCA-FLOW-{db.query(DiagIncident).count() + 1}",
        instance_id=instance_id,
        db_type="oracle",
        instance_name="orders-oracle",
        title=kwargs["title"],
        trigger_source=kwargs["trigger_source"],
        status="root_caused",
        severity="warning",
        primary_layer="database",
        root_cause_summary="连接池耗尽是当前首要根因假设",
        evidence_count=2,
        root_cause_count=1,
        started_at=datetime.now(),
        root_caused_at=datetime.now(),
        ai_summary="现有证据支持连接池耗尽假设，仍需现场验证。",
        ai_result={
            "execution_status": execution_status,
            "data_quality": "good" if execution_status == "success" else "partial",
            "conclusion_status": "hypotheses_require_validation",
        },
    )
    db.add(incident)
    db.flush()
    db.add_all([
        DiagEvidence(
            incident_id=incident.id,
            source="manual_symptom",
            phenomenon=kwargs["symptom"],
            evidence=f"客户现场描述：{kwargs['symptom']}",
            raw={
                "symptom": kwargs["symptom"],
                "intent_summary": kwargs["intent_summary"],
                "intent_signals": kwargs["intent_signals"],
                "recommended_cards": kwargs["recommended_cards"],
            },
        ),
        DiagEvidence(
            incident_id=incident.id,
            source="inspection",
            phenomenon="活动连接逼近连接池上限",
            evidence="active_connections=98, pool_limit=100",
            judgement="连接池余量不足",
            raw={"execution_status": execution_status},
        ),
        DiagRootCause(
            incident_id=incident.id,
            layer="database",
            cause=incident.root_cause_summary,
            rank=1,
            is_primary=1,
            confidence=82,
        ),
    ])
    db.commit()
    return orchestrator.get_incident_detail(db, incident.id)


def _persist_failed_rca(db, instance_id, **kwargs):
    incident = DiagIncident(
        incident_no="INC-RCA-FLOW-FAILED",
        instance_id=instance_id,
        db_type="oracle",
        instance_name="orders-oracle",
        title=kwargs["title"],
        trigger_source=kwargs["trigger_source"],
        status="failed",
        severity="unknown",
        root_cause_summary="诊断流程异常：synthetic collection failure",
        evidence_count=0,
        root_cause_count=0,
        started_at=datetime.now(),
        ai_result={
            "execution_status": "failed",
            "data_quality": "missing",
            "conclusion_status": "not_available",
        },
    )
    db.add(incident)
    db.commit()
    return orchestrator.get_incident_detail(db, incident.id)


def _approve(db, plan_id, revision):
    governance.request_approval(db, plan_id, actor=REQUESTER, expected_revision=revision)
    row = plans._load(db, plan_id, REQUESTER, owner_only=True)
    governance.approve_plan(
        db,
        plan_id,
        actor=APPROVER,
        expected_revision=revision,
        expected_binding_hash=row.approval_binding_hash,
    )


def _client(factory):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(diagnosis_center.router, prefix="/api/v1")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[assistant._actor] = lambda: REQUESTER
    return TestClient(app)


def test_rca_requirement_runs_through_clarification_approval_queue_and_get(env, monkeypatch):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", _model_selects_context)
    monkeypatch.setattr(orchestrator, "run_diagnosis", _persist_rca)
    monkeypatch.setattr(diagnosis_center, "_create_ticket_for_incident",
                        lambda *_a, **_k: pytest.fail("RCA capability must not create a ticket"))
    monkeypatch.setattr(diagnosis_center, "_notify_incident",
                        lambda *_a, **_k: pytest.fail("RCA capability must not notify"))

    with env.factory() as db:
        instance = db.get(DBInstance, 10)
        clarification = capability_dispatch.dispatch(
            db, actor=REQUESTER, text=TEXT, instance=instance, request_id="rca-flow-clarify")
        assert clarification["action"] == "clarify"
        assert {item["key"] for item in clarification["questions"]} == {
            "symptom", "time_window", "impact_scope"}
        assert db.query(DBCapabilityPlan).count() == 0

        confirmation = capability_dispatch.dispatch(
            db, actor=REQUESTER, text=TEXT, instance=instance,
            request_id="rca-flow-confirm", answers=ANSWERS)
        assert confirmation["action"] == "confirm"
        assert confirmation["capability"]["capability_id"] == CAPABILITY
        plan = confirmation["plan"]
        assert plan["steps"][0]["capability_id"] == CAPABILITY

    with _client(env.factory) as client:
        refused = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == {"code": "approval_required"}
        with env.factory() as db:
            assert db.query(DBCapabilityBatch).count() == 0
            assert db.query(DBCapabilityExecution).count() == 0
            _approve(db, plan["plan_id"], plan["revision"])

        queued = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        replay = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert queued.status_code == replay.status_code == 200
        queued_plan, replayed_plan = queued.json(), replay.json()
        assert queued_plan["status"] == replayed_plan["status"] == "approved"
        assert queued_plan["execution_queue_state"] == replayed_plan["execution_queue_state"] == "queued"
        assert queued_plan["execution_batch_id"] == replayed_plan["execution_batch_id"]
        with env.factory() as db:
            assert db.query(DBCapabilityBatch).count() == 1
            assert db.query(DBCapabilityExecution).count() == 1

        worker = plan_queue.process_one(env.factory, worker_id="rca-flow-worker")
        assert worker["queue_state"] == "archived", worker
        assert worker["outcome"] == "succeeded"

        plan_response = client.get(f"/api/v1/assistant/plans/{plan['plan_id']}")
        assert plan_response.status_code == 200, plan_response.text
        saved_plan = plan_response.json()
        assert saved_plan["execution_batch_id"] == queued_plan["execution_batch_id"]
        assert saved_plan["execution_queue_state"] == "archived"
        step_ref = saved_plan["steps"][0]["result_ref"]
        assert step_ref["kind"] == "capability_effect"
        assert step_ref["schema"] == "diagnosis.root_cause_result/v1"
        inline = step_ref["inline"]
        assert inline["outcome"] == "success"
        assert inline["root_cause_count"] == 1 and inline["evidence_count"] == 2
        assert inline["sampling"] == {
            "requested_time_window": ANSWERS["time_window"],
            "impact_scope": ANSWERS["impact_scope"],
            "collection_mode": "current_snapshot",
            "historical_replay_performed": False,
        }

        incident_response = client.get(
            f"/api/v1/diagnosis-center/incidents/{inline['incident_id']}")
        assert incident_response.status_code == 200, incident_response.text
        incident = incident_response.json()
        assert incident["incident_no"] == inline["incident_no"]
        assert len(incident["evidences"]) == 2 and len(incident["root_causes"]) == 1
        manual = next(item for item in incident["evidences"] if item["source"] == "manual_symptom")
        assert ANSWERS["time_window"] in manual["raw"]["intent_summary"]
        assert "当前快照" in manual["raw"]["intent_summary"]
        assert incident["ticket_id"] is None and incident["status"] == "root_caused"


def test_disabled_rca_capability_is_not_admitted(env, monkeypatch):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", _model_selects_context)
    with env.factory() as db:
        registry.set_enabled(db, CAPABILITY, enabled=False, actor=REQUESTER)
        result = capability_dispatch.dispatch(
            db, actor=REQUESTER, text=TEXT, instance=db.get(DBInstance, 10),
            request_id="rca-disabled", answers=ANSWERS)
        assert result["action"] == "navigate"
        assert result["reason_code"] == "diagnosis_not_available"
        assert db.query(DBCapabilityPlan).count() == 0


def test_read_only_diagnosis_permission_cannot_admit_rca_collection(env, monkeypatch):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", _model_selects_context)
    with env.factory() as db:
        result = capability_dispatch.dispatch(
            db, actor={"user_id": 3}, text=TEXT, instance=db.get(DBInstance, 10),
            request_id="rca-read-only", answers=ANSWERS)
        assert result["action"] == "navigate"
        assert result["reason_code"] == "diagnosis_not_available"
        assert db.query(DBCapabilityPlan).count() == 0


def test_scoped_operator_can_list_admit_and_execute_rca(env, monkeypatch):
    operator = {"user_id": 2}
    monkeypatch.setattr(orchestrator, "run_diagnosis", _persist_rca)
    with env.factory() as db:
        catalog = registry.list_capabilities(db, actor=operator, engine="oracle")
        item = next(entry for entry in catalog["items"] if entry["capability_id"] == CAPABILITY)
        assert item["availability"] == "available" and item["plan_executable"] is True
        assert item["requires_instance"] is True
        plan = plans.create_plan(
            db, actor=operator, capability_id=CAPABILITY, definition_version=1,
            instance_id=10, params=ANSWERS)
        governance.request_approval(
            db, plan["plan_id"], actor=operator, expected_revision=plan["revision"])
        row = plans._load(db, plan["plan_id"], operator, owner_only=True)
        governance.approve_plan(
            db, plan["plan_id"], actor=APPROVER, expected_revision=plan["revision"],
            expected_binding_hash=row.approval_binding_hash)
        plan_queue.enqueue_batch(
            db, [plan["plan_id"]], actor=operator,
            idempotency_key="rca-scoped-operator", schema_version=2)
    worker = plan_queue.process_one(env.factory, worker_id="rca-scoped-worker")
    assert worker["queue_state"] == "archived" and worker["outcome"] == "succeeded"
    with env.factory() as db:
        saved = plans.read_plan(db, plan["plan_id"], actor=operator)
        assert saved["steps"][0]["result_ref"]["inline"]["incident_id"] > 0


def test_partial_diagnosis_stays_partial_with_a_durable_incident(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_diagnosis",
                        lambda *args, **kwargs: _persist_rca(
                            *args, execution_status="partial", **kwargs))
    with env.factory() as db:
        plan = plans.create_plan(
            db, actor=REQUESTER, capability_id=CAPABILITY, definition_version=1,
            instance_id=10, params=ANSWERS)
        _approve(db, plan["plan_id"], plan["revision"])
        plan_queue.enqueue_batch(
            db, [plan["plan_id"]], actor=REQUESTER,
            idempotency_key="rca-partial-queue", schema_version=2)
    worker = plan_queue.process_one(env.factory, worker_id="rca-partial-worker")
    assert worker["queue_state"] == "archived"
    assert worker["outcome"] == "partial"
    with env.factory() as db:
        finished = plans.read_plan(db, plan["plan_id"], actor=REQUESTER)
        assert finished["outcome"] == "partial"
        assert finished["steps"][0]["status"] == "partial"
        inline = finished["steps"][0]["result_ref"]["inline"]
        assert inline["reason_code"] == "collection_incomplete"
        assert inline["incident_id"] == db.query(DiagIncident.id).scalar()


def test_failed_diagnosis_is_archived_as_failed_instead_of_unknown(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_diagnosis", _persist_failed_rca)
    with env.factory() as db:
        plan = plans.create_plan(
            db, actor=REQUESTER, capability_id=CAPABILITY, definition_version=1,
            instance_id=10, params=ANSWERS)
        _approve(db, plan["plan_id"], plan["revision"])
        plan_queue.enqueue_batch(
            db, [plan["plan_id"]], actor=REQUESTER,
            idempotency_key="rca-failed-queue", schema_version=2)
    worker = plan_queue.process_one(env.factory, worker_id="rca-failed-worker")
    assert worker["queue_state"] == "archived"
    assert worker["outcome"] == "failed"
    with env.factory() as db:
        finished = plans.read_plan(db, plan["plan_id"], actor=REQUESTER)
        inline = finished["steps"][0]["result_ref"]["inline"]
        assert inline["outcome"] == "failed"
        assert inline["error_code"] == "diagnosis_failed"
        assert inline["incident_id"] == db.query(DiagIncident.id).scalar()
        assert db.query(DiagEvidence).filter_by(
            incident_id=inline["incident_id"], source="manual_symptom").count() == 1


def test_result_context_cannot_consistently_replace_frozen_customer_params(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_diagnosis", _persist_rca)
    original = capability_handler.HANDLERS["diagnosis.root_cause"]

    def tamper(ctx):
        value = original(ctx)
        window, impact = "different window", "different impact"
        value["sampling"]["requested_time_window"] = window
        value["sampling"]["impact_scope"] = impact
        evidence = ctx.db.query(DiagEvidence).filter_by(
            incident_id=value["incident_id"], source="manual_symptom").one()
        raw = dict(evidence.raw)
        raw["intent_summary"] = customer_context_summary(window, impact)
        evidence.raw = raw
        ctx.db.commit()
        return value

    monkeypatch.setitem(capability_handler.HANDLERS, "diagnosis.root_cause", tamper)
    with env.factory() as db:
        plan = plans.create_plan(
            db, actor=REQUESTER, capability_id=CAPABILITY, definition_version=1,
            instance_id=10, params=ANSWERS)
        _approve(db, plan["plan_id"], plan["revision"])
        finished = plans.execute_plan(
            db, plan["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
        assert finished["outcome"] == "unknown"
        assert finished["outcome_kind"] == "result_unknown"
        assert finished["steps"][0]["result_ref"] is None
