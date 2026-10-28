from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.check_result import DBCheckResult
from app.services.inspection import custom_metric_service, jobs, report_builder, result_writer, runner
from app.tasks import inspection_scheduler


def run_scheduled_results(results):
    """Drive the scheduler with a list of terminal job outcomes.

    Scheduled runs now execute through the durable queue, so the seam to stub is
    `jobs.execute` rather than `runner.run_unified_inspection`. Stubbing the
    queue's terminal result keeps what these tests actually assert - that the
    scheduler reports the execution outcome, never the presence of a report.
    """
    db = MagicMock()
    policy = SimpleNamespace(policy_key="outcome-test")
    db.query.return_value.filter.return_value.all.return_value = [policy]
    instances = [SimpleNamespace(id=index + 1, db_type="mysql") for index in range(len(results))]
    pending = list(results)

    def execute(_db, _row, _token):
        item = pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(status=item, failure_reason=None)

    with (
        patch.object(inspection_scheduler, "SessionLocal", return_value=db),
        patch.object(inspection_scheduler.policy_service, "ensure_default_policies"),
        patch.object(inspection_scheduler.policy_service, "is_policy_due", return_value=True),
        patch.object(inspection_scheduler, "_assigned_instances", return_value=instances),
        patch.object(jobs, "enqueue", side_effect=lambda *_a, **_k: SimpleNamespace(id="job-id")),
        patch.object(jobs, "claim", side_effect=lambda *_a, **_k: SimpleNamespace(id="job-id")),
        patch.object(jobs, "execute", side_effect=execute),
        patch.object(inspection_scheduler.policy_service, "mark_policy_run") as mark,
    ):
        inspection_scheduler.run_due_inspection_jobs()
    db.close.assert_called_once()
    return mark.call_args.args[1:3]


@pytest.mark.parametrize("states,expected", [
    (["success"], "success"),
    (["failed"], "failed"),
    (["partial"], "partial"),
    (["success", "failed"], "partial"),
    (["failed", "failed"], "failed"),
    (["partial", "failed"], "partial"),
    (["unknown"], "failed"),
])
def test_scheduler_uses_execution_status_not_presence_of_report(states, expected):
    status, message = run_scheduled_results(list(states))
    assert status == expected
    if expected != "success":
        assert "失败" in message


@pytest.mark.parametrize("result", [None, "", {}, "not-a-real-status"])
def test_missing_task_is_not_success(result):
    assert run_scheduled_results([result])[0] == "failed"


def test_exception_and_later_success_are_preserved():
    status, message = run_scheduled_results([
        RuntimeError("connection unavailable"), "success",
    ])
    assert status == "partial"
    assert "实例 1" in message
    assert "connection unavailable" in message


def test_no_assigned_instances_still_skips():
    assert run_scheduled_results([])[0] == "skipped"


def test_missing_evidence_is_not_healthy():
    assert result_writer.execution_summary([])["task_status"] == "failed"


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_incomplete_collection_does_not_claim_no_risk(status):
    text = report_builder._conclusion({"task_status": status, "risk_level": "normal"}, 3)
    assert "失败" in text
    assert "未发现" not in text


@pytest.mark.parametrize("source", ["custom_sql", "host_template", "host_overview", "host_disabled"])
@pytest.mark.parametrize("has_success", [False, True])
def test_actual_metric_collection_failures_reach_task_and_scheduler(source, has_success):
    instance = SimpleNamespace(id=8, instance_name="test-db", db_type="mysql")
    if source == "custom_sql":
        @contextmanager
        def failed_connection(_instance):
            raise RuntimeError("database unavailable")
            yield  # pragma: no cover

        findings = custom_metric_service.collect_policy_metrics(instance, [{
            "key": "custom.orders", "name": "Orders", "enabled": True,
            "source_type": "database_sql", "db_type": "mysql",
            "sql": "SELECT 1 AS metric_value", "result_field": "metric_value",
            "value_type": "number",
        }], connection_factory=failed_connection)
    else:
        overview = {"ssh_enabled": False} if source == "host_disabled" else {
            "error": "SSH unavailable", "ssh_enabled": True,
        }
        policy = SimpleNamespace(item_config_json=[{"key": "host.os.type", "enabled": True}])
        with patch("app.services.host_overview.get_instance_host_overview", return_value=overview):
            findings = runner._collect_host(instance, policy if source == "host_template" else None)
    rows = result_writer.save_findings(MagicMock(), instance, "collector-failure", findings)
    assert rows and all(row.metric_value == "collection_failed" for row in rows)
    if has_success:
        rows.append(DBCheckResult(metric_name="cpu", metric_value="95", check_status="critical"))
    task = result_writer._task_summary("collector-failure", rows, instance)
    expected = "partial" if has_success else "failed"
    assert task["task_status"] == expected
    assert task["failure_reason"]
    if not has_success:
        assert task["overall_score"] is None
    assert run_scheduled_results([task["task_status"]])[0] == expected


def test_business_risk_is_not_a_collection_failure():
    rows = [DBCheckResult(metric_name="cpu", metric_value="95", check_status="critical")]
    assert result_writer.execution_summary(rows) == {"task_status": "success", "failure_reason": None}
