import logging
from datetime import datetime
from decimal import Decimal, DecimalException

from sqlalchemy.orm import Session
from app.collectors.oracle.sql_text import validate_stored_sql_text

from app.models.slow_sql import DBSlowSQL
from app.services import slow_sql_analysis, system_settings

logger = logging.getLogger("app.services.slow_sql")

# Counters remain JSON numbers for existing clients; do not silently round in JS.
MAX_EXACT_COUNTER = 2**53 - 1


def get_list(
    db: Session,
    instance_id: int | None = None,
    db_type: str | None = None,
    risk_level: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    query = db.query(DBSlowSQL)
    if instance_id:
        query = query.filter(DBSlowSQL.instance_id == instance_id)
    if db_type:
        query = query.filter(DBSlowSQL.db_type == db_type)
    if risk_level:
        query = query.filter(DBSlowSQL.risk_level == risk_level)
    total = query.count()
    items = (
        query.order_by(DBSlowSQL.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def get_by_id(db: Session, slow_sql_id: int):
    return db.query(DBSlowSQL).filter(DBSlowSQL.id == slow_sql_id).first()


def analyze(db: Session, obj: DBSlowSQL):
    from app.schemas.slow_sql import SlowSQLResponse

    # Generic guidance is not a model result and must not overwrite prior evidence.
    return {
        **SlowSQLResponse.model_validate(obj).model_dump(),
        "status": "not_analyzed", "analysis_status": "not_run", "analysis_kind": "generic_guidance",
        "persisted": False,
        "message": "仅提供通用排查建议，未执行 AI 或执行计划分析。",
        "guidance": "检查 WHERE/JOIN 字段索引、返回行数、排序/分组开销，并结合真实执行计划继续分析。",
    }


class SlowSQLCollectionError(RuntimeError):
    pass


def _number(value, name, *, integer=False, scale=None):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError(name)
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError(name)
        if scale is not None:
            number *= scale
        if integer and (number != number.to_integral_value() or number > MAX_EXACT_COUNTER):
            raise ValueError(name)
        if not integer and number > Decimal("9999999999999999.99"):
            raise ValueError(name)
        return int(number) if integer else number
    except (DecimalException, ValueError, TypeError):
        bound = f"; expected an integer from 0 to {MAX_EXACT_COUNTER}" if integer else ""
        raise SlowSQLCollectionError(f"Invalid or unrepresentable {name}{bound}; collection was not saved") from None


def collect(db: Session, instance, *, hours=1) -> dict:
    """Read first, validate the complete batch, then append atomically. Never prune history here."""
    if instance.db_type not in {"oracle", "mysql"}:
        raise ValueError(f"Unsupported database type: {instance.db_type}")
    instance_id = instance.id
    try:
        threshold = system_settings.get_int(db, "slow_sql_threshold_ms", 3000)
        critical = system_settings.get_int(db, "slow_sql_critical_ms", 5000)
        if threshold < 0 or critical < 0:
            raise SlowSQLCollectionError("Slow SQL thresholds must be nonnegative")
        if instance.db_type == "oracle":
            # Use the realtime rows contract, not the removed health metric key top_sql.
            snapshot = slow_sql_analysis.get_slow_sql_list(instance, "top_avg", hours=hours)
            if not isinstance(snapshot, dict) or snapshot.get("execution_status") != "success":
                raise SlowSQLCollectionError("Oracle slow SQL query failed; history was preserved")
            rows = snapshot.get("rows")
            metadata = snapshot.get("metadata", {})
        else:
            from app.collectors.mysql.mysql_connection import mysql_connection
            from app.collectors.mysql.mysql_health_collector import MYSQL_QUERIES
            from app.core.database_options import redact_database_error

            try:
                with mysql_connection(instance, read_timeout=10) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(MYSQL_QUERIES["slow_sql"])
                        rows = cursor.fetchall()
            except Exception as exc:
                raise SlowSQLCollectionError(
                    f"MySQL slow SQL query failed: {redact_database_error(exc)}"
                ) from exc
            metadata = {"source": "mysql.performance_schema", "statistics_scope": "digest_cumulative",
                        "window_aggregation": False, "hours_applied": False, "analysis_status": "not_run"}
        if not isinstance(rows, list) or any(not isinstance(row, dict) or "error" in row for row in rows):
            raise SlowSQLCollectionError("Slow SQL query returned invalid or failed rows")

        captured_at = datetime.now()
        pending = []
        evidence = []
        for row in rows:
            oracle = instance.db_type == "oracle"
            sql_id = row.get("sql_id" if oracle else "digest")
            if not isinstance(sql_id, str) or not sql_id or len(sql_id) > 100:
                raise SlowSQLCollectionError("Slow SQL row has no valid SQL identity")
            executions = _number(row.get("executions" if oracle else "count_star"), "executions", integer=True)
            if oracle:
                total_ms = _number(row.get("elapsed_time_us"), "total_elapsed_ms", scale=Decimal("0.001"))
                avg_ms = total_ms / executions if executions else Decimal(0)
            else:
                total_ms = _number(row.get("total_seconds"), "total_seconds") * 1000
                avg_ms = _number(row.get("avg_seconds"), "avg_seconds") * 1000
            _number(total_ms, "total_elapsed_ms")
            _number(avg_ms, "avg_elapsed_ms")
            if executions == 0 or avg_ms < threshold:
                continue
            try:
                sql_text = validate_stored_sql_text(row.get("sql_text" if oracle else "digest_text"))
            except ValueError as exc:
                raise SlowSQLCollectionError(str(exc)) from exc
            values = {
                "instance_id": instance.id, "db_type": instance.db_type, "sql_id": sql_id,
                "schema_name": row.get("schema_name"), "sql_text": sql_text,
                "executions": executions, "avg_elapsed_ms": avg_ms, "total_elapsed_ms": total_ms,
                "risk_level": "critical" if avg_ms > critical else "warning", "capture_time": captured_at,
                "create_time": captured_at,
            }
            for target, source in (("logical_reads", "buffer_gets"), ("physical_reads", "disk_reads"),
                                   ("rows_processed", "rows_processed" if oracle else "sum_rows_examined")):
                value = row.get(source)
                values[target] = _number(value, target, integer=True) if value is not None else None
            pending.append(DBSlowSQL(**values))
            evidence.append({"sql_id": sql_id, "child_number": row.get("child_number"), "con_id": row.get("con_id"),
                             "avg_elapsed_ms": str(avg_ms), "total_elapsed_ms": str(total_ms)})
        db.add_all(pending)
        db.flush()
        records = [{"id": record.id, **item} for record, item in zip(pending, evidence)]
        db.commit()
    except Exception:
        db.rollback()
        raise

    count = len(records)
    return {
        "ok": True, "status": "success" if count else "no_data", "execution_status": "success",
        "analysis_status": "not_run", "plan_status": "not_requested", "persisted": bool(count),
        "instance_id": instance_id, "count": count, "read_count": len(rows), "matched_count": count,
        "saved_count": count, "threshold_ms": threshold, "threshold_operator": ">=",
        "history_preserved": True, "deleted_count": 0, "capture_time": captured_at.isoformat(),
        "record_ids": [record["id"] for record in records], "records": records, "source_metadata": metadata,
        "persistence_scope": "legacy_metric_snapshot",
        "persistence_limitations": ["Child/container identity is returned in records but has no dedicated history columns.",
                                    "No analysis report or time-window delta was generated."],
        "message": f"读取 {len(rows)} 行，达到阈值 >= {threshold}ms 的 {count} 行，保存 {count} 行；历史保留，未执行 AI 或执行计划分析。",
    }
