import json
import socket
from contextlib import contextmanager
from copy import copy, deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai as api
from app.core.database import get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.ai_report import AIReport
from app.models.db_instance import DBInstance
from app.models.diagnosis_evaluation import (
    DiagnosisEvalAudit, DiagnosisEvalCase, DiagnosisEvalCaseResult, DiagnosisEvalDataset, DiagnosisEvalRun,
)
from app.schemas.ai import AIDiagnosisRequest
from app.services import ai_config, diagnosis_evaluation as evaluation
from app.services.diagnosis import ai_orchestrator as orchestrator


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("AI architecture regression must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(ai_config.requests.sessions.Session, "request", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (DBInstance, AIProviderConfig, AIReport, DiagnosisEvalDataset, DiagnosisEvalCase,
                  DiagnosisEvalRun, DiagnosisEvalCaseResult, DiagnosisEvalAudit):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="offline", db_type="oracle", host="127.0.0.1", port=1521,
                               username="fixture", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


def client(db):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def provider(db, **changes):
    config = AIProviderConfig(**{
        "provider_name": "offline-model", "provider_type": "ollama", "base_url": "http://127.0.0.1:11434",
        "model_name": "qwen3-local", "timeout_seconds": 20, "enabled": 1, "is_default": 1, **changes,
    })
    db.add(config)
    db.commit()
    return config


def conclusion(**changes):
    return json.dumps({
        "summary": "Observed pressure; verify cause.", "risk_level": "warning", "evidence_refs": ["e1"],
        "root_causes": [{"cause": "resource pressure", "evidence_refs": ["e1"]}],
        "recommendations": ["Collect samples"], "missing_evidence": ["time window not verified"], **changes,
    })


def row(status="normal", **changes):
    return NS(**{"check_category": "database", "check_item": "pressure", "check_status": status,
                 "metric_name": "pressure", "metric_value": "90", "threshold_value": "80",
                 "finding": "pressure measured", "suggestion": "collect samples", "raw_data": {}, **changes})


@pytest.mark.parametrize("metrics,risk,quality", [
    ({}, "unknown", "missing"),
    ({"checks": []}, "unknown", "missing"),
    ({"checks": [{"status": "failed"}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "not_collected"}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "normal"}]}, "normal", "good"),
    ({"checks": [{"status": "ok"}]}, "normal", "good"),
    ({"checks": [{"status": "warning"}]}, "warning", "good"),
    ({"checks": [{"check_status": "critical"}]}, "critical", "good"),
    ({"checks": [{"check_status": "normal"}, {"status": "failed"}]}, "unknown", "partial"),
    ({"checks": [{"check_status": "critical"}, {"status": "failed"}]}, "critical", "partial"),
    ({"checks": [{"check_status": "normal", "raw_data": {"execution_status": "failed"}}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "warning", "metric_name": "collector_status"}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "normal", "raw_data": {"items": [{"error": "failed"}]}}]}, "unknown", "missing"),
    ({"checks": [{"status": "success"}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "normal", "execution_status": "running"}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "normal", "raw_data": {"execution_status": "timed_out"}}]}, "unknown", "missing"),
    ({"checks": [{"check_status": "normal", "data_quality": "stale"}]}, "unknown", "missing"),
    ({"checks": [None, "bad"]}, "unknown", "missing"),
])
def test_rule_and_model_api_persist_truthful_input_status(db, metrics, risk, quality):
    payload = {"db_type": "oracle", "instance": "fixture", "metrics": metrics}
    direct = client(db).post("/ai/diagnose", json=payload)
    assert direct.status_code == 200
    assert direct.json()["risk_level"] == risk
    assert direct.json()["data_quality"] == quality
    response = client(db).post("/ai/diagnose-ai", json=payload)
    assert response.status_code == 200, response.text
    saved = db.query(AIReport).one()
    assert saved.risk_level == risk
    assert saved.diagnosis["local_result"] == response.json()["local_result"] == direct.json()


@pytest.mark.parametrize("rows,risk", [([], "unknown"), ([row("failed")], "unknown"),
                                        ([row("normal")], "normal"), ([row("critical")], "critical"),
                                        ([row("normal"), row("not_collected")], "unknown")])
def test_inspection_results_use_same_truth_gate(db, rows, risk):
    with patch("app.services.inspection.dispatch.run_collecting_rows", return_value=("offline-task", rows)):
        response = client(db).post("/ai/diagnose-ai", json={"instance_id": 1, "instance": "offline", "db_type": "oracle"})
    assert response.status_code == 200, response.text
    assert response.json()["local_result"]["risk_level"] == risk
    saved = db.query(AIReport).one()
    assert saved.diagnosis["local_result"] == response.json()["local_result"]
    assert saved.risk_level == risk
    if risk == "normal":
        assert "pressure" not in response.json()["local_result"]["summary"]


@pytest.mark.parametrize("status,risk", [("normal", "unknown"), ("critical", "critical")])
def test_inspection_and_model_failure_preserve_context(db, status, risk):
    provider(db)
    with patch("app.services.inspection.dispatch.run_collecting_rows", side_effect=RuntimeError("collection interrupted")), \
         patch.object(ai_config, "call_ai_provider", side_effect=TimeoutError("model offline")) as call:
        response = client(db).post("/ai/diagnose-ai", json={
            "instance_id": 1, "instance": "offline", "db_type": "oracle",
            "metrics": {"checks": [{"check_status": status}]},
        })
    result = response.json()
    assert response.status_code == 200, response.text
    assert result["local_result"]["risk_level"] == risk
    assert result["local_result"]["inspection_error"] == "collection interrupted"
    assert "collection interrupted" in call.call_args.args[1]
    assert result["ai_error"] == "TimeoutError"
    assert result["ai_inference"]["validation_status"] == "failed"
    saved = db.query(AIReport).one()
    assert saved.risk_level == risk
    assert saved.diagnosis["ai_error"] == result["ai_error"]


@pytest.mark.parametrize("raw", ["plain diagnosis", "", "{", "[]", '{"summary":"only"}',
                                  conclusion(evidence_refs=[]), conclusion(evidence_refs=["missing"]),
                                  conclusion(summary="  "), conclusion(risk_level="bogus"),
                                  conclusion(root_causes=[{"cause": "pressure", "evidence_refs": ["missing"]}])])
def test_general_diagnosis_rejects_unusable_or_unreferenced_output(raw):
    with pytest.raises(ValueError):
        ai_config.validate_diagnosis(raw, {"e1"})


def test_general_diagnosis_allows_supported_normal_without_inventing_root_cause():
    result = ai_config.validate_diagnosis(conclusion(risk_level="normal", root_causes=[]), {"e1"})
    assert result["root_causes"] == []
    with pytest.raises(ValueError, match="downgraded"):
        ai_config.validate_diagnosis(conclusion(), {"e1"}, risk_floor="critical")
    with pytest.raises(ValueError, match="incomplete"):
        ai_config.validate_diagnosis(conclusion(risk_level="normal"), {"e1"}, incomplete=True)


def test_general_api_success_keeps_string_and_persists_inference_identity(db):
    provider(db)
    text = ai_config.AIProviderText(conclusion(evidence_refs=["metric-0"], root_causes=[]), {
        "actual_model": "revision-42", "finish_reason": "stop", "response_id": "req-42",
    })
    with patch.object(ai_config, "call_ai_provider", return_value=text):
        response = client(db).post("/ai/diagnose-ai", json={
            "instance": "fixture", "db_type": "oracle", "metrics": {"checks": [{"check_status": "warning"}]},
        })
    result = response.json()
    assert response.status_code == 200
    assert result["ai_model_result"] == str(text)
    assert result["ai_error"] is None
    assert result["ai_inference"]["actual_model"] == "revision-42"
    assert result["ai_inference"]["validation_limitations"]
    assert db.query(AIReport).one().diagnosis["ai_inference"] == result["ai_inference"]


def test_general_api_invalid_output_is_not_persisted_as_diagnosis(db):
    provider(db)
    with patch.object(ai_config, "call_ai_provider", return_value="UNSUPPORTED_DIAGNOSIS_TEXT"):
        response = client(db).post("/ai/diagnose-ai", json={
            "instance": "fixture", "db_type": "oracle", "metrics": {"checks": [{"check_status": "critical"}]},
        })
    assert response.json()["ai_model_result"] is None
    assert response.json()["ai_error"]
    assert db.query(AIReport).one().risk_level == "critical"


def test_empty_context_does_not_call_configured_model(db):
    provider(db)
    with patch.object(ai_config, "call_ai_provider") as call:
        response = client(db).post("/ai/diagnose-ai", json={"instance": "fixture", "db_type": "oracle"})
    call.assert_not_called()
    assert response.json()["ai_error"] == "no_supporting_evidence"


@pytest.mark.parametrize("valid", [True, False])
def test_evidence_chain_api_validates_and_persists_model_result(db, valid):
    provider(db)
    chain = {"instance": {"id": 1, "instance_name": "offline", "db_type": "oracle"},
             "summary": {"evidence_count": 1, "severity_counts": {"warning": 1}},
             "sections": [{"key": "inspection", "title": "inspection", "items": [
                 {"source": "inspection", "title": "pressure", "summary": "pressure measured", "severity": "warning"},
             ]}], "prompt_context": "pressure measured"}
    text = conclusion(evidence_refs=["section-0-item-0"], root_causes=[]) if valid else "non-diagnostic text"
    with patch("app.services.ai_evidence.build_evidence_chain", return_value=chain), \
         patch.object(ai_config, "call_ai_provider", return_value=text):
        response = client(db).post("/ai/evidence-diagnose", json={"instance_id": 1})
    result = response.json()
    assert response.status_code == 200, response.text
    assert bool(result["ai_error"]) is not valid
    assert result["ai_model_result"] == (text if valid else None)
    assert db.query(AIReport).one().diagnosis["ai_model_result"] == result["ai_model_result"]


def test_empty_evidence_chain_remains_unknown_without_model(db):
    chain = {"instance": {"id": 1, "instance_name": "offline", "db_type": "oracle"}, "sections": [], "summary": {}}
    with patch("app.services.ai_evidence.build_evidence_chain", return_value=chain):
        response = client(db).post("/ai/evidence-diagnose", json={"instance_id": 1})
    assert response.json()["local_result"]["risk_level"] == "unknown"
    assert db.query(AIReport).one().risk_level == "unknown"


def http_response(body, status=200):
    return NS(json=lambda: body, raise_for_status=lambda: None, status_code=status)


@contextmanager
def ollama_transport(body):
    generation = Mock(return_value=http_response(body))

    def post(url, **kwargs):
        if url.endswith("/api/show"):
            return http_response({"model_info": {"test.context_length": 32768}})
        return generation(url, **kwargs)

    with patch.object(ai_config.requests, "post", side_effect=post):
        yield generation


def transport_config(**changes):
    return NS(**{"id": 1, "provider_type": "openai_compatible", "model_name": "configured-alias",
                 "base_url": "http://127.0.0.1:11434/v1", "timeout_seconds": 1, "api_key_cipher": None, **changes})


def openai_body(**changes):
    return {"id": "response-42", "model": "actual-revision", "usage": {"total_tokens": 42},
            "choices": [{"finish_reason": "stop", "message": {"content": "legacy text"}}], **changes}


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", None])
def test_openai_rejects_incomplete_refusal_and_keeps_failure_metadata(reason):
    body = openai_body(choices=[{"finish_reason": reason, "message": {"content": "partial"}}])
    with patch.object(ai_config.requests, "post", return_value=http_response(body)):
        with pytest.raises(ai_config.AIProviderResponseError) as error:
            ai_config.call_openai_compatible(transport_config(), "context", json_mode=True)
    assert error.value.metadata["actual_model"] == "actual-revision"
    assert error.value.metadata["finish_reason"] == reason


@pytest.mark.parametrize("message", [{"content": "text", "refusal": "refused"}, {"content": None},
                                     {"content": []}, {"content": " "}, {"content": "text", "tool_calls": [{}]}])
def test_openai_validates_message_not_just_finish_reason(message):
    body = openai_body(choices=[{"finish_reason": "stop", "message": message}])
    with patch.object(ai_config.requests, "post", return_value=http_response(body)):
        with pytest.raises(ai_config.AIProviderResponseError):
            ai_config.call_ai_provider(transport_config(), "context")


def test_openai_legacy_string_contract_retains_actual_metadata():
    with patch.object(ai_config.requests, "post", return_value=http_response(openai_body())) as post:
        result = ai_config.call_ai_provider(transport_config(), "context", json_mode=True)
    assert isinstance(result, str)
    assert result.strip() == "legacy text"
    assert json.loads(json.dumps({"text": result}))["text"] == result
    assert result.metadata["actual_model"] == "actual-revision"
    assert result.metadata["configured_model"] == "configured-alias"
    assert result.metadata["response_id"] == "response-42"
    assert result.metadata["usage"] == {"total_tokens": 42}
    assert copy(result) == deepcopy(result) == result
    assert deepcopy(result).metadata == result.metadata
    assert post.call_args.kwargs["allow_redirects"] is False
    assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("body", [{"done": False, "response": "text"},
                                  {"done": True, "done_reason": "length", "response": "text"},
                                  {"done": True, "refusal": "no", "response": "text"},
                                  {"response": "text"}])
def test_ollama_non_json_also_rejects_incomplete_output(body):
    with ollama_transport(body):
        with pytest.raises(ai_config.AIProviderResponseError):
            ai_config.call_ollama(transport_config(), "context")


@pytest.mark.parametrize("reason", [None, "stop"])
def test_legacy_ollama_complete_text_and_qwen_policy_remain_usable(reason):
    body = {"done": True, "response": "legacy text", "model": "actual", "prompt_eval_count": 3, "eval_count": 4}
    if reason:
        body["done_reason"] = reason
    with ollama_transport(body) as post:
        result = ai_config.call_ollama(transport_config(model_name="qwen3-local"), "context")
    assert result == "legacy text"
    assert result.metadata["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["think"] is False
    assert post.call_args.kwargs["json"]["prompt"].startswith("/no_think\n")


@pytest.mark.parametrize("adapter", [ai_config.call_openai_compatible, ai_config.call_ollama])
def test_redirects_cannot_move_context_to_another_recipient(adapter):
    with patch.object(ai_config.requests, "post", return_value=http_response({}, status=307)) as post:
        with pytest.raises(ai_config.AIProviderResponseError, match="provider_redirect_rejected"):
            adapter(transport_config(), "private context")
    assert post.call_count == 1
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("failure", [TimeoutError("offline"), ai_config.AIProviderResponseError("context_exceeded"),
                                     "UNSUPPORTED_DIAGNOSIS_TEXT", conclusion(evidence_refs=["unknown"])])
def test_orchestrator_never_silently_retries_another_provider(db, failure):
    selected = provider(db)
    provider(db, provider_name="other-local", base_url="http://10.0.0.1:11434", is_default=0)
    provider(db, provider_name="external", base_url="https://vendor.invalid", is_default=0)
    seen = []
    def fake(config, prompt):
        seen.append((config.id, config.base_url, prompt))
        assert config is not selected
        if isinstance(failure, Exception):
            raise failure
        return failure
    with patch.object(orchestrator, "call_ai_provider", side_effect=fake):
        result = orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, [{"source": "fixture", "evidence": "PRIVATE_MARKER"}], [], "critical")
    assert len(seen) == 1
    assert seen[0][0] == selected.id
    assert result["tier"] == "local"
    assert "critical" in result["summary"]
    assert result["attempts"][0]["ok"] is False
    assert result["fallback_reason"] == "model_failed"


def test_remote_ollama_is_not_a_private_trust_marker():
    assert orchestrator._tier_of(transport_config(provider_type="ollama", base_url="https://vendor.invalid")) == "external"
    assert orchestrator._tier_of(transport_config(base_url="http://127.0.0.1")) == "private"
    assert orchestrator._tier_of(transport_config(base_url="http://[::1]")) == "private"


def test_orchestrator_local_success_and_reference_mapping(db):
    provider(db)
    with patch.object(orchestrator, "call_ai_provider", return_value=conclusion()) as call:
        result = orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, [{"source": "fixture", "evidence": "pressure"}], [], "warning")
    assert result["tier"] == "private"
    assert result["attempts"][0]["ok"] is True
    assert "ID e1" in call.call_args.args[1]
    assert result["inference"]["evidence_manifest"][0]["evidence"] == "pressure"


def test_orchestrator_config_copy_does_not_follow_midflight_mutation(db):
    original = provider(db)
    def fake(selected, prompt):
        original.base_url = "https://changed.invalid"
        assert selected.base_url == "http://127.0.0.1:11434"
        return conclusion()
    with patch.object(orchestrator, "call_ai_provider", side_effect=fake):
        result = orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, [{"evidence": "pressure"}], [], "warning")
    assert result["tier"] == "private"


@pytest.mark.parametrize("evidence", [[], [{"source": "manual_symptom", "phenomenon": "slow"}],
                                      [{"source": "fixture", "evidence": "counter", "polarity": "counter"}],
                                      [{"source": "fixture", "evidence": "failed", "execution_status": "failed"}]])
def test_orchestrator_requires_supporting_not_context_evidence(db, evidence):
    provider(db)
    with patch.object(orchestrator, "call_ai_provider") as call:
        result = orchestrator.orchestrate(db, db.get(DBInstance, 1), {}, evidence, [], "unknown")
    call.assert_not_called()
    assert result["fallback_reason"] == "no_supporting_evidence"


def gold_case(db):
    dataset = evaluation.create_dataset(db, {"name": "offline-contract"}, "tester")
    evaluation.add_case(db, dataset, {
        "case_key": "pressure", "title": "pressure", "db_type": "oracle", "domain": "database",
        "input_json": {"evidences": [
            {"id": "e1", "fact": "pressure measured", "cause_code": "pressure", "cause": "rule claim"},
            {"id": "c1", "fact": "storage normal", "polarity": "counter"},
        ]}, "expected_root_causes": [{"cause_code": "pressure"}], "required_evidence_refs": ["e1"],
        "expert_label_by": "tester", "status": "active",
    }, "tester")
    evaluation.freeze_dataset(db, dataset, "tester")
    return dataset


@pytest.mark.parametrize("refs,reason", [([], "missing_supporting_evidence"), (["missing"], "unknown_evidence_ref"),
                                         (["c1"], "counter_evidence_used_as_support"), ("e1", "invalid_evidence_refs")])
@pytest.mark.parametrize("submitted", [True, False])
def test_hybrid_validates_model_before_merge_and_measures_rejection(db, refs, reason, submitted):
    dataset = gold_case(db)
    provider(db)
    predictions = [{"cause_code": "pressure", "cause": "unsupported model assertion", "score": 1,
                    "evidence_refs": refs, "source": "rule"}]
    payload = {"dataset_id": dataset.id, "mode": "hybrid"}
    if submitted:
        payload["predictions_by_case"] = {"pressure": predictions}
    with patch.object(evaluation, "call_ai_provider", return_value=json.dumps({"root_causes": predictions})) as call:
        run = evaluation.run_evaluation(db, payload, "tester")
    assert call.call_count == (0 if submitted else 1)
    result = db.query(DiagnosisEvalCaseResult).one()
    assert result.predictions[0]["cause"] == "rule claim"
    assert result.predictions[0]["source"] == "rule"
    assert result.predictions[0]["contributions"][0]["source"] == "rule"
    assert result.ungrounded_count == 1
    assert reason in result.rejected_predictions[0]["reasons"]
    assert result.rejected_predictions[0]["prediction"]["source"] == "model"
    assert result.degraded_to_rule == 1
    assert run.threshold_status == "degraded"
    assert run.ungrounded_fact_rate == 0.5
    detail = evaluation.run_detail(db, run)
    assert detail["source_metrics"]["model"]["ungrounded_fact_rate"] == 1.0
    assert detail["source_metrics"]["rule"]["ungrounded_fact_rate"] == 0.0
    assert detail["actual_rule_fallback_rate"] == 1.0


def test_hybrid_legitimate_claims_keep_independent_provenance(db):
    dataset = gold_case(db)
    run = evaluation.run_evaluation(db, {"dataset_id": dataset.id, "mode": "hybrid", "predictions_by_case": {
        "pressure": [{"cause_code": "pressure", "cause": "model hypothesis", "evidence_refs": ["e1"], "score": 1}],
    }}, "tester")
    result = db.query(DiagnosisEvalCaseResult).one()
    assert result.ungrounded_count == 0
    assert result.degraded_to_rule == 0
    assert result.predictions[0]["source"] == "hybrid"
    claims = result.predictions[0]["contributions"]
    assert [(claim["source"], claim["cause"]) for claim in claims] == [("rule", "rule claim"), ("model", "model hypothesis")]
    assert run.threshold_status == "passed"


def test_all_rule_fallback_never_claims_model_was_measured(db):
    dataset = gold_case(db)
    provider(db)
    with patch.object(evaluation, "call_ai_provider", side_effect=TimeoutError("offline")):
        run = evaluation.run_evaluation(db, {"dataset_id": dataset.id, "mode": "hybrid"}, "tester")
    detail = evaluation.run_detail(db, run)
    assert detail["source_metrics"]["model"]["ungrounded_fact_rate"] is None
    assert detail["source_metrics"]["model"]["unavailable_cases"] == 1
    assert detail["actual_rule_fallback_rate"] == 1.0
    assert run.threshold_status == "degraded"


def test_evaluation_uses_actual_usage_instead_of_unlabelled_estimates(db):
    dataset = gold_case(db)
    provider(db)
    text = ai_config.AIProviderText(json.dumps({"root_causes": [
        {"cause_code": "pressure", "cause": "pressure", "evidence_refs": ["e1"], "score": 1},
    ]}), {"actual_model": "revision-42", "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    with patch.object(evaluation, "call_ai_provider", return_value=text):
        run = evaluation.run_evaluation(db, {"dataset_id": dataset.id, "mode": "model"}, "tester")
    result = db.query(DiagnosisEvalCaseResult).one()
    assert run.prompt_tokens == result.prompt_tokens == 10
    assert run.completion_tokens == result.completion_tokens == 20
    assert result.raw_output["inference"]["actual_model"] == "revision-42"


@pytest.mark.parametrize("candidate", [None, {"cause_code": "pressure", "score": "nan", "evidence_refs": ["e1"]},
                                       {"cause_code": "pressure", "score": {}, "evidence_refs": "e1"}])
def test_malformed_model_candidates_are_counted_as_rejected(candidate):
    case = NS(input_json={"evidences": [{"id": "e1", "fact": "pressure"}]}, forbidden_claims=[])
    accepted, rejected, count = evaluation._validate_predictions(case, [candidate])
    assert not accepted
    assert count == len(rejected) == 1
