from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from app.analyzers.rules import Finding
from app.collectors.utils import json_safe
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance


def target_snapshot(instance) -> dict:
    return normalize_target_snapshot({
        "id": instance.id, "name": getattr(instance, "instance_name", None),
        "host": getattr(instance, "host", None), "port": getattr(instance, "port", None),
        "db_type": instance.db_type, "status": "recorded",
    }, instance_id=instance.id, db_type=instance.db_type)


def normalize_target_snapshot(snapshot, *, instance_id: int, db_type: str) -> dict:
    if (
        isinstance(snapshot, dict)
        and snapshot.get("id") == instance_id
        and snapshot.get("db_type") == db_type
        and snapshot.get("status") == "recorded"
        and all(snapshot.get(key) not in (None, "") for key in ("name", "host", "port"))
    ):
        return {key: snapshot[key] for key in ("id", "name", "host", "port", "db_type", "status")}
    return {"id": instance_id, "name": str(instance_id), "host": "", "port": "",
            "db_type": db_type, "status": "historical_context_missing"}


def report_target(rows) -> dict:
    for row in rows:
        raw = row.raw_data if isinstance(row.raw_data, dict) else {}
        snapshot = raw.get("inspection_target_snapshot")
        target = normalize_target_snapshot(snapshot, instance_id=row.instance_id, db_type=row.db_type)
        if target["status"] == "recorded":
            return target
    first = rows[0]
    return normalize_target_snapshot(None, instance_id=first.instance_id, db_type=first.db_type)


def save_findings(
    db: Session,
    instance: DBInstance,
    task_id: str,
    findings: Iterable[Finding],
    *,
    commit: bool = True,
) -> list[DBCheckResult]:
    snapshot = target_snapshot(instance)

    def evidence(raw):
        value = json_safe(raw)
        result = dict(value) if isinstance(value, dict) else {"raw_payload": value}
        result["inspection_target_snapshot"] = snapshot
        return result

    rows = [
        DBCheckResult(
            task_id=task_id,
            instance_id=instance.id,
            db_type=instance.db_type,
            check_category=finding.check_category,
            check_item=finding.check_item,
            check_status=finding.check_status,
            metric_name=finding.metric_name,
            metric_value=_string_value(finding.metric_value),
            threshold_value=_string_value(finding.threshold_value),
            finding=finding.finding,
            suggestion=finding.suggestion,
            raw_data=evidence(finding.raw_data),
        )
        for finding in findings
    ]
    db.add_all(rows)
    if commit:
        db.commit()
        for row in rows:
            db.refresh(row)
    else:
        db.flush()
    return rows


def list_results(
    db: Session,
    instance_id: int | None = None,
    db_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    *,
    allowed_instance_ids: set[int] | None = None,
) -> tuple[list[DBCheckResult], int]:
    query = db.query(DBCheckResult)
    if allowed_instance_ids is not None:
        query = query.filter(DBCheckResult.instance_id.in_(allowed_instance_ids))
    if instance_id:
        query = query.filter(DBCheckResult.instance_id == instance_id)
    if db_type:
        query = query.filter(DBCheckResult.db_type == db_type)
    if status:
        query = query.filter(DBCheckResult.check_status == status)
    total = query.count()
    items = (
        query.order_by(DBCheckResult.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def list_tasks(
    db: Session,
    instance_id: int | None = None,
    db_type: str | None = None,
    limit: int = 50,
    *,
    allowed_instance_ids: set[int] | None = None,
) -> list[dict]:
    normalized_limit = max(limit, 1)
    latest_task_query = db.query(
        DBCheckResult.task_id,
        func.max(DBCheckResult.id).label("latest_result_id"),
    )
    if allowed_instance_ids is not None:
        outside_tasks = db.query(DBCheckResult.task_id).filter(
            DBCheckResult.instance_id.not_in(allowed_instance_ids),
        )
        latest_task_query = latest_task_query.filter(
            DBCheckResult.instance_id.in_(allowed_instance_ids),
            DBCheckResult.task_id.not_in(outside_tasks),
        )
    if instance_id is not None:
        latest_task_query = latest_task_query.filter(DBCheckResult.instance_id == instance_id)
    if db_type:
        latest_task_query = latest_task_query.filter(DBCheckResult.db_type == db_type)

    latest_tasks = (
        latest_task_query
        .group_by(DBCheckResult.task_id)
        .order_by(func.max(DBCheckResult.id).desc())
        .limit(normalized_limit)
        .all()
    )
    task_ids = [task_id for task_id, _ in latest_tasks]
    if not task_ids:
        return []

    rows_query = (
        db.query(DBCheckResult)
        .options(
            load_only(
                DBCheckResult.id,
                DBCheckResult.task_id,
                DBCheckResult.instance_id,
                DBCheckResult.db_type,
                DBCheckResult.check_status,
                DBCheckResult.metric_name,
                DBCheckResult.metric_value,
                DBCheckResult.raw_data,
                DBCheckResult.create_time,
            )
        )
        .filter(DBCheckResult.task_id.in_(task_ids))
    )
    if allowed_instance_ids is not None:
        rows_query = rows_query.filter(DBCheckResult.instance_id.in_(allowed_instance_ids))
    if instance_id is not None:
        rows_query = rows_query.filter(DBCheckResult.instance_id == instance_id)
    if db_type:
        rows_query = rows_query.filter(DBCheckResult.db_type == db_type)

    rows = rows_query.order_by(DBCheckResult.id.desc()).all()
    grouped: OrderedDict[str, list[DBCheckResult]] = OrderedDict(
        (task_id, []) for task_id in task_ids
    )
    for row in rows:
        grouped[row.task_id].append(row)

    # Runs stored before the queue existed carry no target snapshot, so their display name has
    # to come from the instance table. That fallback is one batched query for the whole page:
    # looking the name up per task would reinstate the N+1 the single grouped count query above
    # was written to remove.
    summaries = {
        task_id: _task_summary(task_id, items, None)
        for task_id, items in grouped.items()
    }
    fallback_names = resolve_instance_names(
        db,
        [
            summary["instance_id"]
            for summary in summaries.values()
            if summary["target_snapshot_status"] != "recorded"
        ],
    )
    for summary in summaries.values():
        if summary["target_snapshot_status"] == "recorded":
            continue
        summary["instance_name"] = fallback_names.get(
            summary["instance_id"], summary["instance_name"]
        )
    return list(summaries.values())


def resolve_instance_names(db: Session, instance_ids: Iterable[int | None]) -> dict[int, str]:
    """Current instance names, in **one** query, for ids whose snapshot is missing.

    Only a display name: the evidence block keeps saying ``historical_context_missing``, because
    today's name is not proof of what the target was called when the run happened.
    """
    ids = sorted({int(value) for value in instance_ids if value is not None})
    if not ids:
        return {}
    rows = (
        db.query(DBInstance.id, DBInstance.instance_name)
        .filter(DBInstance.id.in_(ids))
        .all()
    )
    return {
        int(instance_id): str(name) if name not in (None, "") else str(instance_id)
        for instance_id, name in rows
    }


def get_task_rows(
    db: Session,
    task_id: str,
    instance_id: int | None = None,
) -> list[DBCheckResult]:
    query = db.query(DBCheckResult).filter(DBCheckResult.task_id == task_id)
    if instance_id is not None:
        query = query.filter(DBCheckResult.instance_id == instance_id)
    return query.order_by(DBCheckResult.id.asc()).all()


def count_by_task_ids(db: Session, task_ids: Iterable[str]) -> dict[str, int]:
    """Row count per run id, in **one** grouped query.

    The run list needs a row count for every item on the page. Querying per item is the N+1
    shape that made the old list unusable at depth, and computing the count in Python from a
    second source (job status, report totals) would eventually disagree with the result table.
    """
    ids = [task_id for task_id in dict.fromkeys(task_ids) if task_id]
    if not ids:
        return {}
    rows = (
        db.query(DBCheckResult.task_id, func.count(DBCheckResult.id))
        .filter(DBCheckResult.task_id.in_(ids))
        .group_by(DBCheckResult.task_id)
        .all()
    )
    return {task_id: int(count) for task_id, count in rows}


def severity_counts_by_task_ids(
    db: Session, task_ids: Iterable[str]
) -> dict[str, dict[str, int]]:
    """Severity counts per run id, folded through the one severity mapping in this module.

    A grouped query keeps it to a single round trip; folding each database status through
    :func:`_severity` means the result cannot drift from :func:`_counts`, which is what the
    detail view and the report builder use.
    """
    ids = [task_id for task_id in dict.fromkeys(task_ids) if task_id]
    if not ids:
        return {}
    rows = (
        db.query(DBCheckResult.task_id, DBCheckResult.check_status, func.count(DBCheckResult.id))
        .filter(DBCheckResult.task_id.in_(ids))
        .group_by(DBCheckResult.task_id, DBCheckResult.check_status)
        .all()
    )
    counts: dict[str, dict[str, int]] = {}
    for task_id, status, count in rows:
        bucket = counts.setdefault(task_id, {"critical": 0, "warning": 0, "info": 0, "normal": 0})
        severity = _severity(status)
        bucket[severity] = bucket.get(severity, 0) + int(count)
    return counts


def row_to_dict(row: DBCheckResult) -> dict:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "check_category": row.check_category,
        "check_item": row.check_item,
        "check_status": row.check_status,
        "metric_name": row.metric_name,
        "metric_value": row.metric_value,
        "threshold_value": row.threshold_value,
        "finding": row.finding,
        "suggestion": row.suggestion,
        "raw_data": row.raw_data,
        "create_time": _dt(row.create_time),
    }


def _task_summary(
    task_id: str,
    rows: list[DBCheckResult],
    instance: DBInstance | None,
) -> dict:
    counts = _counts(rows)
    execution = execution_summary(rows)
    score = score_for_counts(
        counts, task_status=execution["task_status"], result_count=len(rows),
    )
    first = rows[0]
    target = report_target(rows)
    create_time = max((row.create_time for row in rows if row.create_time), default=None)
    return {
        "task_id": task_id,
        "instance_id": first.instance_id,
        "instance_name": target["name"],
        "target_snapshot_status": target["status"],
        "db_type": first.db_type,
        "overall_score": score,
        "risk_level": risk_level_for(counts, score, result_count=len(rows)),
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "info_count": counts["info"],
        "normal_count": counts["normal"],
        "total_count": len(rows),
        "create_time": _dt(create_time),
        **execution,
        **account_capability_summary(rows),
        **inspection_template_summary(rows),
    }


def execution_summary(rows: list[DBCheckResult]) -> dict:
    if not rows:
        return {"task_status": "failed", "failure_reason": "巡检没有返回检查结果"}
    states = [(row, finding_execution_status(row)) for row in rows]
    collector_failures = [row for row, state in states if state != "success"]
    completed_rows = [
        row for row, state in states
        if state in {"success", "partial"} and not _is_runtime_summary(row)
    ]
    if not collector_failures:
        if not completed_rows:
            return {"task_status": "failed", "failure_reason": "巡检仅返回运行摘要，未获得检查结果"}
        return {"task_status": "success", "failure_reason": None}
    reasons = []
    # List queries are descending and detail queries ascending; choose the same reasons.
    for row in sorted(collector_failures, key=lambda item: getattr(item, "id", None) or 0):
        raw = row.raw_data if isinstance(row.raw_data, dict) else {}
        item_errors = [
            str(item["error"]) for item in (raw.get("items") or [])
            if isinstance(item, dict) and item.get("error")
        ]
        reason = str(
            raw.get("error") or raw.get("message") or "; ".join(item_errors)
            or row.finding or row.metric_value or row.check_item or "巡检采集失败"
        ).strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    return {
        "task_status": "partial" if completed_rows or any(state == "partial" for _, state in states) else "failed",
        "failure_reason": "\n".join(reasons[:3]) or "巡检采集失败",
    }


def _is_runtime_summary(row) -> bool:
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    return bool(raw.get("inspection_runtime")) or row.metric_name in {
        "collector_status", "script_name", "account_capability", "inspection", "executed_sections",
    } or raw.get("evidence_kind") == "execution_summary"


def finding_execution_status(row) -> str:
    """Interpret collector evidence, independently of the health threshold verdict."""
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    metric_value = str(row.metric_value or "").strip().lower()
    status = str(row.check_status or "").strip().lower()
    execution = str(raw.get("execution_status") or "").lower()
    if execution in {"failed", "error", "failure", "connection_failed", "collection_failed"}:
        return "failed"
    if execution in {"partial", "skipped", "unknown"}:
        return execution
    if (
        metric_value == "collection_failed" or row.metric_name == "collector_error"
        or raw.get("error") or status in {"error", "failed", "failure"}
    ):
        return "failed"
    state = str(raw.get("state") or "").lower()
    if state in {"collection_failed", "failed", "error", "connection_failed"}:
        return "failed"
    not_executed = {"not_executed", "not_supported", "not_applicable", "skipped"}
    if state in not_executed or execution in not_executed or raw.get("executed") is False:
        return "skipped"
    if row.metric_name == "collector_status" and (
        status in {"critical", "warning"}
        or metric_value in {"degraded", "error", "failed", "failure", "connection_failed"}
    ):
        return "failed"
    if row.metric_name == "executed_sections" or raw.get("evidence_kind") == "execution_summary":
        if raw.get("failed_count") or raw.get("skipped_count"):
            return "partial" if raw.get("executed_count") else "failed"
    raw_status = str(raw.get("status") or "").lower()
    if raw_status in {"error", "failed", "failure", "skipped"}:
        return "skipped" if raw_status == "skipped" else "failed"
    # Oracle collectors encode query exceptions as warning + message, not health risks.
    if raw.get("sql") and raw_status == "warning" and raw.get("message"):
        return "failed"
    items = raw.get("items")
    if isinstance(items, list) and any(isinstance(item, dict) and item.get("error") for item in items):
        return "partial" if any(isinstance(item, dict) and not item.get("error") for item in items) else "failed"
    return "success"


def account_capability_summary(rows: list[DBCheckResult]) -> dict:
    capability_labels = {
        "full_inspection": "SYSDBA 完整巡检",
        "standard_inspection": "SYSTEM 标准巡检",
        "limited_inspection": "权限受限巡检",
        "business_limited": "业务账号降级巡检",
    }
    for row in rows:
        if row.metric_name != "account_capability":
            continue
        raw_data = row.raw_data if isinstance(row.raw_data, dict) else {}
        capability = str(raw_data.get("capability") or row.metric_value or "unknown")
        return {
            "account_capability": capability,
            "account_capability_label": raw_data.get("capability_label") or capability_labels.get(capability, "账号能力未评估"),
            "account_capability_status": raw_data.get("status") or _severity(row.check_status),
            "read_only": capability in {"limited_inspection", "business_limited"},
        }
    return {
        "account_capability": "unknown",
        "account_capability_label": "账号能力未评估",
        "account_capability_status": "unknown",
        "read_only": False,
    }


def inspection_template_summary(rows: list[DBCheckResult]) -> dict:
    for row in rows:
        raw_data = row.raw_data if isinstance(row.raw_data, dict) else {}
        template = raw_data.get("inspection_template")
        if not isinstance(template, dict):
            continue
        return {
            "template_id": template.get("id"),
            "template_key": template.get("policy_key"),
            "template_name": template.get("name"),
            "template_revision": template.get("revision"),
            "template_updated_at": template.get("updated_at"),
            "template_item_count": template.get("item_count"),
        }
    return {
        "template_id": None,
        "template_key": None,
        "template_name": None,
        "template_revision": None,
        "template_updated_at": None,
        "template_item_count": None,
    }


def _counts(rows: list[DBCheckResult]) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0, "normal": 0}
    for row in rows:
        status = _severity(row.check_status)
        counts[status] = counts.get(status, 0) + 1
    return counts


def severity_counts(rows: list[DBCheckResult]) -> dict[str, int]:
    """Public name for :func:`_counts`, so read models can *reuse* instead of recomputing."""
    return _counts(rows)


def _score(counts: dict[str, int]) -> int:
    return max(
        0,
        100
        - counts.get("critical", 0) * 25
        - counts.get("warning", 0) * 10
        - counts.get("info", 0) * 2,
    )


def score_for_counts(
    counts: dict[str, int],
    *,
    task_status: str | None,
    result_count: int | None = None,
) -> int | None:
    """Health score for **already computed** counts.

    ``None`` unless the run was a full success **that actually published results**. A run with
    zero rows and status ``success`` used to score 100/normal while the same response said
    ``results_published=False`` and ``evidence.state="missing"`` - the exact "invented health"
    this read model exists to remove. ``result_count=None`` means "the caller has no basis to
    claim a count", and keeps the status-only rule.
    """
    if task_status != "success":
        return None
    if result_count is not None and result_count <= 0:
        return None
    return _score(counts)


def risk_level_for(
    counts: dict[str, int],
    score: int | None,
    *,
    result_count: int | None = None,
) -> str | None:
    """Risk label consistent with :func:`score_for_counts`; ``None`` when there is no evidence."""
    if result_count is not None and result_count <= 0:
        return None
    return _risk_level(counts, score)


def _risk_level(counts: dict[str, int], score: int | None) -> str:
    if score is None:
        return "warning"
    if counts.get("critical", 0):
        return "critical"
    if counts.get("warning", 0):
        return "warning"
    if counts.get("info", 0):
        return "info"
    return "normal" if score >= 90 else "info"


def _severity(status: str | None) -> str:
    normalized = str(status or "").lower()
    if normalized in {"error", "failed", "failure"}:
        return "warning"
    if normalized in {"critical", "warning", "info"}:
        return normalized
    return "normal"


def _string_value(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ") if value else None
