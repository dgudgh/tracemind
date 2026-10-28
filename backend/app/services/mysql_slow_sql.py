import logging
import re
from collections import Counter

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_slow_sql")

SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")
SLOW_SQL_TIME_RANGES = {
    "1h": {"hours": 1, "label": "1小时"},
    "3h": {"hours": 3, "label": "3小时"},
    "12h": {"hours": 12, "label": "12小时"},
    "24h": {"hours": 24, "label": "24小时"},
    "3d": {"hours": 72, "label": "3天"},
    "7d": {"hours": 168, "label": "7天"},
}


def _normalize_time_range(value: str | None = None) -> dict:
    key = str(value or "24h").strip().lower()
    if key not in SLOW_SQL_TIME_RANGES:
        key = "24h"
    item = SLOW_SQL_TIME_RANGES[key]
    return {"key": key, "hours": item["hours"], "label": item["label"]}


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_query(cur, sql: str, warnings: list[str], label: str, params=None) -> list[dict]:
    try:
        cur.execute(sql, params or ())
        return list(cur.fetchall() or [])
    except Exception as exc:
        warnings.append(f"{label}采集失败：{exc}")
        logger.warning("mysql slow sql section failed | label=%s | error=%s", label, exc)
        return []


def _table_exists(cur, schema: str, table: str) -> bool:
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM information_schema.tables
            WHERE table_schema = %s
              AND UPPER(table_name) = UPPER(%s)
            """,
            (schema, table),
        )
        row = cur.fetchone() or {}
        return _to_int(row.get("cnt") or row.get("COUNT(*)")) > 0
    except Exception:
        return False


def _risk_score(row: dict) -> int:
    score = 0
    avg_seconds = _to_float(row.get("avg_seconds") or row.get("avg_sec"))
    max_seconds = _to_float(row.get("max_seconds"))
    total_seconds = _to_float(row.get("total_seconds") or row.get("total_sec"))
    executions = _to_int(row.get("executions") or row.get("count_star"))
    rows_examined = _to_int(row.get("rows_examined") or row.get("sum_rows_examined"))
    ratio = _to_float(row.get("examined_sent_ratio"))
    tmp_disk = _to_int(row.get("tmp_disk_tables"))
    sort_rows = _to_int(row.get("sort_rows"))
    full_join = _to_int(row.get("select_full_join"))
    no_index = _to_int(row.get("no_index_used"))

    if avg_seconds > 5:
        score += 25
    elif avg_seconds > 1:
        score += 12
    if max_seconds > 60:
        score += 20
    elif max_seconds > 10:
        score += 10
    if total_seconds > 1800:
        score += 20
    elif total_seconds > 300:
        score += 10
    if rows_examined > 100_000_000:
        score += 20
    elif rows_examined > 1_000_000:
        score += 10
    if ratio > 1000:
        score += 15
    elif ratio > 100:
        score += 8
    if tmp_disk > 0:
        score += 8
    if sort_rows > 0:
        score += 5
    if full_join > 0:
        score += 10
    if no_index > 0:
        score += 10
    if executions > 10000:
        score += 15
    elif executions > 1000:
        score += 8
    return min(score, 100)


def _risk_level(score: int) -> str:
    if score >= 70:
        return "critical"
    if score >= 40:
        return "warning"
    return "normal"


def _extract_tables(sql_text: str, default_schema: str = "") -> list[str]:
    if not sql_text:
        return []
    tables = []
    pattern = r"(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+`?([A-Za-z0-9_]+)`?(?:\.`?([A-Za-z0-9_]+)`?)?"
    for first, second in re.findall(pattern, sql_text, flags=re.I):
        schema = first if second else default_schema
        table = second or first
        name = ".".join([part for part in [schema, table] if part])
        if name and name not in tables and first.lower() not in SYSTEM_SCHEMAS:
            tables.append(name)
    return tables[:8]


def _recommendations(row: dict) -> list[str]:
    recs = []
    if _to_int(row.get("no_index_used")) > 0:
        recs.append("存在无索引使用风险，建议检查 WHERE / JOIN 条件索引。")
    if _to_int(row.get("select_full_join")) > 0:
        recs.append("存在全表 JOIN 风险，建议为 JOIN 字段补充合适索引。")
    if _to_int(row.get("tmp_disk_tables")) > 0:
        recs.append("存在磁盘临时表，建议优化 GROUP BY / ORDER BY 或评估 tmp_table_size。")
    if _to_int(row.get("sort_rows")) > 0:
        recs.append("存在排序开销，建议检查排序字段是否可被复合索引覆盖。")
    if _to_float(row.get("examined_sent_ratio")) > 100:
        recs.append("扫描/返回比例偏高，建议提高过滤条件选择性或补充组合索引。")
    if _to_int(row.get("executions")) > 1000 and _to_float(row.get("avg_seconds")) < 1:
        recs.append("高频短 SQL 可能累计消耗较高，建议评估缓存、批处理或减少调用次数。")
    if not recs:
        recs.append("建议结合执行计划、表大小和业务调用频率继续观察。")
    return recs


def _normalize_digest(row: dict) -> dict:
    rows_sent = _to_int(row.get("rows_sent"))
    rows_examined = _to_int(row.get("rows_examined"))
    ratio = round(rows_examined / rows_sent, 2) if rows_sent else float(rows_examined or 0)
    row["executions"] = _to_int(row.get("executions") or row.get("count_star"))
    row["total_seconds"] = round(_to_float(row.get("total_seconds") or row.get("total_sec")), 4)
    row["avg_seconds"] = round(_to_float(row.get("avg_seconds") or row.get("avg_sec")), 4)
    row["max_seconds"] = round(_to_float(row.get("max_seconds")), 4)
    row["min_seconds"] = round(_to_float(row.get("min_seconds")), 4)
    row["rows_sent"] = rows_sent
    row["rows_examined"] = rows_examined
    row["examined_sent_ratio"] = ratio
    row["tmp_tables"] = _to_int(row.get("tmp_tables"))
    row["tmp_disk_tables"] = _to_int(row.get("tmp_disk_tables"))
    row["sort_rows"] = _to_int(row.get("sort_rows"))
    row["sort_merge_passes"] = _to_int(row.get("sort_merge_passes"))
    row["select_full_join"] = _to_int(row.get("select_full_join"))
    row["select_full_range_join"] = _to_int(row.get("select_full_range_join"))
    row["select_scan"] = _to_int(row.get("select_scan"))
    row["no_index_used"] = _to_int(row.get("no_index_used"))
    row["no_good_index_used"] = _to_int(row.get("no_good_index_used"))
    row["risk_score"] = _risk_score(row)
    row["risk_level"] = _risk_level(row["risk_score"])
    row["tables"] = _extract_tables(row.get("digest_text") or "", row.get("schema_name") or "")
    row["recommendations"] = _recommendations(row)
    return row


def _fetch_recent_statement_rows(
    cur,
    warnings: list[str],
    limit: int = 200,
    time_range: str | None = None,
) -> tuple[list[dict], bool]:
    if not _table_exists(cur, "performance_schema", "events_statements_history_long"):
        warnings.append("performance_schema.events_statements_history_long 未开启或无权限，无法按所选时间范围计算真实慢 SQL 样本。")
        return [], False
    range_info = _normalize_time_range(time_range)
    hours = int(range_info["hours"])
    before_warning_count = len(warnings)
    event_time_expr = (
        "DATE_ADD(DATE_SUB(NOW(), INTERVAL CAST(u.VARIABLE_VALUE AS UNSIGNED) SECOND), "
        "INTERVAL FLOOR(e.TIMER_START / 1000000000000) SECOND)"
    )
    rows = _safe_query(
        cur,
        f"""
        SELECT
          NULLIF(e.CURRENT_SCHEMA, '') AS schema_name,
          e.DIGEST AS digest,
          LEFT(e.DIGEST_TEXT, 1200) AS digest_text,
          COUNT(*) AS executions,
          ROUND(SUM(e.TIMER_WAIT) / 1000000000000, 4) AS total_seconds,
          ROUND(AVG(e.TIMER_WAIT) / 1000000000000, 4) AS avg_seconds,
          ROUND(MAX(e.TIMER_WAIT) / 1000000000000, 4) AS max_seconds,
          ROUND(MIN(e.TIMER_WAIT) / 1000000000000, 4) AS min_seconds,
          SUM(e.ROWS_SENT) AS rows_sent,
          SUM(e.ROWS_EXAMINED) AS rows_examined,
          SUM(e.CREATED_TMP_TABLES) AS tmp_tables,
          SUM(e.CREATED_TMP_DISK_TABLES) AS tmp_disk_tables,
          SUM(e.SORT_ROWS) AS sort_rows,
          SUM(e.SORT_MERGE_PASSES) AS sort_merge_passes,
          SUM(e.SELECT_FULL_JOIN) AS select_full_join,
          SUM(e.SELECT_FULL_RANGE_JOIN) AS select_full_range_join,
          SUM(e.SELECT_SCAN) AS select_scan,
          SUM(e.NO_INDEX_USED) AS no_index_used,
          SUM(e.NO_GOOD_INDEX_USED) AS no_good_index_used,
          MIN({event_time_expr}) AS first_seen,
          MAX({event_time_expr}) AS last_seen
        FROM performance_schema.events_statements_history_long e
        JOIN performance_schema.global_status u
          ON u.VARIABLE_NAME = 'Uptime'
        WHERE e.DIGEST_TEXT IS NOT NULL
          AND e.TIMER_END IS NOT NULL
          AND COALESCE(e.CURRENT_SCHEMA, '') NOT IN ('mysql','information_schema','performance_schema','sys')
          AND {event_time_expr} >= DATE_SUB(NOW(), INTERVAL {hours} HOUR)
        GROUP BY NULLIF(e.CURRENT_SCHEMA, ''), e.DIGEST, LEFT(e.DIGEST_TEXT, 1200)
        ORDER BY SUM(e.TIMER_WAIT) DESC
        LIMIT %s
        """,
        warnings,
        "慢 SQL 时间窗口样本",
        (limit,),
    )
    if len(warnings) > before_warning_count:
        return [], False
    if not rows:
        warnings.append(f"所选时间范围（{range_info['label']}）内未采集到 statement history long 样本；请确认 events_statements_history_long consumer 已开启，并等待业务 SQL 执行后再查看。")
    return [_normalize_digest(row) for row in rows], True


def _fetch_digest_rows(cur, warnings: list[str], limit: int = 200, time_range: str | None = None) -> list[dict]:
    if not _table_exists(cur, "performance_schema", "events_statements_summary_by_digest"):
        warnings.append("performance_schema.events_statements_summary_by_digest 不存在或无权限，无法采集慢 SQL 摘要。")
        return []
    range_info = _normalize_time_range(time_range)
    hours = int(range_info["hours"])
    rows = _safe_query(
        cur,
        f"""
        SELECT
          SCHEMA_NAME AS schema_name,
          DIGEST AS digest,
          LEFT(DIGEST_TEXT, 1200) AS digest_text,
          COUNT_STAR AS executions,
          ROUND(SUM_TIMER_WAIT / 1000000000000, 4) AS total_seconds,
          ROUND(AVG_TIMER_WAIT / 1000000000000, 4) AS avg_seconds,
          ROUND(MAX_TIMER_WAIT / 1000000000000, 4) AS max_seconds,
          ROUND(MIN_TIMER_WAIT / 1000000000000, 4) AS min_seconds,
          SUM_ROWS_SENT AS rows_sent,
          SUM_ROWS_EXAMINED AS rows_examined,
          SUM_CREATED_TMP_TABLES AS tmp_tables,
          SUM_CREATED_TMP_DISK_TABLES AS tmp_disk_tables,
          SUM_SORT_ROWS AS sort_rows,
          SUM_SORT_MERGE_PASSES AS sort_merge_passes,
          SUM_SELECT_FULL_JOIN AS select_full_join,
          SUM_SELECT_FULL_RANGE_JOIN AS select_full_range_join,
          SUM_SELECT_SCAN AS select_scan,
          SUM_NO_INDEX_USED AS no_index_used,
          SUM_NO_GOOD_INDEX_USED AS no_good_index_used,
          FIRST_SEEN AS first_seen,
          LAST_SEEN AS last_seen
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
          AND COALESCE(SCHEMA_NAME, '') NOT IN ('mysql','information_schema','performance_schema','sys')
          AND LAST_SEEN >= DATE_SUB(NOW(), INTERVAL {hours} HOUR)
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT %s
        """,
        warnings,
        "慢 SQL Digest 摘要",
        (limit,),
    )
    return [_normalize_digest(row) for row in rows]


def _fetch_running_slow_sql(cur, warnings: list[str]) -> list[dict]:
    rows = _safe_query(
        cur,
        """
        SELECT
          t.PROCESSLIST_ID AS process_id,
          t.THREAD_ID AS thread_id,
          t.PROCESSLIST_USER AS user,
          t.PROCESSLIST_HOST AS host,
          t.PROCESSLIST_DB AS db,
          t.PROCESSLIST_TIME AS time_seconds,
          t.PROCESSLIST_STATE AS state,
          t.PROCESSLIST_INFO AS sql_text
        FROM performance_schema.threads t
        WHERE t.TYPE = 'FOREGROUND'
          AND COALESCE(t.PROCESSLIST_COMMAND, '') <> 'Sleep'
          AND COALESCE(t.PROCESSLIST_TIME, 0) >= 3
        ORDER BY t.PROCESSLIST_TIME DESC
        LIMIT 100
        """,
        warnings,
        "当前正在执行的慢 SQL",
    )
    if rows:
        return rows
    rows = _safe_query(cur, "SHOW FULL PROCESSLIST", warnings, "SHOW FULL PROCESSLIST")
    return [
        {
            "process_id": row.get("Id") or row.get("ID"),
            "thread_id": row.get("Id") or row.get("ID"),
            "user": row.get("User"),
            "host": row.get("Host"),
            "db": row.get("db") or row.get("DB"),
            "time_seconds": row.get("Time"),
            "state": row.get("State"),
            "sql_text": row.get("Info"),
        }
        for row in rows
        if str(row.get("Command") or "").lower() != "sleep" and _to_int(row.get("Time")) >= 3
    ]


def _fetch_config(cur, warnings: list[str]) -> dict:
    names = [
        "slow_query_log",
        "slow_query_log_file",
        "long_query_time",
        "log_queries_not_using_indexes",
        "min_examined_row_limit",
        "performance_schema",
    ]
    placeholders = ",".join(["%s"] * len(names))
    variables = _safe_query(
        cur,
        f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({placeholders})",
        warnings,
        "慢 SQL 配置",
        tuple(names),
    )
    config = {row.get("Variable_name"): row.get("Value") for row in variables}
    consumers = []
    if _table_exists(cur, "performance_schema", "setup_consumers"):
        consumers = _safe_query(
            cur,
            """
            SELECT NAME AS name, ENABLED AS enabled
            FROM performance_schema.setup_consumers
            WHERE NAME IN (
              'events_statements_current',
              'events_statements_history',
              'events_statements_history_long',
              'statements_digest'
            )
            ORDER BY NAME
            """,
            warnings,
            "Performance Schema statements consumer",
        )
    else:
        warnings.append("performance_schema.setup_consumers 不存在或无权限，无法检查 statements consumer。")
    suggestions = []
    if str(config.get("slow_query_log", "")).upper() not in ("ON", "1"):
        suggestions.append("建议开启 slow_query_log，用于采集真实 SQL 原文。")
    if str(config.get("performance_schema", "")).upper() not in ("ON", "1"):
        suggestions.append("performance_schema 未开启会影响 Digest、执行样本和排行分析。")
    if not any(row.get("name") == "events_statements_history_long" and str(row.get("enabled")).upper() == "YES" for row in consumers):
        suggestions.append("events_statements_history_long 未开启时，无法查看 SQL 最近执行样本。")
    return {"variables": config, "consumers": consumers, "suggestions": suggestions}


def _build_rankings(rows: list[dict]) -> dict:
    def top(key, reverse=True):
        return sorted(rows, key=lambda r: _to_float(r.get(key)), reverse=reverse)[:50]

    return {
        "by_avg_time": top("avg_seconds"),
        "by_total_time": top("total_seconds"),
        "by_max_time": top("max_seconds"),
        "by_executions": top("executions"),
        "by_rows_examined": top("rows_examined"),
        "by_rows_sent": top("rows_sent"),
        "by_examined_sent_ratio": top("examined_sent_ratio"),
        "by_tmp_disk_tables": top("tmp_disk_tables"),
        "by_sort_rows": top("sort_rows"),
        "by_full_join": top("select_full_join"),
        "by_no_index_used": top("no_index_used"),
    }


def _build_summary(rows: list[dict]) -> dict:
    critical = sum(1 for row in rows if row.get("risk_level") == "critical")
    warning = sum(1 for row in rows if row.get("risk_level") == "warning")
    risk_level = "critical" if critical else "warning" if warning else "normal"
    return {
        "risk_level": risk_level,
        "slow_sql_count": len(rows),
        "critical_count": critical,
        "warning_count": warning,
        "max_avg_seconds": max((_to_float(row.get("avg_seconds")) for row in rows), default=0),
        "max_max_seconds": max((_to_float(row.get("max_seconds")) for row in rows), default=0),
        "max_total_seconds": max((_to_float(row.get("total_seconds")) for row in rows), default=0),
        "max_rows_examined": max((_to_int(row.get("rows_examined")) for row in rows), default=0),
        "tmp_disk_sql_count": sum(1 for row in rows if _to_int(row.get("tmp_disk_tables")) > 0),
        "full_join_sql_count": sum(1 for row in rows if _to_int(row.get("select_full_join")) > 0),
        "no_index_sql_count": sum(1 for row in rows if _to_int(row.get("no_index_used")) > 0),
    }


def _build_risks(rows: list[dict], config: dict, running: list[dict]) -> list[dict]:
    risks = []

    def add(level: str, title: str, message: str, suggestion: str):
        risks.append({"level": level, "title": title, "message": message, "suggestion": suggestion})

    if running:
        add("warning", "当前存在正在执行的慢 SQL", f"当前有 {len(running)} 条 SQL 执行超过 3 秒。", "优先查看线程、来源主机和 SQL 文本，必要时联动锁与事务中心。")
    tmp_disk = sum(1 for row in rows if _to_int(row.get("tmp_disk_tables")) > 0)
    if tmp_disk:
        add("warning", "存在磁盘临时表 SQL", f"{tmp_disk} 条 Digest 产生过磁盘临时表。", "检查 GROUP BY / ORDER BY，必要时优化索引或评估临时表参数。")
    full_join = sum(1 for row in rows if _to_int(row.get("select_full_join")) > 0)
    if full_join:
        add("warning", "存在全表 JOIN 风险", f"{full_join} 条 Digest 出现 Select_full_join。", "检查 JOIN 字段索引，优先处理执行次数和扫描行数高的 SQL。")
    no_index = sum(1 for row in rows if _to_int(row.get("no_index_used")) > 0)
    if no_index:
        add("warning", "存在无索引使用风险", f"{no_index} 条 Digest 报告 No_index_used。", "结合涉及表、过滤字段和执行计划评估组合索引。")
    if str((config.get("variables") or {}).get("slow_query_log", "")).upper() not in ("ON", "1"):
        add("warning", "慢日志未开启", "slow_query_log 未开启，当前仅基于 Performance Schema 摘要分析。", "建议生成配置方案开启慢日志并设置合理 long_query_time。")
    if not rows and not risks:
        add("normal", "当前未发现慢 SQL 摘要", "Performance Schema 中未采集到慢 SQL Digest。", "确认 performance_schema 和 statements_digest consumer 是否开启。")
    return risks


def _build_recommendations(rows: list[dict]) -> list[dict]:
    items = []
    for row in sorted(rows, key=lambda r: _to_int(r.get("risk_score")), reverse=True)[:20]:
        items.append(
            {
                "digest": row.get("digest"),
                "schema_name": row.get("schema_name"),
                "digest_text": row.get("digest_text"),
                "risk_level": row.get("risk_level"),
                "risk_score": row.get("risk_score"),
                "tables": row.get("tables", []),
                "suggestions": row.get("recommendations", []),
            }
        )
    return items


def _build_table_profile(rows: list[dict]) -> list[dict]:
    counter = Counter()
    for row in rows:
        for table in row.get("tables", []):
            counter[table] += _to_int(row.get("rows_examined"))
    return [{"table": table, "rows_examined": rows_examined} for table, rows_examined in counter.most_common(30)]


def get_slow_sql_top(instance: DBInstance, limit: int = 50, time_range: str | None = None) -> list:
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                warnings: list[str] = []
                rows, history_available = _fetch_recent_statement_rows(cur, warnings, limit, time_range)
                if history_available and rows:
                    return rows
                return _fetch_digest_rows(cur, warnings, limit, time_range)
    except Exception as exc:
        logger.error("get_slow_sql_top failed: %s", exc)
        return []


def get_slow_sql_center(instance: DBInstance, limit: int = 200, time_range: str | None = None) -> dict:
    warnings: list[str] = []
    rows: list[dict] = []
    running: list[dict] = []
    config: dict = {"variables": {}, "consumers": [], "suggestions": []}
    range_info = _normalize_time_range(time_range)
    data_source = "events_statements_history_long"
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                rows, history_available = _fetch_recent_statement_rows(cur, warnings, limit, range_info["key"])
                if not history_available or not rows:
                    rows = _fetch_digest_rows(cur, warnings, limit, range_info["key"])
                    data_source = "events_statements_summary_by_digest"
                running = _fetch_running_slow_sql(cur, warnings)
                config = _fetch_config(cur, warnings)
    except Exception as exc:
        logger.exception("mysql slow sql center failed: %s", exc)
        warnings.append(f"慢 SQL 诊断中心采集失败：{exc}")

    summary = _build_summary(rows)
    rankings = _build_rankings(rows)
    risks = _build_risks(rows, config, running)
    return {
        "status": "ok" if not any("采集失败" in item for item in warnings) else "degraded",
        "instance": {"id": instance.id, "name": instance.instance_name, "host": instance.host, "port": instance.port},
        "time_range": range_info,
        "data_source": data_source,
        "summary": summary,
        "rankings": rankings,
        "digest_details": rows,
        "current_running": running,
        "config": config,
        "table_profile": _build_table_profile(rows),
        "risk_items": risks,
        "recommendations": _build_recommendations(rows),
        "trend": {
            "items": [],
            "message": "慢 SQL 趋势需要定时快照入库后展示，当前为实时 Performance Schema 摘要。",
        },
        "slow_log": {
            "items": [],
            "message": "未配置 SSH，无法读取慢查询日志文件。当前仅基于 Performance Schema 摘要分析。",
        },
        "warnings": warnings,
        "items": rows[:50],
        "raw": {"digest_count": len(rows)},
    }
