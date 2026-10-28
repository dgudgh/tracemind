import logging
import re
from time import monotonic
from datetime import datetime, timezone

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.oracle.sql_text import check_sql_deadline, materialize_sql_rows, read_sql_text
from app.collectors.utils import rows_to_dicts, safe_rows
from app.core.database_options import redact_database_error
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.slow_sql_analysis")

ORACLE_QUERY_FAILURE_WARNINGS = [
    "请确认实例监听、网络和账号权限正常。",
    "慢 SQL 实时查询需要访问 v$sql、v$session 等动态性能视图。",
]

SQL_STATS_COLUMNS = (
    "sql_id, child_number, con_id, plan_hash_value, parsing_schema_name AS schema_name, "
    "last_active_time, first_load_time, last_load_time, executions, "
    "elapsed_time AS elapsed_time_us, cpu_time AS cpu_time_us, "
    "elapsed_time / 1000000 AS elapsed_seconds, cpu_time / 1000000 AS cpu_seconds, "
    "ROUND(elapsed_time / 1000000 / DECODE(executions, 0, 1, executions), 2) AS avg_elapsed_seconds, "
    "ROUND(cpu_time / 1000000 / DECODE(executions, 0, 1, executions), 2) AS avg_cpu_seconds, "
    "buffer_gets, disk_reads, rows_processed, "
    "ROUND(buffer_gets / DECODE(executions, 0, 1, executions), 2) AS avg_buffer_gets, "
    "ROUND(disk_reads / DECODE(executions, 0, 1, executions), 2) AS avg_disk_reads, "
    "sql_fulltext AS sql_text "
)


def _top_sql_query(order_by, *, executed_only=False):
    predicate = "executions > 0 AND " if executed_only else ""
    return (f"SELECT * FROM (SELECT {SQL_STATS_COLUMNS} FROM v$sql WHERE {predicate}"
            f"last_active_time >= SYSDATE - :hours / 24 ORDER BY {order_by}, sql_id, child_number) "
            "WHERE ROWNUM <= 51")

QUERIES = {
    "active": {
        "title": "当前正在执行的SQL",
        "sql": (
            "SELECT s.sid, s.serial#, s.serial# AS serial_num, s.con_id, s.sql_child_number AS child_number, "
            "s.username, s.status, s.machine, s.program, s.module, "
            "  s.sql_id, s.event, s.wait_class, s.seconds_in_wait, "
            "  s.last_call_et AS running_seconds, "
            "  q.sql_fulltext AS sql_text "
            "FROM v$session s "
            "LEFT JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number AND s.con_id = q.con_id "
            "WHERE s.status = 'ACTIVE' AND s.username IS NOT NULL AND s.sql_id IS NOT NULL "
            "ORDER BY s.last_call_et DESC"
        ),
    },
    "top_elapsed": {
        "title": "执行时间最长的SQL",
        "sql": _top_sql_query("elapsed_time DESC", executed_only=True),
    },
    "top_avg": {
        "title": "平均执行最慢的SQL",
        "sql": _top_sql_query("elapsed_time / executions DESC", executed_only=True),
    },
    "top_buffer": {
        "title": "逻辑读最高的SQL",
        "sql": _top_sql_query("buffer_gets DESC"),
    },
    "top_disk": {
        "title": "物理读最高的SQL",
        "sql": _top_sql_query("disk_reads DESC"),
    },
    "waiting": {
        "title": "正在等待的SQL",
        "sql": (
            "SELECT s.sid, s.serial#, s.serial# AS serial_num, s.con_id, s.sql_child_number AS child_number, "
            "s.username, s.sql_id, s.event, s.wait_class, "
            "  s.seconds_in_wait, s.blocking_session, "
            "  q.sql_fulltext AS sql_text "
            "FROM v$session s "
            "LEFT JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number AND s.con_id = q.con_id "
            "WHERE s.username IS NOT NULL AND s.wait_class <> 'Idle' "
            "ORDER BY s.seconds_in_wait DESC"
        ),
    },
}


def get_slow_sql_list(instance: DBInstance, category: str = "top_avg", hours: int = 1) -> dict:
    if category not in QUERIES or type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError("category must be supported and hours must be between 1 and 168")
    logger.info("慢SQL实时查询 | instance=%s | category=%s | hours=%s", instance.instance_name, category, hours)
    query_info = QUERIES[category]
    session_query = category in {"active", "waiting"}
    metadata = {
        "instance_id": instance.id, "source": "oracle.v$session" if session_query else "oracle.v$sql",
        "read_only": True, "persisted": False, "analysis_status": "not_run", "plan_status": "not_requested",
        "requested_hours": hours, "hours_applied": not session_query,
        "time_scope": "current_sessions" if session_query else "last_active_filter",
        "statistics_scope": "current_session" if session_query else "cursor_lifetime_cumulative",
        "window_aggregation": False, "limit": 50,
        "sql_text_source": "v$sql.sql_fulltext",
        "row_identity": ["instance_id", "con_id", "sid", "serial_num"] if session_query
                        else ["instance_id", "con_id", "sql_id", "child_number"],
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with oracle_connection(instance) as conn:
            conn.call_timeout = 10000
            deadline = monotonic() + 30
            sql = query_info["sql"]
            if session_query:
                sql = "SELECT * FROM (" + sql + ") WHERE ROWNUM <= 51"
            major_version = int(conn.version.split(".", 1)[0])
            if major_version < 12:
                sql = sql.replace("s.con_id,", "0 AS con_id,").replace("child_number, con_id,", "child_number, 0 AS con_id,")
                sql = sql.replace(" AND s.con_id = q.con_id", "")
            with conn.cursor() as cursor:
                container_sql = ("SYS_CONTEXT('USERENV','CON_NAME') AS container_name, "
                                 "SYS_CONTEXT('USERENV','CON_ID') AS container_id, ") if major_version >= 12 else "'NON-CDB' AS container_name, '0' AS container_id, "
                check_sql_deadline(conn, deadline)
                cursor.execute("SELECT " + container_sql + "SYS_CONTEXT('USERENV','INSTANCE_NAME') AS instance_name, SYSDATE AS database_time FROM dual")
                check_sql_deadline(conn, deadline)
                context = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                check_sql_deadline(conn, deadline)
                if len(context) != 1:
                    raise RuntimeError("Oracle context query did not return exactly one row")
                metadata.update(context[0], oracle_version=conn.version)
                cursor.execute(sql, {} if session_query else {"hours": hours})
                check_sql_deadline(conn, deadline)
                fetched = rows_to_dicts(cursor, cursor.fetchall())
                check_sql_deadline(conn, deadline)
                metadata["truncated"] = len(fetched) > 50
                rows = safe_rows(materialize_sql_rows(fetched[:50], connection=conn, deadline=deadline))
            for row in rows:
                row["instance_id"] = instance.id
            metadata["distinct_sql_ids"] = len({row.get("sql_id") for row in rows if row.get("sql_id")})
    except Exception as exc:  # noqa: BLE001 - keep the UI/API alive when a monitored database is offline.
        logger.warning(
            "慢SQL实时查询失败 | instance=%s | category=%s | error=%s",
            instance.instance_name,
            category,
            redact_database_error(exc),
        )
        return {
            "category": category,
            "title": query_info["title"],
            "rows": [],
            "count": 0,
            "status": "degraded",
            "execution_status": "failed",
            "metadata": metadata,
            "message": f"实时慢 SQL 查询失败：{redact_database_error(exc)}",
            "warnings": ORACLE_QUERY_FAILURE_WARNINGS,
        }

    return {
        "category": category,
        "title": query_info["title"],
        "rows": rows,
        "count": len(rows),
        "status": "success",
        "execution_status": "success",
        "metadata": metadata,
    }


def get_sql_detail(instance: DBInstance, sql_id: str, *, child_number=None, con_id=None, plan_hash_value=None) -> dict:
    logger.info("SQL详情 | instance=%s | sql_id=%s", instance.instance_name, sql_id)
    try:
        with oracle_connection(instance) as conn:
            conn.call_timeout = 10000
            modern = int(conn.version.split(".", 1)[0]) >= 12
            predicates, binds = ["sql_id = :sql_id"], {"sql_id": sql_id}
            for name, value in (("child_number", child_number), ("con_id", con_id), ("plan_hash_value", plan_hash_value)):
                if value is not None:
                    if type(value) is not int or value < 0:
                        raise ValueError(f"Invalid {name}")
                    if name == "con_id" and not modern:
                        if value != 0:
                            raise ValueError("Non-CDB Oracle has no requested container")
                        continue
                    predicates.append(f"{name} = :{name}")
                    binds[name] = value
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM (SELECT sql_fulltext AS sql_text, child_number, "
                               + ("con_id" if modern else "0 AS con_id")
                               + ", plan_hash_value, executions, elapsed_time AS elapsed_time_us, cpu_time AS cpu_time_us, "
                                 "buffer_gets, disk_reads, rows_processed FROM v$sql WHERE " + " AND ".join(predicates)
                               + " ORDER BY child_number) WHERE ROWNUM = 1", binds)
                matched = rows_to_dicts(cursor, cursor.fetchall())
                if not matched:
                    return {"sql_id": sql_id, "sql_fulltext": None, "plan_text": "", "awr_text": None,
                            "status": "not_found", "execution_status": "failed", "plan_status": "not_collected",
                            "message": "所选游标/容器/计划已不在缓存中，未替换为其他记录。"}
                selected = matched[0]
                sql_fulltext = read_sql_text(selected["sql_text"])
                selected_child = selected["child_number"]
                selected_con = selected["con_id"]
                plan_status, plan_error = "not_collected", None
                cursor.execute("SELECT " + ("SYS_CONTEXT('USERENV','CON_ID')" if modern else "'0'") + " AS con_id FROM dual")
                current_con = int(cursor.fetchone()[0])
                plan_text = ""
                if current_con != int(selected_con):
                    plan_error = "所选游标不在当前连接容器，未读取其他容器的计划。"
                else:
                    try:
                        cursor.execute("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :child_number, 'ALLSTATS LAST'))",
                                       {"sql_id": sql_id, "child_number": selected_child})
                        plan_text = "\n".join(str(r[0]) for r in cursor.fetchall() if r[0])
                        plan_hashes = re.findall(r"^\s*Plan hash value:\s*(\d+)[ \t]*$", plan_text, re.MULTILINE)
                        if plan_hashes != [str(selected["plan_hash_value"])]:
                            plan_error = "未取得与所选游标一致的执行计划，缓存可能已变化。"
                            plan_text = ""
                        else:
                            plan_status = "collected"
                    except Exception as exc:
                        plan_error = redact_database_error(exc)
    except Exception as exc:  # noqa: BLE001 - offline instances should return a readable degraded result.
        logger.warning("SQL详情查询失败 | instance=%s | sql_id=%s | error=%s", instance.instance_name, sql_id, redact_database_error(exc))
        return {
            "sql_id": sql_id,
            "sql_fulltext": "无法获取SQL文本",
            "plan_text": "",
            "awr_text": None,
            "status": "degraded",
            "execution_status": "failed",
            "message": f"SQL 详情查询失败：{redact_database_error(exc)}",
            "warnings": ORACLE_QUERY_FAILURE_WARNINGS,
        }

    return {
        "sql_id": sql_id,
        "sql_fulltext": sql_fulltext,
        "plan_text": plan_text,
        "awr_text": None,
        "status": "success" if plan_status == "collected" and sql_fulltext else "partial",
        "execution_status": "success" if sql_fulltext else "partial", "sql_text_status": "complete" if sql_fulltext else "not_collected",
        "child_number": selected_child, "con_id": selected_con, "plan_hash_value": selected["plan_hash_value"],
        "plan_status": plan_status, "plan_error": plan_error, "analysis_status": "not_run",
        "metrics": {key: selected.get(key) for key in ("executions", "elapsed_time_us", "cpu_time_us", "buffer_gets", "disk_reads", "rows_processed")},
    }
