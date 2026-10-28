import logging
import re
from collections import Counter, defaultdict
from datetime import datetime

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_lock")

SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")


def _to_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _duration_label(seconds) -> str:
    seconds = _to_int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400} 天 {seconds % 86400 // 3600} 小时"
    if seconds >= 3600:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分钟"
    if seconds >= 60:
        return f"{seconds // 60} 分钟 {seconds % 60} 秒"
    return f"{seconds} 秒"


def _safe_query(cur, sql: str, warnings: list[str], label: str, params=None) -> list[dict]:
    try:
        cur.execute(sql, params or ())
        return list(cur.fetchall() or [])
    except Exception as exc:
        warnings.append(f"{label}采集失败：{exc}")
        logger.warning("mysql lock section failed | label=%s | error=%s", label, exc)
        return []


def _safe_status(cur, names: list[str], warnings: list[str], label: str) -> dict:
    placeholders = ",".join(["%s"] * len(names))
    rows = _safe_query(
        cur,
        f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})",
        warnings,
        label,
        tuple(names),
    )
    return {str(row.get("Variable_name") or row.get("VARIABLE_NAME") or ""): row.get("Value") for row in rows}


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
    except Exception as exc:
        logger.debug("mysql table existence check failed | %s.%s | %s", schema, table, exc)
        return False


def _risk_level_for_transaction(row: dict) -> str:
    seconds = _to_int(row.get("trx_seconds") or row.get("duration_sec"))
    rows_locked = _to_int(row.get("trx_rows_locked"))
    rows_modified = _to_int(row.get("trx_rows_modified"))
    lock_structs = _to_int(row.get("trx_lock_structs"))
    if seconds > 1800 or rows_locked > 10000 or rows_modified > 100000 or lock_structs > 10000:
        return "critical"
    if seconds > 300 or rows_locked > 1000 or rows_modified > 10000 or lock_structs > 1000:
        return "warning"
    return "normal"


def _fetch_sessions(cur, warnings: list[str]) -> list[dict]:
    rows = _safe_query(
        cur,
        """
        SELECT
          t.PROCESSLIST_ID AS process_id,
          t.THREAD_ID AS thread_id,
          t.PROCESSLIST_USER AS user,
          t.PROCESSLIST_HOST AS host,
          t.PROCESSLIST_DB AS db,
          t.PROCESSLIST_COMMAND AS command,
          t.PROCESSLIST_TIME AS time_seconds,
          t.PROCESSLIST_STATE AS state,
          t.PROCESSLIST_INFO AS sql_text
        FROM performance_schema.threads t
        WHERE t.TYPE = 'FOREGROUND'
          AND t.PROCESSLIST_ID IS NOT NULL
        ORDER BY t.PROCESSLIST_TIME DESC
        LIMIT 500
        """,
        warnings,
        "当前会话",
    )
    if rows:
        return rows

    rows = _safe_query(cur, "SHOW FULL PROCESSLIST", warnings, "SHOW FULL PROCESSLIST")
    normalized = []
    for row in rows:
        normalized.append(
            {
                "process_id": row.get("Id") or row.get("ID"),
                "thread_id": row.get("Id") or row.get("ID"),
                "user": row.get("User"),
                "host": row.get("Host"),
                "db": row.get("db") or row.get("DB"),
                "command": row.get("Command"),
                "time_seconds": row.get("Time"),
                "state": row.get("State"),
                "sql_text": row.get("Info"),
            }
        )
    return normalized


def _fetch_transactions(cur, warnings: list[str]) -> list[dict]:
    rows = _safe_query(
        cur,
        """
        SELECT
          trx_id,
          trx_state,
          trx_started,
          TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS trx_seconds,
          trx_mysql_thread_id,
          trx_query,
          trx_operation_state,
          trx_tables_in_use,
          trx_tables_locked,
          trx_lock_structs,
          trx_lock_memory_bytes,
          trx_rows_locked,
          trx_rows_modified,
          trx_isolation_level,
          trx_unique_checks,
          trx_foreign_key_checks
        FROM information_schema.innodb_trx
        ORDER BY trx_started
        """,
        warnings,
        "当前事务",
    )
    for row in rows:
        row["duration_sec"] = _to_int(row.get("trx_seconds"))
        row["duration_label"] = _duration_label(row.get("trx_seconds"))
        row["risk_level"] = _risk_level_for_transaction(row)
    return rows


def _fetch_lock_waits(cur, warnings: list[str], *, strict: bool = False) -> list[dict]:
    rows = []
    query_succeeded = False
    if _table_exists(cur, "performance_schema", "data_lock_waits") and _table_exists(cur, "performance_schema", "data_locks"):
        warning_count = len(warnings)
        rows = _safe_query(
            cur,
            """
            SELECT
              w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,
              w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id,
              r.THREAD_ID AS waiting_thread_id,
              b.THREAD_ID AS blocking_thread_id,
              wr.trx_mysql_thread_id AS waiting_process_id,
              br.trx_mysql_thread_id AS blocking_process_id,
              r.OBJECT_SCHEMA AS object_schema,
              r.OBJECT_NAME AS object_name,
              r.INDEX_NAME AS index_name,
              r.LOCK_TYPE AS waiting_lock_type,
              r.LOCK_MODE AS waiting_lock_mode,
              b.LOCK_MODE AS blocking_lock_mode,
              r.LOCK_STATUS AS waiting_lock_status,
              wr.trx_query AS waiting_sql,
              br.trx_query AS blocking_sql,
              TIMESTAMPDIFF(SECOND, wr.trx_started, NOW()) AS waiting_trx_seconds,
              TIMESTAMPDIFF(SECOND, br.trx_started, NOW()) AS blocking_trx_seconds
            FROM performance_schema.data_lock_waits w
            JOIN performance_schema.data_locks r
              ON w.REQUESTING_ENGINE_LOCK_ID = r.ENGINE_LOCK_ID
            JOIN performance_schema.data_locks b
              ON w.BLOCKING_ENGINE_LOCK_ID = b.ENGINE_LOCK_ID
            LEFT JOIN information_schema.innodb_trx wr
              ON wr.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
            LEFT JOIN information_schema.innodb_trx br
              ON br.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
            """,
            warnings,
            "锁等待",
        )
        query_succeeded = len(warnings) == warning_count
    if not rows and _table_exists(cur, "information_schema", "innodb_lock_waits"):
        warning_count = len(warnings)
        rows = _safe_query(
            cur,
            """
            SELECT
              w.requesting_trx_id AS waiting_trx_id,
              w.blocking_trx_id AS blocking_trx_id,
              r.trx_mysql_thread_id AS waiting_process_id,
              b.trx_mysql_thread_id AS blocking_process_id,
              r.trx_query AS waiting_sql,
              b.trx_query AS blocking_sql,
              TIMESTAMPDIFF(SECOND, r.trx_started, NOW()) AS waiting_trx_seconds,
              TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_trx_seconds
            FROM information_schema.innodb_lock_waits w
            LEFT JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id
            LEFT JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id
            """,
            warnings,
            "锁等待兼容视图",
        )
        query_succeeded = query_succeeded or len(warnings) == warning_count

    if strict and not query_succeeded:
        raise RuntimeError("mysql_lock_query_failed")

    for row in rows:
        wait_seconds = _to_int(row.get("waiting_trx_seconds"))
        blocked = _to_int(row.get("blocked_count"))
        row["wait_seconds"] = wait_seconds
        row["duration_label"] = _duration_label(wait_seconds)
        row["risk_level"] = "critical" if wait_seconds >= 300 or blocked >= 5 else ("warning" if wait_seconds > 0 else "normal")
        row["suggestion"] = "确认阻塞源会话业务来源，必要时生成 KILL QUERY 或 KILL SESSION 方案。"
    return rows


def _fetch_metadata_locks(cur, warnings: list[str]) -> list[dict]:
    rows = _safe_query(
        cur,
        """
        SELECT
          ml.OBJECT_TYPE AS object_type,
          ml.OBJECT_SCHEMA AS object_schema,
          ml.OBJECT_NAME AS object_name,
          ml.LOCK_TYPE AS lock_type,
          ml.LOCK_DURATION AS lock_duration,
          ml.LOCK_STATUS AS lock_status,
          ml.OWNER_THREAD_ID AS owner_thread_id,
          t.PROCESSLIST_ID AS process_id,
          t.PROCESSLIST_USER AS user,
          t.PROCESSLIST_HOST AS host,
          t.PROCESSLIST_DB AS db,
          t.PROCESSLIST_TIME AS time_seconds,
          t.PROCESSLIST_STATE AS state,
          t.PROCESSLIST_INFO AS sql_text
        FROM performance_schema.metadata_locks ml
        LEFT JOIN performance_schema.threads t
          ON ml.OWNER_THREAD_ID = t.THREAD_ID
        WHERE COALESCE(ml.OBJECT_SCHEMA, '') NOT IN ('mysql','performance_schema','information_schema','sys')
        ORDER BY FIELD(ml.LOCK_STATUS, 'PENDING', 'GRANTED'), t.PROCESSLIST_TIME DESC
        LIMIT 500
        """,
        warnings,
        "元数据锁",
    )
    for row in rows:
        seconds = _to_int(row.get("time_seconds"))
        pending = str(row.get("lock_status") or "").upper() == "PENDING"
        row["risk_level"] = "critical" if pending and seconds >= 300 else ("warning" if pending else "normal")
        row["duration_label"] = _duration_label(seconds)
    return rows


def _fetch_wait_events(cur, warnings: list[str]) -> list[dict]:
    return _safe_query(
        cur,
        """
        SELECT
          EVENT_NAME AS event_name,
          COUNT_STAR AS count_star,
          ROUND(SUM_TIMER_WAIT / 1000000000000, 6) AS total_seconds,
          ROUND(AVG_TIMER_WAIT / 1000000000000, 6) AS avg_seconds,
          ROUND(MAX_TIMER_WAIT / 1000000000000, 6) AS max_seconds
        FROM performance_schema.events_waits_summary_global_by_event_name
        WHERE EVENT_NAME LIKE %s
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT 50
        """,
        warnings,
        "锁等待事件",
        ("wait/lock/%",),
    )


def _fetch_innodb_status(cur, warnings: list[str]) -> dict:
    rows = _safe_query(cur, "SHOW ENGINE INNODB STATUS", warnings, "InnoDB 状态")
    row = rows[0] if rows else {}
    raw = row.get("Status") or row.get("status") or ""
    match = re.search(r"LATEST DETECTED DEADLOCK\s*-+\s*(.*?)(?:\n-{10,}\n[A-Z ]+\n-{10,}|\Z)", raw, re.S)
    latest_deadlock = match.group(1).strip()[:8000] if match else ""
    history = ""
    history_match = re.search(r"History list length\s+(\d+)", raw)
    if history_match:
        history = history_match.group(1)
    return {
        "raw": raw,
        "deadlock_detected": bool(latest_deadlock),
        "latest_deadlock": latest_deadlock,
        "history_list_length": _to_int(history),
        "deadlock_summary": _parse_deadlock(latest_deadlock),
    }


def _extract_deadlock_sql(block: str) -> str:
    lines = [line.strip() for line in block.splitlines()]
    start = 0
    for idx, line in enumerate(lines):
        if "MySQL thread id" in line:
            start = idx + 1
            break
    candidates = []
    skip_prefixes = (
        "***",
        "TRANSACTION",
        "mysql tables in use",
        "LOCK WAIT",
        "TABLE LOCK",
        "RECORD LOCKS",
        "PHYSICAL RECORD",
        "Record lock",
        "Trx read view",
    )
    for line in lines[start:]:
        if not line or line.startswith(skip_prefixes):
            continue
        if line.startswith(("---", "-------")):
            continue
        candidates.append(line)
    return candidates[-1] if candidates else ""


def _parse_lock_lines(block: str, status: str) -> list[dict]:
    items = []
    for line in block.splitlines():
        if "table `" not in line:
            continue
        table_match = re.search(r"table `([^`]+)`.`([^`]+)`", line)
        if not table_match:
            continue
        index_match = re.search(r"index `([^`]+)`", line)
        mode_match = re.search(r"lock mode ([A-Z,\s]+?)(?:\s|$)", line)
        lock_type = "RECORD" if "RECORD LOCKS" in line else "TABLE" if "TABLE LOCK" in line else "UNKNOWN"
        lower = line.lower()
        scope = "Next-Key / Gap Lock" if "gap" in lower else "Insert Intention" if "insert intention" in lower else "Record Lock" if lock_type == "RECORD" else "Table Lock"
        items.append(
            {
                "table": f"{table_match.group(1)}.{table_match.group(2)}",
                "object_schema": table_match.group(1),
                "object_name": table_match.group(2),
                "index": index_match.group(1) if index_match else "",
                "lock_mode": (mode_match.group(1).strip() if mode_match else ""),
                "lock_type": lock_type,
                "lock_scope": scope,
                "status": status,
                "raw": line.strip(),
            }
        )
    return items


def _transaction_sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"\*\*\* \((\d+)\) TRANSACTION:", text))
    sections = {}
    for idx, match in enumerate(matches):
        trx_no = match.group(1)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[trx_no] = text[match.start():end]
    return sections


def _extract_lock_block(text: str, trx_no: str, marker: str) -> str:
    pattern = rf"\*\*\* \({re.escape(trx_no)}\) {marker}:(.*?)(?=\n\*\*\* \(|\Z)"
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else ""


def _classify_deadlock(transactions: list[dict], lock_objects: list[dict], text: str) -> str:
    lower = text.lower()
    indexes = {str(item.get("index") or "").upper() for item in lock_objects if item.get("index")}
    if "foreign key" in lower:
        return "foreign_key"
    if "insert intention" in lower or "duplicate" in lower:
        return "insert_duplicate"
    if "gap" in lower or "next-key" in lower:
        return "gap_lock"
    if any(not idx or idx != "PRIMARY" for idx in indexes):
        return "missing_index"
    sql_text = " ".join(str(t.get("query") or "").upper() for t in transactions)
    if any(word in sql_text for word in ("UPDATE", "DELETE", "INSERT")) and len(transactions) >= 2:
        tables_by_trx = []
        for trx in transactions:
            tables = {item.get("table") for item in (trx.get("holds") or []) + (trx.get("waits_for") or []) if item.get("table")}
            tables_by_trx.append(tables)
        if len(tables_by_trx) >= 2 and tables_by_trx[0] and tables_by_trx[1] and tables_by_trx[0] & tables_by_trx[1]:
            return "cross_update"
        return "batch_update"
    return "unknown"


def _deadlock_analysis(deadlock_type: str) -> dict:
    mapping = {
        "cross_update": {
            "root_cause": "疑似多个事务以不同顺序更新相同业务对象，形成循环等待。",
            "suggestions": ["统一事务内表访问顺序", "缩短事务持有时间", "应用层捕获 Deadlock 错误并重试", "必要时把批量更新拆成小批次提交"],
        },
        "missing_index": {
            "root_cause": "疑似 UPDATE/DELETE/JOIN 条件未命中合适索引，导致锁范围扩大。",
            "suggestions": ["检查 WHERE / JOIN 条件是否命中高选择性索引", "评估补充组合索引", "收集表和索引统计信息", "避免大范围更新长时间持锁"],
        },
        "gap_lock": {
            "root_cause": "检测到 Gap Lock / Next-Key Lock，可能与范围查询、非唯一索引或 REPEATABLE READ 隔离级别有关。",
            "suggestions": ["检查范围查询条件和索引选择性", "优先使用唯一键等值更新", "评估是否可在业务允许时调整隔离级别", "减少并发范围更新或插入冲突"],
        },
        "insert_duplicate": {
            "root_cause": "疑似并发插入唯一键或插入意向锁冲突。",
            "suggestions": ["调整并发插入逻辑", "使用幂等写入策略", "检查唯一键设计和冲突重试逻辑", "必要时按业务键分片或排队写入"],
        },
        "batch_update": {
            "root_cause": "疑似批量更新事务持锁时间过长，引发并发等待和死锁。",
            "suggestions": ["拆分批量更新并分批提交", "控制单批影响行数", "降低高峰期并发更新", "确保过滤条件命中索引"],
        },
        "foreign_key": {
            "root_cause": "疑似外键检查或父子表更新顺序导致锁等待。",
            "suggestions": ["统一父子表更新顺序", "检查外键列索引", "减少长事务内跨表更新", "评估外键约束对高并发写入的影响"],
        },
        "unknown": {
            "root_cause": "当前死锁日志格式未能明确归类，需要结合原始日志和业务上下文人工确认。",
            "suggestions": ["保留原始死锁日志", "开启 innodb_print_all_deadlocks 采集历史死锁", "结合应用日志定位业务入口", "检查事务访问顺序、索引和批量更新"],
        },
    }
    impact = [
        "InnoDB 已自动回滚其中一个事务以打破死锁。",
        "业务可能收到 Deadlock found when trying to get lock 或 1213 错误。",
        "如果应用没有捕获并重试，相关业务操作可能失败。",
    ]
    result = mapping.get(deadlock_type, mapping["unknown"]).copy()
    result["impact"] = impact
    return result


def _parse_deadlock(text: str) -> dict:
    if not text:
        return {}
    warnings = []
    sections = _transaction_sections(text)
    rollback_match = re.search(r"WE ROLL BACK TRANSACTION \((\d+)\)", text)
    victim = f"transaction_{rollback_match.group(1)}" if rollback_match else ""
    time_match = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", text)
    if not time_match:
        warnings.append("死锁日志中未解析到发生时间，建议开启 innodb_print_all_deadlocks 并采集 Error Log。")

    transactions = []
    lock_objects = []
    for trx_no, block in sections.items():
        trx_id_match = re.search(r"TRANSACTION\s+([^,\s]+)", block)
        thread_match = re.search(r"MySQL thread id\s+(\d+)", block)
        holds = _parse_lock_lines(_extract_lock_block(text, trx_no, r"HOLDS THE LOCK\(S\)"), "holding")
        waits_for = _parse_lock_lines(_extract_lock_block(text, trx_no, r"WAITING FOR THIS LOCK TO BE GRANTED"), "waiting")
        lock_objects.extend(holds)
        lock_objects.extend(waits_for)
        transactions.append(
            {
                "trx_no": _to_int(trx_no),
                "trx_id": trx_id_match.group(1) if trx_id_match else "",
                "mysql_thread_id": _to_int(thread_match.group(1)) if thread_match else 0,
                "query": _extract_deadlock_sql(block),
                "holds": holds,
                "waits_for": waits_for,
                "status": "被 InnoDB 回滚" if victim == f"transaction_{trx_no}" else "持锁并等待",
            }
        )

    tables = sorted({item.get("table") for item in lock_objects if item.get("table")})
    indexes = sorted({item.get("index") for item in lock_objects if item.get("index")})
    deadlock_type = _classify_deadlock(transactions, lock_objects, text)
    analysis = _deadlock_analysis(deadlock_type)
    primary_object = lock_objects[0] if lock_objects else {}
    summary = {
        "deadlock_detected": True,
        "deadlock_time": time_match.group(1).replace("T", " ") if time_match else "",
        "transaction_count": len(transactions),
        "object_schema": primary_object.get("object_schema", ""),
        "object_name": primary_object.get("object_name", ""),
        "index_name": primary_object.get("index", ""),
        "victim_transaction": victim,
        "deadlock_type": deadlock_type,
        "risk_level": "warning",
    }
    return {
        "summary": summary,
        "transactions": transactions,
        "lock_objects": lock_objects,
        "analysis": analysis,
        "history": {
            "last_24h_count": 0,
            "last_7d_count": 0,
            "top_tables": [],
            "top_sql_fingerprints": [],
            "top_hosts": [],
            "message": "历史死锁需要开启 innodb_print_all_deadlocks，并通过 SSH/Error Log 定时采集后统计。",
        },
        "raw_deadlock_text": text,
        "warnings": warnings,
        "involved_tables": tables,
        "involved_indexes": indexes,
        "transaction_sqls": [trx.get("query") for trx in transactions if trx.get("query")],
        "suggestion": "；".join(analysis.get("suggestions", [])[:3]),
    }


def parse_deadlock_text(text: str) -> dict:
    return _parse_deadlock(text)


def _build_blocking_chains(lock_waits: list[dict], sessions_by_process: dict) -> list[dict]:
    waiters_by_blocker: dict[str, list[dict]] = defaultdict(list)
    waiting_nodes = set()
    blocking_nodes = set()
    for row in lock_waits:
        waiting = str(row.get("waiting_process_id") or row.get("waiting_thread_id") or row.get("waiting_trx_id") or "")
        blocking = str(row.get("blocking_process_id") or row.get("blocking_thread_id") or row.get("blocking_trx_id") or "")
        if not waiting or not blocking:
            continue
        waiters_by_blocker[blocking].append(row)
        waiting_nodes.add(waiting)
        blocking_nodes.add(blocking)

    roots = sorted(blocking_nodes - waiting_nodes) or sorted(blocking_nodes)
    chains = []
    for root in roots:
        rows = waiters_by_blocker.get(root, [])
        if not rows:
            continue
        blocked_count = len(rows)
        max_wait = max(_to_int(r.get("wait_seconds") or r.get("waiting_trx_seconds")) for r in rows)
        session = sessions_by_process.get(str(root), {})
        chain_rows = []
        for row in rows:
            chain_rows.append(
                {
                    "level": 1,
                    "waiting_thread_id": row.get("waiting_process_id") or row.get("waiting_thread_id"),
                    "blocking_thread_id": row.get("blocking_process_id") or row.get("blocking_thread_id"),
                    "object": ".".join([str(v) for v in [row.get("object_schema"), row.get("object_name")] if v]),
                    "index": row.get("index_name"),
                    "waiting_sql": row.get("waiting_sql"),
                    "blocking_sql": row.get("blocking_sql"),
                    "wait_seconds": row.get("wait_seconds"),
                }
            )
        chains.append(
            {
                "root_blocker_thread_id": root,
                "root_blocker_user": session.get("user"),
                "root_blocker_host": session.get("host"),
                "blocked_count": blocked_count,
                "max_wait_seconds": max_wait,
                "duration_label": _duration_label(max_wait),
                "chain": chain_rows,
                "risk_level": "critical" if blocked_count >= 5 or max_wait >= 300 else ("warning" if blocked_count >= 3 or max_wait > 0 else "normal"),
                "suggestion": "优先确认根阻塞会话的 SQL、用户和来源主机，必要时生成安全处理方案。",
            }
        )
    return chains


def _build_risk_items(summary: dict, transactions: list[dict], lock_waits: list[dict], chains: list[dict], mdl: list[dict], innodb: dict, row_stats: dict) -> list[dict]:
    risks = []

    def add(level: str, title: str, message: str, suggestion: str, evidence=None):
        risks.append({"level": level, "title": title, "item": title, "message": message, "suggestion": suggestion, "evidence": evidence or {}})

    if summary["lock_wait_count"] > 0:
        add("critical" if summary["lock_wait_count"] > 5 else "warning", "当前存在锁等待", f"当前锁等待 {summary['lock_wait_count']} 个。", "查看阻塞链路，优先处理根阻塞会话。")
    if summary["long_transaction_count"] > 0:
        add("critical" if summary["max_trx_seconds"] > 1800 else "warning", "存在长事务", f"最长事务 {summary['max_trx_seconds']} 秒。", "确认事务来源，避免 Undo/Purge 堆积和 DDL 阻塞。")
    if summary["root_blocker_count"] > 0:
        add("critical" if any(c.get("risk_level") == "critical" for c in chains) else "warning", "存在根阻塞会话", f"识别到 {summary['root_blocker_count']} 个根阻塞会话。", "先确认根阻塞 SQL，必要时生成 KILL QUERY/SESSION 方案。")
    pending_mdl = [row for row in mdl if str(row.get("lock_status") or "").upper() == "PENDING"]
    if pending_mdl:
        add("critical" if any(_to_int(row.get("time_seconds")) >= 300 for row in pending_mdl) else "warning", "存在 MDL 等待", f"当前 MDL 等待 {len(pending_mdl)} 个。", "检查是否有 DDL 被长事务或查询阻塞。")
    if innodb.get("deadlock_detected"):
        add("warning", "最近发生死锁", "InnoDB 状态中存在最近死锁记录。", "统一更新顺序，检查索引命中情况，缩短事务持有时间。")
    if _to_int(row_stats.get("Innodb_row_lock_current_waits")) > 0:
        waits = _to_int(row_stats.get("Innodb_row_lock_current_waits"))
        add("critical" if waits > 5 else "warning", "当前行锁等待", f"Innodb_row_lock_current_waits={waits}。", "结合锁等待明细定位热点对象和阻塞源。")
    if _to_int(row_stats.get("Innodb_row_lock_time_avg")) > 1000:
        avg = _to_int(row_stats.get("Innodb_row_lock_time_avg"))
        add("critical" if avg > 5000 else "warning", "行锁平均等待较高", f"Innodb_row_lock_time_avg={avg} ms。", "检查热点行更新、索引条件和事务提交频率。")
    if not risks:
        add("normal", "当前未发现明显锁事务风险", "没有发现当前锁等待、长事务、MDL 等待或最近死锁风险。", "继续观察慢 SQL、连接数和 InnoDB 状态趋势。")
    return risks


def _enrich_transactions(transactions: list[dict], sessions_by_process: dict, blocking_processes: set, waiting_processes: set) -> None:
    for row in transactions:
        process_id = str(row.get("trx_mysql_thread_id") or "")
        session = sessions_by_process.get(process_id, {})
        row["user"] = session.get("user")
        row["host"] = session.get("host")
        row["db"] = session.get("db")
        row["command"] = session.get("command")
        row["state"] = session.get("state")
        row["is_blocker"] = process_id in blocking_processes
        row["is_waiting"] = process_id in waiting_processes
        row["impact"] = []
        if row.get("risk_level") in ("warning", "critical"):
            row["impact"] = ["可能影响 Undo 清理", "可能阻塞其它 DML 或 DDL", "可能扩大复制延迟"]
        row["suggestion"] = "确认业务是否仍在执行，必要时生成 KILL QUERY 或 KILL SESSION 方案。" if row.get("risk_level") != "normal" else "持续观察。"


def get_lock_transaction_center(instance: DBInstance, *, strict_lock_waits: bool = False) -> dict:
    warnings: list[str] = []
    raw = {}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                sessions = _fetch_sessions(cur, warnings)
                transactions = _fetch_transactions(cur, warnings)
                lock_waits = _fetch_lock_waits(cur, warnings, strict=strict_lock_waits)
                metadata_locks = _fetch_metadata_locks(cur, warnings)
                wait_events = _fetch_wait_events(cur, warnings)
                row_lock_stats = _safe_status(
                    cur,
                    [
                        "Innodb_row_lock_current_waits",
                        "Innodb_row_lock_time",
                        "Innodb_row_lock_time_avg",
                        "Innodb_row_lock_time_max",
                        "Innodb_row_lock_waits",
                    ],
                    warnings,
                    "行锁统计",
                )
                table_lock_stats = _safe_status(cur, ["Table_locks_immediate", "Table_locks_waited"], warnings, "表锁统计")
                innodb = _fetch_innodb_status(cur, warnings)

        sessions_by_process = {str(row.get("process_id")): row for row in sessions if row.get("process_id") is not None}
        waiting_processes = {str(row.get("waiting_process_id") or row.get("waiting_thread_id")) for row in lock_waits if row.get("waiting_process_id") or row.get("waiting_thread_id")}
        blocking_processes = {str(row.get("blocking_process_id") or row.get("blocking_thread_id")) for row in lock_waits if row.get("blocking_process_id") or row.get("blocking_thread_id")}
        _enrich_transactions(transactions, sessions_by_process, blocking_processes, waiting_processes)
        long_transactions = [row for row in transactions if _to_int(row.get("trx_seconds")) > 300]
        blocking_chains = _build_blocking_chains(lock_waits, sessions_by_process)

        object_counts = Counter(
            ".".join([str(v) for v in [row.get("object_schema"), row.get("object_name")] if v])
            for row in lock_waits + metadata_locks
            if row.get("object_schema") and row.get("object_name")
        )
        hot_objects = [{"object_name": name, "wait_count": count} for name, count in object_counts.most_common(30)]
        active_sessions = [row for row in sessions if str(row.get("command") or "").upper() not in ("SLEEP", "BINLOG DUMP")]
        max_trx = max((_to_int(row.get("trx_seconds")) for row in transactions), default=0)
        max_lock_wait = max((_to_int(row.get("wait_seconds") or row.get("waiting_trx_seconds")) for row in lock_waits), default=0)
        metadata_lock_wait_count = sum(1 for row in metadata_locks if str(row.get("lock_status") or "").upper() == "PENDING")
        summary = {
            "risk_level": "normal",
            "session_count": len(sessions),
            "active_session_count": len(active_sessions),
            "transaction_count": len(transactions),
            "long_transaction_count": len(long_transactions),
            "lock_wait_count": len(lock_waits),
            "blocking_session_count": len(blocking_processes),
            "metadata_lock_wait_count": metadata_lock_wait_count,
            "deadlock_detected": bool(innodb.get("deadlock_detected")),
            "max_trx_seconds": max_trx,
            "max_trx_label": _duration_label(max_trx),
            "max_lock_wait_seconds": max_lock_wait,
            "root_blocker_count": len(blocking_chains),
            "history_list_length": innodb.get("history_list_length", 0),
        }
        risk_items = _build_risk_items(summary, transactions, lock_waits, blocking_chains, metadata_locks, innodb, row_lock_stats)
        summary["risk_level"] = "critical" if any(r["level"] == "critical" for r in risk_items) else ("warning" if any(r["level"] == "warning" for r in risk_items) else "normal")
        raw.update({"processlist": sessions, "innodb_trx": transactions, "innodb_status": innodb.get("raw", "")})
        return {
            "status": "ok",
            "instance": {"id": instance.id, "name": instance.instance_name, "host": instance.host, "port": instance.port},
            "summary": summary,
            "sections": {
                "sessions": sessions,
                "transactions": transactions,
                "long_transactions": long_transactions,
                "lock_waits": lock_waits,
                "blocking_chains": blocking_chains,
                "metadata_locks": metadata_locks,
                "deadlocks": [innodb.get("deadlock_summary")] if innodb.get("deadlock_detected") else [],
                "row_lock_stats": row_lock_stats,
                "table_lock_stats": table_lock_stats,
                "wait_events": wait_events,
                "hot_objects": hot_objects,
            },
            "risk_items": risk_items,
            "actions": {"kill_query_plan_enabled": True, "kill_session_plan_enabled": True},
            "warnings": warnings,
            "raw": raw,
            "items": transactions,
        }
    except Exception as exc:
        logger.exception("get_lock_transaction_center failed")
        return {
            "status": "error",
            "error": str(exc),
            "summary": {},
            "sections": {},
            "risk_items": [],
            "warnings": warnings,
            "items": [],
        }


def get_transactions(instance: DBInstance) -> list:
    return get_lock_transaction_center(instance).get("sections", {}).get("transactions", [])


def get_lock_waits(instance: DBInstance) -> list:
    return get_lock_transaction_center(instance).get("sections", {}).get("lock_waits", [])


def generate_kill_plan(instance: DBInstance, thread_id: int, action: str = "kill_session") -> dict:
    thread_id = _to_int(thread_id)
    if thread_id <= 0:
        return {"status": "error", "message": "无效的线程 ID", "script": ""}
    if action == "kill_query":
        sql = f"KILL QUERY {thread_id};"
        message = "已生成 KILL QUERY 方案，只终止当前 SQL，不断开连接。"
        warnings = ["如果 SQL 属于未提交事务，事务本身可能仍然存在，请继续观察事务状态。"]
    else:
        sql = f"KILL {thread_id};"
        message = "已生成 KILL SESSION 方案，会断开整个会话。"
        warnings = ["如果该会话存在未提交事务，KILL 后会触发回滚。", "大事务回滚可能持续较长时间，执行前请确认业务来源和影响范围。"]
    return {
        "status": "plan_only",
        "action": action,
        "message": message,
        "sql": sql,
        "script": sql,
        "warnings": warnings,
    }
