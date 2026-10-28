"""AI dispatch only chooses registered capabilities; reads execute, effects require confirmation."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.api.v1 import assistant
from app.cmdb.models import AssetInstance
from app.core.database import Base, get_db
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_plan import DBCapabilityPlan
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch, capability_registry as registry, plan_service as plans, plan_governance as governance

ACTOR = {"user_id": 1}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="dispatch-admin", password_hash="unused", role="admin", status="active"),
            User(id=4, username="dispatch-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="orders", db_type="mysql", host="10.0.0.10", port=3306,
                       username="unused", enabled=1, password_cipher="env://DISPATCH"),
            DBInstance(id=11, instance_name="ora", db_type="oracle", host="10.0.0.11", port=1521,
                       service_name="orcl", oracle_connect_type="service_name", username="system",
                       password_cipher="env://DISPATCH", enabled=1),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="10.0.0.10", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
            AIProviderConfig(provider_name="fake", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="fake", enabled=1, is_default=1),
        ])
        session.commit()
        registry.sync_definitions(session, actor=ACTOR)
        yield session
    engine.dispose()


def _answer(monkeypatch, capability_id, params=None):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: json.dumps({
        "capability_id": capability_id, "params": params or {}, "confidence": 0.9, "reason": "test"}))


def test_model_candidates_only_offer_effects_with_a_durable_result_contract():
    common = {"execution": "server", "plan_executable": True,
              "availability": "available", "handler_status": "wired", "kind": "collect"}
    known = {**common, "capability_id": "report.document.generate", "definition_version": 3,
             "output_schema": "report.document_result/v1"}
    transport_only = {**common, "capability_id": "report.document.generate", "definition_version": 2,
                      "output_schema": "object"}
    future = {**common, "capability_id": "new.unproved.collect", "definition_version": 1,
              "output_schema": "object"}
    read = {**common, "capability_id": "report.catalog.read", "kind": "read"}
    assert capability_dispatch._candidates({"items": [known, transport_only, future, read]}) == [known, read]


def test_read_capability_is_executed_through_a_plan(db, monkeypatch):
    _answer(monkeypatch, "report.catalog.read", {"page": 1, "page_size": 10})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告", instance=db.get(DBInstance, 10))
    assert value["action"] == "executed"
    assert value["plan"]["outcome"] == "succeeded"
    assert value["capability"]["capability_id"] == "report.catalog.read"
    assert value["provider"]["model"] == "fake"
    assert value["provider"]["revision"]
    assert isinstance(value["duration_ms"], int)


def test_effect_capability_is_only_confirmed(db, monkeypatch):
    _answer(monkeypatch, "report.document.generate", {"report_type": "unified", "include_ai": False})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="生成报告", instance=db.get(DBInstance, 10))
    assert value["action"] == "confirm"
    assert value["plan"]["status"] == "validated"


def test_unregistered_or_unknown_capability_falls_back_to_navigation(db, monkeypatch):
    _answer(monkeypatch, "fake.unknown.read")
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="随便做点什么")
    assert value["action"] == "navigate"


def test_no_provider_returns_local_navigation(db, monkeypatch):
    db.query(AIProviderConfig).update({"enabled": 0})
    db.commit()
    monkeypatch.setattr(capability_dispatch, "call_ai_provider",
                        lambda *a, **k: pytest.fail("a disabled provider must not be called"))
    monkeypatch.setattr(plans, "create_plan", lambda *a, **k: pytest.fail("no provider must create no plan"))
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告")
    assert value["action"] == "navigate" and value["source"] == "local"
    assert db.query(DBCapabilityPlan).count() == 0


def test_current_instance_engine_filters_the_catalog(db, monkeypatch):
    original = registry.list_capabilities
    observed = []

    def filtered(session, **kwargs):
        observed.append(kwargs.get("engine"))
        return original(session, **kwargs)

    monkeypatch.setattr(capability_dispatch.registry, "list_capabilities", filtered)
    _answer(monkeypatch, None)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="看看当前数据库",
                                           instance=db.get(DBInstance, 10))
    assert value["action"] == "navigate"
    assert observed == ["mysql"]


def test_prompt_includes_bounded_parameter_contract(db):
    catalog = registry.list_capabilities(db, actor=ACTOR, engine="mysql")
    candidates = capability_dispatch._candidates(catalog)
    prompt = capability_dispatch._prompt("生成报告", candidates, db.get(DBInstance, 10))
    assert "params_schema=" in prompt
    assert '"required"' in prompt
    assert '"type"' in prompt
    assert 'target_key是前端菜单页面标识，不是目标实例名或实例ID' in prompt
    assert 'execute或clarify时必须为null' in prompt


def test_execution_target_is_bound_by_server_and_never_taken_from_model_menu_key(db, monkeypatch):
    # Real provider regression: the ambiguous old prompt put "orders-mysql" in
    # target_key and rejected otherwise correct capability/params selections.
    current = db.get(DBInstance, 10)
    current.instance_name = "orders-mysql"
    db.commit()
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: json.dumps({
        "decision": "execute", "capability_id": "report.document.generate",
        "params": {"report_type": "after_sales", "window_hours": 168, "include_ai": False, "format": "markdown"},
        "steps": [], "questions": [], "target_key": None, "goal": "生成最近7天售后报告",
        "confidence": 0.95, "reason": "实例与参数均已明确"}))
    result = capability_dispatch.dispatch(db, actor=ACTOR, text="生成这个实例最近7天的售后报告，不需要AI解读",
                                          instance=current)
    assert result["action"] == "confirm"
    assert result["plan"]["instance_id"] == 10
    assert result["plan"]["steps"][0]["params"]["window_hours"] == 168
    assert result["plan"]["steps"][0]["params"]["include_ai"] is False


def test_model_instance_name_cannot_be_accepted_as_a_menu_identifier(db, monkeypatch):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: json.dumps({
        "decision": "navigate", "capability_id": "report.catalog.read", "params": {},
        "target_key": "orders-mysql", "confidence": 0.95, "reason": "open"}))
    result = capability_dispatch.dispatch(db, actor=ACTOR, text="打开报告页面", instance=db.get(DBInstance, 10))
    assert result["reason_code"] == "invalid_output"
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("raw", [
    '{"capability_id":"report.catalog.read","capability_id":"report.catalog.read",'
    '"params":{},"confidence":0.9,"reason":"x"}',
    '{"capability_id":"report.catalog.read","params":{},"confidence":NaN,"reason":"x"}',
    '{"capability_id":"report.catalog.read","params":{},"confidence":0.9,"reason":"x","extra":1}',
    '{"capability_id":"report.catalog.read","params":{},"confidence":2,"reason":"x"}',
])
def test_invalid_model_contract_navigates_without_a_plan(db, monkeypatch, raw):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: raw)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                           instance=db.get(DBInstance, 10))
    assert value["action"] == "navigate" and value["source"] == "local"
    assert value["reason_code"] == "invalid_output"
    assert db.query(DBCapabilityPlan).count() == 0


def test_only_default_provider_is_called_without_cross_provider_failover(db, monkeypatch):
    db.add(AIProviderConfig(provider_name="second", provider_type="openai_compatible",
                            base_url="http://127.0.0.1:8", model_name="second",
                            enabled=1, is_default=0))
    db.commit()
    called = []

    def invalid(config, *args, **kwargs):
        called.append(config.provider_name)
        return "not-json"

    monkeypatch.setattr(capability_dispatch, "call_ai_provider", invalid)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告")
    assert value["action"] == "navigate"
    assert called == ["fake"]
    assert db.query(DBCapabilityPlan).count() == 0


def test_empty_candidate_catalog_does_not_call_the_model(db, monkeypatch):
    monkeypatch.setattr(capability_dispatch.registry, "list_capabilities",
                        lambda *a, **k: {"items": []})
    monkeypatch.setattr(capability_dispatch, "call_ai_provider",
                        lambda *a, **k: pytest.fail("an empty catalog must not reach the model"))
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告")
    assert value["action"] == "navigate" and value["source"] == "local"
    assert db.query(DBCapabilityPlan).count() == 0


def test_low_confidence_match_requires_manual_selection(db, monkeypatch):
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: json.dumps({
        "capability_id": "report.catalog.read", "params": {}, "confidence": 0.0, "reason": "uncertain"}))
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="也许看看报告")
    assert value["action"] == "navigate" and value["source"] == "ai"
    assert "capability" not in value
    assert db.query(DBCapabilityPlan).count() == 0


def test_invalid_capability_params_return_the_selected_capability_for_manual_input(db, monkeypatch):
    _answer(monkeypatch, "report.catalog.read", {"unknown": 1})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告")
    assert value["action"] == "navigate" and value["source"] == "ai"
    assert value["capability"]["capability_id"] == "report.catalog.read"
    assert db.query(DBCapabilityPlan).count() == 0


def test_plan_creation_error_propagates_after_one_model_call(db, monkeypatch):
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        return json.dumps({"capability_id": "report.catalog.read",
                           "params": {"page": 1, "page_size": 10},
                           "confidence": 0.9, "reason": "test"})

    monkeypatch.setattr(capability_dispatch, "call_ai_provider", answer)
    monkeypatch.setattr(plans, "create_plan", lambda *a, **k: (_ for _ in ()).throw(plans.PlanError("scope_denied")))
    with pytest.raises(plans.PlanError, match="scope_denied"):
        capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告", instance=db.get(DBInstance, 10))
    assert calls == [1]


def test_execution_error_propagates_without_another_model_call(db, monkeypatch):
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        return json.dumps({"capability_id": "report.catalog.read",
                           "params": {"page": 1, "page_size": 10},
                           "confidence": 0.9, "reason": "test"})

    monkeypatch.setattr(capability_dispatch, "call_ai_provider", answer)
    monkeypatch.setattr(plans, "execute_plan", lambda *a, **k: (_ for _ in ()).throw(plans.PlanError("target_changed")))
    with pytest.raises(plans.PlanError, match="target_changed"):
        capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告", instance=db.get(DBInstance, 10))
    assert calls == [1]
    assert db.query(DBCapabilityPlan).count() == 1


@pytest.fixture()
def dispatch_http(monkeypatch):
    database = SimpleNamespace(rollback=lambda: None)

    def isolated_db():
        yield database

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[assistant._actor] = lambda: ACTOR
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_dispatch_http_rejects_extra_fields_before_service(dispatch_http, monkeypatch):
    monkeypatch.setattr(assistant.capability_dispatch, "dispatch",
                        lambda *a, **k: pytest.fail("invalid input must not reach dispatch"))
    response = dispatch_http.post("/api/v1/assistant/dispatch-intent",
                                  json={"text": "列出报告", "extra": True})
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "invalid_dispatch_request"}


def test_dispatch_http_maps_plan_errors_instead_of_navigating(dispatch_http, monkeypatch):
    def denied(*args, **kwargs):
        raise plans.PlanError("scope_denied")

    monkeypatch.setattr(assistant.capability_dispatch, "dispatch", denied)
    response = dispatch_http.post("/api/v1/assistant/dispatch-intent", json={"text": "列出报告"})
    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "scope_denied"}


def test_same_request_id_replays_without_second_model_call(db, monkeypatch):
    calls = {"n": 0}
    def answer(*args, **kwargs):
        calls["n"] += 1
        return json.dumps({"capability_id": "report.catalog.read", "params": {"page": 1, "page_size": 10},
                           "confidence": 0.9, "reason": "test"})
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", answer)
    first = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                         instance=db.get(DBInstance, 10), request_id="req-replay-1")
    second = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                          instance=db.get(DBInstance, 10), request_id="req-replay-1")
    assert calls["n"] == 1
    assert second.get("replayed") is True
    assert first["plan"]["plan_id"] == second["plan"]["plan_id"]


def test_confirmed_effect_plan_can_be_approved_and_executed(db, monkeypatch):
    _answer(monkeypatch, "operations.startup.plan")
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="生成启动方案",
                                         instance=db.get(DBInstance, 11))
    assert value["action"] == "confirm"
    plan_id = value["plan"]["plan_id"]
    revision = value["plan"]["revision"]
    governance.request_approval(db, plan_id, actor=ACTOR, expected_revision=revision)
    row = plans._load(db, plan_id, ACTOR, owner_only=True)
    governance.approve_plan(db, plan_id, actor={"user_id": 4},
                            expected_revision=revision, expected_binding_hash=row.approval_binding_hash)
    finished = plans.execute_plan(db, plan_id, actor=ACTOR, execution_guard=lambda: None)
    assert finished["outcome"] == "succeeded"
    result = finished["steps"][0]["result_ref"]["inline"]
    assert result["schema"] == "capability.effect_result/v1"
    assert result["data"]["plan_only"] is True
    assert result["data"]["execution_blocked"] is True


def test_no_matching_capability_falls_back_to_registered_workflow(db, monkeypatch):
    _answer(monkeypatch, None)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="列出巡检任务",
                                         instance=db.get(DBInstance, 10))
    assert value["action"] == "workflow"
    assert value["workflow"]["workflow_id"] == "inspection.jobs.list"
    assert value["plan"]["plan_id"].startswith("pl")


def test_workflow_without_instance_requires_manual_target(db, monkeypatch):
    _answer(monkeypatch, None)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="查看巡检上下文")
    assert value["action"] == "navigate"
    assert value["reason_code"] == "missing_instance"


def test_all_instances_read_is_persisted_as_a_batch(db, monkeypatch):
    _answer(monkeypatch, "performance.slow_sql.read", {"page": 1, "page_size": 10})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="帮我巡检所有数据库",
                                         instance=db.get(DBInstance, 10))
    assert value["action"] == "batch"
    assert value["batch"]["batch_id"]
    assert value["batch"]["total"] == len(value["plans"])


def test_dispatch_receipt_can_be_read_by_request_id(db, monkeypatch):
    _answer(monkeypatch, "report.catalog.read", {"page": 1, "page_size": 10})
    first = capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                         instance=db.get(DBInstance, 10), request_id="req-read-receipt")
    cached = capability_dispatch.read_receipt(db, actor=ACTOR, request_id="req-read-receipt")
    assert cached["replayed"] is True
    assert cached["plan"]["plan_id"] == first["plan"]["plan_id"]
    assert capability_dispatch.read_receipt(db, actor=ACTOR, request_id="req-missing") is None


def test_same_request_id_with_different_input_conflicts(db, monkeypatch):
    _answer(monkeypatch, "report.catalog.read", {"page": 1, "page_size": 10})
    capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                 instance=db.get(DBInstance, 10), request_id="req-conflict")
    with pytest.raises(plans.PlanError) as error:
        capability_dispatch.dispatch(db, actor=ACTOR, text="换一个需求",
                                     instance=db.get(DBInstance, 10), request_id="req-conflict")
    assert error.value.code == "idempotency_conflict"


def test_dispatch_metrics_aggregate_audit_rows(db, monkeypatch):
    from starlette.requests import Request
    from app.api.v1 import assistant as assistant_api
    _answer(monkeypatch, "report.catalog.read", {"page": 1, "page_size": 10})
    capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告", instance=db.get(DBInstance, 10),
                                 request_id="metrics-1")
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: "not-json")
    capability_dispatch.dispatch(db, actor=ACTOR, text="坏响应", request_id="metrics-2")
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/assistant/dispatch-metrics", "headers": []})
    request.state.auth = {"user_id": 1, "username": "admin", "role": "admin"}
    value = assistant_api.dispatch_metrics(request, actor=ACTOR, db=db, days=7)
    assert value["schema"] == "assistant.dispatch_metrics/v1"
    assert value["total"] >= 2
    assert value["by_action"]["executed"] >= 1
    assert value["by_reason"]["invalid_output"] >= 1


def test_explicit_oracle_request_does_not_run_on_selected_mysql(db, monkeypatch):
    _answer(monkeypatch, "performance.slow_sql.read", {"page": 1, "page_size": 10})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="检查当前Oracle数据库的锁等待",
                                         instance=db.get(DBInstance, 10))
    assert value["action"] == "navigate"
    assert value["reason_code"] == "target_engine_mismatch"


def test_explicit_oracle_batch_only_targets_oracle(db, monkeypatch):
    _answer(monkeypatch, "performance.lock.read", {"page": 1, "page_size": 10})
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="检查所有Oracle数据库的锁等待",
                                         instance=db.get(DBInstance, 10))
    assert value["action"] == "batch"
    assert {plan["instance_id"] for plan in value["plans"]} == {11}


def test_workflow_navigation_does_not_prepare_a_plan(db, monkeypatch):
    _answer(monkeypatch, None)
    value = capability_dispatch.dispatch(db, actor=ACTOR, text="打开巡检任务")
    assert value["action"] == "navigate"
    assert value["reason_code"] == "workflow_navigation_only"
    assert "plan" not in value


def test_receipt_read_rechecks_current_plan_visibility(db, monkeypatch):
    _answer(monkeypatch, "report.catalog.read", {"page": 1, "page_size": 10})
    capability_dispatch.dispatch(db, actor=ACTOR, text="列出报告",
                                 instance=db.get(DBInstance, 10), request_id="req-revoked")
    original = plans._load
    monkeypatch.setattr(plans, "_load", lambda *a, **k: (_ for _ in ()).throw(
        plans.PlanError("permission_denied")))
    assert capability_dispatch.read_receipt(db, actor=ACTOR, request_id="req-revoked") is None
    monkeypatch.setattr(plans, "_load", original)


def test_receipt_route_is_registered_for_restricted_scope():
    from app.services import request_scope
    rule = request_scope._rule("/assistant/dispatch-intent/{request_id}", "GET", {"request_id": "req-1"})
    assert rule is not None
    assert rule.location == "catalog"


def test_missing_request_id_gets_a_unique_server_key(db, monkeypatch):
    _answer(monkeypatch, None)
    first = capability_dispatch.dispatch(db, actor=ACTOR, text="列出巡检任务",
                                         instance=db.get(DBInstance, 10))
    second = capability_dispatch.dispatch(db, actor=ACTOR, text="列出巡检模板",
                                          instance=db.get(DBInstance, 10))
    assert first["action"] == "workflow" and second["action"] == "workflow"
    assert first["request_id"] != second["request_id"]
    assert first["plan"]["plan_id"] != second["plan"]["plan_id"]


def test_workflow_navigation_targets_are_real_modules_and_skip_pageless_capability():
    """导航目标必须是真实存在的模块；无页面的能力不得留导航项。

    能力矩阵（`operations.capability_matrix.read`）已改为无页面：能力仍在（handler=wired），
    但 `target_key` 为空。若这里把 `operations.capabilities.list` 加回 WORKFLOW_NAV，
    模型就会导航到一个已删除的页面——点了没反应，且不会有任何报错。
    """
    from app.api.v1.module_config import DEFAULT_MODULES

    keys = {item["module_key"] for item in DEFAULT_MODULES}
    dangling = {workflow: target for workflow, target in capability_dispatch.WORKFLOW_NAV.items()
                if target not in keys}
    assert not dangling, f"导航目标不是真实模块：{dangling}"
    assert "operations.capabilities.list" not in capability_dispatch.WORKFLOW_NAV
    assert "ops.matrix" not in keys
