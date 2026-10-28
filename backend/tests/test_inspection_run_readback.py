"""One run, four read surfaces, one set of numbers.

The failure this file guards against is not a crash: the list was aggregated from
``DBCheckResult`` while the detail was built from the job row, so a run that produced no rows
appeared in one place and not the other. An operator asking "did the 10:00 inspection run?"
could get "no report" for a run that had in fact failed loudly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers the whole model metadata
from app.analyzers.rules import Finding
from app.api.v1.inspection import (
    api_get_inspection_job,
    api_get_task,
    api_list_inspection_jobs,
)
from app.api.v1.report import _collect_unified_reports
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_policy import DBInspectionPolicy
from app.models.report_center import ReportArtifact
from app.services.inspection import jobs, report_builder, result_writer, run_readback


@pytest.fixture
def context():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        instance = DBInstance(
            instance_name="run-read-target", host="127.0.0.1", port=3306,
            db_type="mysql", username="monitor", password_cipher="env://TEST_DB_PASSWORD",
            enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key="run-read-policy", name="Run read policy", enabled=1,
            db_types_json=["mysql"],
            item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
        )
        db.add_all([instance, policy])
        db.commit()
        yield SimpleNamespace(db=db, instance=instance, policy=policy)
    engine.dispose()


def _request(role: str = "admin", user_id: int = 7) -> SimpleNamespace:
    """An authenticated admin; permission plumbing is covered by its own test module."""
    return SimpleNamespace(
        state=SimpleNamespace(auth={"user_id": user_id, "role": role, "source": "db"}),
        headers={},
    )


def list_jobs(context, **kwargs) -> dict:
    params = {
        "instance_id": None, "db_type": None, "status": None, "outcome": None,
        "trigger_type": None, "include_legacy": False, "page": 1, "page_size": 50,
    }
    params.update(kwargs)
    return api_list_inspection_jobs(_request(), db=context.db, **params)


def get_job(context, job_id: str) -> dict:
    return api_get_inspection_job(job_id, _request(), db=context.db)


def ok_finding(name: str = "ok") -> Finding:
    return Finding(
        "synthetic", name, "normal", "one", "1", "", "ok", "",
        {"execution_status": "success", "sql": "SELECT 1", "rows": [{"one": 1}]},
    )


def failed_finding(name: str = "broken") -> Finding:
    return Finding(
        "synthetic", name, "warning", "two", "2", "", "collected nothing", "",
        {"execution_status": "failed", "error": "connection reset by peer"},
    )


def completed_partial_job(context):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    assert jobs.claim(context.db, job.id, "owner")
    assert jobs.commit_results(context.db, job.id, "owner", [ok_finding(), failed_finding()])
    context.db.expire_all()
    return context.db.get(type(job), job.id)


def zero_result_failed_job(context):
    """A real execution path, not a hand-written row: the template disappears mid-queue."""
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    context.policy.enabled = 0
    context.db.commit()
    assert jobs.claim(context.db, job.id, "owner")
    finished = jobs.execute(context.db, job, "owner")
    context.db.expire_all()
    return context.db.get(type(job), finished.id)


def test_list_detail_and_report_agree_on_the_number_of_results(context):
    job = completed_partial_job(context)
    stored = context.db.query(DBCheckResult).filter_by(task_id=job.id).count()
    assert stored == 2

    item = next(entry for entry in list_jobs(context)["items"] if entry["job_id"] == job.id)
    detail = get_job(context, job.id)
    report = api_get_task(job.id, None, db=context.db)
    unified = next(
        entry for entry in _collect_unified_reports(context.db, "inspection", 50)
        if entry["source_id"] == job.id
    )

    assert item["result_count"] == stored
    assert detail["results"]["total_count"] == stored
    assert report["task"]["total_count"] == stored
    assert item["results_published"] is True
    assert detail["run"]["results_published"] is True
    # The list aggregates severity with one grouped query, the detail folds the loaded rows;
    # both must land on result_writer._counts.
    assert item["counts"] == detail["results"]["counts"] == result_writer._counts(
        result_writer.get_task_rows(context.db, job.id)
    )
    # The unified catalogue reads the same run; its summary restates the same number.
    assert f"总项 {stored}" in unified["summary"]


def test_list_and_detail_agree_on_status_outcome_and_reason(context):
    job = completed_partial_job(context)
    item = next(entry for entry in list_jobs(context)["items"] if entry["job_id"] == job.id)
    detail = get_job(context, job.id)

    assert item["status"] == detail["run"]["status"] == "partial"
    assert item["outcome"] == detail["run"]["outcome"] == "partial"
    assert item["outcome_reason"] == detail["run"]["outcome_reason"]
    assert item["outcome_kind"] == detail["run"]["outcome_kind"] == "collector_error"
    # The reason is redacted on write; the read model must not re-expose raw text.
    assert "password" not in json.dumps(detail, ensure_ascii=False).lower()


def test_list_item_and_detail_run_block_use_the_same_keys_for_the_same_facts(context):
    """One column, one key name. `job_status` next to `status` was a two-answer read model."""
    job = completed_partial_job(context)
    item = next(entry for entry in list_jobs(context)["items"] if entry["job_id"] == job.id)
    run = get_job(context, job.id)["run"]

    list_only = set(item) - set(run)
    assert list_only == set(run_readback.LIST_ONLY_FIELDS), list_only
    # The detail additionally carries the event stream; nothing else is exclusive to it.
    assert set(run) - set(item) == {"progress"}, set(run) - set(item)

    for key in item:
        if key in run_readback.LIST_ONLY_FIELDS:
            continue
        assert {key: item[key]} == {key: run[key]}, f"list and detail disagree on {key}"

    for forbidden in ("job_status",):
        assert forbidden not in item
        assert forbidden not in run
    assert run["status"] == job.status


def test_report_verdict_matches_the_run_outcome_without_recomputing_counts(context):
    job = completed_partial_job(context)
    detail = get_job(context, job.id)
    rows = result_writer.get_task_rows(context.db, job.id)
    report = report_builder.build_report_payload(context.db, job.id)

    assert detail["results"]["task_status"] == report["task"]["task_status"]
    assert detail["run"]["outcome"] == detail["results"]["task_status"]
    # Same function, same numbers: the read model borrows, it does not re-derive.
    assert detail["results"]["counts"] == result_writer._counts(rows)
    assert detail["results"]["derived_from"] == "result_writer.execution_summary"


def test_zero_result_failed_run_is_visible_with_a_reason_instead_of_missing(context):
    job = zero_result_failed_job(context)
    item = next(entry for entry in list_jobs(context)["items"] if entry["job_id"] == job.id)
    detail = get_job(context, job.id)

    assert item["result_count"] == 0
    assert item["results_published"] is False
    assert item["outcome"] == "failed"
    # A classification code, not a guess parsed from the message.
    assert item["outcome_kind"] == "template_missing"
    assert detail["evidence"]["state"] == "missing"
    assert detail["results"]["total_count"] == 0
    # Zero rows must not be turned into "the collection failed" by execution_summary([]).
    assert detail["results"]["task_status"] is None


def test_failed_run_detail_returns_the_step_that_failed(context):
    job = zero_result_failed_job(context)
    detail = get_job(context, job.id)

    last = detail["steps"][-1]
    assert last["stage"] == "failed"
    assert last["status"] == "failed"
    assert last["detail"], "a failed run without a reason is indistinguishable from a bug"
    assert last["failure_code"] == "template_missing"
    assert len(detail["steps"]) >= 2  # queued, validating, failed


def test_failed_run_is_in_the_report_catalogue_with_no_score(context):
    job = zero_result_failed_job(context)
    unified = next(
        entry for entry in _collect_unified_reports(context.db, "inspection", 50)
        if entry["source_id"] == job.id
    )
    assert unified["status"] == "failed"
    assert unified["score"] is None
    # Nothing to export when nothing was published.
    assert unified["export_url"] is None


def test_outcome_filter_reaches_zero_result_runs_and_status_filter_does_not(context):
    failed = zero_result_failed_job(context)
    detail = get_job(context, failed.id)
    assert detail["run"]["outcome"] == "failed"

    failed_items = list_jobs(context, outcome="failed")["items"]
    assert [item["job_id"] for item in failed_items] == [failed.id]

    success_items = list_jobs(context, status="success")["items"]
    assert failed_items and not success_items

    assert list_jobs(context, status="failed")["items"][0]["job_id"] == failed.id
    assert list_jobs(context, outcome="success")["items"] == []


def test_queued_run_with_no_events_and_no_results_is_not_reported_as_failed(context):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    detail = get_job(context, job.id)

    assert detail["run"]["outcome"] is None
    assert detail["run"]["terminal"] is False
    assert detail["results"]["task_status"] is None
    assert detail["results"]["total_count"] == 0
    assert detail["evidence"]["state"] == "missing"
    assert [step["stage"] for step in detail["steps"]] == ["queued"]
    assert detail["run"]["percent_basis"] == "missing_stage_events"


def test_plan_block_reads_the_frozen_snapshot_not_the_live_template(context):
    job = completed_partial_job(context)
    context.policy.name = "renamed-after-the-run"
    context.db.commit()

    detail = get_job(context, job.id)
    assert detail["plan"]["policy_name"] == "Run read policy"
    assert detail["plan"]["plan_source"] == "frozen_policy_snapshot"
    assert detail["plan"]["plan_contract"] == "v0_snapshot"
    assert detail["plan"]["policy_revision"] == job.policy_revision
    # Advisory only: it never changes what the run means.
    assert detail["plan"]["template_changed_since_freeze"] is True


def test_evidence_records_the_target_the_run_actually_used(context):
    job = completed_partial_job(context)
    context.instance.host = "renamed-after-the-run.invalid"
    context.db.commit()

    detail = get_job(context, job.id)
    assert detail["evidence"]["state"] == "recorded"
    assert detail["evidence"]["target"]["host"] == "127.0.0.1"
    assert detail["evidence"]["row_count"] == 2


def test_reports_block_shows_synced_index_and_not_synced_as_a_state_not_an_error(context):
    job = completed_partial_job(context)
    detail = get_job(context, job.id)
    assert detail["reports"]["state"] == "synced"
    assert detail["reports"]["source_key"] == f"source:inspection:{job.id}"
    # A run from before automatic indexing has no artifact. That is a state the API
    # reports, not an exception and not a fabricated link.
    context.db.query(ReportArtifact).filter_by(source_key=f"source:inspection:{job.id}").delete()
    context.db.commit()
    context.db.expire_all()
    assert get_job(context, job.id)["reports"]["state"] == "not_synced"


def test_legacy_result_set_without_a_job_is_listed_only_when_asked(context):
    context.db.add(DBCheckResult(
        task_id="20261019120000123456", instance_id=context.instance.id, db_type="mysql",
        check_status="normal", check_item="legacy", raw_data={},
    ))
    context.db.commit()

    # The job endpoint 404s: there is no run row, and inventing one would be a lie.
    with pytest.raises(HTTPException) as missing:
        api_get_inspection_job("20261019120000123456", _request(), db=context.db)
    assert missing.value.status_code == 404

    plain = list_jobs(context)["items"]
    assert plain == []

    marked = list_jobs(context, include_legacy=True)["items"]
    legacy = next(item for item in marked if item["job_id"] == "20261019120000123456")
    assert legacy["linkage"] == "legacy_result_only"
    assert legacy["outcome"] == "success"
    assert "队列接入之前" in legacy["outcome_reason"]


def test_run_sources_puts_jobs_before_legacy_rows_and_stays_within_the_limit(context):
    job = completed_partial_job(context)
    context.db.add(DBCheckResult(
        task_id="20261019120000999999", instance_id=context.instance.id, db_type="mysql",
        check_status="normal", check_item="legacy", raw_data={},
    ))
    context.db.commit()

    limited = run_readback.run_sources(context.db, 1)
    assert len(limited) == 1 and limited[0]["job_id"] == job.id

    both = run_readback.run_sources(context.db, 10)
    assert {item["linkage"] for item in both} == {"job", "legacy_result_only"}
