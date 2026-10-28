"""Offline regressions for the diagnosis entrypoints reviewed on 2026-10-15."""
import json
import socket
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai as api, operations
from app.core.database import Base, get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.ai_report import AIReport
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagArchProfile, DiagEvidence, DiagIncident
from app.models.oracle_awr import AwrReport
from app.services import ai_config, ai_evidence, operation_ai
from app.services.diagnosis import evidence_chain, orchestrator, rca_engine


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Diagnosis entrypoint regressions must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(ai_config.requests.sessions.Session, "request", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="canonical", db_type="oracle", host="fixture.invalid",
                               port=1521, username="fixture", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


def client(db):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(operations.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def row(status, **extra):
    return NS(**{"check_category": "database", "check_item": "pressure", "check_status": status,
                 "metric_name": "pressure", "metric_value": "90", "threshold_value": "80",
                 "finding": "Synthetic measured pressure", "suggestion": "Verify", "raw_data": {}, **extra})


def provider(db):
    db.add(AIProviderConfig(provider_name="synthetic", provider_type="ollama", base_url="http://model.invalid",
                           model_name="synthetic", enabled=1, is_default=1, timeout_seconds=1))
    db.commit()


@pytest.mark.parametrize("statuses,risk,quality,status", [
    ([], "unknown", "missing", "failed"),
    (["failed"], "unknown", "missing", "failed"),
    (["normal", "failed"], "unknown", "partial", "partial"),
    (["critical", "failed"], "critical", "partial", "partial"),
    (["normal"], "normal", "good", "success"),
    (["critical"], "critical", "good", "success"),
])
def test_legacy_route_preserves_collection_truth_in_response_and_report(db, monkeypatch, statuses, risk, quality, status):
    rows = [row(value) for value in statuses]
    monkeypatch.setattr(operation_ai.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("synthetic-task", rows))
    monkeypatch.setattr(operation_ai, "generate_markdown_report", lambda *_: "Synthetic inspection")
    response = client(db).post("/operations/oracle/1/inspect-ai")
    assert response.status_code == 200, response.text
    result = response.json()
    saved = db.get(AIReport, result["ai_report_id"])
    assert result["status"] == saved.diagnosis["status"] == status
    assert saved.risk_level == result["local_ai_result"]["risk_level"] == risk
    assert saved.diagnosis["local_rule_result"]["data_quality"] == quality
    assert saved.diagnosis["local_rule_result"] == result["local_ai_result"]


@pytest.mark.parametrize("output", ["Unsupported unreferenced assertion", "{", "", json.dumps({
    "summary": "Synthetic", "risk_level": "normal", "evidence_refs": ["metric-0"], "root_causes": [],
    "recommendations": ["Verify"], "missing_evidence": []})])
def test_legacy_route_rejects_invalid_or_downgraded_model_result(db, monkeypatch, output):
    provider(db)
    monkeypatch.setattr(operation_ai.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("synthetic-task", [row("critical")]))
    monkeypatch.setattr(operation_ai, "generate_markdown_report", lambda *_: "Synthetic critical inspection")
    monkeypatch.setattr(ai_config, "call_ai_provider", lambda *_: output)
    result = client(db).post("/operations/oracle/1/inspect-ai").json()
    saved = db.get(AIReport, result["ai_report_id"])
    assert result["status"] == "partial" and result["analysis_status"] == "failed"
    assert saved.risk_level == "critical"
    assert result["ai_model_result"] is None and saved.diagnosis["ai_model_result"] is None
    assert result["ai_inference"]["validation_status"] == "failed"


def test_legacy_failure_does_not_call_model(db, monkeypatch):
    provider(db)
    monkeypatch.setattr(operation_ai.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("synthetic-task", [row("failed")]))
    monkeypatch.setattr(operation_ai, "generate_markdown_report", lambda *_: "Synthetic connection failed")
    call = Mock(side_effect=AssertionError("Failed collection is not model evidence"))
    monkeypatch.setattr(ai_config, "call_ai_provider", call)
    result = client(db).post("/operations/oracle/1/inspect-ai").json()
    assert result["status"] == "failed" and result["ai_error"] == "no_supporting_evidence"
    call.assert_not_called()


@pytest.mark.parametrize("timeout", [False, True])
def test_legacy_success_and_timeout_keep_model_provenance_separate(db, monkeypatch, timeout):
    provider(db)
    monkeypatch.setattr(operation_ai.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("synthetic-task", [row("warning")]))
    monkeypatch.setattr(operation_ai, "generate_markdown_report", lambda *_: "Synthetic inspection")
    output = ai_config.AIProviderText(json.dumps({
        "summary": "Observed pressure", "risk_level": "warning", "evidence_refs": ["metric-0"],
        "root_causes": [], "recommendations": ["Verify"], "missing_evidence": [],
    }), {"actual_model": "synthetic-revision", "finish_reason": "stop"})
    call = Mock(side_effect=TimeoutError("offline") if timeout else None, return_value=output)
    monkeypatch.setattr(ai_config, "call_ai_provider", call)
    result = client(db).post("/operations/oracle/1/inspect-ai").json()
    saved = db.get(AIReport, result["ai_report_id"])
    assert saved.risk_level == "warning" and result["execution_status"] == "success"
    assert saved.diagnosis["ai_inference"] == result["ai_inference"]
    if timeout:
        assert result["status"] == "partial" and result["ai_error"] == "TimeoutError"
        assert result["ai_model_result"] is None
    else:
        assert result["status"] == "success" and result["analysis_status"] == "completed"
        assert result["ai_model_result"] == str(output)
        assert result["ai_inference"]["actual_model"] == "synthetic-revision"


@pytest.mark.parametrize("instance_id,db_type,expected", [(1, "mysql", 400), (99, "oracle", 404), (0, "oracle", 404)])
def test_general_api_rejects_wrong_or_missing_identity_before_collection(db, monkeypatch, instance_id, db_type, expected):
    collect = Mock(side_effect=AssertionError("Identity must be validated first"))
    monkeypatch.setattr("app.services.inspection.dispatch.run_collecting_rows", collect)
    response = client(db).post("/ai/diagnose-ai", json={"instance_id": instance_id, "instance": "wrong", "db_type": db_type})
    assert response.status_code == expected
    assert db.query(AIReport).count() == 0
    collect.assert_not_called()


def test_general_api_uses_canonical_name_and_rejects_disabled_instance(db, monkeypatch):
    collect = Mock(return_value=("synthetic-task", [row("normal")]))
    monkeypatch.setattr("app.services.inspection.dispatch.run_collecting_rows", collect)
    result = client(db).post("/ai/diagnose-ai", json={"instance_id": 1, "instance": "old-name", "db_type": "oracle"}).json()
    saved = db.get(AIReport, result["ai_report_id"])
    assert saved.title == "AI诊断 - canonical" and saved.db_type == "oracle"
    db.get(DBInstance, 1).enabled = 0
    db.commit()
    assert client(db).post("/ai/diagnose-ai", json={"instance_id": 1, "instance": "canonical", "db_type": "oracle"}).status_code == 400
    assert collect.call_count == 1


def pipeline(db, monkeypatch, rows):
    profile = DiagArchProfile(instance_id=1, arch_type="standalone", arch_tags=[], priority_template={}, details={})
    db.add(profile)
    db.commit()
    monkeypatch.setattr(orchestrator.arch_profiler, "profile_architecture", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(orchestrator.inspection_dispatch, "run_collecting_rows", lambda *_a, **_k: ("synthetic-task", rows))
    monkeypatch.setattr(orchestrator.collectors_runner, "run_collectors", lambda *_: [])
    monkeypatch.setattr("app.services.suggestion_aggregator.rebuild_suggestions", lambda *_: None)


@pytest.mark.parametrize("statuses,symptom", [([], None), (["failed"], None), ([], "Synthetic query timeout")])
def test_empty_failed_or_manual_only_pipeline_never_claims_health_or_location(db, monkeypatch, statuses, symptom):
    pipeline(db, monkeypatch, [row(status) for status in statuses])
    result = orchestrator.run_diagnosis(db, 1, symptom=symptom)
    saved = db.get(DiagIncident, result["id"])
    assert result["score"] is None and saved.score is None
    assert result["severity"] == "unknown" and result["status"] == "diagnosing"
    assert result["root_cause_count"] == 0 and result["root_caused_at"] is None
    assert result["conclusion_status"] == "not_available"
    assert result["execution_status"] != "success"
    assert result["data_quality"] == "missing"
    listed, total = orchestrator.list_incidents(db)
    assert total == 1 and listed[0]["score"] is None
    assert listed[0]["execution_status"] == result["execution_status"]


def test_collected_normal_inspection_remains_usable_without_fabricated_root(db, monkeypatch):
    pipeline(db, monkeypatch, [row("normal")])
    result = orchestrator.run_diagnosis(db, 1)
    assert result["severity"] == "normal" and result["execution_status"] == "success"
    assert result["score"] == 100 and result["root_cause_count"] == 0
    assert result["root_caused_at"] is None


def test_inspection_rollback_preserves_current_risk_and_partial_truth(db, monkeypatch):
    pipeline(db, monkeypatch, [])
    awr(db, datetime.now() - timedelta(minutes=10))
    monkeypatch.setattr(orchestrator.inspection_dispatch, "run_collecting_rows", Mock(side_effect=RuntimeError("Synthetic connection failed")))
    result = orchestrator.run_diagnosis(db, 1)
    saved = db.get(DiagIncident, result["id"])
    assert saved is not None and result["severity"] == "critical"
    assert result["execution_status"] == "partial" and result["data_quality"] == "partial"
    assert result["score"] is None
    assert saved.ai_result["collection_failures"][0]["source"] == "inspection"


def test_failed_collector_cannot_be_hidden_by_healthy_inspection(db, monkeypatch):
    pipeline(db, monkeypatch, [row("normal")])
    monkeypatch.setattr(orchestrator.collectors_runner, "run_collectors", Mock(side_effect=RuntimeError("Synthetic source failed")))
    result = orchestrator.run_diagnosis(db, 1)
    assert result["execution_status"] == "partial" and result["severity"] == "unknown"
    assert result["score"] is None and result["root_cause_count"] == 0


@pytest.mark.parametrize("raw", [{"execution_status": "failed"}, {"error": "Synthetic query failed"},
                                  {"polarity": "counter"}, {"source": "manual_symptom"}])
def test_non_supporting_inspection_cannot_be_laundered_through_real_rule_conversion(db, monkeypatch, raw):
    pipeline(db, monkeypatch, [row("critical", raw_data=raw)])
    result = orchestrator.run_diagnosis(db, 1)
    assert result["root_cause_count"] == 0 and result["score"] is None
    assert result["status"] == "diagnosing" and result["severity"] == "unknown"
    assert all(value["source"] != "inspection" for value in result["evidences"])


@pytest.mark.parametrize("source,raw", [("manual_symptom", {}), ("inspection", {"execution_status": "failed"}),
                                       ("inspection", {"polarity": "counter"})])
def test_non_supporting_items_cannot_be_ranked_as_root_cause(db, source, raw):
    evidence = DiagEvidence(incident_id=1, layer="sql", source=source, raw=raw, weight=1.0,
                           severity="critical", phenomenon="Synthetic context", evidence="Synthetic text")
    assert rca_engine.rank_root_causes(db, 1, [evidence], None) == []


T0 = datetime(2026, 10, 15, 10)


def awr(db, end, *, begin=None, **extra):
    report = AwrReport(instance_id=1, begin_time=begin or end - timedelta(minutes=30), end_time=end,
        status="analyzed", performance_score=50, analysis_results={
            "wait_analysis": {"top_events": [{"event_name": "log file sync", "db_time_pct": 50}]},
            "sql_analysis": {"top_elapsed_time_sql": [{"sql_id": "synthetic", "elapsed_time": 90, "executions": 1}]},
        }, **extra)
    db.add(report)
    db.commit()
    return report


@pytest.mark.parametrize("delta", [-3601, 1, -86400])
def test_awr_outside_current_observation_window_is_not_diagnostic_evidence(db, delta):
    awr(db, T0 + timedelta(seconds=delta))
    assert evidence_chain._from_awr(db, db.get(DBInstance, 1), as_of=T0) == []
    assert evidence_chain._from_awr_top_sql(db, db.get(DBInstance, 1), as_of=T0) == []


def test_awr_uses_observation_time_not_import_id_and_preserves_window(db):
    current = awr(db, T0 - timedelta(minutes=10))
    awr(db, T0 - timedelta(days=10))
    nodes = evidence_chain._from_awr(db, db.get(DBInstance, 1), as_of=T0)
    assert len(nodes) == 1 and nodes[0]["raw"]["awr_report_id"] == current.id
    assert nodes[0]["raw"]["observation_window"]["end_time"] == current.end_time.isoformat()
    assert evidence_chain._from_awr_top_sql(db, db.get(DBInstance, 1), as_of=T0)


def test_awr_without_time_or_with_reversed_time_is_not_supporting(db):
    report = awr(db, T0, begin=T0 + timedelta(minutes=1))
    assert evidence_chain._from_awr(db, db.get(DBInstance, 1), as_of=T0) == []
    report.begin_time = None
    db.commit()
    assert evidence_chain._from_awr_top_sql(db, db.get(DBInstance, 1), as_of=T0) == []


def test_historical_awr_remains_visible_but_cannot_inflate_current_risk(db):
    awr(db, datetime(2000, 1, 2))
    section = ai_evidence._awr_evidence(db, db.get(DBInstance, 1), 20)
    item = section["items"][0]
    assert item["historical_severity"] == "warning" and item["severity"] == "unknown"
    assert not ai_evidence.is_supporting_evidence(item)
    result = ai_evidence.build_local_conclusion({"sections": [section]})
    assert result["risk_level"] == "unknown" and result["data_quality"] == "missing"


def test_historical_awr_does_not_reduce_current_observation_completeness(db):
    current = awr(db, datetime.now() - timedelta(minutes=10))
    current.performance_score = 95
    db.commit()
    awr(db, datetime(2000, 1, 2))
    section = ai_evidence._awr_evidence(db, db.get(DBInstance, 1), 20)
    assert len(section["items"]) == 2
    result = ai_evidence.build_local_conclusion({"sections": [section]})
    assert result["risk_level"] == "normal" and result["data_quality"] == "good"
    assert result["execution_status"] == "success"


@pytest.mark.parametrize("limit,history_count", [(1, 1), (20, 25)])
def test_evidence_diagnose_prioritizes_current_awr_before_history_limit(db, limit, history_count):
    current = awr(db, datetime.now() - timedelta(minutes=10))
    current.performance_score = 95
    db.commit()
    history = [awr(db, datetime(2000, 1, 2) + timedelta(days=index)) for index in range(history_count)]
    payload = {"instance_id": 1}
    if limit != 20:
        payload["limit"] = limit
    response = client(db).post("/ai/evidence-diagnose", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    saved = db.get(AIReport, result["ai_report_id"])
    assert saved.risk_level == result["local_result"]["risk_level"] == "normal"
    assert saved.diagnosis["local_result"] == result["local_result"]
    assert result["local_result"]["execution_status"] == "success"
    assert result["local_result"]["data_quality"] == "good"
    section = next(section for section in result["evidence_chain"]["sections"] if section["key"] == "awr")
    items = section["items"]
    assert len(items) == limit
    assert [item["raw_data"]["awr_report_id"] for item in items] == [
        current.id, *[report.id for report in reversed(history)][:limit - 1],
    ]
    assert not items[0]["context_only"] and ai_evidence.is_supporting_evidence(items[0])
    assert all(item["context_only"] and item["severity"] == "unknown"
               and not ai_evidence.is_supporting_evidence(item) for item in items[1:])
    assert saved.diagnosis["evidence_chain"] == result["evidence_chain"]


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4, 5, 20])
def test_awr_current_observation_order_and_history_fill_share_one_limit(db, limit):
    now = datetime.now()
    latest = awr(db, now - timedelta(minutes=5))
    older = awr(db, now - timedelta(minutes=20))
    latest_tie = awr(db, latest.end_time)
    history = [awr(db, datetime(2000, 1, 3)), awr(db, datetime(2000, 1, 2))]
    section = ai_evidence._awr_evidence(db, db.get(DBInstance, 1), limit)
    assert set(section) == {"key", "title", "items"}
    assert section["key"] == "awr"
    expected = [latest_tie.id, latest.id, older.id, history[1].id, history[0].id][:limit]
    assert [item["raw_data"]["awr_report_id"] for item in section["items"]] == expected
    assert len(section["items"]) <= limit


@pytest.mark.parametrize("invalid", ["missing_time", "reversed_time", "future", "failed", "unmeasured"])
def test_invalid_newer_awr_does_not_displace_current_support_at_limit_one(db, invalid):
    now = datetime.now()
    current = awr(db, now - timedelta(minutes=10))
    newer = awr(db, now - timedelta(minutes=1))
    if invalid == "missing_time":
        newer.begin_time = None
    elif invalid == "reversed_time":
        newer.begin_time = newer.end_time + timedelta(minutes=1)
    elif invalid == "future":
        newer.end_time = now + timedelta(minutes=10)
    elif invalid == "failed":
        newer.status = "failed"
    else:
        newer.performance_score = None
    db.commit()
    items = ai_evidence._awr_evidence(db, db.get(DBInstance, 1), 1)["items"]
    assert len(items) == 1 and items[0]["raw_data"]["awr_report_id"] == current.id
    assert ai_evidence.is_supporting_evidence(items[0])
