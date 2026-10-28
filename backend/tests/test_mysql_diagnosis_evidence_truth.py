"""ISS-58: real isolated SQLite/HTTP persistence; collectors are offline fixtures, not FIELD."""
from copy import deepcopy
import hashlib
import json
import socket
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.analyzers.mysql import mysql_diagnosis_engine as diagnosis
from app.api.v1.mysql_ops import operations_mysql_router
from app.core.database import Base, get_db
from app.models.db_instance import DBInstance
from app.models.inspection_diagnosis import DBInspectionModuleResult, DBInspectionTask


@pytest.fixture
def env(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("ISS-58 fixtures must not contact a real DB, network or model")

    monkeypatch.setattr(socket.socket, "connect", deny)
    engine = create_engine(f"sqlite:///{tmp_path / 'evidence.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        instance = DBInstance(instance_name="ISS-58 offline fixture", db_type="mysql", host="fixture.invalid",
                              port=3306, username="fixture", password_cipher="fixture-unused", enabled=1)
        db.add(instance)
        db.commit()
        instance_id = instance.id
    app = FastAPI()
    app.include_router(operations_mysql_router)

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app) as client:
        yield SimpleNamespace(factory=factory, client=client, instance_id=instance_id)
    engine.dispose()


def basic(**patch):
    return {"raw": {"variables": {"version": "8.0.36", "max_connections": 100},
                    "status": {"Threads_connected": 0}}, "summary": {"risk_level": "normal"}, **patch}


def run_pair(env, monkeypatch, payload, binlog=None):
    specs = diagnosis._module_specs()
    monkeypatch.setattr(diagnosis, "_module_specs", lambda: [specs[0], specs[3]])
    monkeypatch.setattr(diagnosis, "get_basic_info", lambda instance: deepcopy(payload))
    monkeypatch.setattr(diagnosis, "get_binlog_overview", lambda instance: deepcopy(binlog if binlog is not None else {
        "variables": {"log_bin": "ON"}, "summary": {"risk_level": "normal"}}))
    result = env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    assert result.status_code == 200, result.text
    return result.json()


def all_reads(env, saved):
    base = f"/operations/mysql/{env.instance_id}/inspection"
    task_id = saved["task"]["task_id"]
    detail = env.client.get(base + "/" + task_id)
    latest = env.client.get(base + "/latest")
    report = env.client.get(base + f"/{task_id}/report")
    ai = env.client.post(base + f"/{task_id}/ai-analyze")
    modules = env.client.get(base + f"/{task_id}/modules")
    ticket = env.client.post(base + f"/{task_id}/ticket-plan")
    for response in (detail, latest, report, ai, modules, ticket):
        assert response.status_code == 200, response.text
    assert detail.json() == latest.json() == saved
    assert report.json() == saved["report"]
    assert modules.json()["items"] == saved["modules"]
    assert ai.json()["ai_input"]["inspection_result"] == saved["task"]
    assert ai.json()["ai_input"]["module_scores"] == saved["modules"]
    assert ai.json()["analysis"]["summary"] == saved["task"]["summary"]
    assert ticket.json()["ticket_plan"]["priority"] == saved["task"]["risk_level"]
    return ai.json()


@pytest.mark.parametrize("payload,state", [
    ({}, "unknown"),
    ({"error": "offline permission refused", "summary": {"risk_level": "normal"}}, "failed"),
    ({"status": "degraded", "summary": {"risk_level": "normal", "table_count": 0}, "sections": {}}, "failed"),
    ({"status": "ok", "summary": {"risk_level": "normal", "count": 0}, "raw": {"digest_count": 0}}, "unknown"),
    (basic(warnings=["one required query unavailable"]), "partial"),
    (basic(execution_status="unknown"), "unknown"),
    (basic(summary={"risk_level": "unknown"}), "unknown"),
    (basic(data_quality="missing"), "unknown"),
])
def test_incomplete_collector_through_commit_fresh_session_http_report_and_ai(env, monkeypatch, payload, state):
    saved = run_pair(env, monkeypatch, payload)
    module = saved["modules"][0]
    assert module["execution_status"] == state
    assert module["score"] is None
    assert module["risk_level"] != "normal"
    assert "未发现明显" not in module["summary"]
    assert saved["task"]["task_status"] == "partial", "Other module has real evidence; not whole success"
    assert saved["task"]["overall_score"] is None
    assert saved["report"]["risk_score"] is None
    assert saved["report"]["task_status"] == "partial"
    assert "未生成整体健康评分" in saved["report"]["overall_conclusion"]
    all_reads(env, saved)
    with env.factory() as db:
        stored = db.scalar(select(DBInspectionTask))
        assert stored.overall_score is None
        assert db.scalar(select(DBInspectionModuleResult).order_by(DBInspectionModuleResult.id)).score is None


def test_the_real_default_loop_retains_failed_modules_not_an_empty_list(env, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("offline collector refused")

    for name in ("get_basic_info", "get_replication_status", "get_binlog_overview", "get_innodb_status",
                 "get_lock_transaction_center", "get_slow_sql_center", "get_table_index_center",
                 "get_table_statistics_center", "get_security_summary", "get_error_log_info", "generate_backup_plan"):
        monkeypatch.setattr(diagnosis, name, failed)
    response = env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert len(saved["modules"]) == len(diagnosis._module_specs()) == 12
    assert sum(item["execution_status"] == "failed" for item in saved["modules"]) == 11
    assert saved["modules"][-1]["module_key"] == "host_context"
    assert saved["modules"][-1]["execution_status"] == "unknown", "Configured host context was never collected"
    assert all(item["score"] is None and item["risk_level"] == "unknown" for item in saved["modules"])
    assert saved["task"]["overall_score"] is None and saved["task"]["risk_level"] == "unknown"
    assert saved["report"]["risk_score"] is None
    ai = all_reads(env, saved)
    assert "未发现明显巡检风险" not in ai["analysis"]["summary"]
    assert all(item["raw_data"]["execution_status"] in {"failed", "unknown"} for item in saved["findings"])


def test_empty_module_registry_is_saved_unknown_not_100(env, monkeypatch):
    monkeypatch.setattr(diagnosis, "_module_specs", lambda: [])
    response = env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["modules"] == []
    assert saved["task"]["task_status"] == "unknown"
    assert saved["task"]["overall_score"] is None and saved["task"]["risk_level"] == "unknown"
    all_reads(env, saved)


def test_default_module_sources_do_not_mistake_configuration_or_plans_for_host_and_backup_execution(env, monkeypatch):
    payloads = {
        "get_basic_info": basic(),
        "get_replication_status": {"variables": {"read_only": "ON"}, "replicas": []},
        "get_binlog_overview": {"variables": {"log_bin": "OFF"}},
        "get_innodb_status": {"raw": "INNODB STATUS offline fixture", "metrics": {"lock_waits": 0}},
        "get_lock_transaction_center": {"sections": {"row_lock_stats": {"Innodb_row_lock_current_waits": 0}}},
        "get_slow_sql_center": {"digest_details": [], "config": {"variables": {"slow_query_log": "ON"}}},
        "get_table_index_center": {"tables": [{"table_schema": "fixture", "table_name": "orders"}]},
        "get_table_statistics_center": {"sections": {"config": {"variables": {"innodb_stats_persistent": "ON"}}}},
        "get_security_summary": {"users": [{"user": "fixture", "host": "localhost", "account_locked": "Y"}]},
        "get_error_log_info": {"variables": {"log_error": "/fixture/not-read.log"}},
        "generate_backup_plan": {"status": "plan_only", "script": "fixture text, never executed"},
    }
    for name, payload in payloads.items():
        monkeypatch.setattr(diagnosis, name, lambda *args, _payload=payload, **kwargs: deepcopy(_payload))
    with env.factory() as db:
        db.get(DBInstance, env.instance_id).ssh_host = "configured-but-not-collected.invalid"
        db.commit()
    response = env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert len(saved["modules"]) == 12
    assert sum(item["execution_status"] == "success" for item in saved["modules"]) == 10
    by_key = {item["module_key"]: item for item in saved["modules"]}
    assert by_key["host_context"]["execution_status"] == "unknown"
    assert by_key["logs_backup"]["execution_status"] == "partial"
    assert by_key["logs_backup"]["score"] is None
    assert saved["task"]["task_status"] == "partial" and saved["task"]["overall_score"] is None
    all_reads(env, saved)


def test_module_insert_failure_rolls_back_task_and_all_results(env, monkeypatch):
    monkeypatch.setattr(diagnosis, "get_basic_info", lambda instance: basic())
    spec = diagnosis._module_specs()[0]
    monkeypatch.setattr(diagnosis, "_module_specs", lambda: [spec])

    def fail_insert(*args):
        raise RuntimeError("isolated module writer failure")

    event.listen(DBInspectionModuleResult, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="isolated module writer failure"):
            env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    finally:
        event.remove(DBInspectionModuleResult, "before_insert", fail_insert)
    with env.factory() as db:
        assert db.query(DBInspectionTask).count() == 0
        assert db.query(DBInspectionModuleResult).count() == 0


@pytest.mark.parametrize("level,expected_score,expected_risk", [("normal", 100, "normal"), ("critical", 75, "critical"),
                                                               ("warning", 90, "normal")])
def test_complete_observations_preserve_existing_module_scoring_and_risk_rules(env, monkeypatch, level, expected_score, expected_risk):
    payload = basic(risk_items=[] if level == "normal" else [{"level": level, "item": "measured fixture", "message": "known observation"}])
    saved = run_pair(env, monkeypatch, payload)
    module = saved["modules"][0]
    assert module["score"] == expected_score and module["risk_level"] == expected_risk
    assert saved["task"]["task_status"] == "success"
    assert saved["task"]["overall_score"] == round((expected_score + 100) / 2)
    assert saved["task"]["score_basis"] == "complete_evidence"
    all_reads(env, saved)


def test_a_real_zero_score_is_not_replaced_by_a_missing_value(env, monkeypatch):
    saved = run_pair(env, monkeypatch, basic(risk_items=[
        {"level": "critical", "item": f"observed-{i}", "message": f"measured-{i}"} for i in range(4)]))
    assert saved["modules"][0]["score"] == 0
    assert saved["task"]["overall_score"] == 50
    assert saved["task"]["risk_level"] == "critical"
    all_reads(env, saved)


@pytest.mark.parametrize("kind", ["empty", "all_failed", "summary_only", "retained_observation", "retained_unknown_verdict"])
def test_historical_read_projection_never_rewrites_customer_rows(env, kind):
    with env.factory() as db:
        task = DBInspectionTask(task_id="historical-" + kind, instance_id=env.instance_id, db_type="mysql",
                                overall_score=100, risk_level="normal", summary="old unverified healthy summary")
        if kind == "retained_unknown_verdict":
            task.risk_level = "unknown"
        db.add(task)
        if kind != "empty":
            evidence = {"summary": {"risk_level": "normal"}, "counts": {}, "warnings": [], "error": None}
            raw = {"summary": {"risk_level": "normal"}}
            if kind == "all_failed":
                evidence["error"] = "historical collector refused"
            if kind in {"retained_observation", "retained_unknown_verdict"}:
                raw = {"sections": {"row_lock_stats": {"Innodb_row_lock_current_waits": 0}}}
            db.add(DBInspectionModuleResult(task_id=task.task_id, instance_id=env.instance_id,
                module_key="locks_transactions", module_name="historical fixture", score=100, risk_level="normal",
                summary="old unverified healthy summary", evidence=evidence, raw_data=raw, top_risks=[]))
        db.commit()
        before = [dict(row._mapping) for row in db.execute(DBInspectionTask.__table__.select())]
        before_modules = [dict(row._mapping) for row in db.execute(DBInspectionModuleResult.__table__.select())]
        saved = diagnosis.get_mysql_diagnosis(db, env.instance_id, task.task_id)
        assert saved["task"]["overall_score"] is None and saved["task"]["risk_level"] == "unknown"
        assert saved["task"]["coverage_basis"] == "manifest_missing"
        assert saved["task"]["expected_module_count"] is None
        assert "old unverified healthy summary" not in saved["report"]["overall_conclusion"]
        if kind in {"retained_observation", "retained_unknown_verdict"}:
            assert saved["modules"][0]["score"] == 100
            assert saved["task"]["task_status"] == "partial"
        assert not db.dirty
    all_reads(env, saved)
    with env.factory() as db:
        assert before == [dict(row._mapping) for row in db.execute(DBInspectionTask.__table__.select())]
        assert before_modules == [dict(row._mapping) for row in db.execute(DBInspectionModuleResult.__table__.select())]


def test_manifest_is_frozen_once_owned_ordered_and_not_revalidated_against_todays_registry(env, monkeypatch):
    specs = diagnosis._module_specs()
    calls = []

    def registry():
        calls.append(True)
        assert len(calls) == 1
        return [specs[0], specs[3]]

    monkeypatch.setattr(diagnosis, "_module_specs", registry)
    monkeypatch.setattr(diagnosis, "get_basic_info", lambda instance: basic())
    monkeypatch.setattr(diagnosis, "get_binlog_overview", lambda instance: {"variables": {"log_bin": "ON"}})
    response = env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    assert response.status_code == 200, response.text
    saved = response.json()
    manifests = [row["evidence"]["collection"]["manifest"] for row in saved["modules"]]
    assert manifests[0] == manifests[1]
    manifest = manifests[0]
    assert manifest["schema"] == "mysql.diagnosis_module_manifest/v1"
    assert manifest["task_id"] == saved["task"]["task_id"] and manifest["instance_id"] == env.instance_id
    assert manifest["module_keys"] == [specs[0].key, specs[3].key]
    body = {key: value for key, value in manifest.items() if key != "digest"}
    assert manifest["digest"] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    assert saved["task"]["coverage_basis"] == "complete" and saved["task"]["expected_module_count"] == 2
    monkeypatch.setattr(diagnosis, "_module_specs", lambda: pytest.fail("History must not use today's registry"))
    all_reads(env, saved)


@pytest.mark.parametrize("kind", ["duplicate", "invalid_key", "long_key", "empty_name", "long_name", "not_callable", "not_spec"])
def test_invalid_registry_fails_before_collecting_or_persisting_a_smaller_complete_scope(env, monkeypatch, kind):
    spec = diagnosis._module_specs()[0]
    bad = diagnosis.ModuleSpec(
        key="Bad/key" if kind == "invalid_key" else "x" * 101 if kind == "long_key" else spec.key,
        name="" if kind == "empty_name" else "x" * 101 if kind == "long_name" else spec.name,
        collector=None if kind == "not_callable" else spec.collector,
    )
    specs = [spec, spec] if kind == "duplicate" else [object()] if kind == "not_spec" else [bad]
    monkeypatch.setattr(diagnosis, "_module_specs", lambda: specs)
    monkeypatch.setattr(diagnosis, "_safe_collect", lambda *args: pytest.fail("Invalid registry must fail before collecting"))
    with pytest.raises(ValueError, match="mysql_diagnosis_invalid_module_specs"):
        env.client.post(f"/operations/mysql/{env.instance_id}/inspect-diagnosis")
    with env.factory() as db:
        assert db.query(DBInspectionTask).count() == db.query(DBInspectionModuleResult).count() == 0


COVERAGE_FAILURES = ["deleted", "duplicate", "wrong_key", "reordered", "conflict", "bad_digest", "bad_schema",
                     "no_manifest", "missing_one", "duplicate_expected", "invalid_expected", "foreign_task", "foreign_instance"]


def corrupt_coverage(db, task_id, kind):
    """Corrupt real persisted run evidence, not a fake parallel implementation of coverage."""
    rows = db.query(DBInspectionModuleResult).filter_by(task_id=task_id).order_by(DBInspectionModuleResult.id).all()
    if kind == "deleted":
        db.delete(rows[-1])
    elif kind == "duplicate":
        db.add(DBInspectionModuleResult(**{column.name: deepcopy(getattr(rows[0], column.name))
               for column in DBInspectionModuleResult.__table__.columns if column.name not in {"id", "create_time"}}))
    elif kind == "wrong_key":
        rows[-1].module_key = "other_valid_key"
    elif kind == "reordered":
        rows[0].module_key, rows[-1].module_key = rows[-1].module_key, rows[0].module_key
    else:
        for index, row in enumerate(rows):
            evidence = deepcopy(row.evidence)
            manifest = evidence["collection"]["manifest"]
            if kind == "no_manifest" or kind == "missing_one" and index == 0:
                del evidence["collection"]["manifest"]
            elif kind == "bad_digest":
                manifest["digest"] = "0" * 64
            else:
                if kind == "conflict" and index == 0:
                    manifest["module_keys"] = list(reversed(manifest["module_keys"]))
                elif kind == "bad_schema":
                    manifest["schema"] = "unsupported/v9"
                elif kind == "duplicate_expected":
                    manifest["module_keys"] = [manifest["module_keys"][0]] * 2
                elif kind == "invalid_expected":
                    manifest["module_keys"] = ["Bad/key", manifest["module_keys"][1]]
                elif kind == "foreign_task":
                    # A complete, internally consistent manifest from a different run is not this run's proof.
                    manifest["task_id"] = "other-frozen-run"
                elif kind == "foreign_instance":
                    manifest["instance_id"] += 1
                manifest["digest"] = diagnosis._manifest_digest({key: value for key, value in manifest.items() if key != "digest"})
            row.evidence = evidence
    db.commit()


@pytest.mark.parametrize("kind", COVERAGE_FAILURES)
def test_manifest_coverage_failures_are_consistent_on_every_read_without_repairing_history(env, monkeypatch, kind):
    saved = run_pair(env, monkeypatch, basic())
    task_id = saved["task"]["task_id"]
    with env.factory() as db:
        corrupt_coverage(db, task_id, kind)
        before = {model: [dict(row._mapping) for row in db.execute(model.__table__.select())]
                  for model in (DBInspectionTask, DBInspectionModuleResult)}
        view = diagnosis.get_mysql_diagnosis(db, env.instance_id, task_id)
        assert view["task"]["overall_score"] is None and view["task"]["task_status"] == "partial"
        assert view["task"]["coverage_basis"] != "complete"
        assert all(module["score"] == 100 for module in view["modules"]), "Coverage failure does not erase measured modules"
        assert view["task"]["risk_level"] == "unknown"
        if kind == "deleted":
            assert view["task"]["expected_module_count"] == 2 and view["task"]["module_count"] == 1
    all_reads(env, view)
    with env.factory() as db:
        assert before == {model: [dict(row._mapping) for row in db.execute(model.__table__.select())]
                          for model in (DBInspectionTask, DBInspectionModuleResult)}


@pytest.mark.parametrize("stored_score", [100, None, -1])
def test_complete_manifest_cannot_validate_task_score_inconsistent_with_real_zero_modules(env, monkeypatch, stored_score):
    risks = [{"level": "critical", "item": f"zero-{i}", "message": f"zero fixture {i}"} for i in range(4)]
    saved = run_pair(env, monkeypatch, basic(risk_items=risks), {"variables": {"log_bin": "ON"}, "risk_items": risks})
    assert saved["task"]["overall_score"] == 0 and saved["task"]["task_status"] == "success"
    with env.factory() as db:
        task = db.query(DBInspectionTask).one()
        task.overall_score, task.risk_level, task.summary = stored_score, "normal", "FALSE_TASK_HEALTH"
        db.commit()
        view = diagnosis.get_mysql_diagnosis(db, env.instance_id, task.task_id)
        assert view["task"]["task_status"] == "unknown" and view["task"]["overall_score"] is None
        assert view["task"]["score_basis"] == ("inconsistent_score" if stored_score == 100 else "invalid_score")
        assert view["task"]["coverage_basis"] == "complete"
        assert all(module["score"] == 0 for module in view["modules"])
        assert view["task"]["risk_level"] == "critical"
        assert "FALSE_TASK_HEALTH" not in view["task"]["summary"]
    all_reads(env, view)
    with env.factory() as db:
        task = db.query(DBInspectionTask).one()
        assert task.overall_score == stored_score and task.risk_level == "normal" and not db.dirty


def test_consistent_zero_task_cannot_hide_a_more_severe_measured_module_risk(env, monkeypatch):
    risks = [{"level": "critical", "item": f"zero-{i}", "message": f"zero fixture {i}"} for i in range(4)]
    saved = run_pair(env, monkeypatch, basic(risk_items=risks), {"variables": {"log_bin": "ON"}, "risk_items": risks})
    with env.factory() as db:
        task = db.query(DBInspectionTask).one()
        task.risk_level = "normal"
        db.commit()
        view = diagnosis.get_mysql_diagnosis(db, env.instance_id, task.task_id)
        assert view["task"]["task_status"] == "success" and view["task"]["overall_score"] == 0
        assert view["task"]["risk_level"] == "critical"
        assert view["task"]["summary"] != task.summary
    all_reads(env, view)


def test_no_dto_only_fields_are_passed_to_orm_and_payloads_are_json_safe(env, monkeypatch):
    saved = run_pair(env, monkeypatch, basic())
    assert "score_basis" not in DBInspectionTask.__table__.columns
    assert "execution_status" not in DBInspectionModuleResult.__table__.columns
    assert json.loads(json.dumps(saved))["task"]["score_basis"] == "complete_evidence"
    with env.factory() as db:
        task = db.scalar(select(DBInspectionTask))
        assert diagnosis.project_mysql_diagnosis_task(db, task) == saved["task"]
        assert not db.dirty
