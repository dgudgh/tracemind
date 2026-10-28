"""Offline regressions through real inspection/signal evidence conversions."""

import json
import os
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1 import ai as api
from app.core.database import Base
from app.models.ai_provider_config import AIProviderConfig
from app.models.ai_report import AIReport
from app.models.check_result import DBCheckResult
from app.models.diagnosis_center import DiagEvidence
from app.models.db_instance import DBInstance
from app.models.inspection_diagnosis import DBDiagnosisFinding, DBInspectionTask
from app.schemas.ai import AIDiagnosisRequest
from app.services import ai_config, ai_evidence
from app.services.diagnosis import ai_orchestrator, collectors_runner, evidence_chain


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real external actions forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(ai_config.requests.sessions.Session, "request", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(os, "system", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="offline", db_type="oracle", host="127.0.0.1",
                               port=1521, username="fixture", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


def add_provider(db):
    db.add(AIProviderConfig(provider_name="offline", provider_type="ollama",
                            base_url="http://127.0.0.1:11434", model_name="fixture",
                            enabled=1, is_default=1))
    db.commit()


def add_task(db, risk="unknown", score=None):
    task = DBInspectionTask(task_id="truth-task", instance_id=1, db_type="oracle", risk_level=risk,
                            overall_score=score, summary="Collection outcome retained for review")
    db.add(task)
    db.commit()
    return task


def add_check(db, status="critical", raw=None, **changes):
    values = dict(task_id="truth-task", instance_id=1, db_type="oracle", check_status=status,
                  check_item="Disk usage", metric_name="disk_usage", metric_value="99.2",
                  finding="Measured data disk usage", raw_data=raw or {})
    values.update(changes)
    row = DBCheckResult(**values)
    db.add(row)
    db.commit()
    return row


def evidence_diagnose(db):
    result = api.ai_evidence_diagnose(api.EvidenceDiagnosisRequest(instance_id=1), db)
    saved = db.query(AIReport).order_by(AIReport.id.desc()).first()
    assert saved.risk_level == result["local_result"]["risk_level"]
    assert saved.diagnosis["local_result"] == result["local_result"]
    return result


@pytest.mark.parametrize("risk,score,expected", [("unknown", None, "unknown"),
                                               ("normal", None, "unknown"),
                                               ("critical", None, "critical")])
def test_real_task_conversion_never_promotes_unmeasured_task(db, monkeypatch, risk, score, expected):
    task = add_task(db, risk, score)
    add_provider(db)
    model = Mock(side_effect=AssertionError("No measured supporting evidence"))
    monkeypatch.setattr(ai_config, "call_ai_provider", model)
    chain = ai_evidence.build_evidence_chain(db, 1)
    item = chain["sections"][0]["items"][0]
    assert item["raw_data"]["summary"] == task.summary
    assert item["raw_data"]["task_id"] == task.task_id
    assert item["execution_status"] == "unknown"
    assert not ai_evidence.is_supporting_evidence(item)
    result = evidence_diagnose(db)
    assert result["local_result"]["risk_level"] == expected
    assert result["local_result"]["data_quality"] != "good"
    assert not result["local_result"]["root_causes"]
    assert result["ai_error"] == "no_supporting_evidence"
    model.assert_not_called()


@pytest.mark.parametrize("status", ["warning", "critical"])
def test_derived_partial_keeps_known_risk_through_both_api_paths(db, monkeypatch, status):
    raw = {"items": [{"disk": "data", "used_pct": 99.2}, {"error": "other disk probe failed"}]}
    row = add_check(db, status, raw)
    add_provider(db)
    model = Mock(side_effect=AssertionError("Partial row cannot be cited as complete evidence"))
    monkeypatch.setattr(ai_config, "call_ai_provider", model)
    monkeypatch.setattr("app.services.inspection.dispatch.run_collecting_rows", lambda *_a, **_k: (row.task_id, [row]))
    general = api.ai_diagnose_with_model(AIDiagnosisRequest(instance_id=1, instance="offline", db_type="oracle"), db)
    chain = evidence_diagnose(db)
    for result in (general, chain):
        assert result["local_result"]["risk_level"] == status
        assert result["local_result"]["execution_status"] == "partial"
        assert result["local_result"]["data_quality"] == "partial"
        assert result["ai_model_result"] is None
    assert all(report.risk_level == status for report in db.query(AIReport))
    model.assert_not_called()


@pytest.mark.parametrize("source", ["check", "finding"])
def test_failed_inspection_raw_data_survives_actual_chain(db, source):
    raw = {"execution_status": "failed", "error": "query timed out", "sql": "fixture query"}
    if source == "check":
        add_check(db, raw=raw)
    else:
        add_task(db)
        db.add(DBDiagnosisFinding(task_id="truth-task", instance_id=1, db_type="oracle",
                                  check_item="Failed probe", severity="critical", finding="query unavailable",
                                  raw_data=raw))
        db.commit()
    chain = ai_evidence.build_evidence_chain(db, 1)
    item = next(item for section in chain["sections"] for item in section["items"]
                if item.get("raw_data", {}).get("error"))
    assert item["raw_data"] == raw
    assert ai_evidence.evidence_state(item) == ("unknown", "failed")
    result = evidence_diagnose(db)
    assert result["local_result"]["risk_level"] == "unknown"
    assert result["local_result"]["execution_status"] == "failed"
    assert result["local_result"]["root_causes"] == []


def test_good_critical_and_failed_task_remain_partial_critical(db, monkeypatch):
    add_task(db)
    add_check(db)
    add_provider(db)
    model_text = json.dumps({"summary": "Measured critical risk; collection incomplete", "risk_level": "critical",
                             "evidence_refs": ["section-0-item-1"], "root_causes": [],
                             "recommendations": ["Verify disk pressure"], "missing_evidence": ["Other checks failed"]})
    monkeypatch.setattr(ai_config, "call_ai_provider", Mock(return_value=model_text))
    result = evidence_diagnose(db)
    assert result["local_result"]["risk_level"] == "critical"
    assert result["local_result"]["data_quality"] == "partial"
    assert result["ai_model_result"] == model_text
    assert set(result["ai_inference"]["evidence_manifest"]) == {"section-0-item-1"}


def test_complete_measured_task_still_has_normal_success(db):
    add_task(db, "normal", 100)
    result = evidence_diagnose(db)
    assert result["local_result"]["risk_level"] == "normal"
    assert result["local_result"]["execution_status"] == "success"
    assert result["local_result"]["data_quality"] == "good"


def failed_signal_evidence(db, monkeypatch):
    source = SimpleNamespace(source_type="redis", name="offline-source", id=7)
    monkeypatch.setattr(collectors_runner, "list_sources", lambda *_: [source])
    monkeypatch.setattr(collectors_runner, "_context", lambda *_: None)
    monkeypatch.setitem(collectors_runner.REGISTRY, "redis", Mock(side_effect=TimeoutError("fixture timeout")))
    signals = collectors_runner.run_collectors(db, db.get(DBInstance, 1))
    node = evidence_chain._from_signals(signals)[0]
    row = DiagEvidence(incident_id=1, **node)
    db.add(row)
    db.commit()
    db.refresh(row)
    return evidence_chain.to_dict(row)


def test_real_collector_error_survives_storage_and_is_not_support(db, monkeypatch):
    add_provider(db)
    evidence = failed_signal_evidence(db, monkeypatch)
    assert evidence["raw"]["error"] == "fixture timeout"
    assert evidence["raw"]["data_source_id"] == 7
    assert evidence["metric_name"] == "collector_error"
    assert evidence["execution_status"] == "failed"
    assert ai_orchestrator._supporting_evidence_ids([evidence]) == set()
    model = Mock(side_effect=AssertionError("Failed collection is not a supporting fact"))
    monkeypatch.setattr(ai_orchestrator, "call_ai_provider", model)
    result = ai_orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, [evidence],
                                        [{"cause": "unsupported memory pressure"}], "warning")
    assert result["tier"] == "local" and result["fallback_reason"] == "no_supporting_evidence"
    assert "unsupported memory pressure" not in result["summary"]
    model.assert_not_called()


@pytest.mark.parametrize("reference,missing,valid", [("e1", ["failed collection"], False),
                                                    ("e2", [], False),
                                                    ("e2", ["failed collection"], True)])
def test_mixed_real_collector_error_cannot_be_borrowed_as_model_support(db, monkeypatch, reference, missing, valid):
    add_provider(db)
    failed = failed_signal_evidence(db, monkeypatch)
    measured = DiagEvidence(incident_id=1, layer="database", source="inspection", severity="critical",
                            phenomenon="Disk measured at 99.2 percent", evidence="disk usage=99.2",
                            metric_name="disk_usage", metric_value="99.2", raw={"execution_status": "success"})
    db.add(measured)
    db.commit()
    evidence = [failed, evidence_chain.to_dict(measured)]
    text = json.dumps({"summary": "Critical disk pressure", "risk_level": "critical", "evidence_refs": [reference],
                       "root_causes": [{"cause": "Disk pressure", "evidence_refs": [reference]}],
                       "recommendations": ["Inspect disk"], "missing_evidence": missing})
    model = Mock(return_value=text)
    monkeypatch.setattr(ai_orchestrator, "call_ai_provider", model)
    result = ai_orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, evidence, [], "critical")
    assert ai_orchestrator._supporting_evidence_ids(evidence) == {"e2"}
    assert (result["tier"] == "private") is valid
    assert result["attempts"][0]["ok"] is valid
    assert model.call_count == 1


def test_legacy_collector_error_without_raw_or_execution_status_is_not_support():
    row = {"source": "redis", "metric_name": "collector_error", "metric_value": "0",
           "phenomenon": "Collector failed", "severity": "warning"}
    assert ai_orchestrator._supporting_evidence_ids([row]) == set()
