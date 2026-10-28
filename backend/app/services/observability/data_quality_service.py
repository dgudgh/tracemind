from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.observability import (
    ObservabilityAlertEvent,
    ObservabilityCollectLog,
    ObservabilityCollectTask,
    ObservabilityCurrentMetric,
    ObservabilityDataQualityPolicy,
    ObservabilityDataQualitySnapshot,
    ObservabilityMetricHistory,
    ObservabilityQueryAudit,
    ObservabilitySource,
)


METRIC_SOURCE_TYPES = {
    "zabbix",
    "prometheus",
    "exporter",
    "database_direct",
    "ssh_host",
    "nginx",
    "tomcat_jvm",
}
EVENT_SOURCE_TYPES = {"alertmanager", "webhook"}

STATUS_PRIORITY = {
    "missing": 6,
    "stale": 5,
    "degraded": 4,
    "unknown": 3,
    "healthy": 2,
    "disabled": 1,
}

DEFAULT_POLICY = {
    "scope_key": "global",
    "policy_name": "平台默认数据质量策略",
    "source_id": None,
    "source_type": None,
    "freshness_warn_seconds": 180,
    "freshness_critical_seconds": 600,
    "completeness_window_minutes": 60,
    "min_completeness_pct": 90,
    "max_clock_skew_seconds": 60,
    "cardinality_budget": 5000,
    "hot_retention_days": 7,
    "warm_retention_days": 30,
    "archive_retention_days": 180,
    "anomaly_sensitivity": 3.0,
    "evaluate_interval_seconds": 900,
    "retention_interval_seconds": 86400,
    "retention_auto_execute": 0,
    "enabled": 1,
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def policy_scope_key(*, source_id: int | None = None, source_type: str | None = None) -> str:
    if source_id is not None:
        return f"source:{int(source_id)}"
    if source_type:
        return f"type:{source_type.strip().lower()}"
    return "global"


def policy_to_dict(row: ObservabilityDataQualityPolicy | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row.id,
        "scope_key": row.scope_key,
        "policy_name": row.policy_name,
        "source_id": row.source_id,
        "source_type": row.source_type,
        "freshness_warn_seconds": row.freshness_warn_seconds,
        "freshness_critical_seconds": row.freshness_critical_seconds,
        "completeness_window_minutes": row.completeness_window_minutes,
        "min_completeness_pct": row.min_completeness_pct,
        "max_clock_skew_seconds": row.max_clock_skew_seconds,
        "cardinality_budget": row.cardinality_budget,
        "hot_retention_days": row.hot_retention_days,
        "warm_retention_days": row.warm_retention_days,
        "archive_retention_days": row.archive_retention_days,
        "anomaly_sensitivity": float(row.anomaly_sensitivity or 0),
        "evaluate_interval_seconds": row.evaluate_interval_seconds,
        "retention_interval_seconds": row.retention_interval_seconds,
        "retention_auto_execute": row.retention_auto_execute,
        "last_retention_at": row.last_retention_at,
        "enabled": row.enabled,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def ensure_default_policy(db: Session) -> ObservabilityDataQualityPolicy:
    row = (
        db.query(ObservabilityDataQualityPolicy)
        .filter(ObservabilityDataQualityPolicy.scope_key == "global")
        .first()
    )
    if row:
        return row
    row = ObservabilityDataQualityPolicy(**DEFAULT_POLICY)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_policies(db: Session) -> list[dict[str, Any]]:
    ensure_default_policy(db)
    rows = (
        db.query(ObservabilityDataQualityPolicy)
        .order_by(ObservabilityDataQualityPolicy.scope_key.asc())
        .all()
    )
    return [policy_to_dict(row) for row in rows]


def upsert_policy(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    source_id = data.get("source_id")
    source_type = (data.get("source_type") or "").strip().lower() or None
    if source_id is not None:
        source = db.query(ObservabilitySource).filter(ObservabilitySource.id == int(source_id)).first()
        if not source:
            raise ValueError("observability source not found")
        source_type = source.source_type
    scope_key = policy_scope_key(source_id=source_id, source_type=source_type)
    row = (
        db.query(ObservabilityDataQualityPolicy)
        .filter(ObservabilityDataQualityPolicy.scope_key == scope_key)
        .first()
    )
    if not row:
        base = policy_to_dict(resolve_policy(db, source_id=source_id, source_type=source_type))
        base.pop("id", None)
        base.pop("create_time", None)
        base.pop("update_time", None)
        base.update({
            "scope_key": scope_key,
            "policy_name": data.get("policy_name") or f"{scope_key} 数据质量策略",
            "source_id": source_id,
            "source_type": source_type,
        })
        row = ObservabilityDataQualityPolicy(**base)
        db.add(row)
    mutable_fields = (
        "policy_name",
        "freshness_warn_seconds",
        "freshness_critical_seconds",
        "completeness_window_minutes",
        "min_completeness_pct",
        "max_clock_skew_seconds",
        "cardinality_budget",
        "hot_retention_days",
        "warm_retention_days",
        "archive_retention_days",
        "anomaly_sensitivity",
        "evaluate_interval_seconds",
        "retention_interval_seconds",
        "retention_auto_execute",
        "enabled",
    )
    for field in mutable_fields:
        if field in data and data[field] is not None:
            setattr(row, field, data[field])
    _validate_policy(row)
    db.commit()
    db.refresh(row)
    return policy_to_dict(row)


def resolve_policy(
    db: Session,
    *,
    source_id: int | None,
    source_type: str | None,
) -> ObservabilityDataQualityPolicy:
    ensure_default_policy(db)
    keys = []
    if source_id is not None:
        keys.append(policy_scope_key(source_id=source_id))
    if source_type:
        keys.append(policy_scope_key(source_type=source_type))
    keys.append("global")
    rows = (
        db.query(ObservabilityDataQualityPolicy)
        .filter(
            ObservabilityDataQualityPolicy.scope_key.in_(keys),
            ObservabilityDataQualityPolicy.enabled == 1,
        )
        .all()
    )
    by_key = {row.scope_key: row for row in rows}
    for key in keys:
        if key in by_key:
            return by_key[key]
    return ensure_default_policy(db)


def evaluate_data_quality(
    db: Session,
    *,
    source_id: int | None = None,
    persist: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    evaluated_at = now or _utcnow()
    query = db.query(ObservabilitySource)
    if source_id is not None:
        query = query.filter(ObservabilitySource.id == source_id)
    sources = query.order_by(ObservabilitySource.id.asc()).all()
    if source_id is not None and not sources:
        raise ValueError("observability source not found")

    items = [_evaluate_source(db, source, evaluated_at) for source in sources]
    if persist:
        for item in items:
            db.add(_snapshot_from_item(item, evaluated_at))
        db.commit()

    status_counts = {key: 0 for key in STATUS_PRIORITY}
    for item in items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    enabled_items = [item for item in items if item["status"] != "disabled"]
    overall_status = max(
        (item["status"] for item in enabled_items),
        key=lambda status: STATUS_PRIORITY.get(status, 0),
        default="unknown",
    )
    average_score = round(sum(item["score"] for item in enabled_items) / len(enabled_items)) if enabled_items else 0
    trusted_count = len([item for item in enabled_items if item["status"] == "healthy"])
    return {
        "overall_status": overall_status,
        "score": average_score,
        "source_count": len(items),
        "enabled_source_count": len(enabled_items),
        "trusted_source_count": trusted_count,
        "trusted_ratio": round(trusted_count / len(enabled_items), 4) if enabled_items else 0,
        "status_counts": status_counts,
        "items": items,
        "persisted": bool(persist),
        "evaluated_at": evaluated_at,
    }


def list_snapshots(
    db: Session,
    *,
    source_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = db.query(ObservabilityDataQualitySnapshot)
    if source_id is not None:
        query = query.filter(ObservabilityDataQualitySnapshot.source_id == source_id)
    rows = (
        query.order_by(
            ObservabilityDataQualitySnapshot.evaluated_at.desc(),
            ObservabilityDataQualitySnapshot.id.desc(),
        )
        .limit(max(1, min(int(limit or 100), 1000)))
        .all()
    )
    return [_snapshot_to_dict(row) for row in rows]


def retention_plan(db: Session, *, source_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    evaluated_at = now or _utcnow()
    query = db.query(ObservabilitySource)
    if source_id is not None:
        query = query.filter(ObservabilitySource.id == source_id)
    sources = query.order_by(ObservabilitySource.id.asc()).all()
    if source_id is not None and not sources:
        raise ValueError("observability source not found")
    items = []
    for source in sources:
        policy = resolve_policy(db, source_id=source.id, source_type=source.source_type)
        cutoff = evaluated_at - timedelta(days=policy.archive_retention_days)
        count = _scalar_count(
            db.query(ObservabilityMetricHistory).filter(
                ObservabilityMetricHistory.source_id == source.id,
                ObservabilityMetricHistory.metric_time < cutoff,
            )
        )
        items.append({
            "source_id": source.id,
            "source_name": source.source_name,
            "source_type": source.source_type,
            "archive_retention_days": policy.archive_retention_days,
            "cutoff": cutoff,
            "expired_sample_count": count,
        })
    return {
        "items": items,
        "expired_sample_count": sum(item["expired_sample_count"] for item in items),
        "generated_at": evaluated_at,
    }


def run_retention(
    db: Session,
    *,
    source_id: int | None = None,
    dry_run: bool = True,
    confirmation: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = retention_plan(db, source_id=source_id, now=now)
    if dry_run:
        return {**plan, "dry_run": True, "deleted_count": 0}
    if confirmation != "DELETE_EXPIRED_OBSERVABILITY":
        raise ValueError("retention execution requires explicit confirmation")
    deleted = 0
    for item in plan["items"]:
        deleted += (
            db.query(ObservabilityMetricHistory)
            .filter(
                ObservabilityMetricHistory.source_id == item["source_id"],
                ObservabilityMetricHistory.metric_time < item["cutoff"],
            )
            .delete(synchronize_session=False)
        )
    db.add(ObservabilityCollectLog(
        source_id=source_id,
        log_level="info",
        log_type="retention",
        message=f"数据保留策略执行完成，删除 {deleted} 条过期指标样本",
        raw_data={"deleted_count": deleted, "source_id": source_id},
    ))
    db.commit()
    return {**plan, "dry_run": False, "deleted_count": deleted}


def _evaluate_source(db: Session, source: ObservabilitySource, now: datetime) -> dict[str, Any]:
    policy = resolve_policy(db, source_id=source.id, source_type=source.source_type)
    policy_data = policy_to_dict(policy)
    source_kind = _source_kind(source.source_type)
    window_start = now - timedelta(minutes=policy.completeness_window_minutes)
    interval_seconds = max(15, int(source.collect_interval_seconds or 60))

    latest_task = (
        db.query(ObservabilityCollectTask)
        .filter(ObservabilityCollectTask.source_id == source.id)
        .order_by(ObservabilityCollectTask.create_time.desc(), ObservabilityCollectTask.id.desc())
        .first()
    )
    latest_metric_at = _latest_metric_time(db, source.id)
    latest_event_at = db.query(func.max(ObservabilityAlertEvent.last_time)).filter(
        ObservabilityAlertEvent.source_id == source.id
    ).scalar()
    latest_query_at = db.query(func.max(ObservabilityQueryAudit.create_time)).filter(
        ObservabilityQueryAudit.source_id == source.id,
        ObservabilityQueryAudit.status == "success",
    ).scalar()
    latest_success_task_at = None
    if latest_task and latest_task.task_status == "success":
        latest_success_task_at = latest_task.end_time or latest_task.start_time or latest_task.create_time

    if source_kind == "metric":
        last_observed_at = latest_metric_at
    elif source_kind == "event":
        last_observed_at = latest_event_at or latest_success_task_at
    else:
        last_observed_at = latest_query_at or latest_success_task_at

    history_query = db.query(ObservabilityMetricHistory).filter(
        ObservabilityMetricHistory.source_id == source.id,
        ObservabilityMetricHistory.metric_time >= window_start,
        ObservabilityMetricHistory.metric_time <= now + timedelta(seconds=policy.max_clock_skew_seconds),
    )
    sample_count = _scalar_count(history_query)
    cardinality = (
        db.query(
            ObservabilityMetricHistory.standard_metric,
            ObservabilityMetricHistory.instance_id,
            ObservabilityMetricHistory.host_id,
        )
        .filter(
            ObservabilityMetricHistory.source_id == source.id,
            ObservabilityMetricHistory.metric_time >= window_start,
        )
        .group_by(
            ObservabilityMetricHistory.standard_metric,
            ObservabilityMetricHistory.instance_id,
            ObservabilityMetricHistory.host_id,
        )
        .count()
    )
    current_series = (
        db.query(
            ObservabilityCurrentMetric.standard_metric,
            ObservabilityCurrentMetric.instance_id,
            ObservabilityCurrentMetric.host_id,
        )
        .filter(ObservabilityCurrentMetric.source_id == source.id)
        .group_by(
            ObservabilityCurrentMetric.standard_metric,
            ObservabilityCurrentMetric.instance_id,
            ObservabilityCurrentMetric.host_id,
        )
        .count()
    )
    cardinality = max(cardinality, current_series)
    expected_samples = 0
    completeness_pct = None
    missing_ratio = None
    if source_kind == "metric":
        expected_intervals = max(1, int(policy.completeness_window_minutes * 60 / interval_seconds))
        expected_samples = max(1, cardinality * expected_intervals) if cardinality else 1
        completeness_pct = round(min(100.0, sample_count * 100.0 / expected_samples), 2)
        missing_ratio = round(max(0.0, 1.0 - completeness_pct / 100.0), 4)

    lag_seconds = None
    clock_skew_seconds = None
    if last_observed_at:
        raw_lag = int((now - _naive_utc(last_observed_at)).total_seconds())
        lag_seconds = max(0, raw_lag)
        clock_skew_seconds = max(0, -raw_lag)
    freshness_status = _freshness_status(
        source_kind=source_kind,
        last_observed_at=last_observed_at,
        lag_seconds=lag_seconds,
        warn_seconds=policy.freshness_warn_seconds,
        critical_seconds=policy.freshness_critical_seconds,
    )

    retention = _retention_counts(db, source.id, policy, now)
    baseline_minimum = max(30, cardinality * 10) if source_kind == "metric" else 1
    baseline_ready = sample_count >= baseline_minimum if source_kind == "metric" else bool(last_observed_at)
    task_status = latest_task.task_status if latest_task else "never_run"

    checks = [
        _check("configuration", "连接配置", "ok" if source.api_url or source.source_type == "webhook" else "failed", source.api_url or "未配置访问地址"),
        _check("freshness", "数据新鲜度", freshness_status, _freshness_message(freshness_status, lag_seconds)),
        _check(
            "completeness",
            "采样完整率",
            _completeness_status(source_kind, completeness_pct, policy.min_completeness_pct),
            "不适用" if completeness_pct is None else f"{completeness_pct:.2f}% / 要求不低于 {policy.min_completeness_pct}%",
        ),
        _check(
            "clock_skew",
            "时钟偏差",
            "failed" if (clock_skew_seconds or 0) > policy.max_clock_skew_seconds else "ok" if last_observed_at else "unknown",
            "暂无可比较时间戳" if clock_skew_seconds is None else f"未来偏移 {clock_skew_seconds}s / 上限 {policy.max_clock_skew_seconds}s",
        ),
        _check(
            "task_health",
            "采集任务",
            "failed" if task_status == "failed" else "ok" if task_status == "success" else "unknown",
            _task_message(latest_task),
        ),
        _check(
            "cardinality",
            "指标基数",
            "failed" if cardinality > policy.cardinality_budget else "ok" if cardinality else "unknown",
            f"{cardinality} / 预算 {policy.cardinality_budget}",
        ),
        _check(
            "baseline",
            "异常基线",
            "ok" if baseline_ready else "unknown",
            f"窗口样本 {sample_count} / 基线最低 {baseline_minimum}",
        ),
        _check(
            "retention",
            "保留策略",
            "warning" if retention["expired"] else "ok",
            f"热 {policy.hot_retention_days} 天 / 温 {policy.warm_retention_days} 天 / 归档 {policy.archive_retention_days} 天；过期 {retention['expired']} 条",
        ),
    ]
    status, score = _overall_source_status(
        source=source,
        source_kind=source_kind,
        freshness_status=freshness_status,
        task_status=task_status,
        completeness_pct=completeness_pct,
        min_completeness_pct=policy.min_completeness_pct,
        clock_skew_seconds=clock_skew_seconds,
        max_clock_skew_seconds=policy.max_clock_skew_seconds,
        cardinality=cardinality,
        cardinality_budget=policy.cardinality_budget,
    )
    return {
        "source_id": source.id,
        "source_name": source.source_name,
        "source_type": source.source_type,
        "source_kind": source_kind,
        "enabled": bool(source.enabled),
        "status": status,
        "score": score,
        "freshness_status": freshness_status,
        "last_observed_at": last_observed_at,
        "lag_seconds": lag_seconds,
        "completeness_pct": completeness_pct,
        "missing_ratio": missing_ratio,
        "sample_count": sample_count,
        "expected_sample_count": expected_samples,
        "clock_skew_seconds": clock_skew_seconds,
        "task_status": task_status,
        "last_task_at": (latest_task.end_time or latest_task.start_time or latest_task.create_time) if latest_task else None,
        "last_task_error": latest_task.error_message if latest_task and latest_task.task_status == "failed" else None,
        "cardinality": cardinality,
        "cardinality_budget": policy.cardinality_budget,
        "baseline_ready": baseline_ready,
        "retention": retention,
        "expired_sample_count": retention["expired"],
        "policy": policy_data,
        "checks": checks,
    }


def _overall_source_status(
    *,
    source: ObservabilitySource,
    source_kind: str,
    freshness_status: str,
    task_status: str,
    completeness_pct: float | None,
    min_completeness_pct: int,
    clock_skew_seconds: int | None,
    max_clock_skew_seconds: int,
    cardinality: int,
    cardinality_budget: int,
) -> tuple[str, int]:
    if not source.enabled:
        return "disabled", 0
    if not source.api_url and source.source_type != "webhook":
        return "missing", 0
    if source_kind == "metric" and freshness_status == "missing":
        return "missing", 0
    if freshness_status == "stale":
        return "stale", 35
    if source_kind != "metric" and freshness_status == "unknown" and task_status == "never_run":
        return "unknown", 50

    score = 100
    degraded = False
    if freshness_status == "warning":
        score -= 15
        degraded = True
    if task_status == "failed":
        score -= 30
        degraded = True
    elif task_status not in {"success", "never_run"}:
        score -= 10
        degraded = True
    if completeness_pct is not None and completeness_pct < min_completeness_pct:
        score -= min(35, max(10, round((min_completeness_pct - completeness_pct) / 2)))
        degraded = True
    if (clock_skew_seconds or 0) > max_clock_skew_seconds:
        score -= 20
        degraded = True
    if cardinality > cardinality_budget:
        score -= 15
        degraded = True
    return ("degraded" if degraded else "healthy", max(0, score))


def _source_kind(source_type: str) -> str:
    if source_type in METRIC_SOURCE_TYPES:
        return "metric"
    if source_type in EVENT_SOURCE_TYPES:
        return "event"
    return "on_demand"


def _latest_metric_time(db: Session, source_id: int) -> datetime | None:
    current_time = db.query(func.max(ObservabilityCurrentMetric.collect_time)).filter(
        ObservabilityCurrentMetric.source_id == source_id
    ).scalar()
    history_time = db.query(func.max(ObservabilityMetricHistory.metric_time)).filter(
        ObservabilityMetricHistory.source_id == source_id
    ).scalar()
    values = [_naive_utc(value) for value in (current_time, history_time) if value]
    return max(values) if values else None


def _freshness_status(
    *,
    source_kind: str,
    last_observed_at: datetime | None,
    lag_seconds: int | None,
    warn_seconds: int,
    critical_seconds: int,
) -> str:
    if last_observed_at is None:
        return "missing" if source_kind == "metric" else "unknown"
    if lag_seconds is None:
        return "unknown"
    if lag_seconds > critical_seconds:
        return "stale"
    if lag_seconds > warn_seconds:
        return "warning"
    return "fresh"


def _freshness_message(status: str, lag_seconds: int | None) -> str:
    if status == "missing":
        return "尚未收到任何指标样本"
    if status == "unknown":
        return "尚无采集或查询证据"
    return f"最近数据延迟 {lag_seconds or 0}s"


def _completeness_status(source_kind: str, value: float | None, minimum: int) -> str:
    if source_kind != "metric":
        return "not_applicable"
    if value is None:
        return "unknown"
    return "ok" if value >= minimum else "failed"


def _task_message(task: ObservabilityCollectTask | None) -> str:
    if not task:
        return "尚未执行采集任务"
    if task.task_status == "failed":
        return task.error_message or "最近采集任务失败"
    return f"最近任务 {task.task_status}，成功 {task.success_count}，失败 {task.fail_count}"


def _retention_counts(
    db: Session,
    source_id: int,
    policy: ObservabilityDataQualityPolicy,
    now: datetime,
) -> dict[str, int]:
    hot_cutoff = now - timedelta(days=policy.hot_retention_days)
    warm_cutoff = now - timedelta(days=policy.warm_retention_days)
    archive_cutoff = now - timedelta(days=policy.archive_retention_days)
    query = db.query(ObservabilityMetricHistory).filter(ObservabilityMetricHistory.source_id == source_id)
    hot = _scalar_count(query.filter(ObservabilityMetricHistory.metric_time >= hot_cutoff))
    warm = _scalar_count(query.filter(
        ObservabilityMetricHistory.metric_time < hot_cutoff,
        ObservabilityMetricHistory.metric_time >= warm_cutoff,
    ))
    archive = _scalar_count(query.filter(
        ObservabilityMetricHistory.metric_time < warm_cutoff,
        ObservabilityMetricHistory.metric_time >= archive_cutoff,
    ))
    expired = _scalar_count(query.filter(ObservabilityMetricHistory.metric_time < archive_cutoff))
    return {"hot": hot, "warm": warm, "archive": archive, "expired": expired}


def _snapshot_from_item(item: dict[str, Any], evaluated_at: datetime) -> ObservabilityDataQualitySnapshot:
    details = {
        "source_name": item["source_name"],
        "source_kind": item["source_kind"],
        "sample_count": item["sample_count"],
        "expected_sample_count": item["expected_sample_count"],
        "baseline_ready": item["baseline_ready"],
        "retention": item["retention"],
        "checks": item["checks"],
        "policy_scope": item["policy"]["scope_key"],
    }
    return ObservabilityDataQualitySnapshot(
        source_id=item["source_id"],
        source_type=item["source_type"],
        status=item["status"],
        score=item["score"],
        freshness_status=item["freshness_status"],
        last_observed_at=item["last_observed_at"],
        lag_seconds=item["lag_seconds"],
        completeness_pct=item["completeness_pct"],
        missing_ratio=item["missing_ratio"],
        clock_skew_seconds=item["clock_skew_seconds"],
        task_status=item["task_status"],
        cardinality=item["cardinality"],
        cardinality_budget=item["cardinality_budget"],
        expired_sample_count=item["expired_sample_count"],
        details=details,
        evaluated_at=evaluated_at,
    )


def _snapshot_to_dict(row: ObservabilityDataQualitySnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_id": row.source_id,
        "source_type": row.source_type,
        "status": row.status,
        "score": row.score,
        "freshness_status": row.freshness_status,
        "last_observed_at": row.last_observed_at,
        "lag_seconds": row.lag_seconds,
        "completeness_pct": float(row.completeness_pct) if row.completeness_pct is not None else None,
        "missing_ratio": float(row.missing_ratio) if row.missing_ratio is not None else None,
        "clock_skew_seconds": row.clock_skew_seconds,
        "task_status": row.task_status,
        "cardinality": row.cardinality,
        "cardinality_budget": row.cardinality_budget,
        "expired_sample_count": row.expired_sample_count,
        "details": row.details,
        "evaluated_at": row.evaluated_at,
    }


def _validate_policy(row: ObservabilityDataQualityPolicy) -> None:
    if row.freshness_warn_seconds < 15:
        raise ValueError("freshness_warn_seconds must be at least 15")
    if row.freshness_critical_seconds <= row.freshness_warn_seconds:
        raise ValueError("freshness_critical_seconds must be greater than freshness_warn_seconds")
    if not 1 <= row.min_completeness_pct <= 100:
        raise ValueError("min_completeness_pct must be between 1 and 100")
    if row.completeness_window_minutes < 5:
        raise ValueError("completeness_window_minutes must be at least 5")
    if row.max_clock_skew_seconds < 0:
        raise ValueError("max_clock_skew_seconds must not be negative")
    if row.cardinality_budget < 1:
        raise ValueError("cardinality_budget must be positive")
    if not 1 <= row.hot_retention_days <= row.warm_retention_days <= row.archive_retention_days:
        raise ValueError("retention days must satisfy hot <= warm <= archive")
    if not Decimal("0.5") <= Decimal(str(row.anomaly_sensitivity)) <= Decimal("10"):
        raise ValueError("anomaly_sensitivity must be between 0.5 and 10")


def _check(key: str, label: str, status: str, message: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "message": message}


def _scalar_count(query) -> int:
    return int(query.with_entities(func.count()).scalar() or 0)


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value
