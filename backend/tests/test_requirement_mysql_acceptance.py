"""Requirement acceptance on an explicitly isolated local MySQL control schema.

Oracle targets below are synthetic inventory rows and the collector is controlled by
the test.  They prove dispatch, durable queue metadata, and failure semantics; they
are not evidence of connectivity to a field Oracle database.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.api.v1 import oracle as oracle_api
from app.cmdb.models import AssetInstance
from app.core.database import Base, get_db
from app.models.access_control import SecurityAuditEvent
from app.models.ai_provider_config import AIProviderConfig
from app.models.alert_event import DBAlertEvent
from app.models.capacity_policy import DBCapacityAlert
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.external_monitoring import PrometheusAlertCache
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.models.user import User
from app.services import capability_dispatch, capability_registry as registry, plan_queue, report_document
from app.services import plan_governance as governance, plan_service as plans


REQUESTER_ID = 1
APPROVER_ID = 4
REQUESTER = {"user_id": REQUESTER_ID}
JOB_ID = "a" * 30
MYSQL_ENV = "MESDBTOOLS_MYSQL_TEST_URL"
MYSQL_SCHEMA = "mesdbtools_requirement_verify"


def _isolated_mysql_url():
    raw = os.environ.get(MYSQL_ENV)
    if not raw:
        pytest.skip(f"{MYSQL_ENV} is not set")
    url = make_url(raw)
    assert url.drivername == "mysql+pymysql", "acceptance URL must use mysql+pymysql"
    assert url.host in {"127.0.0.1", "localhost"}, "acceptance URL must be loopback-only"
    assert url.database == MYSQL_SCHEMA, "refusing to touch a non-test or business schema"
    assert not url.query, "acceptance URL must not redirect through connection options"
    return url


@pytest.fixture()
def mysql_storage():
    """Create only the allow-listed disposable schema; never read application config."""
    engine = create_engine(_isolated_mysql_url(), pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        # Production report windows are computed in naive UTC.  Keep the persisted
        # MySQL fixture on that same clock so the test does not depend on host TZ.
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        db.add_all([
            User(id=REQUESTER_ID, username="requirement-requester", password_hash="unused",
                 role="admin", status="active"),
            User(id=APPROVER_ID, username="requirement-approver", password_hash="unused",
                 role="admin", status="active"),
            DBInstance(id=10, instance_name="orders-mysql", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://NO_LIVE_CONNECTION"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10",
                          db_type="mysql", ip="127.0.0.1", port=3306, host_id=10, region_id=1,
                          site_id=1, zone_id=1, env_id=1, status="active"),
            DBInspectionJob(id=JOB_ID, instance_id=10, db_type="mysql", requested_by=REQUESTER_ID,
                            trigger_type="manual", policy_id=1, policy_snapshot_json={},
                            policy_revision="rev", target_fingerprint="fixture", status="success",
                            stage="completed", created_at=now - timedelta(hours=2),
                            finished_at=now - timedelta(hours=1)),
            DBCheckResult(task_id=JOB_ID, instance_id=10, db_type="mysql", check_category="capacity",
                          check_item="undo usage", check_status="critical", finding="undo 99%",
                          suggestion="扩容 UNDO"),
            DBCheckResult(task_id=JOB_ID, instance_id=10, db_type="mysql", check_category="io",
                          check_item="io latency", check_status="warning", finding="io 30ms"),
            DBAlertEvent(event_uid="requirement-alert:1", source="prometheus", instance_id=10,
                         db_type="mysql", severity="critical", status="firing", title="DB down",
                         last_seen=now - timedelta(minutes=5)),
            DBCapacityAlert(instance_id=10, db_type="mysql", object_name="undo", alert_type="usage",
                            level="critical", message="undo 99%", status="open",
                            first_seen=now - timedelta(minutes=10),
                            last_seen=now - timedelta(minutes=5)),
            AIProviderConfig(provider_name="fixture", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="fixture-model",
                             enabled=1, is_default=1),
        ])
        for offset, instance_id in enumerate((11, 12), start=1):
            db.add(DBInstance(
                id=instance_id, instance_name=f"oracle-{instance_id}", db_type="oracle",
                host="127.0.0.1", port=1520 + offset, service_name=f"fixture{offset}",
                oracle_connect_type="service_name", username="unused", enabled=1,
                password_cipher="env://NO_LIVE_CONNECTION",
            ))
            db.add(AssetInstance(
                id=instance_id, native_instance_id=instance_id, stable_key=f"asset:{instance_id}",
                name=f"asset-{instance_id}", db_type="oracle", ip="127.0.0.1", port=1520 + offset,
                host_id=instance_id, region_id=1, site_id=1, zone_id=1, env_id=1, status="active",
            ))
        db.commit()
        registry.sync_definitions(db, actor=REQUESTER)
    try:
        yield engine, factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _client(factory):
    from app.api.v1 import assistant, report_center as report_api

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.include_router(report_api.router, prefix="/api/v1")

    @app.middleware("http")
    async def identity(request, call_next):
        user_id = APPROVER_ID if request.headers.get("x-test-actor") == "approver" else REQUESTER_ID
        request.state.auth = {
            "user_id": user_id,
            "username": "requirement-approver" if user_id == APPROVER_ID else "requirement-requester",
            "role": "admin",
            "source": "identity",
        }
        return await call_next(request)

    def isolated_db():
        with factory() as db:
            yield db

    def actor(request: Request):
        return {"user_id": request.state.auth["user_id"]}

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[assistant._actor] = actor
    return TestClient(app)


def _answer(capability_id: str, params: dict) -> str:
    return json.dumps({
        "decision": "execute",
        "capability_id": capability_id,
        "params": params,
        "steps": [],
        "questions": [],
        "target_key": None,
        "goal": "requirement acceptance",
        "confidence": 0.99,
        "reason": "fixture selection",
    })


def _approved_plan(db, capability_id: str, instance_id: int | None = None, *, key: str):
    created = plans.create_plan(
        db, actor=REQUESTER, capability_id=capability_id, definition_version=3,
        instance_id=instance_id, params={}, idempotency_key=key,
    )
    governance.request_approval(
        db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"],
    )
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(
        db, created["plan_id"], actor={"user_id": APPROVER_ID},
        expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash,
    )
    return created


def test_requirement_report_dispatch_approval_worker_and_download(mysql_storage, monkeypatch):
    """The customer sentence produces a real evidence-backed report, not a stub result."""
    _, factory = mysql_storage
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *args, **kwargs: _answer(
        "report.document.generate",
        {"report_type": "after_sales", "window_hours": 168,
         "format": "markdown", "include_ai": False},
    ))
    with factory() as db:
        seeded = report_document.collect_evidence(db, db.get(DBInstance, 10), window_hours=168)
        assert seeded["inspection"] is not None, seeded
        assert seeded["alerts"]["total"] == 1, seeded
        assert seeded["capacity"]["total"] == 1, seeded

    with _client(factory) as client:
        dispatched = client.post("/api/v1/assistant/dispatch-intent", json={
            "text": "生成这个实例最近7天的售后分析报告",
            "instance_id": 10,
            "request_id": "requirement-after-sales-7d",
        })
        assert dispatched.status_code == 200, dispatched.text
        selection = dispatched.json()
        assert selection["action"] == "confirm"
        assert selection["capability"]["capability_id"] == "report.document.generate"
        assert selection["params"]["report_type"] == "after_sales"
        assert selection["params"]["window_hours"] == 168
        plan = selection["plan"]

        requested = client.post(
            f"/api/v1/assistant/plans/{plan['plan_id']}/approval/request",
            json={"expected_revision": plan["revision"]},
        )
        assert requested.status_code == 200, requested.text
        assert requested.json()["approval_state"] == "pending"
        binding_hash = requested.json()["binding_hash"]

        approved = client.post(
            f"/api/v1/assistant/plans/{plan['plan_id']}/approval/approve",
            headers={"x-test-actor": "approver"},
            json={"expected_revision": plan["revision"], "expected_binding_hash": binding_hash,
                  "valid_for_seconds": 900},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approval_state"] == "approved"
        assert approved.json()["approved_by"] == APPROVER_ID

        queued = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert queued.status_code == 200, queued.text
        assert queued.json()["execution_queue_state"] == "queued"
        batch_id = queued.json()["execution_batch_id"]

        worked = plan_queue.process_one(factory, worker_id="requirement-report-worker")
        assert worked["queue_state"] == "archived" and worked["outcome"] == "succeeded"
        assert plan_queue.process_one(factory, worker_id="requirement-report-worker") is None

        saved = client.get(f"/api/v1/assistant/plans/{plan['plan_id']}")
        assert saved.status_code == 200, saved.text
        assert saved.json()["execution_batch_id"] == batch_id
        result = saved.json()["steps"][0]["result_ref"]["inline"]
        assert result["schema"] == report_document.RESULT_SCHEMA
        assert result["outcome"] == "success"
        assert result["report_type"] == "after_sales" and result["window_hours"] == 168
        assert result["artifact_id"] and result["download_url"]

        detail = client.get(f"/api/v1/reports/catalog/{result['artifact_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["verification"]["valid"] is True
        assert detail.json()["report_type"] == "report_after_sales"
        assert detail.json()["evidence_manifest"]
        download = client.get(result["download_url"] + "?format=markdown")
        assert download.status_code == 200, download.text
        assert download.headers["x-report-sha256"] == result["sha256"]
        assert "售后分析报告" in download.content.decode("utf-8")
        assert "证据清单" in download.content.decode("utf-8")

    with factory() as db:
        persisted = db.get(DBCapabilityPlan, plan["plan_id"])
        assert persisted.requested_by == REQUESTER_ID
        assert persisted.approved_by == APPROVER_ID
        assert db.query(DBCapabilityBatch).filter_by(batch_id=batch_id).count() == 1
        artifact = db.get(ReportArtifact, result["artifact_id"])
        assert artifact is not None and artifact.source_type == "report_document"
        approvals = db.query(SecurityAuditEvent).filter_by(
            category="capability_plan", target_id=plan["plan_id"], action="approve_plan").all()
        assert {row.actor_user_id for row in approvals} == {APPROVER_ID}


def test_report_window_excludes_old_inspection_and_capacity_evidence(mysql_storage):
    """A seven-day report must not be made successful by evidence last seen eight days ago."""
    _, factory = mysql_storage
    old = datetime.now() - timedelta(days=8)
    with factory() as db:
        job = db.get(DBInspectionJob, JOB_ID)
        job.created_at = old - timedelta(hours=1)
        job.finished_at = old
        alert = db.query(DBAlertEvent).filter_by(instance_id=10).one()
        alert.last_seen = old
        capacity = db.query(DBCapacityAlert).filter_by(instance_id=10).one()
        capacity.first_seen = old - timedelta(hours=1)
        capacity.last_seen = old
        db.commit()

        evidence = report_document.collect_evidence(db, db.get(DBInstance, 10), window_hours=168)
        assert evidence["inspection"] is None
        assert evidence["alerts"]["total"] == 0
        assert evidence["capacity"]["total"] == 0
        assert evidence["evidence"] == []


def test_effect_reference_rejects_old_row_substitution_and_cross_execution_replay(mysql_storage):
    """An existing domain row is not proof that this exact execution produced or touched it."""
    _, factory = mysql_storage
    with factory() as db:
        db.add(PrometheusAlertCache(
            id=99, alert_name="FixtureCPU", alert_status="firing", severity="warning",
            db_instance_id=10, instance_name="orders-mysql", labels_json={}, annotations_json={},
        ))
        db.commit()
        source = _approved_plan(db, "monitor.alert.sync", key="receipt-alert-source")
        finished = plans.execute_plan(
            db, source["plan_id"], actor=REQUESTER, execution_guard=lambda: None,
        )
        assert finished["outcome"] == "succeeded"
        source_ref = deepcopy(finished["steps"][0]["result_ref"])
        imported_ids = source_ref["inline"]["data"]["event_ids"]
        assert imported_ids and 1 not in imported_ids

        source_plan = plans._load(db, source["plan_id"], REQUESTER, owner_only=True)
        source_step = plans._steps(db, source["plan_id"])[0]
        old_row_ref = deepcopy(source_ref)
        old_row_ref["inline"]["data"]["event_ids"] = [1]
        old_row_ref["inline"]["data"]["count"] = 1
        source_step.result_ref_json = old_row_ref
        with pytest.raises(plans.PlanError, match="result_unknown"):
            plans.validate_effect_reference(db, source_plan, source_step)
        db.rollback()

        target = _approved_plan(db, "monitor.alert.sync", key="receipt-alert-target")
        target_plan = plans._load(db, target["plan_id"], REQUESTER, owner_only=True)
        target_step = plans._steps(db, target["plan_id"])[0]
        target_step.started_at = datetime.now()
        replay = deepcopy(source_ref)
        replay["inline"]["execution_id"] = f"{target['plan_id']}:1:1"
        target_step.result_ref_json = replay
        with pytest.raises(plans.PlanError, match="result_unknown"):
            plans.validate_effect_reference(db, target_plan, target_step)


def test_plan_only_reference_rejects_execution_blocked_false(mysql_storage):
    """A script-generation card can never claim that execution is permitted or completed."""
    _, factory = mysql_storage
    with factory() as db:
        created = _approved_plan(db, "operations.startup.plan", instance_id=11, key="receipt-plan-only")
        finished = plans.execute_plan(
            db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None,
        )
        assert finished["outcome"] == "succeeded"
        plan = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
        step = plans._steps(db, created["plan_id"])[0]
        tampered = deepcopy(step.result_ref_json)
        tampered["inline"]["data"]["execution_blocked"] = False
        step.result_ref_json = tampered
        with pytest.raises(plans.PlanError, match="result_unknown"):
            plans.validate_effect_reference(db, plan, step)


def test_oracle_batch_queue_metadata_and_collector_failure_are_truthful(mysql_storage, monkeypatch):
    """Synthetic Oracle failures must settle failed and never look like an empty success."""
    _, factory = mysql_storage
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *args, **kwargs: _answer(
        "performance.lock.read", {"page": 1, "page_size": 50},
    ))
    calls = []

    def failed_collector(instance, *, strict=False):
        calls.append((instance.id, strict))
        raise RuntimeError("controlled_oracle_query_failure")

    monkeypatch.setattr(oracle_api, "_get_blocking_tree", failed_collector)
    with _client(factory) as client:
        dispatched = client.post("/api/v1/assistant/dispatch-intent", json={
            "text": "检查所有 Oracle 数据库的锁等待",
            # An all-scope request must not be narrowed to the selected MySQL header instance.
            "instance_id": 10,
            "request_id": "requirement-oracle-lock-batch",
        })
        assert dispatched.status_code == 200, dispatched.text
        value = dispatched.json()
        assert value["action"] == "batch"
        assert {item["instance_id"] for item in value["plans"]} == {11, 12}
        batch_id = value["batch"]["batch_id"]

        queued = client.get(
            f"/api/v1/assistant/batches/{batch_id}",
            headers={"x-assistant-batch-version": "2"},
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["schema"] == "capability.batch_read/v2"
        assert {(item["instance_id"], item["db_type"], item["capability_id"])
                for item in queued.json()["items"]} == {
                    (11, "oracle", "performance.lock.read"),
                    (12, "oracle", "performance.lock.read"),
                }

        completed = []
        while item := plan_queue.process_one(factory, worker_id="requirement-oracle-worker"):
            completed.append(item)
        assert calls == [(11, True), (12, True)]
        assert len(completed) == 2
        assert all(item["queue_state"] == "archived" and item["outcome"] == "failed"
                   for item in completed)

        settled = client.get(
            f"/api/v1/assistant/batches/{batch_id}",
            headers={"x-assistant-batch-version": "2"},
        )
        assert settled.status_code == 200, settled.text
        body = settled.json()
        assert body["status"] == "settled" and body["outcome"] == "failed"
        assert body["outcomes"] == {"failed": 2}
        assert all(item["status"] == "settled" and item["outcome"] == "failed"
                   and item["outcome_kind"] == "handler_error"
                   and item["result_ref"]["steps"] == [] for item in body["items"])
        for item in body["items"]:
            plan = client.get(f"/api/v1/assistant/plans/{item['plan_id']}")
            assert plan.status_code == 200, plan.text
            assert plan.json()["outcome"] == "failed"
            assert plan.json()["steps"][0]["status"] == "failed"
            assert plan.json()["steps"][0]["result_ref"] is None

    with factory() as db:
        members = db.query(DBCapabilityExecution).filter_by(batch_id=batch_id).all()
        assert len(members) == 2 and all(member.state == "archived" for member in members)
