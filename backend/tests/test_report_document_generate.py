"""Evidence-backed report generation: real rows, cited AI narrative, rule fallback."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.core.database import get_db
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.ai_provider_config import AIProviderConfig
from app.models.alert_event import DBAlertEvent
from app.models.capacity_policy import DBCapacityAlert
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_queue, plan_service as plans, report_document

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}
JOB = "a" * 30


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'report-document.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        now = datetime.utcnow()
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="rep-requester", password_hash="unused", role="admin", status="active"),
            User(id=2, username="rep-operator", password_hash="unused", role="user", status="active"),
            User(id=4, username="rep-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="orders-mysql", db_type="mysql", host="10.0.0.10", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_REPORT_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="10.0.0.10", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
            DBInspectionJob(id=JOB, instance_id=10, db_type="mysql", requested_by=1, trigger_type="manual",
                            policy_id=1, policy_snapshot_json={}, policy_revision="rev", target_fingerprint="f",
                            status="success", stage="completed", created_at=now - timedelta(hours=2),
                            finished_at=now - timedelta(hours=1)),
            DBCheckResult(task_id=JOB, instance_id=10, db_type="mysql", check_category="capacity",
                          check_item="undo usage", check_status="critical", finding="undo 99%",
                          suggestion="扩容 UNDO"),
            DBCheckResult(task_id=JOB, instance_id=10, db_type="mysql", check_category="io",
                          check_item="io latency", check_status="warning", finding="io 30ms"),
            DBAlertEvent(event_uid="alert:1", source="prometheus", instance_id=10, db_type="mysql",
                         severity="critical", status="firing", title="DB down", last_seen=now - timedelta(minutes=5)),
            DBCapacityAlert(instance_id=10, db_type="mysql", object_name="undo", alert_type="usage",
                            level="critical", message="undo 99%", status="open"),
            AIProviderConfig(provider_name="fixture", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="fixture-model", enabled=1, is_default=1),
        ])
        session.flush()
        session.add(UserAccessRole(user_id=2, role_id=roles["dba_operator"].id))
        session.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="operate"))
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _run(db, **params):
    base = {"report_type": "unified", "window_hours": 168, "format": "markdown", "include_ai": False}
    created = plans.create_plan(db, actor=REQUESTER, capability_id="report.document.generate",
                                definition_version=3, instance_id=10, params={**base, **params})
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def _approve(db, plan):
    governance.request_approval(db, plan["plan_id"], actor=REQUESTER, expected_revision=plan["revision"])
    row = plans._load(db, plan["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, plan["plan_id"], actor=APPROVER,
                            expected_revision=plan["revision"], expected_binding_hash=row.approval_binding_hash)


def _client(factory):
    from app.api.v1 import assistant, report_center as report_api

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(report_api.router, prefix="/api/v1")

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.auth = {"user_id": 1, "username": "rep-requester", "role": "admin"}
        return await call_next(request)

    def isolated_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[assistant._actor] = lambda: REQUESTER
    return TestClient(app)


def test_generates_artifact_with_evidence_and_hash(db):
    from app.models.db_instance import DBInstance as _I
    report_document.generate(db, db.get(_I, 10), report_type="unified", window_hours=168,
                             format="markdown", include_ai=False, execution_id="debug:1:1", actor="debug")
    finished = _run(db)
    assert finished["outcome"] == "succeeded", finished
    inline = finished["steps"][0]["result_ref"]["inline"]
    artifact = db.get(ReportArtifact, inline["artifact_id"])
    assert artifact.artifact_kind == "document"
    assert artifact.report_type == "report_unified"
    assert artifact.content_sha256 == inline["sha256"]
    assert artifact.instance_id == 10 and artifact.evidence_manifest
    assert "巡检结论" in artifact.content_markdown and "证据清单" in artifact.content_markdown
    assert any(item["type"] == "check_result" for item in artifact.evidence_manifest)
    assert inline["analysis_source"] == "rule_engine"


def test_http_execute_queues_once_worker_archives_and_artifact_is_downloadable(db):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    params = {"report_type": "unified", "window_hours": 168, "format": "markdown", "include_ai": False}
    plan = plans.create_plan(db, actor=REQUESTER, capability_id="report.document.generate",
                             definition_version=3, instance_id=10, params=params)
    db.commit()

    with _client(factory) as client:
        refused = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert refused.status_code == 409
        assert refused.json()["detail"] == {"code": "approval_required"}
        assert db.query(DBCapabilityBatch).count() == 0
        assert db.query(DBCapabilityExecution).count() == 0

        _approve(db, plan)
        queued = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        replay = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert queued.status_code == replay.status_code == 200
        assert queued.json()["execution_queue_state"] == replay.json()["execution_queue_state"] == "queued"
        assert queued.json()["execution_batch_id"] == replay.json()["execution_batch_id"]
        db.expire_all()
        assert db.query(DBCapabilityBatch).count() == 1
        assert db.query(DBCapabilityExecution).count() == 1

        worker = plan_queue.process_one(factory, worker_id="report-document-worker")
        assert worker["queue_state"] == "archived"
        assert worker["outcome"] == "succeeded"

        saved = client.get(f"/api/v1/assistant/plans/{plan['plan_id']}")
        assert saved.status_code == 200
        value = saved.json()
        assert value["execution_queue_state"] == "archived"
        reference = value["steps"][0]["result_ref"]
        assert reference["kind"] == "capability_effect"
        assert reference["schema"] == report_document.RESULT_SCHEMA
        inline = reference["inline"]
        assert inline["schema"] == report_document.RESULT_SCHEMA
        assert inline["instance_id"] == 10 and inline["outcome"] == "success"
        assert inline["download_url"] == f"/api/v1/reports/catalog/{inline['artifact_id']}/download"

        detail = client.get(f"/api/v1/reports/catalog/{inline['artifact_id']}")
        assert detail.status_code == 200
        assert detail.json()["verification"]["valid"] is True
        assert detail.json()["content_json"]["schema"] == report_document.SCHEMA
        download = client.get(inline["download_url"] + "?format=markdown")
        assert download.status_code == 200
        assert download.headers["x-report-sha256"] == inline["sha256"]
        assert "证据清单" in download.content.decode("utf-8")

    db.expire_all()
    artifact = db.get(ReportArtifact, inline["artifact_id"])
    artifact.content_sha256 = "0" * 64
    db.commit()
    persisted_plan = plans._load(db, plan["plan_id"], REQUESTER, owner_only=True)
    assert plan_queue._evidence(db, persisted_plan) is False


def test_no_evidence_partial_is_typed_and_archived_without_an_artifact(db):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    db.query(DBCheckResult).delete()
    db.query(DBInspectionJob).delete()
    db.query(DBAlertEvent).delete()
    db.query(DBCapacityAlert).delete()
    db.commit()
    plan = plans.create_plan(
        db, actor=REQUESTER, capability_id="report.document.generate", definition_version=3,
        instance_id=10,
        params={"report_type": "unified", "window_hours": 24, "format": "html", "include_ai": False},
    )
    _approve(db, plan)
    plan_queue.enqueue_batch(db, [plan["plan_id"]], actor=REQUESTER,
                             idempotency_key="report-no-evidence", schema_version=2)

    worker = plan_queue.process_one(factory, worker_id="report-partial-worker")
    assert worker["queue_state"] == "archived" and worker["outcome"] == "partial"
    saved = plans.read_plan(db, plan["plan_id"], actor=REQUESTER)
    inline = saved["steps"][0]["result_ref"]["inline"]
    assert inline == {
        "schema": report_document.RESULT_SCHEMA,
        "outcome": "partial",
        "reason_code": "no_evidence",
        "artifact_id": None,
        "artifact_uid": None,
        "sha256": None,
        "instance_id": 10,
        "report_type": "unified",
        "format": "html",
        "window_hours": 24,
        "include_ai": False,
        "evidence_count": 0,
        "download_url": None,
    }
    assert db.query(ReportArtifact).count() == 0


def test_scoped_operator_can_admit_target_bound_report_generation(db):
    operator = {"user_id": 2}
    catalog = registry.list_capabilities(db, actor=operator, engine="mysql")
    item = next(row for row in catalog["items"] if row["capability_id"] == "report.document.generate")
    assert item["availability"] == "available" and item["plan_executable"] is True
    plan = plans.create_plan(
        db, actor=operator, capability_id="report.document.generate", definition_version=3,
        instance_id=10,
        params={"report_type": "unified", "window_hours": 24, "format": "markdown", "include_ai": False},
    )
    assert plan["status"] == "validated"


def test_after_sales_variant_and_no_evidence_partial(db):
    finished = _run(db, report_type="after_sales")
    inline = finished["steps"][0]["result_ref"]["inline"]
    artifact = db.get(ReportArtifact, inline["artifact_id"])
    assert artifact.report_type == "report_after_sales"
    assert "售后分析报告" in artifact.title

    db.query(DBCheckResult).delete()
    db.query(DBInspectionJob).delete()
    db.query(DBAlertEvent).delete()
    db.query(DBCapacityAlert).delete()
    db.commit()
    empty = _run(db)
    assert empty["outcome"] == "partial"
    assert empty["steps"][0]["result_ref"]["inline"]["reason_code"] == "no_evidence"
    assert db.query(ReportArtifact).count() == 1


def test_ai_citations_are_validated_and_failure_degrades(db, monkeypatch):
    check_id = db.query(DBCheckResult).order_by(DBCheckResult.id).first().id
    monkeypatch.setattr(report_document, "call_ai_provider", lambda *a, **k: json.dumps({
        "summary": "AI 摘要", "risks": [{"text": "undo 风险", "severity": "critical",
                                        "evidence_keys": [f"check:{check_id}"]}],
        "recommendations": [{"text": "扩容 undo", "evidence_keys": [f"check:{check_id}"]}],
        "root_causes": [{"text": "undo 不足", "confidence": 0.8, "evidence_keys": [f"check:{check_id}"]}],
    }))
    finished = _run(db, include_ai=True)
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["analysis_source"] == "ai" and inline["model"] == "fixture-model"

    monkeypatch.setattr(report_document, "call_ai_provider", lambda *a, **k: "not-json")
    finished = _run(db, include_ai=True, window_hours=167)
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["analysis_source"] == "rule_engine"
    assert db.get(ReportArtifact, inline["artifact_id"]).content_json["ai_error"]


def test_unknown_ai_citation_degrades_and_same_execution_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(report_document, "call_ai_provider", lambda *a, **k: json.dumps({
        "summary": "bad", "risks": [{"text": "x", "severity": "critical", "evidence_keys": ["check:999999"]}],
        "recommendations": [], "root_causes": [],
    }))
    finished = _run(db, include_ai=True)
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["analysis_source"] == "rule_engine"
    artifact = db.get(ReportArtifact, inline["artifact_id"])
    assert "unknown_evidence" in (artifact.content_json.get("ai_error") or "")

    instance = db.get(DBInstance, 10)
    first = report_document.generate(db, instance, report_type="unified", window_hours=168,
                                     format="markdown", include_ai=False, execution_id="retry:same", actor="t")
    second = report_document.generate(db, instance, report_type="unified", window_hours=168,
                                      format="markdown", include_ai=False, execution_id="retry:same", actor="t")
    assert first["artifact_id"] == second["artifact_id"]
    assert db.query(ReportArtifact).filter_by(source_key="report:document:retry:same").count() == 1


def test_ai_provider_falls_back_to_the_next_enabled_provider(db, monkeypatch):
    db.add(AIProviderConfig(provider_name="bad-default", provider_type="openai_compatible",
                            base_url="http://127.0.0.1:9", model_name="bad", enabled=1, is_default=1))
    db.commit()
    check_id = db.query(DBCheckResult).order_by(DBCheckResult.id).first().id

    def fake_call(config, *_args, **_kwargs):
        if config.provider_name == "bad-default":
            raise RuntimeError("provider down")
        return json.dumps({"summary": "fallback", "risks": [],
                           "recommendations": [{"text": "fix", "evidence_keys": [f"check:{check_id}"]}],
                           "root_causes": []})
    monkeypatch.setattr(report_document, "call_ai_provider", fake_call)
    finished = _run(db, include_ai=True)
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["analysis_source"] == "ai" and inline["model"] == "fixture-model"


def test_http_generate_route_returns_both_documents(db):
    from starlette.requests import Request
    from app.api.v1 import report_center as api

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/reports/document/generate", "headers": []})
    request.state.auth = {"user_id": 1, "username": "admin", "role": "admin"}
    body = api.ReportDocumentGenerateRequest(instance_id=10, report_type="both", include_ai=False)
    result = api.generate_report_document(body, request, db)
    assert result["schema"] == "report.document_batch/v1"
    assert [item["report_type"] for item in result["items"]] == ["unified", "after_sales"]
    assert all(item["artifact_id"] for item in result["items"])


def test_ai_prompt_never_contains_host_or_port(db, monkeypatch):
    prompts = []
    def capture(config, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps({"summary": "ok", "risks": [], "recommendations": [], "root_causes": []})
    monkeypatch.setattr(report_document, "call_ai_provider", capture)
    _run(db, include_ai=True)
    assert prompts and "10.0.0.10" not in prompts[0] and "3306" not in prompts[0]
    assert "orders-mysql" in prompts[0]


def test_invented_number_in_ai_summary_degrades(db, monkeypatch):
    monkeypatch.setattr(report_document, "call_ai_provider", lambda *a, **k: json.dumps({
        "summary": "有 99999% 的风险", "risks": [], "recommendations": [], "root_causes": [],
    }))
    finished = _run(db, include_ai=True)
    inline = finished["steps"][0]["result_ref"]["inline"]
    assert inline["analysis_source"] == "rule_engine"
    assert "invented_number" in (db.get(ReportArtifact, inline["artifact_id"]).content_json.get("ai_error") or "")


def test_batch_route_rolls_back_when_second_report_fails(db, monkeypatch):
    from starlette.requests import Request
    from app.api.v1 import report_center as api
    calls = {"n": 0}
    original = report_document.generate
    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second failed")
        return original(*args, **kwargs)
    monkeypatch.setattr(report_document, "generate", flaky)
    request = Request({"type": "http", "method": "POST", "path": "/api/v1/reports/document/generate", "headers": []})
    request.state.auth = {"user_id": 1, "username": "admin", "role": "admin"}
    body = api.ReportDocumentGenerateRequest(instance_id=10, report_type="both", include_ai=False)
    with pytest.raises(RuntimeError):
        api.generate_report_document(body, request, db)
    assert db.query(ReportArtifact).count() == 0
