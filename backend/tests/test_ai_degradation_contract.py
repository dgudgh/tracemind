"""C12: one degradation vocabulary; an unavailable model never changes the local truth."""
import socket
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import diagnosis_center, mysql_ops, oracle_awr
from app.core.database import Base, get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_preparation import DBCapabilityPreparation
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagArchProfile, DiagIncident
from app.models.oracle_awr import AwrReport
from app.services import ai_config, ai_degradation
from app.services.diagnosis import ai_orchestrator, orchestrator
from test_assistant_api import api as assistant_api, signing_key_path  # noqa: F401

MARKER = "MARKER-9f3"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("C12 regressions must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="c12", db_type="oracle", host="fixture.invalid",
                               port=1521, username="fixture", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


def serve(db, router):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def provider(db):
    db.add(AIProviderConfig(provider_name="c12-model", provider_type="ollama", base_url="http://model.invalid",
                            model_name="c12", enabled=1, is_default=1, timeout_seconds=1))
    db.commit()


def raising(marker):
    def call(*args, **kwargs):
        raise TimeoutError(f"provider timeout {marker}")
    return call


@pytest.fixture
def workflow_api(assistant_api):
    for model in (DBCapabilityPlan, DBCapabilityPreparation, AIProviderConfig):
        model.__table__.create(assistant_api.engine, checkfirst=True)
    return assistant_api


def test_workflow_entrypoints_still_work_when_no_model_exists(workflow_api, monkeypatch):
    api = workflow_api
    with api.factory() as db:
        assert db.query(AIProviderConfig).count() == 0
    calls = []

    def denied(*args, **kwargs):
        calls.append(args)
        raise AssertionError("no model egress without a configured model")

    monkeypatch.setattr(ai_config, "call_ai_provider", denied)
    monkeypatch.setattr(ai_config.requests, "post", denied)

    matched = api.request("POST", "/workflows/match", json={"text": "查看巡检任务", "matcher_version": 1})
    assert matched.status_code == 200, matched.text
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 0

    prepared = api.request("POST", "/workflows/inspection.jobs.list/versions/1/prepare", user=2,
                           headers={"Idempotency-Key": "c12-prepare"},
                           json={"operation": "prepare", "slots": {},
                                 "scope": {"mode": "explicit", "targets": [{"instance_id": 10}]}})
    assert prepared.status_code == 200, prepared.text
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 1

    created = api.request("POST", "/plans", json={"capability_id": "inspection.run.query",
                                                 "definition_version": 1})
    assert created.status_code == 201, created.text
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
    assert calls == []


def test_missing_default_config_is_not_configured_not_a_failed_call(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_config, "call_ai_provider", lambda *args, **kwargs: calls.append(args))
    response = serve(db, diagnosis_center.router).post(
        "/diagnosis-center/route-intent", json={"text": "awr 报告怎么看"})
    assert response.status_code == 200, response.text
    assert response.json()["degradation"] == {"status": ai_degradation.AI_NOT_CONFIGURED,
                                             "local_result_available": True, "reason_code": None}
    assert calls == []


def test_model_timeout_never_downgrades_the_incident_verdict(db, monkeypatch):
    provider(db)
    profile = DiagArchProfile(instance_id=1, arch_type="standalone", arch_tags=[], priority_template={}, details={})
    db.add(profile)
    db.commit()
    rows = [NS(check_category="database", check_item="pressure", check_status="critical",
               metric_name="pressure", metric_value="90", threshold_value="80",
               finding="Synthetic measured pressure", suggestion="Verify", raw_data={})]
    monkeypatch.setattr(orchestrator.arch_profiler, "profile_architecture", lambda *_a, **_k: profile)
    monkeypatch.setattr(orchestrator.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("c12", rows))
    monkeypatch.setattr(orchestrator.collectors_runner, "run_collectors", lambda *_: [])
    monkeypatch.setattr("app.services.suggestion_aggregator.rebuild_suggestions", lambda *_: None)
    monkeypatch.setattr(ai_orchestrator, "call_ai_provider", raising(MARKER))

    result = orchestrator.run_diagnosis(db, 1)
    saved = db.get(DiagIncident, result["id"])
    assert result["ai_result"]["tier"] == "local"
    assert result["ai_result"]["degradation"]["status"] == ai_degradation.AI_MODEL_FAILED
    assert saved.status != "failed"
    assert result["severity"] != "unknown"


def test_route_intent_never_echoes_provider_exception_text(db, monkeypatch):
    provider(db)
    monkeypatch.setattr(ai_config, "call_ai_provider", raising(MARKER))
    response = serve(db, diagnosis_center.router).post(
        "/diagnosis-center/route-intent", json={"text": "awr 报告怎么看"})
    assert response.status_code == 200, response.text
    assert MARKER not in response.text
    body = response.json()
    assert body["degradation"]["status"] == ai_degradation.AI_MODEL_FAILED
    assert body["degradation"]["reason_code"] == "provider_unavailable"


def test_awr_analysis_never_echoes_provider_exception_text(db, monkeypatch, tmp_path):
    provider(db)
    report_file = tmp_path / "awr.html"
    report_file.write_text("<html>AWR fixture</html>", encoding="utf-8")
    db.add(AwrReport(instance_id=1, begin_snap=1, end_snap=2, begin_time=datetime(2026, 10, 1, 0),
                     end_time=datetime(2026, 10, 1, 1), status="success", file_path=str(report_file)))
    db.commit()
    monkeypatch.setattr(oracle_awr, "collect_environment", lambda *_a, **_k: {})
    monkeypatch.setattr(oracle_awr, "get_cpu_risk_level", lambda *_a, **_k: {"level": "LOW", "score": 100})
    monkeypatch.setattr(oracle_awr, "get_environment_prompt", lambda *_a, **_k: "env")
    monkeypatch.setattr(ai_config, "call_ai_provider", raising(MARKER))

    response = serve(db, oracle_awr.router).post("/oracle/1/awr/analyze", json={"begin_snap": 1, "end_snap": 2})
    assert response.status_code == 200, response.text
    assert MARKER not in response.text
    assert response.json()["degradation"]["status"] == ai_degradation.AI_MODEL_FAILED


def test_mysql_slow_sql_ai_entry_point_is_rule_only(db, monkeypatch):
    center = {"summary": {"risk_level": "normal"}, "risk_items": [], "recommendations": ["检查索引选择性"]}
    monkeypatch.setattr(mysql_ops, "get_slow_sql_center", lambda *_a, **_k: center)

    response = serve(db, mysql_ops.operations_mysql_router).post("/operations/mysql/1/slow-sql/ai-analyze")
    assert response.status_code == 200, response.text
    assert "AI 分析将" not in response.text and "将基于" not in response.text
    body = response.json()
    assert body["status"] == ai_degradation.AI_NOT_CONFIGURED
    assert body["degradation"] == {"status": ai_degradation.AI_NOT_CONFIGURED,
                                  "local_result_available": True, "reason_code": "not_applicable"}
    assert body["result_source"] == "local_rules"
    assert body["recommendations"] == ["检查索引选择性"]


@pytest.mark.parametrize("kwargs", [
    {"status": ai_degradation.AI_MODEL_FAILED, "local_result_available": True, "reason_code": "随便写的"},
    {"status": "made_up", "local_result_available": True},
    {"status": ai_degradation.AI_MODEL_FAILED, "local_result_available": True, "reason_code": "boom MARKER-9f3"},
])
def test_vocabulary_rejects_free_text(kwargs):
    with pytest.raises(ValueError):
        ai_degradation.degradation(**kwargs)
    assert ai_degradation.degradation(ai_degradation.AI_MODEL_OK, local_result_available=True) == {
        "status": "ok", "local_result_available": True, "reason_code": None}


# --- C12: a degraded answer must hand over to a model-free path -----------------------------

def test_manual_handoff_is_a_closed_vocabulary_and_only_required_when_the_model_did_not_answer():
    assert ai_degradation.manual_for(
        ai_degradation.degradation(ai_degradation.AI_MODEL_OK, local_result_available=True),
        "diagnosis.evidence_review") == {"required": False, "step": None, "capability_id": None, "model_free": True}
    for status in (ai_degradation.AI_NOT_CONFIGURED, ai_degradation.AI_MODEL_FAILED,
                   ai_degradation.AI_OUTPUT_REJECTED, ai_degradation.AI_NOT_SENT):
        value = ai_degradation.manual_for(ai_degradation.degradation(status, local_result_available=True), "awr.report_read")
        assert value == {"required": True, "step": "awr.report_read", "capability_id": "performance.awr.query",
                         "model_free": True}
    for step in ("", "manual", "awr.report_read ", "diagnosis.evidence_review\n"):
        with pytest.raises(ValueError):
            ai_degradation.manual_handoff(step)
    with pytest.raises(ValueError):
        ai_degradation.manual_for({"status": "made_up"}, "awr.report_read")


def test_every_manual_step_points_at_a_registered_capability_not_a_dead_hint():
    """Removing a manual capability must fail here instead of stranding the user on a dead hint."""
    from app.services.capability_declarations import CAPABILITY_DECLARATIONS

    registered = {item["capability_id"] for item in CAPABILITY_DECLARATIONS}
    for step, capability_id in ai_degradation.MANUAL_STEPS.items():
        if capability_id is not None:
            assert capability_id in registered, (step, capability_id)
    # The two page-level reviews carry no capability on purpose: they read evidence already returned.
    assert ai_degradation.MANUAL_STEPS["diagnosis.evidence_review"] is None
    assert ai_degradation.MANUAL_STEPS["workflow.manual_draft"] is None


def test_a_route_intent_without_a_model_still_returns_local_routing_and_a_manual_step(db, monkeypatch):
    monkeypatch.setattr(ai_config, "call_ai_provider", lambda *args, **kwargs: pytest.fail("no model may be called"))
    body = serve(db, diagnosis_center.router).post(
        "/diagnosis-center/route-intent", json={"text": "awr 报告怎么看"}).json()
    assert body["degradation"]["status"] == ai_degradation.AI_NOT_CONFIGURED
    assert body["manual"]["required"] is True and body["manual"]["step"] == "diagnosis.evidence_review"
    assert body["manual"]["model_free"] is True
    # The local answer is still a real answer, not an empty placeholder.
    assert body["source"] == "local"


def test_a_failing_model_on_awr_hands_over_to_reading_the_report(db, monkeypatch, tmp_path):
    provider(db)
    report_file = tmp_path / "awr2.html"
    report_file.write_text("<html>AWR fixture</html>", encoding="utf-8")
    db.add(AwrReport(instance_id=1, begin_snap=3, end_snap=4, begin_time=datetime(2026, 10, 2, 0),
                     end_time=datetime(2026, 10, 2, 1), status="success", file_path=str(report_file)))
    db.commit()
    monkeypatch.setattr(oracle_awr, "collect_environment", lambda *_a, **_k: {})
    monkeypatch.setattr(oracle_awr, "get_cpu_risk_level", lambda *_a, **_k: {"level": "LOW", "score": 100})
    monkeypatch.setattr(oracle_awr, "get_environment_prompt", lambda *_a, **_k: "env")
    monkeypatch.setattr(ai_config, "call_ai_provider", raising(MARKER))

    body = serve(db, oracle_awr.router).post("/oracle/1/awr/analyze", json={"begin_snap": 3, "end_snap": 4}).json()
    assert body["degradation"]["status"] == ai_degradation.AI_MODEL_FAILED
    assert body["manual"] == {"required": True, "step": "awr.report_read",
                              "capability_id": "performance.awr.query", "model_free": True}
    # Local environment analysis survives the model failure.
    assert "environment" in body and body["cpu_risk"]["level"] == "LOW"


def test_the_rule_only_mysql_entry_publishes_the_manual_path_it_actually_serves(db, monkeypatch):
    center = {"summary": {"risk_level": "normal"}, "risk_items": [], "recommendations": []}
    monkeypatch.setattr(mysql_ops, "get_slow_sql_center", lambda *_a, **_k: center)
    body = serve(db, mysql_ops.operations_mysql_router).post("/operations/mysql/1/slow-sql/ai-analyze").json()
    assert body["manual"] == {"required": True, "step": "slow_sql.raw_list",
                              "capability_id": "performance.slow_sql.read", "model_free": True}


def test_the_diagnosis_orchestrator_keeps_the_local_verdict_and_adds_the_manual_step(db, monkeypatch):
    provider(db)
    profile = DiagArchProfile(instance_id=1, arch_type="standalone", arch_tags=[], priority_template={}, details={})
    db.add(profile)
    db.commit()
    rows = [NS(check_category="database", check_item="pressure", check_status="critical",
               metric_name="pressure", metric_value="90", threshold_value="80",
               finding="Synthetic measured pressure", suggestion="Verify", raw_data={})]
    monkeypatch.setattr(orchestrator.arch_profiler, "profile_architecture", lambda *_a, **_k: profile)
    monkeypatch.setattr(orchestrator.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("c12", rows))
    monkeypatch.setattr(orchestrator.collectors_runner, "run_collectors", lambda *_: [])
    monkeypatch.setattr("app.services.suggestion_aggregator.rebuild_suggestions", lambda *_: None)
    monkeypatch.setattr(ai_orchestrator, "call_ai_provider", raising(MARKER))

    result = orchestrator.run_diagnosis(db, 1)
    ai_result = result["ai_result"]
    assert ai_result["tier"] == "local" and ai_result["degradation"]["status"] == ai_degradation.AI_MODEL_FAILED
    assert ai_result["manual"]["required"] is True and ai_result["manual"]["capability_id"] is None
    assert ai_result["summary"] and result["severity"] != "unknown"
