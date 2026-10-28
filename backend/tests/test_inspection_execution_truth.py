from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.analyzers.rules import Finding
from app.api.v1 import inspection as api
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.services.inspection import report_builder, result_writer, runner


def finding(*, metric="oracle.script:test", value="0 rows", status="normal", raw=None):
    return Finding("SQL", "Check", status, metric, value, "", "collected", "", raw)


def evidence(*, failed=False, rows=None):
    rows = [] if rows is None else rows
    return {
        "sql": "SELECT name FROM v$database",
        "columns": [] if failed else ["NAME"],
        "rows": rows,
        "row_count": len(rows),
        "status": "warning" if failed else "normal",
        "message": "ORA-01031: insufficient privileges" if failed else None,
        "truncated": False,
        "read_only": True,
    }


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inspection-evidence.sqlite'}")
    DBInstance.__table__.create(engine)
    DBCheckResult.__table__.create(engine)
    with Session(engine) as session:
        instance = DBInstance(
            instance_name="isolated-fixture", db_type="oracle", host="192.0.2.1",
            port=1521, username="fixture", password_cipher="unused", enabled=1,
        )
        session.add(instance)
        session.commit()
        yield session, instance
    engine.dispose()


def read_surfaces(session, instance, task_id):
    session.expire_all()
    request = Request({"type": "http", "headers": []})
    request.state.auth = {"role": "admin"}
    listed = api.api_list_tasks(request, instance.id, "oracle", 50, session)["items"]
    task = next(item for item in listed if item["task_id"] == task_id)
    detail = api.api_get_task(task_id, instance.id, session)
    report = api.api_get_report(task_id, instance.id, session)
    assert detail == report
    results = api.api_list_results(request, instance.id, "oracle", None, 1, 50, session)
    saved = {item["id"]: item for item in results["items"] if item["task_id"] == task_id}
    for row in detail["report"]["raw_appendix"]["rows"]:
        assert saved[row["id"]] == row
    for key in ("task_status", "failure_reason", "overall_score", "risk_level", "total_count"):
        assert task[key] == detail["task"][key]
    assert detail["task"]["total_count"] == len(detail["findings"])
    return task, detail


@pytest.mark.parametrize("success", [False, True])
def test_sql_failure_survives_persistence_and_api_surfaces(db, success):
    session, instance = db
    failed = finding(value="error", status="warning", raw=evidence(failed=True))
    findings = [failed]
    if success:
        findings.append(finding(raw=evidence(rows=[{"NAME": "LAB"}])))
    result_writer.save_findings(session, instance, "sql-failure", findings)
    task, detail = read_surfaces(session, instance, "sql-failure")
    assert task["task_status"] == ("partial" if success else "failed")
    assert task["overall_score"] is None
    assert "ORA-01031" in task["failure_reason"]
    saved = detail["report"]["raw_appendix"]["rows"][0]["raw_data"]
    assert saved == {
        **failed.raw_data,
        "inspection_target_snapshot": {
            "id": instance.id, "name": "isolated-fixture", "host": "192.0.2.1",
            "port": 1521, "db_type": "oracle", "status": "recorded",
        },
    }
    assert "inspection_target_snapshot" not in failed.raw_data
    assert detail["findings"][0]["evidence"]["execution_status"] == "failed"
    assert detail["modules"][0]["score"] is None


def test_successful_empty_query_is_not_an_empty_collection(db):
    session, instance = db
    raw = evidence()
    result_writer.save_findings(session, instance, "zero-rows", [finding(raw=raw)])
    task, detail = read_surfaces(session, instance, "zero-rows")
    assert task["task_status"] == "success"
    assert detail["findings"][0]["evidence"]["row_count"] == 0
    assert detail["findings"][0]["evidence"]["rows"] == []
    assert detail["findings"][0]["evidence"]["columns"] == ["NAME"]


def test_empty_collector_creates_a_persisted_failed_task(db):
    session, instance = db
    with patch("app.services.inspection.execution_policy.resolve_execution_policy", return_value=None), patch.object(
        runner, "collect_findings", return_value=[],
    ):
        result = runner.run_unified_inspection(session, instance)
    assert result["task"] is not None
    task, _detail = read_surfaces(session, instance, result["task"]["task_id"])
    assert task["task_status"] == "failed"
    assert task["overall_score"] is None


def test_connection_failure_is_saved_without_success_or_score(db):
    session, instance = db
    with patch("app.services.inspection.execution_policy.resolve_execution_policy", return_value=None), patch.object(
        runner, "_collect_oracle", side_effect=ConnectionError("connection refused"),
    ):
        result = runner.run_unified_inspection(session, instance)
    task, detail = read_surfaces(session, instance, result["task"]["task_id"])
    assert task["task_status"] == "failed"
    assert task["overall_score"] is None
    assert "connection refused" in task["failure_reason"]
    assert detail["modules"][0]["score"] is None


def test_preflight_connection_failure_never_calls_collectors(db):
    session, instance = db
    with patch("app.services.inspection.execution_policy.resolve_execution_policy", side_effect=ConnectionError(
        "preflight connection refused",
    )), patch.object(runner, "collect_findings") as collect:
        result = runner.run_unified_inspection(session, instance)
    collect.assert_not_called()
    task, _ = read_surfaces(session, instance, result["task"]["task_id"])
    assert task["task_status"] == "failed"
    assert task["overall_score"] is None


def test_persistence_failure_cannot_return_success(db):
    session, instance = db
    with patch("app.services.inspection.execution_policy.resolve_execution_policy", return_value=None), patch.object(
        runner, "collect_findings", return_value=[finding(raw=evidence())],
    ), patch.object(session, "commit", side_effect=RuntimeError("write failed")):
        with pytest.raises(RuntimeError, match="write failed"):
            runner.run_unified_inspection(session, instance)
    session.rollback()
    assert session.query(DBCheckResult).count() == 0


@pytest.mark.parametrize("metric,raw", [
    ("collector_error", {"error": "ORA-01031"}),
    ("executed_sections", {"executed_count": 0, "failed_count": 1, "skipped_count": 0}),
    ("executed_sections", {"executed_count": 0, "failed_count": 0, "skipped_count": 1}),
])
def test_runtime_metadata_does_not_turn_failed_checks_into_partial_success(metric, raw):
    rows = [
        finding(metric="script_name", raw={"inspection_runtime": True}),
        finding(metric="account_capability", raw={"inspection_runtime": True}),
        finding(metric=metric, status="warning", raw=raw),
    ]
    assert result_writer.execution_summary(rows)["task_status"] == "failed"


@pytest.mark.parametrize("has_valid_item", [False, True])
def test_nested_section_errors_are_not_health_warnings(has_valid_item):
    items = [{"subsection": "locks", "error": "permission denied"}]
    if has_valid_item:
        items.append({"subsection": "sessions", "count": 5})
    row = finding(raw={"items": items, "sql": ["SELECT 1"], "status": "warning"})
    assert result_writer.execution_summary([row])["task_status"] == (
        "partial" if has_valid_item else "failed"
    )


def test_numeric_fallback_on_failed_collection_cannot_be_thresholded_to_healthy():
    row = finding(metric="oracle.tablespace.used_pct", value="0", status="warning", raw={
        **evidence(failed=True), "template_threshold_eligible": True,
    })
    policy = SimpleNamespace(item_config_json=[{
        "key": "oracle.tablespace.used_pct", "warning_value": 80, "critical_value": 90,
    }], threshold_config_json={})
    runner._apply_policy_thresholds([row], policy)
    assert row.check_status == "warning"
    assert "template_threshold" not in row.raw_data


@pytest.mark.parametrize("failed", [False, True])
def test_analyzer_never_substitutes_derived_or_error_rows_for_sql_result(failed):
    raw = evidence(failed=failed, rows=[] if failed else [{"NAME": "LAB"}])
    original = deepcopy(raw)
    row = finding(metric="used_pct", raw={"derived_pct": 12.5})
    row.check_category = "表空间状态"
    runner._attach_oracle_query_evidence([row], {"_query_evidence": {"tablespace_usage": raw}})
    for key in ("sql", "columns", "rows", "row_count", "status", "truncated"):
        assert row.raw_data[key] == original[key]
    assert raw == original


@pytest.mark.parametrize("failed", [False, True])
def test_unanalyzed_oracle_query_keeps_zero_rows_or_failure(failed):
    raw = evidence(failed=failed)
    rows = []
    runner._ensure_oracle_query_findings(rows, {"_query_evidence": {"tablespace_usage": raw}}, {
        "oracle.tablespace.used_pct",
    })
    assert rows
    assert rows[0].raw_data["sql"] == raw["sql"]
    assert rows[0].raw_data["rows"] == []
    assert result_writer.execution_summary(rows)["task_status"] == ("failed" if failed else "success")


@pytest.mark.parametrize("metric", ["used_pct", "script_name", "account_capability"])
def test_legacy_metrics_do_not_gain_fabricated_sql_or_query_rows(metric):
    row = SimpleNamespace(
        db_type="oracle", metric_name=metric, metric_value="12.5", threshold_value="80",
        check_category="表空间状态", check_status="normal",
        raw_data={"tablespace_name": "USERS", "used_pct": 12.5, "major_version": 19},
    )
    raw = deepcopy(row.raw_data)
    result = report_builder._finding_evidence(row)
    for key in ("sql", "columns", "rows", "row_count"):
        assert key not in result
    assert result["execution_status"] == "unknown"
    assert row.raw_data == raw


def test_custom_collector_exception_preserves_completed_checks(db):
    session, instance = db
    completed = finding(raw=evidence(rows=[{"NAME": "LAB"}]))
    policy = SimpleNamespace(item_config_json=[])
    with patch.object(runner, "_collect_oracle", return_value=[completed]), patch(
        "app.services.inspection.custom_metric_service.collect_policy_metrics",
        side_effect=RuntimeError("custom collector interrupted"),
    ):
        rows = runner.collect_findings(session, instance, policy)
    assert rows[0] is completed
    assert result_writer.execution_summary(rows)["task_status"] == "partial"


def test_risk_warning_does_not_imply_sql_failure():
    row = finding(status="critical", value="95", raw={**evidence(), "status": "critical"})
    assert result_writer.execution_summary([row])["task_status"] == "success"


@pytest.mark.parametrize("state", ["not_supported", "not_applicable", "not_executed", "collection_failed"])
def test_state_only_unexecuted_results_do_not_make_a_healthy_task(db, state):
    session, instance = db
    result_writer.save_findings(session, instance, "state-only", [finding(raw={"state": state})])
    task, detail = read_surfaces(session, instance, "state-only")
    assert task["task_status"] == "failed"
    assert task["overall_score"] is None
    assert detail["findings"][0]["execution_status"] == ("failed" if state == "collection_failed" else "skipped")


def test_second_query_for_same_template_key_cannot_lose_failure():
    successful = finding(metric="used_pct", raw={
        **evidence(), "query_name": "tablespace_usage",
        "template_item_key": "oracle.tablespace.used_pct",
    })
    rows = [successful]
    runner._ensure_oracle_query_findings(rows, {"_query_evidence": {
        "tablespace_usage": successful.raw_data,
        "asm_usage": evidence(failed=True),
    }}, {"oracle.tablespace.used_pct"})
    assert len(rows) == 2
    assert result_writer.execution_summary(rows)["task_status"] == "partial"


def test_real_script_executor_with_fake_cursor_keeps_attempted_sql(db):
    from app.services.inspection import oracle_script_checks as scripts

    class Cursor:
        description = [("NAME",)]

        def execute(self, sql):
            self.sql = sql
            if "denied_view" in sql:
                raise RuntimeError("ORA-01031: insufficient privileges")

        def fetchmany(self, _limit):
            return [] if "empty_view" in self.sql else [("LAB",)]

        def close(self):
            pass

    connection = SimpleNamespace(cursor=Cursor, rollback=lambda: None)
    sections = [
        scripts.ScriptSqlSection(
            order=index, key=key, title=key, sql=f"SELECT name FROM {key}",
            line_number=index * 10, item_key=f"oracle.script:{key}",
        )
        for index, key in enumerate(("valid_view", "empty_view", "denied_view"), 1)
    ]
    with patch.object(scripts, "_resolve_sqlplus_substitutions", return_value={}):
        findings = scripts._execute_sections(connection, {"script_name": "fixture.sql"}, sections)
    session, instance = db
    result_writer.save_findings(session, instance, "script-executor", findings)
    task, detail = read_surfaces(session, instance, "script-executor")
    assert task["task_status"] == "partial"
    assert task["overall_score"] is None
    saved = detail["report"]["raw_appendix"]["rows"][1:]
    for row, section in zip(saved, sections, strict=True):
        assert row["raw_data"]["sql"] == section.sql
        assert row["raw_data"]["line_number"] == section.line_number
    assert saved[0]["raw_data"]["rows"] == [{"NAME": "LAB"}]
    assert saved[1]["raw_data"]["row_count"] == 0
    assert saved[1]["raw_data"]["columns"] == ["NAME"]
    assert saved[2]["raw_data"]["rows"] == []
    assert "ORA-01031" in saved[2]["raw_data"]["message"]
