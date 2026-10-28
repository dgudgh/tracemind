"""Business goals must survive clarification and registered multi-step dispatch."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_plan import DBCapabilityPlan
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch as dispatch, capability_registry as registry, plan_service as plans

ACTOR = {"user_id": 1}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="goal-admin", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://GOAL_UNUSED"),
            DBInstance(id=11, instance_name="oracle", db_type="oracle", host="127.0.0.1", port=1521,
                       service_name="orcl", oracle_connect_type="service_name", username="unused",
                       enabled=1, password_cipher="env://GOAL_UNUSED"),
            AIProviderConfig(provider_name="fake", provider_type="openai_compatible",
                             base_url="http://127.0.0.1:9", model_name="fake", enabled=1, is_default=1),
        ])
        session.commit()
        registry.sync_definitions(session, actor=ACTOR)
        yield session
    engine.dispose()


def answer(monkeypatch, **extra):
    value = {"capability_id": "report.catalog.read", "params": {}, "confidence": 0.9, "reason": "test", **extra}
    monkeypatch.setattr(dispatch, "call_ai_provider", lambda *args, **kwargs: json.dumps(value))


def test_generic_root_cause_request_asks_for_context_not_a_context_lookup(db, monkeypatch):
    answer(monkeypatch, capability_id="diagnosis.investigation.read")
    result = dispatch.dispatch(db, actor=ACTOR, text="客户现场出现问题，需要进行端到端根因分析",
                               instance=db.get(DBInstance, 11), request_id="goal-clarify-001")
    assert result["action"] == "clarify"
    assert {q["key"] for q in result["questions"]} == {"symptom", "time_window", "impact_scope"}
    assert result["capability"]["capability_id"] == dispatch.RCA_CAPABILITY
    assert result["provider"]["name"] == "fake"
    assert db.query(DBCapabilityPlan).count() == 0
    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id="goal-clarify-001")
    assert receipt["questions"] == result["questions"]
    assert receipt["provider"] == result["provider"]


def test_filled_root_cause_goal_prepares_real_collect_plan_not_the_model_context_reader(db, monkeypatch):
    answer(monkeypatch, capability_id="diagnosis.investigation.read")
    monkeypatch.setattr(plans, "execute_plan", lambda *a, **k: pytest.fail("collection requires explicit execution"))
    details = {"symptom": "订单提交超时", "time_window": "今天10:00至10:30", "impact_scope": "订单接口"}
    result = dispatch.dispatch(db, actor=ACTOR, text="客户现场问题，请进行端到端根因分析",
                               instance=db.get(DBInstance, 11), request_id="goal-collect-001", answers=details)
    assert result["action"] == "confirm"
    assert result["plan"]["capability_id"] == dispatch.RCA_CAPABILITY
    assert result["plan"]["steps"][0]["params"] == details
    assert result["plan"]["status"] == "validated"
    assert result["plan"]["approval_state"] == "pending"
    assert result["request_id"] == "goal-collect-001"


def test_model_can_clarify_a_non_diagnosis_goal_without_executing(db, monkeypatch):
    answer(monkeypatch, decision="clarify", questions=[{"key": "report_type", "label": "需要哪类报告？"}])
    result = dispatch.dispatch(db, actor=ACTOR, text="帮我处理报告", request_id="goal-report-001")
    assert result["action"] == "clarify"
    assert result["questions"][0]["key"] == "report_type"
    assert db.query(DBCapabilityPlan).count() == 0


def test_two_registered_read_cards_execute_as_one_persisted_plan(db, monkeypatch):
    answer(monkeypatch, steps=[
        {"capability_id": "report.catalog.read", "params": {"page": 1, "page_size": 10}, "title": "读取报告"},
        {"capability_id": "inspection.run.query", "params": {"page": 1, "page_size": 10}, "title": "读取巡检任务"},
    ], goal="核对已有报告和巡检任务")
    result = dispatch.dispatch(db, actor=ACTOR, text="列出报告和巡检任务", instance=db.get(DBInstance, 10),
                               request_id="goal-multiple-001")
    assert result["action"] == "executed"
    assert result["plan"]["outcome"] == "succeeded"
    assert [step["status"] for step in result["plan"]["steps"]] == ["succeeded", "succeeded"]
    assert db.query(DBCapabilityPlan).count() == 1


def test_page_navigation_creates_no_plan(db, monkeypatch):
    answer(monkeypatch, decision="navigate", target_key="report.unified")
    result = dispatch.dispatch(db, actor=ACTOR, text="打开统一报告页面", request_id="goal-navigate-001")
    assert result["action"] == "navigate"
    assert result["target_key"] == "report.unified"
    assert db.query(DBCapabilityPlan).count() == 0


def test_all_scope_is_not_narrowed_by_the_selected_engine(db, monkeypatch):
    answer(monkeypatch, capability_id="performance.slow_sql.read", params={"page": 1, "page_size": 10})
    result = dispatch.dispatch(db, actor=ACTOR, text="检查所有数据库的慢SQL", instance=db.get(DBInstance, 10),
                               request_id="goal-all-001")
    assert result["action"] == "batch"
    assert {row["instance_id"] for row in result["plans"]} == {10, 11}


def test_explicitly_excluded_engine_never_reaches_the_model_or_execution(db, monkeypatch):
    monkeypatch.setattr(dispatch, "call_ai_provider", lambda *a, **k: pytest.fail("excluded target"))
    result = dispatch.dispatch(db, actor=ACTOR, text="不要检查Oracle，只检查MySQL的慢SQL",
                               instance=db.get(DBInstance, 11), request_id="goal-exclude-001")
    assert result["reason_code"] == "target_engine_mismatch"
    assert db.query(DBCapabilityPlan).count() == 0


def test_failed_claim_is_queryable_and_never_reinvokes_model(db, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "call_ai_provider", lambda *a, **k: calls.append(1) or json.dumps({
        "capability_id": "report.catalog.read", "params": {}, "confidence": 0.9, "reason": "test"}))
    monkeypatch.setattr(plans, "create_plan", lambda *a, **k: (_ for _ in ()).throw(plans.PlanError("scope_denied")))
    with pytest.raises(plans.PlanError):
        dispatch.dispatch(db, actor=ACTOR, text="列出报告", request_id="goal-failed-001")
    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id="goal-failed-001")
    assert receipt["action"] == "failed" and receipt["reason_code"] == "scope_denied"
    assert dispatch.dispatch(db, actor=ACTOR, text="列出报告", request_id="goal-failed-001")["action"] == "failed"
    assert len(calls) == 1


def test_no_model_does_not_prepare_or_execute_root_cause(db, monkeypatch):
    db.query(AIProviderConfig).update({"enabled": 0})
    db.commit()
    monkeypatch.setattr(dispatch, "call_ai_provider", lambda *a, **k: pytest.fail("no model"))
    result = dispatch.dispatch(db, actor=ACTOR, text="进行端到端根因分析", instance=db.get(DBInstance, 11))
    assert result["reason_code"] == "ai_not_configured"
    assert db.query(DBCapabilityPlan).count() == 0


def test_model_cannot_invent_missing_customer_incident_facts(db, monkeypatch):
    answer(monkeypatch, capability_id=dispatch.RCA_CAPABILITY, params={
        "symptom": "订单全部超时", "time_window": "今天10点", "impact_scope": "所有订单用户"})
    result = dispatch.dispatch(db, actor=ACTOR, text="客户现场出现问题，需要进行端到端根因分析",
                               instance=db.get(DBInstance, 10), request_id="goal-no-invent-001")
    assert result["action"] == "clarify"
    assert {q["key"] for q in result["questions"]} == {"symptom", "time_window", "impact_scope"}
    assert db.query(DBCapabilityPlan).count() == 0


def test_changed_clarification_answers_cannot_reuse_the_same_request_key(db, monkeypatch):
    answer(monkeypatch, capability_id="diagnosis.investigation.read")
    dispatch.dispatch(db, actor=ACTOR, text="根因分析", request_id="goal-answer-key-001", answers={"symptom": "订单超时"})
    with pytest.raises(plans.PlanError, match="idempotency_conflict"):
        dispatch.dispatch(db, actor=ACTOR, text="根因分析", request_id="goal-answer-key-001", answers={"symptom": "登录失败"})
    receipt = dispatch.read_receipt(db, actor=ACTOR, request_id="goal-answer-key-001")
    assert receipt["answers"]["symptom"] == "订单超时"


def test_model_navigation_cannot_replace_an_explicit_diagnosis_goal(db, monkeypatch):
    answer(monkeypatch, decision="navigate", capability_id=dispatch.RCA_CAPABILITY, target_key="diagnose.smart")
    result = dispatch.dispatch(db, actor=ACTOR, text="请对客户现场订单超时做端到端根因分析",
                               instance=db.get(DBInstance, 10), request_id="goal-no-nav-001")
    assert result["action"] == "clarify"
    assert {q["key"] for q in result["questions"]} == {"symptom", "time_window", "impact_scope"}
    assert db.query(DBCapabilityPlan).count() == 0


def test_explicit_open_page_never_executes_a_model_selected_reader(db, monkeypatch):
    answer(monkeypatch, capability_id="performance.slow_sql.read")
    result = dispatch.dispatch(db, actor=ACTOR, text="打开慢SQL分析页面", request_id="goal-page-only-001")
    assert result["action"] == "navigate"
    assert result["target_key"] == "perf.slowsql"
    assert db.query(DBCapabilityPlan).count() == 0


def test_querying_root_cause_history_cannot_create_a_new_diagnosis(db, monkeypatch):
    answer(monkeypatch, capability_id=dispatch.RCA_CAPABILITY)
    result = dispatch.dispatch(db, actor=ACTOR, text="查看已有根因分析结果", request_id="goal-history-001")
    assert result["action"] == "navigate"
    assert result["target_key"] == "diagnose.smart"
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("requirement", [
    "对当前 MySQL 实例做一次巡检",
    "不要查看旧巡检结果，重新执行一次巡检",
    "停止巡检后再执行一次巡检",
    "执行巡检并生成报告",
])
def test_unreleased_inspection_cannot_be_replaced_by_model_selected_collectors(db, monkeypatch, requirement):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", False)
    answer(monkeypatch, capability_id="operations.runtime_overview.read", steps=[
        {"capability_id": "operations.runtime_overview.read", "params": {}, "title": "运行概览"},
        {"capability_id": "performance.workload.collect", "params": {}, "title": "采集工作负载"},
    ])
    monkeypatch.setattr(plans, "create_plan", lambda *a, **k: pytest.fail("no substitute inspection plan"))
    value = dispatch.dispatch(db, actor=ACTOR, text=requirement,
                              instance=db.get(DBInstance, 10))
    assert value["action"] == "navigate"
    assert value["reason_code"] == "inspection_not_available"
    assert value["target_key"] is None  # The unreleased card is not an authorized navigation binding.
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("text", [
    "打开巡检页面", "查看已有巡检结果", "不要做巡检，只列出报告",
    "准备一份巡检报告", "进行巡检结果汇总", "查看执行巡检的历史记录",
    "不执行巡检", "取消巡检", "停止巡检后查看报告",
])
def test_inspection_execution_guard_preserves_navigation_history_and_negation(text):
    assert dispatch._inspection_execution_goal(text) is False


@pytest.mark.parametrize("requirement", ["准备一份巡检报告", "进行巡检结果汇总"])
def test_inspection_report_goal_still_prepares_report_without_inspection_gate(db, monkeypatch, requirement):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", False)
    answer(monkeypatch, capability_id="report.document.generate", params={"report_type": "unified", "include_ai": False})
    value = dispatch.dispatch(db, actor=ACTOR, text=requirement, instance=db.get(DBInstance, 10))
    assert value["action"] == "confirm"
    assert value["plan"]["capability_id"] == "report.document.generate"


@pytest.mark.parametrize("requirement", ["执行一次巡检", "执行巡检并生成报告"])
def test_released_inspection_still_cannot_use_model_collectors_as_a_substitute(db, monkeypatch, requirement):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    answer(monkeypatch, capability_id="performance.workload.collect", params={"policy_id": 999}, steps=[
        {"capability_id": "performance.workload.collect", "params": {"policy_id": 999}, "title": "替代采集"},
        {"capability_id": "report.document.generate", "params": {}, "title": "生成报告"},
    ])
    monkeypatch.setattr(plans, "create_plan", lambda *a, **k: pytest.fail("cannot turn wrong selection into inspection"))
    value = dispatch.dispatch(db, actor=ACTOR, text=requirement, instance=db.get(DBInstance, 10))
    assert value["action"] == "navigate"
    assert value["reason_code"] == "inspection_capability_required"
    assert value["target_key"] == "inspect.run"
    assert db.query(DBCapabilityPlan).count() == 0


def test_navigation_target_must_come_from_available_candidate_bindings():
    candidates = {"inspection.template.read": {"bindings": [{"target_key": "inspect.templates"}]}}
    assert dispatch._available_navigation_target(candidates, "inspect.run") is None
    assert dispatch._available_navigation_target(candidates, "inspect.templates") == "inspect.templates"


def test_model_normalized_time_separator_preserves_customer_provided_rca_context(db, monkeypatch):
    answer(monkeypatch, capability_id=dispatch.RCA_CAPABILITY, params={
        "symptom": "订单提交超时", "time_window": "今天 10:00-10:30", "impact_scope": "订单接口"})
    result = dispatch.dispatch(db, actor=ACTOR,
        text="请对当前实例做端到端根因分析：订单提交超时，今天 10:00 至 10:30，影响订单接口",
        instance=db.get(DBInstance, 10))
    assert result["action"] == "confirm"
    assert result["plan"]["steps"][0]["params"]["time_window"] == "今天 10:00-10:30"
    assert result["plan"]["approval_state"] == "pending"


def test_normalizing_time_separator_does_not_accept_an_invented_time_window(db, monkeypatch):
    answer(monkeypatch, capability_id=dispatch.RCA_CAPABILITY, params={
        "symptom": "订单提交超时", "time_window": "今天 09:00-10:30", "impact_scope": "订单接口"})
    result = dispatch.dispatch(db, actor=ACTOR,
        text="请对当前实例做端到端根因分析：订单提交超时，今天 10:00 至 10:30，影响订单接口",
        instance=db.get(DBInstance, 10))
    assert result["action"] == "clarify"
    assert [question["key"] for question in result["questions"]] == ["time_window"]
    assert result["answers"] == {"symptom": "订单提交超时", "impact_scope": "订单接口"}
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("time_window", ["今天 10:00", "今天 10:00-10", "10:30", "今天 10:00-10:3"])
def test_grounded_rca_time_window_cannot_shrink_to_a_range_substring(db, monkeypatch, time_window):
    answer(monkeypatch, capability_id=dispatch.RCA_CAPABILITY, params={
        "symptom": "订单提交超时", "time_window": time_window, "impact_scope": "订单接口"})
    result = dispatch.dispatch(db, actor=ACTOR,
        text="请对当前实例做端到端根因分析：订单提交超时，今天 10:00 至 10:30，影响订单接口",
        instance=db.get(DBInstance, 10))
    assert result["action"] == "clarify"
    assert [question["key"] for question in result["questions"]] == ["time_window"]
    assert result["answers"] == {"symptom": "订单提交超时", "impact_scope": "订单接口"}
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize(("text", "value", "accepted"), [
    ("故障时间今天10:00至10:30", "今天10:00-10:30", True),
    ("故障时间今天10点到11点", "今天10点-11点", True),
    ("故障时间2026-10-20至2026-10-23", "2026-10-20-2026-10-23", True),
    ("故障时间2026-10-20至2026-10-23", "2026-10-20", False),
])
def test_complete_time_range_grounding(text, value, accepted):
    assert dispatch._grounded_time_window(value, text) is accepted
