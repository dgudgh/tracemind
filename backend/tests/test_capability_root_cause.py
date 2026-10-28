"""The RCA capability runs only as an approved effect and proves its persisted incident."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagEvidence, DiagIncident, DiagRootCause
from app.models.user import User
from app.services import access_control, capability_handler
from app.services import capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans
from app.services.capability_declarations import CAPABILITY_DECLARATIONS
from app.services.diagnosis import orchestrator


CAPABILITY = "diagnosis.root_cause.execute"
REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}
PARAMS = {
    "symptom": "订单提交持续超时并伴随连接数上涨",
    "time_window": "2026-10-23 08:00-08:30 UTC",
    "impact_scope": "实例 2 上的订单服务",
    "title": "订单服务超时根因分析",
}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        access_control.ensure_builtin_roles(session)
        session.add_all([
            User(id=1, username="rca-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="rca-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="oracle-10", db_type="oracle", host="127.0.0.1",
                       port=1521, username="unused", enabled=1,
                       password_cipher="env://SYNTHETIC_RCA_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10",
                          db_type="oracle", ip="127.0.0.1", port=1521, host_id=10, region_id=1,
                          site_id=1, zone_id=1, env_id=1, status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor=REQUESTER)
        yield session
    engine.dispose()


def _persisted_success(db, instance_id, **kwargs):
    incident = DiagIncident(
        incident_no=f"INC-TEST-{len(db.query(DiagIncident).all()) + 1}",
        instance_id=instance_id,
        db_type="oracle",
        instance_name="oracle-10",
        title=kwargs["title"],
        trigger_source=kwargs["trigger_source"],
        status="root_caused",
        severity="warning",
        primary_layer="database",
        root_cause_summary="连接池耗尽是当前首要根因假设",
        evidence_count=1,
        root_cause_count=1,
        started_at=datetime.now(),
        root_caused_at=datetime.now(),
        ai_result={
            "execution_status": "success",
            "data_quality": "good",
            "conclusion_status": "hypotheses_require_validation",
        },
    )
    db.add(incident)
    db.flush()
    db.add(DiagEvidence(
        incident_id=incident.id,
        source="manual_symptom",
        phenomenon=kwargs["symptom"],
        raw={
            "symptom": kwargs["symptom"],
            "intent_summary": kwargs["intent_summary"],
            "intent_signals": kwargs["intent_signals"],
            "recommended_cards": kwargs["recommended_cards"],
        },
    ))
    db.add(DiagRootCause(
        incident_id=incident.id,
        cause=incident.root_cause_summary,
        layer="database",
        rank=1,
        is_primary=1,
    ))
    db.commit()
    return {"id": incident.id}


def _persisted_failure(db, instance_id, **kwargs):
    incident = DiagIncident(
        incident_no="INC-TEST-FAILED",
        instance_id=instance_id,
        db_type="oracle",
        instance_name="oracle-10",
        title=kwargs["title"],
        trigger_source=kwargs["trigger_source"],
        status="failed",
        severity="unknown",
        root_cause_summary="诊断流程异常：synthetic collector failure",
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
    return {"id": incident.id}


def _approved(db):
    created = plans.create_plan(
        db,
        actor=REQUESTER,
        capability_id=CAPABILITY,
        definition_version=1,
        instance_id=10,
        params=PARAMS,
    )
    governance.request_approval(db, created["plan_id"], actor=REQUESTER,
                                expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"],
                            expected_binding_hash=row.approval_binding_hash)
    return created


def test_root_cause_contract_is_strict_and_approval_gated():
    entry = next(item for item in CAPABILITY_DECLARATIONS if item["capability_id"] == CAPABILITY)
    schema = capability_handler.PARAM_SCHEMA_JSON["diagnosis.root_cause"]
    assert entry["kind"] == "collect" and entry["requires_instance"] is True
    assert entry["output_schema"] == "diagnosis.root_cause_result/v1"
    assert registry.EFFECT_HANDLERS[CAPABILITY] == "diagnosis.root_cause"
    assert schema["required"] == ["symptom", "time_window", "impact_scope"]
    assert schema["additionalProperties"] is False


def test_approved_plan_runs_orchestrator_and_publishes_verified_incident(db, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return _persisted_success(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_diagnosis", run)
    created = _approved(db)
    assert calls == []
    fences = []
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER,
                                  execution_guard=lambda: fences.append("fence"))

    assert finished["outcome"] == "succeeded", finished
    ref = finished["steps"][0]["result_ref"]
    assert ref["kind"] == "capability_effect"
    assert ref["schema"] == "diagnosis.root_cause_result/v1"
    assert ref["inline"]["incident_id"] == db.query(DiagIncident.id).scalar()
    assert ref["inline"]["sampling"] == {
        "requested_time_window": PARAMS["time_window"],
        "impact_scope": PARAMS["impact_scope"],
        "collection_mode": "current_snapshot",
        "historical_replay_performed": False,
    }
    assert calls[0][1]["trigger_source"].startswith("capability:")
    assert PARAMS["time_window"] in calls[0][1]["intent_summary"]
    assert calls[0][1].get("create_ticket") is None and calls[0][1].get("notify") is None
    assert len(fences) >= 2


def test_incident_not_bound_to_execution_is_rejected(db, monkeypatch):
    def wrong_identity(_db, instance_id, **kwargs):
        kwargs["trigger_source"] = "manual"
        return _persisted_success(_db, instance_id, **kwargs)

    monkeypatch.setattr(orchestrator, "run_diagnosis", wrong_identity)
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER,
                                  execution_guard=lambda: None)
    assert finished["outcome"] == "unknown"
    assert finished["outcome_kind"] == "result_unknown"
    assert finished["steps"][0]["result_ref"] is None


def test_early_failure_keeps_a_verified_incident_and_customer_context(db, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_diagnosis", _persisted_failure)
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER,
                                  execution_guard=lambda: None)
    assert finished["outcome"] == "failed"
    assert finished["outcome_kind"] == "diagnosis_failed"
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["incident_id"] == db.query(DiagIncident.id).scalar()
    manual = db.query(DiagEvidence).filter_by(
        incident_id=inline["incident_id"], source="manual_symptom").one()
    assert manual.raw["symptom"] == PARAMS["symptom"]
    assert PARAMS["time_window"] in manual.raw["intent_summary"]


def test_missing_customer_context_is_rejected_before_execution(db):
    with pytest.raises((plans.PlanError, registry.CapabilityError)):
        plans.create_plan(
            db,
            actor=REQUESTER,
            capability_id=CAPABILITY,
            definition_version=1,
            instance_id=10,
            params={"symptom": "连接失败"},
        )
