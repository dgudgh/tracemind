"""性能基线 — 采集关键指标时序快照，计算当前 vs 日/周/月基线偏离。"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.db_instance import DBInstance
from app.models.perf_metric import DBPerfMetric
from app.models.perf_metric_policy import DBPerfMetricPolicy

logger = logging.getLogger("app.services.perf_baseline")


def ensure_policies(db: Session) -> int:
    """为所有启用实例补默认基线采集策略（首次迁移用）。返回新增条数。"""
    existing = {p.instance_id for p in db.query(DBPerfMetricPolicy).all()}
    added = 0
    for inst in db.query(DBInstance).filter(DBInstance.enabled == 1).all():
        if inst.id in existing:
            continue
        db.add(DBPerfMetricPolicy(instance_id=inst.id, enabled=1, interval_minutes=30))
        added += 1
    if added:
        db.commit()
    return added


def run_collect_by_policy(db: Session, policy: DBPerfMetricPolicy) -> dict:
    """按策略执行一次基线采集并回填 last/next/status。"""
    instance = db.query(DBInstance).filter(DBInstance.id == policy.instance_id, DBInstance.enabled == 1).first()
    now = datetime.now()
    if not instance:
        result = {"status": "skipped", "message": "instance missing or disabled"}
    else:
        try:
            r = collect_perf_metrics(db, instance)
            cnt = r.get("count", 0)
            errors = r.get("errors") or []
            result = {
                "status": ("partial" if cnt else "error") if errors else "success" if cnt else "warning",
                "message": (f"已采集 {cnt} 项指标" if cnt else "未采集到指标")
                           + ("；" + errors[0] if errors else ""),
            }
        except Exception as e:
            result = {"status": "error", "message": str(e)}
            db.rollback()
    policy.last_run_time = now
    policy.next_run_time = now + timedelta(minutes=policy.interval_minutes or 30)
    policy.last_status = result.get("status")
    policy.last_message = result.get("message")
    db.commit()
    return result


# 指标显示名
METRIC_LABELS = {
    "cpu_util": "CPU利用率%", "aas": "平均活跃会话(AAS)", "tps": "TPS(事务/秒)",
    "qps": "QPS(执行/秒)", "iops": "IOPS(IO请求/秒)", "redo_mb_s": "Redo(MB/秒)",
    "undo_blocks": "Undo块/秒", "cache_hit_pct": "缓存命中率%",
    "temp_mb_s": "临时数据(MB/秒)", "deadlocks": "死锁累计数",
    "active_sessions": "活跃会话数", "connections": "连接数",
}

# Oracle v$sysmetric 指标名 → 内部名
_ORA_SYSMETRIC = {
    "Host CPU Utilization (%)": "cpu_util",
    "Average Active Sessions": "aas",
    "User Transaction Per Sec": "tps",
    "Executions Per Sec": "qps",
    "I/O Requests per Second": "iops",
    "Physical IO Requests Per Sec": "iops",
    "Redo Generated Per Sec": "redo_mb_s",  # bytes/s → 转 MB
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _collect_oracle(instance) -> dict:
    from app.collectors.oracle.oracle_connection import oracle_connection
    out = {}
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT metric_name, value FROM v$sysmetric "
            "WHERE group_id = 2 AND metric_name IN ("
            "'Host CPU Utilization (%)','Average Active Sessions','User Transaction Per Sec',"
            "'Executions Per Sec','I/O Requests per Second','Physical IO Requests Per Sec',"
            "'Redo Generated Per Sec')"
        )
        for name, value in cur.fetchall():
            key = _ORA_SYSMETRIC.get(name)
            if not key:
                continue
            val = _f(value)
            if val is None:
                continue
            if key == "redo_mb_s":
                val = val / 1024 / 1024
            out[key] = round(val, 4)
    return out


def _collect_mysql(instance) -> dict:
    from app.collectors.mysql.mysql_connection import mysql_connection
    out = {}
    with mysql_connection(instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                        "('Uptime','Questions','Com_commit','Com_rollback','Threads_running','Innodb_data_reads','Innodb_data_writes')")
            st = {r["variable_name"].lower(): _f(r["value"]) for r in cur.fetchall()}
    uptime = st.get("uptime") or 1
    out["qps"] = round((st.get("questions") or 0) / uptime, 4)
    out["tps"] = round(((st.get("com_commit") or 0) + (st.get("com_rollback") or 0)) / uptime, 4)
    out["aas"] = round(st.get("threads_running") or 0, 4)
    out["iops"] = round(((st.get("innodb_data_reads") or 0) + (st.get("innodb_data_writes") or 0)) / uptime, 4)
    return out


def _collect_postgresql(instance) -> dict:
    from app.collectors.postgresql.postgresql_connection import postgresql_connection

    with postgresql_connection(instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM now() - pg_postmaster_start_time()) AS uptime_seconds,
                       numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
                       tup_returned, tup_fetched, temp_bytes, deadlocks
                FROM pg_stat_database
                WHERE datname = current_database()
            """)
            row = cur.fetchone() or {}
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE state = 'active' AND pid <> pg_backend_pid()) AS active_sessions
                FROM pg_stat_activity
            """)
            activity = cur.fetchone() or {}
            statement_calls = None
            try:
                cur.execute("SELECT SUM(calls) AS calls FROM pg_stat_statements")
                statement_calls = _f((cur.fetchone() or {}).get("calls"))
            except Exception:
                conn.rollback()

    row = {str(key).lower(): value for key, value in dict(row).items()}
    activity = {str(key).lower(): value for key, value in dict(activity).items()}
    uptime = max(_f(row.get("uptime_seconds")) or 1, 1)
    hits = _f(row.get("blks_hit")) or 0
    reads = _f(row.get("blks_read")) or 0
    out = {
        "aas": round(_f(activity.get("active_sessions")) or 0, 4),
        "active_sessions": round(_f(activity.get("active_sessions")) or 0, 4),
        "connections": round(_f(row.get("numbackends")) or 0, 4),
        "tps": round(((row.get("xact_commit") or 0) + (row.get("xact_rollback") or 0)) / uptime, 4),
        "iops": round(reads / uptime, 4),
        "cache_hit_pct": round(hits / (hits + reads) * 100, 4) if hits + reads else 100.0,
        "temp_mb_s": round((_f(row.get("temp_bytes")) or 0) / 1024 / 1024 / uptime, 4),
        "deadlocks": round(_f(row.get("deadlocks")) or 0, 4),
    }
    if statement_calls is not None:
        out["qps"] = round(statement_calls / uptime, 4)
    else:
        returned = (_f(row.get("tup_returned")) or 0) + (_f(row.get("tup_fetched")) or 0)
        out["qps"] = round(returned / uptime, 4)
    return out


def _collect_sqlserver(instance) -> dict:
    from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection

    with sqlserver_connection(instance, read_only=True) as conn:
        with conn.cursor(as_dict=True) as cur:
            cur.execute("""
                SELECT DATEDIFF(SECOND, sqlserver_start_time, SYSDATETIME()) AS uptime_seconds
                FROM sys.dm_os_sys_info
            """)
            uptime_row = cur.fetchone() or {}
            cur.execute("""
                SELECT COUNT(*) AS connections,
                       SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS active_sessions
                FROM sys.dm_exec_sessions WHERE is_user_process = 1
            """)
            sessions = cur.fetchone() or {}
            cur.execute("""
                SELECT counter_name, SUM(cntr_value) AS cntr_value
                FROM sys.dm_os_performance_counters
                WHERE (object_name LIKE '%SQL Statistics%' AND counter_name IN ('Batch Requests/sec', 'SQL Compilations/sec'))
                   OR (object_name LIKE '%Databases%' AND instance_name = DB_NAME()
                       AND counter_name IN ('Transactions/sec', 'Log Bytes Flushed/sec'))
                   OR (object_name LIKE '%Buffer Manager%' AND counter_name IN ('Page reads/sec', 'Page writes/sec', 'Buffer cache hit ratio', 'Buffer cache hit ratio base'))
                GROUP BY counter_name
            """)
            counters = {str(row.get("counter_name") or "").lower(): _f(row.get("cntr_value")) or 0 for row in cur.fetchall()}

    uptime = max(_f(uptime_row.get("uptime_seconds")) or 1, 1)
    ratio = counters.get("buffer cache hit ratio", 0)
    ratio_base = counters.get("buffer cache hit ratio base", 0)
    return {
        "connections": round(_f(sessions.get("connections")) or 0, 4),
        "active_sessions": round(_f(sessions.get("active_sessions")) or 0, 4),
        "aas": round(_f(sessions.get("active_sessions")) or 0, 4),
        "qps": round(counters.get("batch requests/sec", 0) / uptime, 4),
        "tps": round(counters.get("transactions/sec", 0) / uptime, 4),
        "iops": round((counters.get("page reads/sec", 0) + counters.get("page writes/sec", 0)) / uptime, 4),
        "redo_mb_s": round(counters.get("log bytes flushed/sec", 0) / 1024 / 1024 / uptime, 4),
        "cache_hit_pct": round(ratio / ratio_base * 100, 4) if ratio_base else 0,
    }


def collect_perf_metrics(db: Session, instance: DBInstance) -> dict:
    metrics = {}
    errors = []
    warnings = []
    db_type = str(instance.db_type or "").lower()
    try:
        if db_type == "oracle":
            metrics = _collect_oracle(instance)
        elif db_type in {"mysql", "mariadb"}:
            metrics = _collect_mysql(instance)
        elif db_type in {"postgres", "postgresql", "pg"}:
            metrics = _collect_postgresql(instance)
        elif db_type in {"sqlserver", "sql_server", "mssql"}:
            metrics = _collect_sqlserver(instance)
        else:
            warnings.append(f"{instance.db_type} 性能基线实时采集暂未接入，已跳过实时连接。")
    except Exception as e:
        errors.append(str(e))
        logger.warning("perf metric collect failed for %s: %s", instance.id, e)

    now = datetime.now()
    for name, value in metrics.items():
        db.add(DBPerfMetric(instance_id=instance.id, db_type=instance.db_type,
                            metric_name=name, value=value, capture_time=now))
    db.commit()
    return {"count": len(metrics), "metrics": metrics, "errors": errors, "warnings": warnings}


def compute_baseline(db: Session, instance_id: int) -> dict:
    now = datetime.now()
    rows = (db.query(DBPerfMetric)
            .filter(DBPerfMetric.instance_id == instance_id,
                    DBPerfMetric.capture_time >= now - timedelta(days=30))
            .all())
    # 按指标归集
    by_metric: dict[str, list] = {}
    for r in rows:
        by_metric.setdefault(r.metric_name, []).append(r)

    items = []
    for name, recs in by_metric.items():
        recs.sort(key=lambda x: x.capture_time)
        current = float(recs[-1].value) if recs[-1].value is not None else None
        day_vals = [float(r.value) for r in recs if r.value is not None and r.capture_time >= now - timedelta(days=1)]
        week_vals = [float(r.value) for r in recs if r.value is not None and r.capture_time >= now - timedelta(days=7)]
        month_vals = [float(r.value) for r in recs if r.value is not None]
        avg = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
        day_avg, week_avg, month_avg = avg(day_vals), avg(week_vals), avg(month_vals)
        base = week_avg if week_avg else month_avg
        dev = round((current - base) / base * 100, 1) if (current is not None and base) else None
        items.append({
            "metric": name, "label": METRIC_LABELS.get(name, name),
            "current": current, "day_avg": day_avg, "week_avg": week_avg, "month_avg": month_avg,
            "deviation_pct": dev, "samples": len(month_vals),
        })
    items.sort(key=lambda x: x["metric"])
    return {"items": items, "total_samples": len(rows)}
