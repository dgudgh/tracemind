from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.capacity_policy import DBCapacityAlert, DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.object_size import DBObjectSizeDaily
from app.services import alert_lifecycle

logger = logging.getLogger("app.services.capacity_monitor")

# Capacity snapshots only read catalogs, but they run on a schedule against real
# production instances. SQL Server has always been bounded at 10s; the other three
# engines were unbounded, so a single slow catalog scan could pin a worker (and the
# scheduler slot behind it) indefinitely. Keep all four engines on the same budget,
# sourced from the single definition in app.collectors.utils.
CAPACITY_QUERY_TIMEOUT_SECONDS = COLLECTION_QUERY_TIMEOUT_SECONDS


def _to_float(value) -> float:
    if value is None:
        return 0.0
    return float(value)


def get_or_create_policy(db: Session, instance_id: int) -> DBCapacityPolicy:
    policy = db.query(DBCapacityPolicy).filter(DBCapacityPolicy.instance_id == instance_id).first()
    if policy:
        return policy
    policy = DBCapacityPolicy(
        instance_id=instance_id,
        enabled=1,
        frequency_minutes=1440,
        forecast_days_threshold=15,
        abnormal_growth_mb=Decimal("10240"),
        abnormal_growth_pct=Decimal("30"),
        retention_days=180,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def update_policy(db: Session, instance_id: int, payload) -> DBCapacityPolicy:
    policy = get_or_create_policy(db, instance_id)
    for key, value in payload.model_dump().items():
        setattr(policy, key, value)
    policy.next_run_time = _next_run_time(policy)
    db.commit()
    db.refresh(policy)
    return policy


def delete_policy(db: Session, instance_id: int) -> None:
    policy = db.query(DBCapacityPolicy).filter(DBCapacityPolicy.instance_id == instance_id).first()
    if policy:
        db.delete(policy)
        db.commit()


def policy_to_dict(policy: DBCapacityPolicy) -> dict:
    return {
        "id": policy.id,
        "instance_id": policy.instance_id,
        "enabled": policy.enabled,
        "frequency_minutes": policy.frequency_minutes,
        "forecast_days_threshold": policy.forecast_days_threshold,
        "abnormal_growth_mb": _to_float(policy.abnormal_growth_mb),
        "abnormal_growth_pct": _to_float(policy.abnormal_growth_pct),
        "retention_days": policy.retention_days,
        "last_run_time": policy.last_run_time.isoformat(sep=" ") if policy.last_run_time else None,
        "last_success_time": policy.last_success_time.isoformat(sep=" ") if policy.last_success_time else None,
        "next_run_time": policy.next_run_time.isoformat(sep=" ") if policy.next_run_time else None,
        "last_status": policy.last_status,
        "last_message": policy.last_message,
        "consecutive_failures": policy.consecutive_failures or 0,
        "sustained_minutes": policy.sustained_minutes or 0,
        "recovery_margin_pct": _to_float(policy.recovery_margin_pct),
        "last_sample_count": policy.last_sample_count or 0,
        "data_freshness_status": policy.data_freshness_status or "unknown",
    }


def _next_run_time(policy: DBCapacityPolicy) -> datetime:
    base = policy.last_run_time or datetime.now()
    return base + timedelta(minutes=policy.frequency_minutes or 1440)


def _oracle_capacity_rows(instance: DBInstance) -> list[dict]:
    from app.collectors.oracle.oracle_connection import oracle_connection

    with oracle_connection(
        instance, call_timeout_ms=CAPACITY_QUERY_TIMEOUT_SECONDS * 1000,
    ) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.tablespace_name, "
            "ROUND((t.unalloc_size + t.used_size) / 1024 / 1024, 2) AS total_mb, "
            "ROUND((t.used_size - NVL(t.free_size, 0)) / 1024 / 1024, 2) AS used_mb, "
            "ROUND(NVL(t.free_size, 0) / 1024 / 1024, 2) AS free_mb, "
            "ROUND((t.used_size - NVL(t.free_size, 0)) / NULLIF(t.unalloc_size + t.used_size, 0) * 100, 2) AS used_pct "
            "FROM ("
            "  SELECT a.tablespace_name, "
            "    a.unalloc_size, "
            "    f.free_size, "
            "    a.used_size "
            "  FROM ("
            "    SELECT tablespace_name, SUM(bytes) AS free_size "
            "    FROM dba_free_space GROUP BY tablespace_name"
            "  ) f, ("
            "    SELECT tablespace_name, "
            "      SUM(user_bytes) AS used_size, "
            "      SUM(DECODE(autoextensible, 'YES', DECODE(SIGN(maxbytes - user_bytes), -1, 0, maxbytes - user_bytes), 0)) AS unalloc_size "
            "    FROM dba_data_files GROUP BY tablespace_name"
            "  ) a WHERE a.tablespace_name = f.tablespace_name(+)"
            ") t ORDER BY used_pct DESC"
        )
        tablespaces = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        for row in tablespaces:
            row["object_name"] = row.get("tablespace_name")
            row["object_type"] = "tablespace"

        asm_diskgroups = []
        try:
            cursor.execute(
                "SELECT name AS object_name, "
                "ROUND(total_mb,2) AS total_mb, "
                "ROUND(total_mb - free_mb,2) AS used_mb, "
                "ROUND(free_mb,2) AS free_mb, "
                "ROUND((total_mb - free_mb) / NULLIF(total_mb,0) * 100,2) AS used_pct "
                "FROM v$asm_diskgroup ORDER BY used_pct DESC"
            )
            asm_diskgroups = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            for row in asm_diskgroups:
                row["tablespace_name"] = row.get("object_name")
                row["object_type"] = "asm_diskgroup"
        except Exception as exc:
            logger.warning("ASM 磁盘组容量采集失败 | instance=%s | error=%s", instance.instance_name, exc)

        return tablespaces + asm_diskgroups


def _mysql_capacity_rows(instance: DBInstance) -> list[dict]:
    from app.collectors.mysql.mysql_connection import mysql_connection

    with mysql_connection(instance, read_timeout=CAPACITY_QUERY_TIMEOUT_SECONDS) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT table_schema AS tablespace_name, "
            "ROUND(SUM(data_length + index_length)/1024/1024,2) AS total_mb, "
            "ROUND(SUM(data_length)/1024/1024,2) AS used_mb, "
            "ROUND(SUM(index_length)/1024/1024,2) AS index_mb, "
            "0 AS free_mb, 100 AS used_pct "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys') "
            "GROUP BY table_schema ORDER BY total_mb DESC"
        )
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["object_name"] = row.get("tablespace_name")
            row["object_type"] = "schema"
            row["capacity_limit_known"] = False
            row["used_pct"] = None
        return result


def _postgresql_capacity_rows(instance: DBInstance) -> list[dict]:
    from app.collectors.postgresql.postgresql_connection import postgresql_connection

    with postgresql_connection(
        instance, statement_timeout_ms=CAPACITY_QUERY_TIMEOUT_SECONDS * 1000,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT datname AS object_name, "
                "ROUND(pg_database_size(datname) / 1024.0 / 1024.0, 2) AS used_mb "
                "FROM pg_database WHERE datallowconn = TRUE "
                "ORDER BY pg_database_size(datname) DESC"
            )
            rows = [dict(row) for row in (cursor.fetchall() or [])]
    for row in rows:
        used_mb = _to_float(row.get("used_mb"))
        row.update({
            "tablespace_name": row.get("object_name"),
            "object_type": "database",
            "total_mb": used_mb,
            "free_mb": None,
            "used_pct": None,
            "capacity_limit_known": False,
        })
    return rows


def _sqlserver_capacity_rows(instance: DBInstance) -> list[dict]:
    from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection

    with sqlserver_connection(instance, read_only=True) as conn:
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(
                "SELECT DB_NAME() AS object_name, "
                "CAST(SUM(CAST(size AS BIGINT)) * 8.0 / 1024 AS DECIMAL(18,2)) AS total_mb, "
                "CAST(SUM(COALESCE(CAST(FILEPROPERTY(name, 'SpaceUsed') AS BIGINT), CAST(size AS BIGINT))) "
                "* 8.0 / 1024 AS DECIMAL(18,2)) AS used_mb "
                "FROM sys.database_files"
            )
            rows = [dict(row) for row in (cursor.fetchall() or []) if row]
    for row in rows:
        total_mb = _to_float(row.get("total_mb"))
        used_mb = min(_to_float(row.get("used_mb")), total_mb) if total_mb else 0
        row.update({
            "tablespace_name": row.get("object_name"),
            "object_type": "database",
            "used_mb": used_mb,
            "free_mb": max(total_mb - used_mb, 0),
            "used_pct": round(used_mb / total_mb * 100, 2) if total_mb else None,
            "capacity_limit_known": True,
        })
    return rows


def _empty_capacity(instance: DBInstance, warning: str) -> dict:
    return {
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "total_mb": None,
        "used_mb": None,
        "free_mb": None,
        "used_pct": None,
        "objects": [],
        "warnings": [warning],
        "unsupported": True,
        "collection_status": "unsupported",
        "capacity_limit_known": False,
    }


def collect_current_capacity(instance: DBInstance) -> dict:
    db_type = str(instance.db_type or "").lower()
    if db_type == "oracle":
        rows = _oracle_capacity_rows(instance)
    elif db_type in {"mysql", "mariadb"}:
        rows = _mysql_capacity_rows(instance)
    elif db_type in {"postgresql", "postgres", "pg"}:
        rows = _postgresql_capacity_rows(instance)
    elif db_type in {"sqlserver", "mssql", "sql server"}:
        rows = _sqlserver_capacity_rows(instance)
    else:
        return _empty_capacity(instance, f"{instance.db_type} 实时容量采集暂未接入，已跳过实时连接。")
    if not rows:
        return {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "total_mb": None,
            "used_mb": None,
            "free_mb": None,
            "used_pct": None,
            "objects": [],
            "warnings": ["容量查询执行成功但未返回任何对象，结果按缺数处理。"],
            "collection_status": "no_data",
            "capacity_limit_known": False,
        }
    total_mb = sum(_to_float(row.get("total_mb")) for row in rows)
    used_mb = sum(_to_float(row.get("used_mb")) for row in rows)
    capacity_limit_known = all(row.get("capacity_limit_known", True) for row in rows)
    return {
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "total_mb": round(total_mb, 2),
        "used_mb": round(used_mb, 2),
        "free_mb": round(total_mb - used_mb, 2) if capacity_limit_known else None,
        "used_pct": round(used_mb / total_mb * 100, 2) if total_mb and capacity_limit_known else None,
        "objects": rows,
        "collection_status": "success",
        "capacity_limit_known": capacity_limit_known,
    }


def _mirror_capacity_alert(db: Session, alert: DBCapacityAlert) -> None:
    db.flush()
    alert_lifecycle.upsert_event(db, {
        "event_uid": f"capacity:{alert.id}",
        "source": "capacity",
        "source_ref": str(alert.id),
        "instance_id": alert.instance_id,
        "db_type": alert.db_type,
        "severity": alert.level,
        "status": "resolved" if alert.status == "closed" else ("suppressed" if alert.status == "isolated" else "firing"),
        "title": alert.message,
        "message": alert.message,
        "object_type": alert.object_type,
        "object_name": alert.object_name,
        "metric_name": alert.alert_type,
        "metric_value": alert.metric_value,
        "threshold_value": alert.threshold_value,
        "first_seen": alert.first_seen or alert.create_time,
        "last_seen": alert.last_seen or alert.create_time,
        "annotations": {
            "capacity_alert_id": alert.id,
            "isolate_reason": alert.isolate_reason,
        },
    })


def _upsert_capacity_alert(
    db: Session,
    instance: DBInstance,
    *,
    object_name: str,
    object_type: str,
    alert_type: str,
    level: str,
    message: str,
    metric_value: Any = None,
    threshold_value: Any = None,
    now: datetime | None = None,
    sustained_minutes: int = 0,
) -> DBCapacityAlert:
    now = now or datetime.now()
    sustained_minutes = max(int(sustained_minutes or 0), 0)
    alert = db.query(DBCapacityAlert).filter(
        DBCapacityAlert.instance_id == instance.id,
        DBCapacityAlert.object_name == object_name,
        DBCapacityAlert.object_type == object_type,
        DBCapacityAlert.alert_type == alert_type,
        DBCapacityAlert.status.in_(["open", "isolated"]),
    ).order_by(DBCapacityAlert.id.desc()).first()
    if not alert:
        alert = DBCapacityAlert(
            instance_id=instance.id,
            db_type=instance.db_type,
            object_name=object_name,
            object_type=object_type,
            alert_type=alert_type,
            level=level,
            message=message,
            status="open",
            occurrence_count=1,
            first_seen=now,
            last_seen=now,
        )
        db.add(alert)
    else:
        alert.level = level
        alert.message = message
        alert.last_seen = now
        alert.occurrence_count = int(alert.occurrence_count or 0) + 1
    alert.metric_value = Decimal(str(metric_value)) if metric_value is not None else None
    alert.threshold_value = Decimal(str(threshold_value)) if threshold_value is not None else None
    # A breach younger than the sustained window is recorded but not published: the row keeps the
    # streak observable instead of turning one sample into a page.
    sustained_since = alert.first_seen or now
    if (now - sustained_since).total_seconds() < sustained_minutes * 60:
        return alert
    if alert.notified_at is None:
        alert.notified_at = now
    _mirror_capacity_alert(db, alert)
    return alert


def _resolve_capacity_alerts(
    db: Session,
    instance: DBInstance,
    *,
    alert_types: set[str] | None = None,
    active_keys: set[tuple[str, str, str]] | None = None,
    hold_keys: set[tuple[str, str, str]] | None = None,
    now: datetime | None = None,
) -> int:
    """Close alerts that are no longer breaching.

    `hold_keys` are keys that stopped breaching but are still inside the recovery margin: closing
    them there is what makes an alert flap, so they stay open for another round.
    """
    now = now or datetime.now()
    query = db.query(DBCapacityAlert).filter(
        DBCapacityAlert.instance_id == instance.id,
        DBCapacityAlert.status == "open",
    )
    if alert_types:
        query = query.filter(DBCapacityAlert.alert_type.in_(alert_types))
    resolved = 0
    for alert in query.all():
        key = (alert.object_name, alert.object_type, alert.alert_type)
        if active_keys is not None and key in active_keys:
            continue
        if hold_keys and key in hold_keys:
            continue
        alert.status = "closed"
        alert.resolved_at = now
        alert.last_seen = now
        _mirror_capacity_alert(db, alert)
        resolved += 1
    return resolved


def collect_snapshot(db: Session, instance: DBInstance) -> dict:
    policy = get_or_create_policy(db, instance.id)
    now = datetime.now()
    try:
        current = collect_current_capacity(instance)
    except Exception as exc:
        policy.last_run_time = now
        policy.next_run_time = _next_run_time(policy)
        policy.last_status = "error"
        policy.last_message = str(exc)[:1000]
        policy.consecutive_failures = int(policy.consecutive_failures or 0) + 1
        policy.last_sample_count = 0
        policy.data_freshness_status = "error"
        _upsert_capacity_alert(
            db,
            instance,
            object_name=instance.instance_name,
            object_type="instance",
            alert_type="collection_failed",
            level="critical",
            message=f"{instance.instance_name} 容量采集失败：{str(exc)[:350]}",
            now=now,
        )
        db.commit()
        raise
    if current.get("unsupported"):
        policy.last_run_time = now
        policy.next_run_time = _next_run_time(policy)
        policy.last_status = "skipped"
        policy.last_message = "; ".join(current.get("warnings") or [])[:1000]
        policy.last_sample_count = 0
        policy.data_freshness_status = "unsupported"
        db.commit()
        analysis = analyze_capacity(db, instance, days=30, policy=policy)
        return {"status": "skipped", "snapshot": current, "analysis": analysis, "alerts": [], "policy": policy_to_dict(policy)}

    if current.get("collection_status") == "no_data":
        policy.last_run_time = now
        policy.next_run_time = _next_run_time(policy)
        policy.last_status = "no_data"
        policy.last_message = "; ".join(current.get("warnings") or [])[:1000]
        policy.consecutive_failures = int(policy.consecutive_failures or 0) + 1
        policy.last_sample_count = 0
        policy.data_freshness_status = "missing"
        alert = _upsert_capacity_alert(
            db,
            instance,
            object_name=instance.instance_name,
            object_type="instance",
            alert_type="data_missing",
            level="warning",
            message=f"{instance.instance_name} 容量查询未返回数据，当前容量状态为未知。",
            now=now,
        )
        db.commit()
        analysis = analyze_capacity(db, instance, days=30, policy=policy)
        return {
            "status": "no_data",
            "snapshot": current,
            "analysis": analysis,
            "alerts": [_alert_to_dict(alert)],
            "policy": policy_to_dict(policy),
        }

    _resolve_capacity_alerts(
        db,
        instance,
        alert_types={"collection_failed", "data_missing"},
        now=now,
    )

    # Cache only database-resolved keys. Raw-string prefetches lose column collation
    # and can pick the wrong row when equivalent historical snapshots already exist.
    today = date.today()
    existing_snapshots: dict[tuple[object, object], DBObjectSizeDaily] = {}

    for row in current["objects"]:
        object_name = row.get("object_name") or row.get("tablespace_name")
        object_type = row.get("object_type") or ("tablespace" if instance.db_type == "oracle" else "schema")
        total_mb = Decimal(str(_to_float(row.get("total_mb"))))
        used_mb = Decimal(str(_to_float(row.get("used_mb"))))
        index_mb = Decimal(str(_to_float(row.get("index_mb"))))
        key = (object_name, object_type)
        snapshot = existing_snapshots.get(key)
        if snapshot is None:
            # Flush, but do not commit, so same-batch aliases work with autoflush off.
            db.flush()
            snapshot = db.query(DBObjectSizeDaily).filter(
                DBObjectSizeDaily.instance_id == instance.id,
                DBObjectSizeDaily.stat_date == today,
                DBObjectSizeDaily.object_name == object_name,
                DBObjectSizeDaily.object_type == object_type,
            ).order_by(DBObjectSizeDaily.id).first()
        if not snapshot:
            snapshot = DBObjectSizeDaily(
                instance_id=instance.id,
                db_type=instance.db_type,
                schema_name=object_name,
                object_name=object_name,
                object_type=object_type,
                row_count=None,
                stat_date=today,
            )
            db.add(snapshot)
        existing_snapshots[key] = snapshot
        snapshot.data_size_mb = used_mb
        snapshot.index_size_mb = index_mb
        snapshot.total_size_mb = total_mb
        snapshot.capacity_limit_known = 1 if row.get("capacity_limit_known", True) else 0
        snapshot.create_time = now

    policy.last_run_time = now
    policy.last_success_time = now
    policy.next_run_time = _next_run_time(policy)
    policy.consecutive_failures = 0
    policy.last_sample_count = len(current["objects"])
    policy.data_freshness_status = "fresh"
    retention_cutoff = now - timedelta(days=max(7, int(policy.retention_days or 180)))
    db.query(DBObjectSizeDaily).filter(
        DBObjectSizeDaily.instance_id == instance.id,
        DBObjectSizeDaily.create_time < retention_cutoff,
    ).delete(synchronize_session=False)
    db.commit()

    analysis = analyze_capacity(db, instance, days=30, policy=policy)
    alerts = _create_alerts_from_analysis(db, instance, analysis["items"], policy)
    policy.last_status = "success"
    policy.last_message = f"collected={len(current['objects'])}, alerts={len(alerts)}"
    db.commit()
    logger.info("容量快照完成 | instance=%s | objects=%s | alerts=%s", instance.instance_name, len(current["objects"]), len(alerts))
    return {"snapshot": current, "analysis": analysis, "alerts": alerts, "policy": policy_to_dict(policy)}


def run_collect_by_policy(db: Session, policy: DBCapacityPolicy) -> dict:
    now = datetime.now()
    instance = db.query(DBInstance).filter(DBInstance.id == policy.instance_id, DBInstance.enabled == 1).first()
    if not instance:
        policy.last_run_time = now
        policy.next_run_time = _next_run_time(policy)
        policy.last_status = "skipped"
        policy.last_message = "instance missing or disabled"
        db.commit()
        return {"status": "skipped", "message": policy.last_message}
    try:
        result = collect_snapshot(db, instance)
        if result.get("status") in {"skipped", "no_data"}:
            status = result["status"]
            return {
                "status": status,
                "message": result.get("policy", {}).get("last_message") or f"capacity {status}",
                **result,
            }
        alerts = result.get("alerts", [])
        return {"status": "success", "message": f"capacity collected, alerts={len(alerts)}", **result}
    except Exception as exc:
        db.rollback()
        policy = db.query(DBCapacityPolicy).filter(DBCapacityPolicy.id == policy.id).first()
        if policy:
            policy.last_run_time = now
            policy.next_run_time = _next_run_time(policy)
            policy.last_status = "error"
            policy.last_message = str(exc)[:1000]
            db.commit()
        return {"status": "error", "message": str(exc)}


def latest_summary(db: Session, instance: DBInstance) -> dict:
    latest_time = (
        db.query(func.max(DBObjectSizeDaily.create_time))
        .filter(DBObjectSizeDaily.instance_id == instance.id)
        .scalar()
    )
    if not latest_time:
        return collect_current_capacity(instance) | {"source": "live"}
    rows = (
        db.query(DBObjectSizeDaily)
        .filter(DBObjectSizeDaily.instance_id == instance.id, DBObjectSizeDaily.create_time == latest_time)
        .all()
    )
    total_mb = sum(_to_float(row.total_size_mb) for row in rows)
    used_mb = sum(_to_float(row.data_size_mb) for row in rows)
    capacity_limit_known = all(bool(row.capacity_limit_known) for row in rows)
    policy = get_or_create_policy(db, instance.id)
    age_minutes = max(0, int((datetime.now() - latest_time).total_seconds() // 60))
    stale_after_minutes = max(15, int(policy.frequency_minutes or 1440) * 2)
    freshness = "fresh" if age_minutes <= stale_after_minutes else "stale"
    if policy.data_freshness_status != freshness:
        policy.data_freshness_status = freshness
        db.commit()
    return {
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "source": "snapshot",
        "snapshot_time": latest_time.isoformat(sep=" "),
        "data_freshness_status": freshness,
        "data_age_minutes": age_minutes,
        "stale_after_minutes": stale_after_minutes,
        "capacity_limit_known": capacity_limit_known,
        "total_mb": round(total_mb, 2),
        "used_mb": round(used_mb, 2),
        "free_mb": round(total_mb - used_mb, 2) if capacity_limit_known else None,
        "used_pct": round(used_mb / total_mb * 100, 2) if total_mb and capacity_limit_known else None,
        "objects": [_snapshot_to_dict(row) for row in rows],
    }


def analyze_capacity(db: Session, instance: DBInstance, days: int = 30, policy: DBCapacityPolicy | None = None) -> dict:
    policy = policy or get_or_create_policy(db, instance.id)
    since = datetime.now() - timedelta(days=days)
    snapshots = (
        db.query(DBObjectSizeDaily)
        .filter(DBObjectSizeDaily.instance_id == instance.id, DBObjectSizeDaily.create_time >= since)
        .order_by(DBObjectSizeDaily.object_name.asc(), DBObjectSizeDaily.create_time.asc())
        .all()
    )
    grouped: dict[str, list[DBObjectSizeDaily]] = {}
    for row in snapshots:
        grouped.setdefault(row.object_name or row.schema_name or "-", []).append(row)

    items = []
    for name, rows in grouped.items():
        first = rows[0]
        latest = rows[-1]
        previous = rows[-2] if len(rows) >= 2 else None
        elapsed_days = max((latest.create_time - first.create_time).total_seconds() / 86400, 1 / 24)
        used_growth_mb = _to_float(latest.data_size_mb) - _to_float(first.data_size_mb)
        daily_growth_mb = used_growth_mb / elapsed_days
        interval_growth_mb = _to_float(latest.data_size_mb) - _to_float(previous.data_size_mb) if previous else 0
        base_used = _to_float(previous.data_size_mb) if previous else _to_float(first.data_size_mb)
        interval_growth_pct = interval_growth_mb / base_used * 100 if base_used > 0 else 0
        capacity_limit_known = bool(latest.capacity_limit_known)
        free_mb = max(_to_float(latest.total_size_mb) - _to_float(latest.data_size_mb), 0) if capacity_limit_known else None
        forecast_days = round(free_mb / daily_growth_mb, 1) if capacity_limit_known and daily_growth_mb > 0 else None

        level = "normal"
        reasons = []
        if interval_growth_mb >= _to_float(policy.abnormal_growth_mb) or interval_growth_pct >= _to_float(policy.abnormal_growth_pct):
            level = "warning"
            reasons.append("本次增长异常")
        if forecast_days is not None and forecast_days <= policy.forecast_days_threshold:
            level = "critical"
            reasons.append(f"预计 {forecast_days} 天后空间不足")

        items.append(
            {
                "object_name": name,
                "object_type": latest.object_type,
                "latest_time": latest.create_time.isoformat(sep=" "),
                "total_mb": round(_to_float(latest.total_size_mb), 2),
                "used_mb": round(_to_float(latest.data_size_mb), 2),
                "free_mb": round(free_mb, 2) if free_mb is not None else None,
                "used_pct": round(_to_float(latest.data_size_mb) / _to_float(latest.total_size_mb) * 100, 2) if capacity_limit_known and _to_float(latest.total_size_mb) else None,
                "capacity_limit_known": capacity_limit_known,
                "sample_count": len(rows),
                "interval_growth_mb": round(interval_growth_mb, 2),
                "interval_growth_pct": round(interval_growth_pct, 2),
                "daily_growth_mb": round(daily_growth_mb, 2),
                "forecast_days": forecast_days,
                "level": level,
                "reason": "；".join(reasons) if reasons else "容量增长正常",
            }
        )

    items.sort(key=lambda item: {"critical": 0, "warning": 1, "normal": 2}[item["level"]])
    return {
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "days": days,
        "policy": policy_to_dict(policy),
        "items": items,
        "summary": {
            "total": len(items),
            "warning": len([item for item in items if item["level"] == "warning"]),
            "critical": len([item for item in items if item["level"] == "critical"]),
        },
    }


def list_snapshots(db: Session, instance_id: int, days: int = 30) -> list[dict]:
    since = datetime.now() - timedelta(days=days)
    rows = (
        db.query(DBObjectSizeDaily)
        .filter(DBObjectSizeDaily.instance_id == instance_id, DBObjectSizeDaily.create_time >= since)
        .order_by(DBObjectSizeDaily.create_time.desc(), DBObjectSizeDaily.object_name.asc())
        .limit(500)
        .all()
    )
    return [_snapshot_to_dict(row) for row in rows]


def _release_expired_isolations(db: Session) -> None:
    now = datetime.now()
    expired = (
        db.query(DBCapacityAlert)
        .filter(DBCapacityAlert.status == "isolated", DBCapacityAlert.isolate_until.is_not(None), DBCapacityAlert.isolate_until <= now)
        .all()
    )
    if not expired:
        return
    for alert in expired:
        alert.status = "open"
        alert.isolate_until = None
        alert.isolate_reason = None
        alert.last_seen = now
        _mirror_capacity_alert(db, alert)
    db.commit()


def list_alerts(
    db: Session,
    instance_id: int | None = None,
    limit: int = 50,
    status: str = "open",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    allowed_instance_ids: frozenset[int] | set[int] | None = None,
) -> list[dict]:
    _release_expired_isolations(db)
    query = db.query(DBCapacityAlert)
    if allowed_instance_ids is not None:
        query = query.filter(DBCapacityAlert.instance_id.in_(allowed_instance_ids))
    if instance_id:
        query = query.filter(DBCapacityAlert.instance_id == instance_id)
    if status != "all":
        query = query.filter(DBCapacityAlert.status == status)
    if date_from:
        query = query.filter(DBCapacityAlert.create_time >= date_from)
    if date_to:
        query = query.filter(DBCapacityAlert.create_time <= date_to)
    rows = query.order_by(DBCapacityAlert.create_time.desc()).limit(limit).all()
    return [_alert_to_dict(row) for row in rows]


def close_alert(db: Session, alert_id: int) -> dict | None:
    alert = db.query(DBCapacityAlert).filter(DBCapacityAlert.id == alert_id).first()
    if not alert:
        return None
    alert.status = "closed"
    alert.resolved_at = datetime.now()
    alert.last_seen = alert.resolved_at
    alert.isolate_until = None
    alert.isolate_reason = None
    _mirror_capacity_alert(db, alert)
    db.commit()
    db.refresh(alert)
    return _alert_to_dict(alert)


def isolate_alert(db: Session, alert_id: int, days: int, reason: str | None = None) -> dict | None:
    alert = db.query(DBCapacityAlert).filter(DBCapacityAlert.id == alert_id).first()
    if not alert:
        return None
    alert.status = "isolated"
    alert.isolate_until = datetime.now() + timedelta(days=days)
    alert.isolate_reason = reason
    alert.last_seen = datetime.now()
    _mirror_capacity_alert(db, alert)
    db.commit()
    db.refresh(alert)
    return _alert_to_dict(alert)


def close_all_alerts(
    db: Session,
    instance_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    allowed_instance_ids: frozenset[int] | set[int] | None = None,
) -> int:
    _release_expired_isolations(db)
    query = db.query(DBCapacityAlert).filter(DBCapacityAlert.status == "open")
    if allowed_instance_ids is not None:
        query = query.filter(DBCapacityAlert.instance_id.in_(allowed_instance_ids))
    if instance_id:
        query = query.filter(DBCapacityAlert.instance_id == instance_id)
    if date_from:
        query = query.filter(DBCapacityAlert.create_time >= date_from)
    if date_to:
        query = query.filter(DBCapacityAlert.create_time <= date_to)
    rows = query.all()
    count = len(rows)
    now = datetime.now()
    for alert in rows:
        alert.status = "closed"
        alert.resolved_at = now
        alert.last_seen = now
        _mirror_capacity_alert(db, alert)
    db.commit()
    return count


def evaluate_data_freshness(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    stale = 0
    fresh = 0
    skipped = 0
    policies = db.query(DBCapacityPolicy).filter(DBCapacityPolicy.enabled == 1).all()
    instances = {
        row.id: row
        for row in db.query(DBInstance).filter(DBInstance.id.in_([p.instance_id for p in policies])).all()
    } if policies else {}
    for policy in policies:
        instance = instances.get(policy.instance_id)
        if not instance or not policy.last_run_time or policy.data_freshness_status == "unsupported":
            skipped += 1
            continue
        last_good = policy.last_success_time
        stale_after = timedelta(minutes=max(15, int(policy.frequency_minutes or 1440) * 2))
        is_stale = not last_good or now - last_good > stale_after
        if is_stale:
            policy.data_freshness_status = "stale"
            _upsert_capacity_alert(
                db,
                instance,
                object_name=instance.instance_name,
                object_type="instance",
                alert_type="data_stale",
                level="warning",
                message=(
                    f"{instance.instance_name} 容量数据已过期；"
                    f"最后成功采集：{last_good.isoformat(sep=' ') if last_good else '从未成功'}。"
                ),
                metric_value=int((now - last_good).total_seconds() // 60) if last_good else None,
                threshold_value=int(stale_after.total_seconds() // 60),
                now=now,
            )
            stale += 1
        else:
            policy.data_freshness_status = "fresh"
            _resolve_capacity_alerts(
                db,
                instance,
                alert_types={"data_stale"},
                now=now,
            )
            fresh += 1
    db.commit()
    return {"stale": stale, "fresh": fresh, "skipped": skipped, "total": len(policies)}


def get_alert_stats(
    db: Session,
    instance_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    allowed_instance_ids: frozenset[int] | set[int] | None = None,
) -> dict:
    _release_expired_isolations(db)
    base = db.query(DBCapacityAlert)
    if allowed_instance_ids is not None:
        base = base.filter(DBCapacityAlert.instance_id.in_(allowed_instance_ids))
    if instance_id:
        base = base.filter(DBCapacityAlert.instance_id == instance_id)
    if date_from:
        base = base.filter(DBCapacityAlert.create_time >= date_from)
    if date_to:
        base = base.filter(DBCapacityAlert.create_time <= date_to)
    total = base.count()
    open_count = base.filter(DBCapacityAlert.status == "open").count()
    critical_count = base.filter(DBCapacityAlert.status == "open", DBCapacityAlert.level == "critical").count()
    warning_count = base.filter(DBCapacityAlert.status == "open", DBCapacityAlert.level == "warning").count()
    closed_count = base.filter(DBCapacityAlert.status == "closed").count()
    isolated_count = base.filter(DBCapacityAlert.status == "isolated").count()
    return {
        "total": total,
        "open": open_count,
        "critical": critical_count,
        "warning": warning_count,
        "closed": closed_count,
        "isolated": isolated_count,
    }


def _breach_profile(item: dict, policy: DBCapacityPolicy) -> tuple[str, Any, Any]:
    """(alert_type, metric, threshold) for one analysed object, breaching or not."""
    alert_type = "forecast_shortage" if item.get("forecast_days") is not None and item["forecast_days"] <= policy.forecast_days_threshold else "abnormal_growth"
    threshold = policy.forecast_days_threshold if alert_type == "forecast_shortage" else policy.abnormal_growth_mb
    metric = item.get("forecast_days") if alert_type == "forecast_shortage" else item.get("interval_growth_mb")
    return alert_type, metric, threshold


def _hold_keys(db: Session, instance: DBInstance, items: list[dict], policy: DBCapacityPolicy) -> set[tuple[str, str, str]]:
    """Open alerts whose object stopped breaching but sits inside the recovery margin.

    Closing these is the flapping mechanism itself: a metric that hovers at the threshold would
    open, close, open, close on every run. They stay open until they are clearly back to normal.

    The alert type comes from the open alert, not from re-deriving it: a normal sample is by
    definition past the threshold, so the breach formula would classify it as the *other* kind
    and the key would never match the alert it is supposed to hold.
    """
    margin = _to_float(policy.recovery_margin_pct) or 0.0
    if margin <= 0:
        return set()
    open_alerts = db.query(DBCapacityAlert).filter(
        DBCapacityAlert.instance_id == instance.id,
        DBCapacityAlert.status == "open",
        DBCapacityAlert.alert_type.in_({"forecast_shortage", "abnormal_growth"}),
    ).all()
    if not open_alerts:
        return set()
    by_object = {(item["object_name"], item.get("object_type") or "tablespace"): item for item in items}
    keys: set[tuple[str, str, str]] = set()
    for alert in open_alerts:
        item = by_object.get((alert.object_name, alert.object_type))
        if not item or item.get("level") != "normal":
            continue
        if alert.alert_type == "forecast_shortage":
            metric = item.get("forecast_days")
            threshold = policy.forecast_days_threshold
            within = metric is not None and threshold is not None and _to_float(metric) <= _to_float(threshold) * (1 + margin / 100)
        else:
            metric = item.get("interval_growth_mb")
            threshold = policy.abnormal_growth_mb
            within = metric is not None and threshold is not None and _to_float(metric) >= _to_float(threshold) * (1 - margin / 100)
        if within:
            keys.add((alert.object_name, alert.object_type, alert.alert_type))
    return keys


def _create_alerts_from_analysis(db: Session, instance: DBInstance, items: list[dict], policy: DBCapacityPolicy) -> list[dict]:
    _release_expired_isolations(db)
    active: list[DBCapacityAlert] = []
    active_keys: set[tuple[str, str, str]] = set()
    now = datetime.now()
    for item in items:
        if item["level"] == "normal":
            continue
        alert_type, metric, threshold = _breach_profile(item, policy)
        object_type = item.get("object_type") or "tablespace"
        alert = _upsert_capacity_alert(
            db,
            instance,
            object_name=item["object_name"],
            object_type=object_type,
            alert_type=alert_type,
            level=item["level"],
            message=f"{instance.instance_name}/{item['object_name']}：{item['reason']}",
            metric_value=metric,
            threshold_value=threshold,
            now=now,
            sustained_minutes=policy.sustained_minutes,
        )
        active.append(alert)
        active_keys.add((item["object_name"], object_type, alert_type))
    _resolve_capacity_alerts(
        db,
        instance,
        alert_types={"forecast_shortage", "abnormal_growth"},
        active_keys=active_keys,
        hold_keys=_hold_keys(db, instance, items, policy),
        now=now,
    )
    db.flush()
    return [_alert_to_dict(alert) for alert in active]


def _snapshot_to_dict(row: DBObjectSizeDaily) -> dict:
    used_mb = _to_float(row.data_size_mb)
    total_mb = _to_float(row.total_size_mb)
    capacity_limit_known = bool(row.capacity_limit_known)
    free_mb = round(total_mb - used_mb, 2) if capacity_limit_known else None
    used_pct = round(used_mb / total_mb * 100, 2) if capacity_limit_known and total_mb > 0 else None
    ct = row.create_time
    if ct and ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return {
        "id": row.id,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "object_name": row.object_name,
        "object_type": row.object_type,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "total_mb": total_mb,
        "used_pct": used_pct,
        "capacity_limit_known": capacity_limit_known,
        "stat_date": row.stat_date.isoformat() if row.stat_date else None,
        "create_time": ct.strftime("%Y-%m-%d %H:%M:%S") if ct else None,
    }


def _alert_to_dict(row: DBCapacityAlert) -> dict:
    ct = row.create_time
    if ct and ct.tzinfo is None:
        ct = ct.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return {
        "id": row.id,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "object_name": row.object_name,
        "object_type": row.object_type,
        "alert_type": row.alert_type,
        "level": row.level,
        "message": row.message,
        "metric_value": _to_float(row.metric_value) if row.metric_value is not None else None,
        "threshold_value": _to_float(row.threshold_value) if row.threshold_value is not None else None,
        "status": row.status,
        # Empty while the breach is still younger than the sustained window: the alert exists but
        # has not been published, so callers must not treat it as a page.
        "notified_at": row.notified_at.strftime("%Y-%m-%d %H:%M:%S") if row.notified_at else None,
        "isolate_until": row.isolate_until.strftime("%Y-%m-%d %H:%M:%S") if row.isolate_until else None,
        "isolate_reason": row.isolate_reason,
        "occurrence_count": row.occurrence_count or 1,
        "first_seen": row.first_seen.strftime("%Y-%m-%d %H:%M:%S") if row.first_seen else None,
        "last_seen": row.last_seen.strftime("%Y-%m-%d %H:%M:%S") if row.last_seen else None,
        "resolved_at": row.resolved_at.strftime("%Y-%m-%d %H:%M:%S") if row.resolved_at else None,
        "create_time": ct.strftime("%Y-%m-%d %H:%M:%S") if ct else None,
    }
