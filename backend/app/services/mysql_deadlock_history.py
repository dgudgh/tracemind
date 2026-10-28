from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.models.db_instance import DBInstance
from app.services.mysql_lock import get_lock_transaction_center


def _normalize_sql(sql: str) -> str:
    normalized = re.sub(r"'[^']*'", "?", sql or "")
    normalized = re.sub(r"\b\d+\b", "?", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:300]


def _fingerprint(sql: str) -> str:
    normalized = _normalize_sql(sql)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _safe_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _collection_plan() -> dict[str, Any]:
    return {
        "status": "plan_only",
        "title": "MySQL 历史死锁采集方案",
        "description": "平台当前会解析 SHOW ENGINE INNODB STATUS 中最近一次死锁；如需完整历史趋势，请开启死锁写入错误日志并由采集任务入库。",
        "steps": [
            "确认 innodb_print_all_deadlocks 当前状态。",
            "低峰期评估后开启 innodb_print_all_deadlocks，让每次死锁写入 MySQL Error Log。",
            "通过 SSH、文件采集器或日志平台采集 Error Log 中的死锁片段。",
            "按时间、对象、SQL 指纹和来源主机入库，形成趋势和 TOP 分析。",
        ],
        "commands": [
            "SHOW VARIABLES LIKE 'innodb_print_all_deadlocks';",
            "SET PERSIST innodb_print_all_deadlocks = ON;",
            "grep -n \"LATEST DETECTED DEADLOCK\" <mysql_error.log>",
        ],
        "warnings": [
            "这里只生成方案，不直接修改数据库参数。",
            "开启前请确认错误日志容量、轮转策略和审计要求。",
        ],
    }


def _items_from_current_status(instance: DBInstance) -> tuple[list[dict[str, Any]], list[str]]:
    data = get_lock_transaction_center(instance)
    warnings = list(data.get("warnings") or [])
    deadlocks = data.get("sections", {}).get("deadlocks", []) or []
    items: list[dict[str, Any]] = []

    for index, deadlock in enumerate(deadlocks, start=1):
        summary = deadlock.get("summary") or {}
        analysis = deadlock.get("analysis") or {}
        sql_samples = [
            item.get("query")
            for item in deadlock.get("transactions", [])
            if item.get("query")
        ]
        sql_fingerprints = [_fingerprint(sql) for sql in sql_samples]
        occurred_at = summary.get("deadlock_time") or ""
        items.append(
            {
                "id": f"current-{index}",
                "source": "SHOW ENGINE INNODB STATUS",
                "collection_status": "current_snapshot",
                "occurred_at": occurred_at,
                "deadlock_type": summary.get("deadlock_type") or "unknown",
                "risk_level": summary.get("risk_level") or "warning",
                "transaction_count": summary.get("transaction_count") or len(deadlock.get("transactions") or []),
                "victim_transaction": summary.get("victim_transaction") or "",
                "involved_tables": deadlock.get("involved_tables") or [],
                "involved_indexes": deadlock.get("involved_indexes") or [],
                "sql_samples": sql_samples,
                "sql_fingerprints": [item for item in sql_fingerprints if item],
                "root_cause": analysis.get("root_cause") or "",
                "suggestions": analysis.get("suggestions") or [],
                "raw_deadlock_text": deadlock.get("raw_deadlock_text") or "",
            }
        )

    return items, warnings


def build_deadlock_history_snapshot(instance: DBInstance) -> dict[str, Any]:
    items, warnings = _items_from_current_status(instance)
    now = datetime.now()
    last_24h_count = 0
    last_7d_count = 0

    for item in items:
        occurred = _safe_time(item.get("occurred_at", ""))
        if not occurred:
            continue
        if occurred >= now - timedelta(hours=24):
            last_24h_count += 1
        if occurred >= now - timedelta(days=7):
            last_7d_count += 1

    if items and last_24h_count == 0 and last_7d_count == 0:
        last_24h_count = 1
        last_7d_count = 1

    table_counter = Counter(table for item in items for table in item.get("involved_tables", []))
    sql_counter = Counter(fp for item in items for fp in item.get("sql_fingerprints", []))

    return {
        "items": items,
        "last_24h_count": last_24h_count,
        "last_7d_count": last_7d_count,
        "top_tables": [{"table": key, "count": value} for key, value in table_counter.most_common(10)],
        "top_sql_fingerprints": [{"fingerprint": key, "count": value} for key, value in sql_counter.most_common(10)],
        "top_hosts": [],
        "collection_plan": _collection_plan(),
        "warnings": warnings,
        "message": (
            "已基于当前 InnoDB 状态生成最近死锁快照；完整历史趋势需要接入 Error Log 采集。"
            if items
            else "当前 InnoDB 状态中未发现最近死锁记录；可按采集方案开启历史死锁采集。"
        ),
    }


def build_deadlock_trend_snapshot(instance: DBInstance) -> dict[str, Any]:
    history = build_deadlock_history_snapshot(instance)
    buckets: Counter[str] = Counter()
    for item in history["items"]:
        occurred = _safe_time(item.get("occurred_at", ""))
        bucket = occurred.strftime("%Y-%m-%d %H:00") if occurred else "current_snapshot"
        buckets[bucket] += 1

    return {
        "items": [
            {"time_bucket": key, "deadlock_count": value, "source": "SHOW ENGINE INNODB STATUS"}
            for key, value in sorted(buckets.items())
        ],
        "top_tables": history["top_tables"],
        "top_sql_fingerprints": history["top_sql_fingerprints"],
        "top_hosts": history["top_hosts"],
        "collection_plan": history["collection_plan"],
        "warnings": history["warnings"],
        "message": history["message"],
    }
