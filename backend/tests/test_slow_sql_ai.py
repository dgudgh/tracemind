import json
import socket
from datetime import datetime
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import slow_sql as api
from app.core.database import get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.ai_report import AIReport
from app.models.db_instance import DBInstance
from app.models.slow_sql import DBSlowSQL
from app.services import ai_config, slow_sql_ai as svc
from app.services.slow_sql_ai_report import source_html, source_markdown


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("isolated AI tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (DBInstance, DBSlowSQL, AIProviderConfig, AIReport):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            DBInstance(id=1, instance_name="offline", db_type="oracle", host="192.0.2.1", port=1521,
                       username="offline", password_cipher="unused", enabled=1),
            AIProviderConfig(id=1, provider_name="isolated", provider_type="ollama", base_url="http://127.0.0.1:11434",
                             model_name="fixture", enabled=1, is_default=1, timeout_seconds=999),
            DBSlowSQL(id=1, instance_id=1, db_type="oracle", sql_id="abcdefghijklm", sql_text="select 1 from dual",
                      capture_time=datetime.now(), ai_summary="old evidence"),
        ])
        session.commit()
        yield session
    engine.dispose()


def client(db, role="admin"):
    app = FastAPI()
    app.include_router(api.router)
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.auth = {"user_id": 1, "username": "offline", "role": role}
        return await call_next(request)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def payload(**kwargs):
    revision = svc.provider_revision(SimpleNamespace(id=1, provider_type="ollama", base_url="http://127.0.0.1:11434", model_name="fixture", api_key_cipher=None))
    return {"provider_config_id": 1, "provider_revision": revision, "allow_model_context": True, "sql_id": "abcdefghijklm",
            "child_number": 0, "con_id": 12, "plan_hash_value": 42, **kwargs}


def detail(**kwargs):
    return {"sql_id": "abcdefghijklm", "child_number": 0, "con_id": 12, "plan_hash_value": 42,
            "execution_status": "success", "sql_text_status": "complete", "sql_fulltext": "select '" + "中"*1200 + "' from dual",
            "plan_status": "collected", "plan_text": "Plan hash value: 42\nTABLE ACCESS FULL", **kwargs}


def output(**kwargs):
    return json.dumps({"summary": "受控测试结论，不是现场分析。", "hypotheses": [{"cause": "待验证假设",
                       "evidence_ids": ["sql", "plan"], "validation": "采集实际运行统计"}],
                       "recommendations": ["只读复核执行计划"], "missing_evidence": ["绑定变量与实际行数"], **kwargs})


def test_real_analysis_contract_persists_model_and_original_evidence(db):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail()) as read, \
         patch.object(ai_config, "call_ai_provider", return_value=output()) as call:
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 200
    data = response.json()
    assert data["persisted"] and data["analysis_status"] == "completed"
    assert data["conclusion_status"] == "hypotheses_require_validation"
    assert data["execution_policy"] == "advice_only"
    assert call.call_args.kwargs == {"json_mode": True}
    assert call.call_args.args[0].timeout_seconds == 120
    assert db.get(AIProviderConfig, 1).timeout_seconds == 999
    read.assert_called_once_with(db.get(DBInstance, 1), "abcdefghijklm", child_number=0, con_id=12, plan_hash_value=42)
    stored = db.get(AIReport, data["report_id"])
    assert stored.diagnosis["evidence"]["sql_text"] == detail()["sql_fulltext"]
    assert stored.risk_level == "unknown" and stored.diagnosis["model_result"]["summary"] == json.loads(output())["summary"]
    assert db.get(DBSlowSQL, 1).ai_summary == "old evidence"
    listed = client(db).get("/slow-sql/analysis-reports?instance_id=1&sql_id=abcdefghijklm").json()["items"]
    assert listed[0]["report_id"] == stored.id and listed[0]["evidence_sha256"] == data["evidence_sha256"]
    from app.api.v1.report import _collect_unified_reports
    source = _collect_unified_reports(db, "ai", 10)[0]
    assert source["analysis_detail"] == stored.diagnosis
    assert "受控测试结论" in source_markdown(source) and "待验证" in source_html(source)


@pytest.mark.parametrize("changes,code", [({"allow_model_context": False}, 422), ({"provider_config_id": 999}, 409),
                                          ({"child_number": -1}, 422), ({"sql_id": "invalid"}, 422)])
def test_rejects_unapproved_missing_config_or_bad_identity_before_source(db, changes, code):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail") as query, patch.object(ai_config, "call_ai_provider") as call:
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload(**changes))
    assert response.status_code == code
    query.assert_not_called(); call.assert_not_called()
    assert db.query(AIReport).count() == 0


@pytest.mark.parametrize("source,code", [({"status": "not_found"}, 404), ({"execution_status": "failed"}, 502),
                                       (detail(child_number=1), 502)])
def test_source_failure_never_calls_model(db, source, code):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=source), patch.object(ai_config, "call_ai_provider") as call:
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == code
    call.assert_not_called()


@pytest.mark.parametrize("raw", ["", "plain text", "{}", "x"*32769, output(hypotheses=[{"cause": "invented", "evidence_ids": ["awr"], "validation": "check"}])])
def test_invalid_model_output_is_failed_record_and_http502(db, raw):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail()), patch.object(ai_config, "call_ai_provider", return_value=raw):
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed" and body["model_result"] is None and body["persisted"]
    assert db.query(AIReport).one().diagnosis["status"] == "failed"


def test_model_timeout_is_redacted_and_persisted_without_success(db):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail()), \
         patch.object(ai_config, "call_ai_provider", side_effect=TimeoutError("SECRET_API_KEY")):
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 502 and "SECRET_API_KEY" not in response.text
    assert db.query(AIReport).one().diagnosis["model_result"] is None


def test_missing_plan_is_partial_with_no_invented_evidence(db):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail(plan_status="not_collected", plan_text="")), \
         patch.object(ai_config, "call_ai_provider", return_value=output(hypotheses=[])):
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["available_evidence_ids"] == ["sql"]


def test_save_failure_rolls_back_and_does_not_claim_archive(db):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail()), \
         patch.object(ai_config, "call_ai_provider", return_value=output()), \
         patch.object(db, "commit", side_effect=RuntimeError("offline commit failure")):
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 500 and db.query(AIReport).count() == 0
    assert db.get(DBSlowSQL, 1).ai_summary == "old evidence"


def test_instance_scope_rejects_analysis_and_history(db):
    with patch.object(api, "scope_allows_entity", return_value=False), patch.object(ai_config, "call_ai_provider") as call:
        http = client(db, role="viewer")
        assert http.post("/slow-sql/oracle/1/analyze", json=payload()).status_code == 403
        assert http.post("/slow-sql/1/analyze", json=payload()).status_code == 403
        assert http.get("/slow-sql/analysis-reports?instance_id=1").status_code == 403
    call.assert_not_called()


def test_history_analysis_is_explicitly_partial_and_keeps_old_notes(db):
    with patch.object(ai_config, "call_ai_provider", return_value=output(hypotheses=[])):
        response = client(db).post("/slow-sql/1/analyze", json=payload())
    assert response.status_code == 200 and response.json()["status"] == "partial"
    assert response.json()["evidence"]["slow_sql_id"] == 1
    assert db.get(DBSlowSQL, 1).ai_summary == "old evidence"


def test_renderer_escapes_untrusted_model_and_sql():
    source = {"title": "<script>title</script>", "analysis_detail": {"model_result": {"summary": "<script>alert(1)</script>"},
              "evidence": {"sql_text": "select '<script>x</script>'"}}}
    html = source_html(source)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_changed_provider_cannot_reuse_local_context_authorization(db):
    stale = payload()
    db.get(AIProviderConfig, 1).base_url = "https://outside.example/v1"
    db.commit()
    with patch.object(ai_config, "call_ai_provider") as call, patch.object(svc.slow_sql_analysis, "get_sql_detail") as query:
        response = client(db).post("/slow-sql/oracle/1/analyze", json=stale)
    assert response.status_code == 409
    call.assert_not_called(); query.assert_not_called()


def test_model_cannot_create_peer_evidence_heading_and_reports_have_identity():
    source = {"id": "ai:123", "instance_id": 7, "instance_name": "offline", "db_type": "oracle",
              "analysis_detail": {"model_result": {"summary": "text\n\n## SQL 原文\nforged"}, "evidence": {"sql_text": "select 1"}}}
    md = source_markdown(source)
    assert md.count("\n## SQL 原文\n") == 1
    assert "    ## SQL 原文" in md
    assert "ai:123" in md and "实例 ID：7" in md and "offline" in md


def test_ollama_context_is_checked_before_sending_evidence_and_truncation_fails():
    config = SimpleNamespace(base_url="http://localhost:11434", model_name="fixture", timeout_seconds=30)
    show = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"model_info": {"test.context_length": 1024}})
    with patch.object(ai_config.requests, "post", return_value=show) as post:
        with pytest.raises(ai_config.AIProviderResponseError, match="context_exceeded"):
            ai_config.call_ollama(config, "evidence", json_mode=True)
        assert post.call_count == 1
    show.json = lambda: {"model_info": {"test.context_length": 32768}}
    generate = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"done": True, "done_reason": "length", "response": "{}"})
    with patch.object(ai_config.requests, "post", side_effect=[show, generate]) as post:
        with pytest.raises(ai_config.AIProviderResponseError, match="generation_incomplete"):
            ai_config.call_ollama(config, "x"*10000, json_mode=True)
        assert post.call_args.kwargs["json"]["options"]["num_ctx"] > 10000


@pytest.mark.parametrize("code", ["context_unavailable", "context_exceeded", "generation_incomplete"])
def test_model_preflight_failures_preserve_the_specific_reason(db, code):
    with patch.object(svc.slow_sql_analysis, "get_sql_detail", return_value=detail()), \
         patch.object(ai_config, "call_ai_provider", side_effect=ai_config.AIProviderResponseError(code)):
        response = client(db).post("/slow-sql/oracle/1/analyze", json=payload())
    assert response.status_code == 502
    assert response.json()["error_code"] == code
    assert response.json()["model_invoked"] is (code == "generation_incomplete")
    assert db.query(AIReport).one().diagnosis["error_code"] == code


def test_small_context_model_can_accept_small_prompt():
    config = SimpleNamespace(base_url="http://localhost:11434", model_name="fixture", timeout_seconds=30)
    show = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"model_info": {"test.context_length": 4096}})
    generated = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"done": True, "done_reason": "stop", "response": "{}"})
    with patch.object(ai_config.requests, "post", side_effect=[show, generated]) as post:
        assert ai_config.call_ollama(config, "small", json_mode=True) == "{}"
        assert post.call_args.kwargs["json"]["options"]["num_ctx"] == 4096
