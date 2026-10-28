import logging
import re
from datetime import datetime

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_table_statistics")

SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")
IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _int(value, default=0):
    return int(_num(value, default))


def _quote_ident(value: str) -> str:
    name = str(value or "").strip("`").strip()
    if not IDENT_RE.match(name):
        raise ValueError(f"非法对象名: {value}")
    return f"`{name}`"


def _fetch(cur, sql: str, warnings: list[str], label: str, params=None) -> list[dict]:
    try:
        cur.execute(sql, params or ())
        rows = list(cur.fetchall() or [])
        normalized = []
        for row in rows:
            if isinstance(row, dict):
                normalized.append({str(key).lower(): value for key, value in row.items()})
            else:
                normalized.append(row)
        return normalized
    except Exception as exc:
        warnings.append(f"{label}采集失败：{exc}")
        logger.warning("mysql table statistics section failed | label=%s | error=%s", label, exc)
        return []


def _normalize_table(row: dict) -> dict:
    data_mb = round(_num(row.get("data_length")) / 1024 / 1024, 2)
    index_mb = round(_num(row.get("index_length")) / 1024 / 1024, 2)
    free_mb = round(_num(row.get("data_free")) / 1024 / 1024, 2)
    total_mb = round(data_mb + index_mb, 2)
    update_time = row.get("update_time")
    days_since_update = None
    if isinstance(update_time, datetime):
        days_since_update = max((datetime.now() - update_time).days, 0)

    risk_level = "normal"
    status = "正常"
    if update_time is None:
        risk_level = "info"
        status = "更新时间为空，InnoDB 可能不维护该字段"
    elif total_mb >= 1024 and days_since_update is not None and days_since_update > 90:
        risk_level = "critical"
        status = "统计信息疑似严重陈旧"
    elif total_mb >= 1024 and days_since_update is not None and days_since_update > 30:
        risk_level = "warning"
        status = "统计信息疑似陈旧"

    return {
        **row,
        "estimated_rows": _int(row.get("table_rows")),
        "data_mb": data_mb,
        "index_mb": index_mb,
        "free_mb": free_mb,
        "total_mb": total_mb,
        "days_since_update": days_since_update,
        "stats_status": status,
        "risk_level": risk_level,
    }


def _fetch_table_stats(cur, warnings: list[str], limit: int) -> list[dict]:
    rows = _fetch(
        cur,
        """
        SELECT table_schema, table_name, engine, table_rows, avg_row_length,
               data_length, index_length, data_free, create_time, update_time,
               check_time, table_collation
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
          AND table_type = 'BASE TABLE'
        ORDER BY (data_length + index_length) DESC, table_rows DESC
        LIMIT %s
        """,
        warnings,
        "表统计信息",
        (limit,),
    )
    return [_normalize_table(row) for row in rows]


def _fetch_table_counts(cur, warnings: list[str]) -> dict:
    rows = _fetch(
        cur,
        """
        SELECT COUNT(*) AS table_count,
               SUM(CASE WHEN update_time IS NOT NULL THEN 1 ELSE 0 END) AS analyzed_table_count
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
          AND table_type = 'BASE TABLE'
        """,
        warnings,
        "表统计汇总",
    )
    return rows[0] if rows else {"table_count": 0, "analyzed_table_count": 0}


def _fetch_config(cur, warnings: list[str]) -> dict:
    names = [
        "innodb_stats_persistent",
        "innodb_stats_auto_recalc",
        "innodb_stats_persistent_sample_pages",
        "innodb_stats_transient_sample_pages",
        "eq_range_index_dive_limit",
        "information_schema_stats_expiry",
    ]
    placeholders = ",".join(["%s"] * len(names))
    rows = _fetch(
        cur,
        f"SHOW VARIABLES WHERE Variable_name IN ({placeholders})",
        warnings,
        "统计信息配置",
        tuple(names),
    )
    variables = {row.get("variable_name"): row.get("value") for row in rows}
    risks = []
    if str(variables.get("innodb_stats_persistent", "")).upper() not in ("ON", "1"):
        risks.append({"level": "warning", "item": "innodb_stats_persistent", "message": "持久化统计信息未开启，重启或缓存变化后执行计划可能波动。"})
    if str(variables.get("innodb_stats_auto_recalc", "")).upper() not in ("ON", "1"):
        risks.append({"level": "info", "item": "innodb_stats_auto_recalc", "message": "自动重算统计信息未开启，DML 后统计信息可能滞后。"})
    if _int(variables.get("innodb_stats_persistent_sample_pages")) and _int(variables.get("innodb_stats_persistent_sample_pages")) < 20:
        risks.append({"level": "warning", "item": "innodb_stats_persistent_sample_pages", "message": "采样页数偏低，大表统计信息可能不稳定。"})
    if _int(variables.get("information_schema_stats_expiry")) > 86400:
        risks.append({"level": "info", "item": "information_schema_stats_expiry", "message": "information_schema 统计缓存时间较长，页面数据可能不是最新。"})
    return {"variables": variables, "risks": risks}


def _fetch_cardinality(cur, warnings: list[str], limit: int) -> list[dict]:
    rows = _fetch(
        cur,
        """
        SELECT s.table_schema, s.table_name, s.index_name, s.seq_in_index,
               s.column_name, s.cardinality, t.table_rows, s.non_unique, s.index_type,
               ROUND(s.cardinality / NULLIF(t.table_rows, 0) * 100, 4) AS selectivity_pct
        FROM information_schema.statistics s
        JOIN information_schema.tables t
          ON s.table_schema = t.table_schema
         AND s.table_name = t.table_name
        WHERE s.table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
          AND t.table_type = 'BASE TABLE'
        ORDER BY selectivity_pct ASC, t.table_rows DESC
        LIMIT %s
        """,
        warnings,
        "索引基数",
        (limit,),
    )
    result = []
    for row in rows:
        table_rows = _num(row.get("table_rows"))
        cardinality = _num(row.get("cardinality"))
        selectivity = _num(row.get("selectivity_pct"))
        risk_level = "normal"
        risk = "正常"
        if table_rows > 10000 and cardinality <= 0:
            risk_level = "warning"
            risk = "大表索引基数为空或为 0，建议评估 ANALYZE TABLE"
        elif table_rows > 10000 and selectivity < 0.1:
            risk_level = "critical"
            risk = "选择性低于 0.1%，需确认索引设计或组合列价值"
        elif table_rows > 10000 and selectivity < 1:
            risk_level = "warning"
            risk = "选择性低于 1%，建议结合 SQL 谓词评估"
        result.append({**row, "risk_level": risk_level, "risk": risk})
    return result


def _fetch_histograms(cur, warnings: list[str], limit: int) -> list[dict]:
    rows = _fetch(
        cur,
        """
        SELECT SCHEMA_NAME AS table_schema, TABLE_NAME AS table_name,
               COLUMN_NAME AS column_name, JSON_EXTRACT(HISTOGRAM, '$."number-of-buckets-specified"') AS buckets,
               JSON_EXTRACT(HISTOGRAM, '$."last-updated"') AS last_updated,
               JSON_EXTRACT(HISTOGRAM, '$."histogram-type"') AS histogram_type
        FROM information_schema.COLUMN_STATISTICS
        WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys')
        LIMIT %s
        """,
        warnings,
        "直方图统计",
        (limit,),
    )
    return rows


def _fetch_partitions(cur, warnings: list[str], limit: int) -> list[dict]:
    rows = _fetch(
        cur,
        """
        SELECT table_schema, table_name, partition_name, table_rows,
               data_length, index_length, update_time,
               ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_mb
        FROM information_schema.partitions
        WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
          AND partition_name IS NOT NULL
        ORDER BY table_schema, table_name, partition_ordinal_position
        LIMIT %s
        """,
        warnings,
        "分区统计",
        (limit,),
    )
    by_table: dict[tuple, list[dict]] = {}
    for row in rows:
        by_table.setdefault((row.get("table_schema"), row.get("table_name")), []).append(row)
    for group in by_table.values():
        counts = [_num(row.get("table_rows")) for row in group if _num(row.get("table_rows")) > 0]
        ratio = max(counts) / max(min(counts), 1) if len(counts) >= 2 else 1
        for row in group:
            row["imbalance_ratio"] = round(ratio, 2)
            row["risk_level"] = "warning" if ratio >= 10 else "normal"
            row["risk"] = "分区行数差异较大" if ratio >= 10 else "正常"
    return rows


def _build_histogram_candidates(cardinality: list[dict]) -> list[dict]:
    candidates = []
    for row in cardinality:
        if row.get("risk_level") in ("warning", "critical") and _int(row.get("seq_in_index")) == 1:
            candidates.append({
                "table_schema": row.get("table_schema"),
                "table_name": row.get("table_name"),
                "column_name": row.get("column_name"),
                "reason": "索引选择性较低，若列值分布倾斜且频繁出现在 WHERE 条件中，可评估 Histogram。",
                "risk_level": row.get("risk_level"),
            })
        if len(candidates) >= 50:
            break
    return candidates


def _build_plan_risks(cardinality: list[dict], stale_stats: list[dict]) -> list[dict]:
    risks = []
    stale_keys = {(row.get("table_schema"), row.get("table_name")) for row in stale_stats}
    for row in cardinality[:80]:
        key = (row.get("table_schema"), row.get("table_name"))
        if row.get("risk_level") in ("warning", "critical"):
            risks.append({
                "level": row.get("risk_level"),
                "table_schema": row.get("table_schema"),
                "table_name": row.get("table_name"),
                "item": row.get("index_name"),
                "message": f"索引 {row.get('index_name')} 列 {row.get('column_name')} 选择性 {row.get('selectivity_pct')}%，可能影响优化器估算。",
                "suggestion": "先 ANALYZE TABLE 更新统计信息，再结合 EXPLAIN 判断是否需要调整索引。",
            })
        if key in stale_keys:
            risks.append({
                "level": "warning",
                "table_schema": row.get("table_schema"),
                "table_name": row.get("table_name"),
                "item": "stale_stats",
                "message": "该表统计信息疑似陈旧，可能导致执行计划估算偏差。",
                "suggestion": "低峰期生成并执行 ANALYZE TABLE 方案。",
            })
    return risks[:100]


def _build_risk_items(stale_stats, cardinality, partitions, config):
    items = []
    if stale_stats:
        items.append({"level": "warning", "item": "统计信息陈旧", "message": f"发现 {len(stale_stats)} 张表统计信息疑似陈旧。", "suggestion": "优先对慢 SQL 涉及的大表生成 ANALYZE TABLE 方案。"})
    card_risks = [row for row in cardinality if row.get("risk_level") in ("warning", "critical")]
    if card_risks:
        items.append({"level": "warning", "item": "索引基数异常", "message": f"发现 {len(card_risks)} 个索引列选择性或基数异常。", "suggestion": "先更新统计信息，再评估索引设计。"})
    part_risks = [row for row in partitions if row.get("risk_level") == "warning"]
    if part_risks:
        items.append({"level": "warning", "item": "分区统计不均衡", "message": f"发现 {len(part_risks)} 个分区行数差异较大。", "suggestion": "检查分区策略和慢 SQL 是否命中分区裁剪。"})
    items.extend(config.get("risks", []))
    if not items:
        items.append({"level": "normal", "item": "统计信息健康", "message": "当前未发现明显统计信息风险。", "suggestion": "继续观察慢 SQL、执行计划和表容量变化。"})
    return items


def get_table_statistics_center(instance: DBInstance, limit: int = 500) -> dict:
    warnings: list[str] = []
    table_stats: list[dict] = []
    table_counts: dict = {"table_count": 0, "analyzed_table_count": 0}
    config: dict = {"variables": {}, "risks": []}
    cardinality: list[dict] = []
    histograms: list[dict] = []
    partitions: list[dict] = []
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                table_counts = _fetch_table_counts(cur, warnings)
                table_stats = _fetch_table_stats(cur, warnings, limit)
                config = _fetch_config(cur, warnings)
                cardinality = _fetch_cardinality(cur, warnings, limit)
                histograms = _fetch_histograms(cur, warnings, 200)
                partitions = _fetch_partitions(cur, warnings, limit)
    except Exception as exc:
        logger.exception("mysql table statistics center failed: %s", exc)
        warnings.append(f"表统计信息中心采集失败：{exc}")

    stale_stats = [row for row in table_stats if row.get("risk_level") in ("warning", "critical")]
    low_cardinality = [row for row in cardinality if row.get("risk_level") in ("warning", "critical")]
    histogram_candidates = _build_histogram_candidates(cardinality)
    partition_risks = [row for row in partitions if row.get("risk_level") == "warning"]
    plan_risks = _build_plan_risks(cardinality, stale_stats)
    risk_items = _build_risk_items(stale_stats, cardinality, partitions, config)
    summary_level = "critical" if any(row.get("risk_level") == "critical" for row in low_cardinality + stale_stats) else "warning" if any(item.get("level") in ("warning", "critical") for item in risk_items) else "normal"

    return {
        "status": "ok" if not any("中心采集失败" in item for item in warnings) else "degraded",
        "instance": {"id": instance.id, "name": instance.instance_name, "host": instance.host, "port": instance.port},
        "summary": {
            "risk_level": summary_level,
            "table_count": _int(table_counts.get("table_count"), len(table_stats)),
            "analyzed_table_count": _int(table_counts.get("analyzed_table_count"), len([row for row in table_stats if row.get("update_time")])),
            "returned_table_count": len(table_stats),
            "stale_stat_table_count": len(stale_stats),
            "row_estimation_risk_count": len([row for row in table_stats if _num(row.get("total_mb")) >= 1024 and row.get("update_time") is None]),
            "low_cardinality_index_count": len(low_cardinality),
            "histogram_count": len(histograms),
            "histogram_candidate_count": len(histogram_candidates),
            "partition_stat_risk_count": len(partition_risks),
        },
        "sections": {
            "table_stats": table_stats,
            "stale_stats": stale_stats,
            "cardinality": cardinality,
            "low_cardinality": low_cardinality,
            "row_estimation": [row for row in table_stats if _num(row.get("total_mb")) >= 1024 and row.get("update_time") is None],
            "histograms": histograms,
            "histogram_candidates": histogram_candidates,
            "partition_stats": partitions,
            "config": config,
            "plan_risks": plan_risks,
        },
        "risk_items": risk_items,
        "recommendations": [item.get("suggestion") for item in risk_items if item.get("suggestion")],
        "actions": {"analyze_table_plan_enabled": True, "histogram_plan_enabled": True},
        "warnings": warnings,
        "items": table_stats,
        "raw": {"table_count": _int(table_counts.get("table_count"), len(table_stats)), "returned_table_count": len(table_stats), "cardinality_count": len(cardinality), "partition_count": len(partitions)},
    }


def build_analyze_table_plan(payload: dict) -> dict:
    owner = _quote_ident(payload.get("schema") or payload.get("table_schema") or payload.get("owner"))
    tables = payload.get("tables") or payload.get("table_names") or payload.get("table_name")
    if isinstance(tables, str):
        tables = [tables]
    if not tables:
        raise ValueError("请选择要分析的表")
    quoted = [f"{owner}.{_quote_ident(name)}" for name in tables[:50]]
    sql = "ANALYZE TABLE\n  " + ",\n  ".join(quoted) + ";"
    return {
        "status": "plan_only",
        "action": "analyze_table",
        "sql": sql,
        "warnings": [
            "ANALYZE TABLE 会更新优化器统计信息，可能导致执行计划变化。",
            "建议在低峰期执行，并观察核心 SQL 的执行计划是否变化。",
            "大表建议分批执行，不要全库一次性 ANALYZE。",
        ],
    }


def build_histogram_plan(payload: dict, drop: bool = False) -> dict:
    owner = _quote_ident(payload.get("schema") or payload.get("table_schema") or payload.get("owner"))
    table = _quote_ident(payload.get("table_name") or payload.get("table"))
    column = _quote_ident(payload.get("column_name") or payload.get("column"))
    buckets = max(min(_int(payload.get("buckets"), 100), 1024), 1)
    if drop:
        sql = f"ANALYZE TABLE {owner}.{table}\nDROP HISTOGRAM ON {column};"
        action = "drop_histogram"
    else:
        sql = f"ANALYZE TABLE {owner}.{table}\nUPDATE HISTOGRAM ON {column} WITH {buckets} BUCKETS;"
        action = "update_histogram"
    return {
        "status": "plan_only",
        "action": action,
        "sql": sql,
        "warnings": [
            "Histogram 可能改变优化器选择，适合数据分布倾斜且经常作为过滤条件的列。",
            "生产执行前请在测试库验证慢 SQL 执行计划变化。",
        ],
    }
