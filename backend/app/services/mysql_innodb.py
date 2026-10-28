import logging
import re

from app.models.db_instance import DBInstance
from app.collectors.mysql.mysql_connection import mysql_connection

logger = logging.getLogger("app.services.mysql_innodb")


INNODB_STATUS_KEYS = (
    "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_reads",
    "Innodb_buffer_pool_write_requests",
    "Innodb_buffer_pool_pages_total",
    "Innodb_buffer_pool_pages_data",
    "Innodb_buffer_pool_pages_free",
    "Innodb_buffer_pool_pages_misc",
    "Innodb_buffer_pool_pages_dirty",
    "Innodb_buffer_pool_pages_flushed",
    "Innodb_buffer_pool_bytes_data",
    "Innodb_buffer_pool_bytes_dirty",
    "Innodb_buffer_pool_wait_free",
    "Innodb_buffer_pool_read_ahead",
    "Innodb_buffer_pool_read_ahead_evicted",
    "Innodb_buffer_pool_read_ahead_rnd",
    "Innodb_data_reads",
    "Innodb_data_writes",
    "Innodb_data_fsyncs",
    "Innodb_data_read",
    "Innodb_data_written",
    "Innodb_os_log_written",
    "Innodb_os_log_fsyncs",
    "Innodb_log_waits",
    "Innodb_log_write_requests",
    "Innodb_log_writes",
    "Innodb_row_lock_current_waits",
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
    "Innodb_row_lock_time_avg",
    "Innodb_row_lock_time_max",
    "Innodb_rows_read",
    "Innodb_rows_inserted",
    "Innodb_rows_updated",
    "Innodb_rows_deleted",
    "Innodb_dblwr_pages_written",
    "Innodb_dblwr_writes",
)

INNODB_VARIABLE_KEYS = (
    "innodb_version",
    "innodb_page_size",
    "innodb_buffer_pool_size",
    "innodb_buffer_pool_chunk_size",
    "innodb_buffer_pool_instances",
    "innodb_log_file_size",
    "innodb_log_files_in_group",
    "innodb_redo_log_capacity",
    "innodb_flush_log_at_trx_commit",
    "sync_binlog",
    "innodb_flush_method",
    "innodb_file_per_table",
    "innodb_max_dirty_pages_pct",
    "innodb_io_capacity",
    "innodb_io_capacity_max",
    "innodb_read_io_threads",
    "innodb_write_io_threads",
    "innodb_thread_concurrency",
    "innodb_lock_wait_timeout",
    "innodb_deadlock_detect",
)

INNODB_METRIC_KEYS = (
    "trx_rseg_history_len",
    "buffer_pool_reads",
    "buffer_pool_read_requests",
    "buffer_pool_write_requests",
    "buffer_pool_pages_total",
    "buffer_pool_pages_free",
    "buffer_pool_pages_dirty",
    "buffer_pool_pages_data",
    "buffer_pool_pages_misc",
    "buffer_pool_pages_flushed",
    "log_lsn_current",
    "log_lsn_checkpoint",
    "log_lsn_last_checkpoint",
    "log_lsn_buf_pool_oldest",
    "lock_row_lock_waits",
    "lock_row_lock_time",
    "dml_reads",
    "dml_inserts",
    "dml_deletes",
    "dml_updates",
)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _ratio(part: int, total: int) -> float:
    return round(part * 100 / total, 2) if total > 0 else 0.0


def _load_global_status(cur) -> dict:
    cur.execute("SHOW GLOBAL STATUS")
    rows = cur.fetchall()
    return {str(row.get("Variable_name") or row.get("VARIABLE_NAME") or "").lower(): row.get("Value") or row.get("VARIABLE_VALUE") or "" for row in rows}


def _load_global_variables(cur) -> dict:
    cur.execute("SHOW GLOBAL VARIABLES")
    rows = cur.fetchall()
    return {str(row.get("Variable_name") or row.get("VARIABLE_NAME") or "").lower(): row.get("Value") or row.get("VARIABLE_VALUE") or "" for row in rows}


def _pick_lower(raw: dict, keys: tuple[str, ...]) -> dict:
    return {key.lower(): raw.get(key.lower(), "") for key in keys}


def _safe_rows(cur, sql: str, warnings: list[str], label: str) -> list[dict]:
    try:
        cur.execute(sql)
        return cur.fetchall()
    except Exception as exc:
        warnings.append(f"{label} 采集失败：{exc}")
        return []


def _build_groups(status: dict, variables: dict, metrics: dict, engine: dict, transactions: dict | None = None, locks: dict | None = None) -> dict:
    total_pages = _to_int(status.get("innodb_buffer_pool_pages_total") or metrics.get("buffer_pool_pages_total"))
    free_pages = _to_int(status.get("innodb_buffer_pool_pages_free") or metrics.get("buffer_pool_pages_free"))
    data_pages = _to_int(status.get("innodb_buffer_pool_pages_data") or metrics.get("buffer_pool_pages_data"))
    misc_pages = _to_int(status.get("innodb_buffer_pool_pages_misc") or metrics.get("buffer_pool_pages_misc"))
    dirty_pages = _to_int(status.get("innodb_buffer_pool_pages_dirty") or metrics.get("buffer_pool_pages_dirty"))
    used_pages = max(total_pages - free_pages, 0) if total_pages else 0
    page_size = _to_int(variables.get("innodb_page_size"), 16384) or 16384
    configured_bytes = _to_int(variables.get("innodb_buffer_pool_size"))
    read_requests = _to_int(status.get("innodb_buffer_pool_read_requests") or metrics.get("buffer_pool_read_requests"))
    disk_reads = _to_int(status.get("innodb_buffer_pool_reads") or metrics.get("buffer_pool_reads"))
    hit_pct = round((1 - disk_reads / read_requests) * 100, 4) if read_requests else 0

    def gb_from_pages(pages: int) -> float:
        return round(pages * page_size / 1024 / 1024 / 1024, 2)

    transactions = transactions or {}
    locks = locks or {}
    log_file_size = _to_int(variables.get("innodb_log_file_size"))
    log_files = _to_int(variables.get("innodb_log_files_in_group"))
    redo_capacity = _to_int(variables.get("innodb_redo_log_capacity")) or (log_file_size * log_files if log_file_size and log_files else 0)

    return {
        "summary": {
            "buffer_pool_hit_pct": hit_pct,
            "buffer_pool_configured_gb": round(configured_bytes / 1024 / 1024 / 1024, 2) if configured_bytes else 0,
            "buffer_pool_used_gb": gb_from_pages(used_pages),
            "buffer_pool_free_gb": gb_from_pages(free_pages),
            "buffer_pool_used_pct": _ratio(used_pages, total_pages),
            "buffer_pool_free_pct": _ratio(free_pages, total_pages),
            "buffer_pool_dirty_pct": _ratio(dirty_pages, total_pages),
            "history_list_length": engine.get("trx_rseg_history_len") or metrics.get("trx_rseg_history_len") or "",
            "long_transaction_count": transactions.get("long_transaction_count", 0),
            "lock_wait_count": locks.get("lock_wait_count", 0),
            "deadlock_detected": engine.get("deadlock_detected", False),
            "innodb_log_waits": _to_int(status.get("innodb_log_waits")),
        },
        "capacity": {
            "innodb_page_size": page_size,
            "buffer_pool_configured_gb": round(configured_bytes / 1024 / 1024 / 1024, 2) if configured_bytes else 0,
            "buffer_pool_total_gb": gb_from_pages(total_pages),
            "buffer_pool_used_gb": gb_from_pages(used_pages),
            "buffer_pool_free_gb": gb_from_pages(free_pages),
            "buffer_pool_data_gb": gb_from_pages(data_pages),
            "buffer_pool_dirty_gb": gb_from_pages(dirty_pages),
        },
        "pages": {
            "innodb_buffer_pool_pages_total": total_pages,
            "buffer_pool_used_pages": used_pages,
            "innodb_buffer_pool_pages_data": data_pages,
            "innodb_buffer_pool_pages_free": free_pages,
            "innodb_buffer_pool_pages_misc": misc_pages,
            "innodb_buffer_pool_pages_dirty": dirty_pages,
            "innodb_buffer_pool_pages_flushed": status.get("innodb_buffer_pool_pages_flushed") or metrics.get("buffer_pool_pages_flushed") or "",
            "buffer_pool_used_pct": _ratio(used_pages, total_pages),
            "buffer_pool_free_pct": _ratio(free_pages, total_pages),
            "buffer_pool_data_pct": _ratio(data_pages, total_pages),
            "buffer_pool_dirty_pct": _ratio(dirty_pages, total_pages),
            "buffer_pool_misc_pct": _ratio(misc_pages, total_pages),
            "trx_rseg_history_len": metrics.get("trx_rseg_history_len") or engine.get("trx_rseg_history_len") or "",
        },
        "read_write": {
            "buffer_pool_hit_pct": hit_pct,
            "buffer_pool_disk_read_pct": round(100 - hit_pct, 4) if read_requests else 0,
            "buffer_pool_disk_reads_per_10k": round(disk_reads * 10000 / read_requests, 2) if read_requests else 0,
            "innodb_buffer_pool_read_requests": read_requests,
            "innodb_buffer_pool_reads": disk_reads,
            "innodb_buffer_pool_write_requests": status.get("innodb_buffer_pool_write_requests") or metrics.get("buffer_pool_write_requests") or "",
            "innodb_buffer_pool_wait_free": status.get("innodb_buffer_pool_wait_free", ""),
            "innodb_buffer_pool_read_ahead": status.get("innodb_buffer_pool_read_ahead", ""),
            "innodb_buffer_pool_read_ahead_evicted": status.get("innodb_buffer_pool_read_ahead_evicted", ""),
            "innodb_buffer_pool_read_ahead_rnd": status.get("innodb_buffer_pool_read_ahead_rnd", ""),
        },
        "io_log": {
            "innodb_data_reads": status.get("innodb_data_reads", ""),
            "innodb_data_writes": status.get("innodb_data_writes", ""),
            "innodb_data_fsyncs": status.get("innodb_data_fsyncs", ""),
            "innodb_data_read": status.get("innodb_data_read", ""),
            "innodb_data_written": status.get("innodb_data_written", ""),
            "innodb_os_log_written": status.get("innodb_os_log_written", ""),
            "innodb_os_log_fsyncs": status.get("innodb_os_log_fsyncs", ""),
            "innodb_log_waits": status.get("innodb_log_waits", ""),
            "innodb_log_write_requests": status.get("innodb_log_write_requests", ""),
            "innodb_log_writes": status.get("innodb_log_writes", ""),
            "log_lsn_current": metrics.get("log_lsn_current", ""),
            "log_lsn_checkpoint": metrics.get("log_lsn_checkpoint", ""),
        },
        "redo_log": {
            "innodb_log_waits": status.get("innodb_log_waits", ""),
            "innodb_os_log_written": status.get("innodb_os_log_written", ""),
            "innodb_log_write_requests": status.get("innodb_log_write_requests", ""),
            "innodb_log_writes": status.get("innodb_log_writes", ""),
            "innodb_flush_log_at_trx_commit": variables.get("innodb_flush_log_at_trx_commit", ""),
            "sync_binlog": variables.get("sync_binlog", ""),
            "innodb_log_file_size": variables.get("innodb_log_file_size", ""),
            "innodb_log_files_in_group": variables.get("innodb_log_files_in_group", ""),
            "innodb_redo_log_capacity": variables.get("innodb_redo_log_capacity", ""),
            "redo_capacity_mb": round(redo_capacity / 1024 / 1024, 2) if redo_capacity else 0,
        },
        "io": {
            "innodb_data_reads": status.get("innodb_data_reads", ""),
            "innodb_data_writes": status.get("innodb_data_writes", ""),
            "innodb_data_fsyncs": status.get("innodb_data_fsyncs", ""),
            "innodb_os_log_written": status.get("innodb_os_log_written", ""),
            "innodb_buffer_pool_pages_flushed": status.get("innodb_buffer_pool_pages_flushed", ""),
            "innodb_dblwr_writes": status.get("innodb_dblwr_writes", ""),
            "innodb_dblwr_pages_written": status.get("innodb_dblwr_pages_written", ""),
            "pending_reads": engine.get("pending_reads", ""),
            "pending_writes": engine.get("pending_writes", ""),
            "pending_fsyncs": engine.get("pending_fsyncs", ""),
        },
        "row_operations": {
            "innodb_rows_read": status.get("innodb_rows_read", ""),
            "innodb_rows_inserted": status.get("innodb_rows_inserted", ""),
            "innodb_rows_updated": status.get("innodb_rows_updated", ""),
            "innodb_rows_deleted": status.get("innodb_rows_deleted", ""),
        },
        "locks_rows": {
            "innodb_row_lock_current_waits": status.get("innodb_row_lock_current_waits", ""),
            "innodb_row_lock_waits": status.get("innodb_row_lock_waits", ""),
            "innodb_row_lock_time": status.get("innodb_row_lock_time", ""),
            "innodb_row_lock_time_avg": status.get("innodb_row_lock_time_avg", ""),
            "innodb_row_lock_time_max": status.get("innodb_row_lock_time_max", ""),
            "innodb_rows_read": status.get("innodb_rows_read", ""),
            "innodb_rows_inserted": status.get("innodb_rows_inserted", ""),
            "innodb_rows_updated": status.get("innodb_rows_updated", ""),
            "innodb_rows_deleted": status.get("innodb_rows_deleted", ""),
        },
        "config": variables,
    }


def _fetch_transactions(cur, warnings: list[str]) -> dict:
    rows = _safe_rows(
        cur,
        """
        SELECT
          trx_id,
          trx_state,
          trx_started,
          TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS trx_seconds,
          trx_mysql_thread_id,
          trx_rows_locked,
          trx_rows_modified,
          trx_query
        FROM information_schema.innodb_trx
        ORDER BY trx_started
        """,
        warnings,
        "InnoDB 事务",
    )
    long_rows = [row for row in rows if _to_int(row.get("trx_seconds")) > 300]
    oldest = rows[0] if rows else {}
    return {
        "active_transaction_count": len(rows),
        "long_transaction_count": len(long_rows),
        "max_trx_seconds": max((_to_int(row.get("trx_seconds")) for row in rows), default=0),
        "oldest_trx_started": str(oldest.get("trx_started") or "") if oldest else "",
        "oldest_trx_query": oldest.get("trx_query") or "",
        "oldest_trx_thread_id": oldest.get("trx_mysql_thread_id") or "",
        "transactions": rows[:20],
    }


def _fetch_locks(cur, warnings: list[str]) -> dict:
    try:
        cur.execute(
            """
            SELECT
              r.ENGINE_TRANSACTION_ID AS waiting_trx_id,
              b.ENGINE_TRANSACTION_ID AS blocking_trx_id,
              r.OBJECT_SCHEMA AS lock_schema,
              r.OBJECT_NAME AS lock_table,
              r.INDEX_NAME AS lock_index,
              r.LOCK_TYPE AS lock_type,
              r.LOCK_MODE AS waiting_lock_mode,
              b.LOCK_MODE AS blocking_lock_mode,
              r.LOCK_STATUS AS lock_status
            FROM performance_schema.data_lock_waits w
            JOIN performance_schema.data_locks r
              ON w.REQUESTING_ENGINE_LOCK_ID = r.ENGINE_LOCK_ID
            JOIN performance_schema.data_locks b
              ON w.BLOCKING_ENGINE_LOCK_ID = b.ENGINE_LOCK_ID
            """
        )
        rows = cur.fetchall()
    except Exception as exc:
        warnings.append(f"Performance Schema 锁等待采集失败，已尝试回退：{exc}")
        rows = _safe_rows(
            cur,
            """
            SELECT
              requesting_trx_id AS waiting_trx_id,
              blocking_trx_id AS blocking_trx_id,
              requested_lock_id AS waiting_lock_mode,
              blocking_lock_id AS blocking_lock_mode
            FROM information_schema.innodb_lock_waits
            """,
            warnings,
            "InnoDB 锁等待回退",
        )
    blocking = {row.get("blocking_trx_id") for row in rows if row.get("blocking_trx_id")}
    waiting = {row.get("waiting_trx_id") for row in rows if row.get("waiting_trx_id")}
    return {
        "lock_wait_count": len(rows),
        "blocking_thread_count": len(blocking),
        "waiting_thread_count": len(waiting),
        "items": rows[:50],
    }


def _build_risk_items(groups: dict, engine: dict, transactions: dict, locks: dict) -> list[dict]:
    risks: list[dict] = []

    def add(level: str, title: str, finding: str, impact: str, suggestion: str):
        risks.append({"level": level, "title": title, "finding": finding, "impact": impact, "suggestion": suggestion})

    summary = groups.get("summary", {})
    hit_pct = float(summary.get("buffer_pool_hit_pct") or 0)
    dirty_pct = float(summary.get("buffer_pool_dirty_pct") or 0)
    history_len = _to_int(summary.get("history_list_length"))
    log_waits = _to_int(summary.get("innodb_log_waits"))

    if hit_pct and hit_pct < 95:
        add("critical", "Buffer Pool 命中率严重偏低", f"当前命中率 {hit_pct}%。", "可能导致大量磁盘读，拖慢 SQL 响应。", "检查热点数据集、慢 SQL、索引和 innodb_buffer_pool_size。")
    elif hit_pct and hit_pct < 99:
        add("warning", "Buffer Pool 命中率偏低", f"当前命中率 {hit_pct}%。", "缓存命中不足会增加 I/O 压力。", "结合大表访问和慢 SQL，评估 Buffer Pool 容量与索引。")

    if dirty_pct > 75:
        add("critical", "InnoDB 脏页比例过高", f"当前脏页比例 {dirty_pct}%。", "可能触发集中刷脏或 checkpoint 压力。", "检查写入高峰、IO 能力和 innodb_max_dirty_pages_pct。")
    elif dirty_pct > 60:
        add("warning", "InnoDB 脏页比例偏高", f"当前脏页比例 {dirty_pct}%。", "写入压力或刷盘能力可能需要关注。", "观察 pages flushed、fsync 和磁盘响应时间。")

    if history_len > 100000:
        add("critical", "History List Length 严重偏高", f"当前 History List Length 为 {history_len}。", "Undo/Purge 积压可能导致查询变慢和空间膨胀。", "优先检查长事务、慢查询、批量更新和 purge 压力。")
    elif history_len > 10000:
        add("warning", "History List Length 偏高", f"当前 History List Length 为 {history_len}。", "可能存在长事务或 purge 跟不上。", "查看事务与 Undo 页面，确认最老事务来源。")

    max_trx = _to_int(transactions.get("max_trx_seconds"))
    if max_trx > 1800:
        add("critical", "存在严重长事务", f"最长事务运行 {max_trx} 秒。", "可能影响 Undo 清理、锁释放和复制延迟。", "定位事务线程、SQL 和来源程序，确认能否提交或终止。")
    elif max_trx > 300:
        add("warning", "存在长事务", f"最长事务运行 {max_trx} 秒。", "长事务可能阻塞 purge 或持有锁。", "建议检查事务 SQL、来源会话和业务调用链。")

    lock_count = _to_int(locks.get("lock_wait_count"))
    if lock_count > 5:
        add("critical", "当前锁等待较多", f"当前锁等待 {lock_count} 个。", "可能导致业务请求堆积或超时。", "定位 blocking_trx_id 和等待表，优先处理阻塞源。")
    elif lock_count > 0:
        add("warning", "存在锁等待", f"当前锁等待 {lock_count} 个。", "部分事务可能被阻塞。", "查看锁等待明细，确认是否为热点行或长事务。")

    if engine.get("deadlock_detected"):
        add("warning", "检测到最近死锁", "SHOW ENGINE INNODB STATUS 中存在 LATEST DETECTED DEADLOCK。", "频繁死锁会导致业务事务回滚。", "检查死锁 SQL、访问顺序和索引设计。")

    if log_waits > 0:
        add("warning", "Redo Log 存在等待", f"Innodb_log_waits={log_waits}。", "Redo 写入可能存在等待，影响事务提交。", "检查 redo 容量、磁盘性能和 innodb_flush_log_at_trx_commit。")

    return risks


def get_innodb_status(instance: DBInstance) -> dict:
    result = {"raw": "", "sections": {}, "metrics": {}, "variables": {}, "groups": {}, "innodb": {}, "summary": {}, "risk_items": [], "warnings": [], "error": None}
    warnings: list[str] = []
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                status = _pick_lower(_load_global_status(cur), INNODB_STATUS_KEYS)
                variables = _pick_lower(_load_global_variables(cur), INNODB_VARIABLE_KEYS)
                cur.execute("SHOW ENGINE INNODB STATUS")
                row = cur.fetchone()
                raw = row.get("Status", "") if row else ""
                result["raw"] = raw
                result["sections"] = _parse_innodb_status(raw)
                engine_metrics = _parse_engine_metrics(raw)
                transactions = _fetch_transactions(cur, warnings)
                locks = _fetch_locks(cur, warnings)

                try:
                    placeholders = ",".join(["%s"] * len(INNODB_METRIC_KEYS))
                    cur.execute("""
                        SELECT name, count
                        FROM information_schema.innodb_metrics
                        WHERE name IN (""" + placeholders + """)
                    """, INNODB_METRIC_KEYS)
                except Exception:
                    cur.execute("SELECT name, count FROM information_schema.innodb_metrics WHERE status = 'enabled'")
                for m in cur.fetchall():
                    if m.get("name") in INNODB_METRIC_KEYS:
                        result["metrics"][m["name"]] = m["count"]

                result["variables"] = variables
                result["metrics"] = {**result["metrics"], **status}
                result["groups"] = _build_groups(status, variables, result["metrics"], engine_metrics, transactions, locks)
                result["innodb"] = {
                    "buffer_pool": {**result["groups"].get("capacity", {}), **result["groups"].get("pages", {}), **result["groups"].get("read_write", {})},
                    "transactions": transactions,
                    "undo_purge": {"history_list_length": result["groups"].get("summary", {}).get("history_list_length"), "active_long_transactions": transactions.get("long_transaction_count", 0)},
                    "locks": locks,
                    "deadlocks": {"deadlock_detected": engine_metrics.get("deadlock_detected", False), "latest_deadlock": engine_metrics.get("latest_deadlock", "")},
                    "redo_log": result["groups"].get("redo_log", {}),
                    "io": result["groups"].get("io", {}),
                    "row_operations": result["groups"].get("row_operations", {}),
                    "config": result["groups"].get("config", {}),
                    "raw_monitor": raw,
                }
                result["risk_items"] = _build_risk_items(result["groups"], engine_metrics, transactions, locks)
                risk_order = {"critical": 4, "high": 3, "warning": 2, "info": 1}
                max_level = max((risk_order.get(item.get("level"), 0) for item in result["risk_items"]), default=0)
                risk_level = {4: "critical", 3: "high", 2: "warning", 1: "info", 0: "normal"}.get(max_level, "normal")
                result["summary"] = {
                    **result["groups"].get("summary", {}),
                    "risk_level": risk_level,
                    "lock_wait_count": locks.get("lock_wait_count", 0),
                    "deadlock_detected": engine_metrics.get("deadlock_detected", False),
                    "io_status": "warning" if _to_int(engine_metrics.get("pending_reads")) or _to_int(engine_metrics.get("pending_writes")) or _to_int(engine_metrics.get("pending_fsyncs")) else "normal",
                }
                result["warnings"] = warnings

    except Exception as e:
        result["error"] = str(e)
    return result


def _parse_innodb_status(raw: str) -> dict:
    sections = {}
    current = None
    lines = []
    for line in raw.split("\n"):
        if line.startswith("---") and line.endswith("---"):
            name = line.strip("-").strip()
            if current:
                sections[current] = "\n".join(lines)
            current = name
            lines = []
            continue
        if current:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines)
    return sections


def _parse_engine_metrics(raw: str) -> dict:
    data = {}
    match = re.search(r"History list length\s+(\d+)", raw)
    if match:
        data["trx_rseg_history_len"] = match.group(1)
    data["deadlock_detected"] = "LATEST DETECTED DEADLOCK" in raw
    if data["deadlock_detected"]:
        deadlock_match = re.search(r"LATEST DETECTED DEADLOCK\s*-+\s*(.*?)(?:\n-+\n[A-Z ]+\n|\Z)", raw, re.S)
        if deadlock_match:
            data["latest_deadlock"] = deadlock_match.group(1).strip()[:4000]
    aio_match = re.search(r"Pending normal aio reads:\s*(\d+).*?aio writes:\s*(\d+)", raw, re.S)
    if aio_match:
        data["pending_reads"] = aio_match.group(1)
        data["pending_writes"] = aio_match.group(2)
    flush_match = re.search(r"Pending flushes.*?fsyncs:\s*(\d+)", raw, re.S)
    if flush_match:
        data["pending_fsyncs"] = flush_match.group(1)
    return data
