import logging
import re
import shlex
from datetime import datetime

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_basic")

SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")

MYSQL_VARIABLE_KEYS = (
    "version",
    "version_comment",
    "version_compile_os",
    "version_compile_machine",
    "protocol_version",
    "mysqlx_port",
    "basedir",
    "datadir",
    "tmpdir",
    "plugin_dir",
    "secure_file_priv",
    "socket",
    "pid_file",
    "log_error",
    "slow_query_log",
    "slow_query_log_file",
    "general_log_file",
    "long_query_time",
    "log_queries_not_using_indexes",
    "max_connections",
    "connect_timeout",
    "wait_timeout",
    "interactive_timeout",
    "open_files_limit",
    "max_allowed_packet",
    "table_open_cache",
    "table_definition_cache",
    "thread_cache_size",
    "tmp_table_size",
    "max_heap_table_size",
    "sort_buffer_size",
    "join_buffer_size",
    "read_buffer_size",
    "read_rnd_buffer_size",
    "character_set_server",
    "collation_server",
    "character_set_database",
    "collation_database",
    "character_set_client",
    "character_set_connection",
    "character_set_results",
    "lower_case_table_names",
    "sql_mode",
    "time_zone",
    "system_time_zone",
    "explicit_defaults_for_timestamp",
    "log_bin",
    "log_bin_basename",
    "log_bin_index",
    "binlog_format",
    "sync_binlog",
    "binlog_row_image",
    "binlog_expire_logs_seconds",
    "expire_logs_days",
    "gtid_mode",
    "enforce_gtid_consistency",
    "gtid_executed",
    "gtid_purged",
    "binlog_do_db",
    "binlog_ignore_db",
    "relay_log",
    "relay_log_index",
    "default_storage_engine",
    "innodb_version",
    "innodb_page_size",
    "innodb_buffer_pool_size",
    "innodb_buffer_pool_chunk_size",
    "innodb_buffer_pool_instances",
    "innodb_log_file_size",
    "innodb_log_files_in_group",
    "innodb_flush_log_at_trx_commit",
    "innodb_flush_method",
    "innodb_file_per_table",
    "innodb_data_file_path",
    "innodb_temp_data_file_path",
    "innodb_undo_directory",
    "innodb_undo_tablespaces",
    "innodb_max_dirty_pages_pct",
    "innodb_io_capacity",
    "innodb_io_capacity_max",
    "innodb_read_io_threads",
    "innodb_write_io_threads",
    "innodb_thread_concurrency",
    "innodb_lock_wait_timeout",
    "innodb_deadlock_detect",
    "performance_schema",
    "event_scheduler",
    "have_ssl",
    "ssl_ca",
    "ssl_cert",
    "ssl_key",
    "require_secure_transport",
    "default_authentication_plugin",
    "group_replication_group_name",
    "group_replication_single_primary_mode",
    "group_replication_start_on_boot",
    "read_only",
    "super_read_only",
    "server_id",
    "server_uuid",
    "hostname",
    "port",
)

MYSQL_STATUS_KEYS = (
    "Uptime",
    "Questions",
    "Queries",
    "Connections",
    "Threads_connected",
    "Threads_running",
    "Threads_cached",
    "Threads_created",
    "Max_used_connections",
    "Aborted_connects",
    "Aborted_clients",
    "Com_commit",
    "Com_rollback",
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
    "Innodb_row_lock_waits",
    "Innodb_row_lock_time",
    "Innodb_row_lock_time_avg",
    "Innodb_log_waits",
    "Innodb_os_log_written",
    "Innodb_data_reads",
    "Innodb_data_writes",
    "Innodb_data_fsyncs",
    "Open_tables",
    "Opened_tables",
    "Open_files",
    "Opened_files",
    "Table_open_cache_hits",
    "Table_open_cache_misses",
    "Table_open_cache_overflows",
    "Created_tmp_tables",
    "Created_tmp_disk_tables",
    "Created_tmp_files",
    "Sort_merge_passes",
    "Sort_rows",
    "Select_full_join",
    "Select_full_range_join",
    "Select_scan",
    "Handler_read_rnd_next",
    "Slow_queries",
)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _ratio(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part * 100 / total, 2)


def _add_risk(risks: list[dict], level: str, item: str, message: str, suggestion: str, evidence: dict | None = None):
    risks.append(
        {
            "level": level,
            "item": item,
            "message": message,
            "suggestion": suggestion,
            "evidence": evidence or {},
        }
    )


def _get_row_value(row: dict | None, key: str = "Value", default: str = "") -> str:
    if not row:
        return default
    return row.get(key) or row.get(key.lower()) or row.get(key.upper()) or default


def _load_all_variables(cur) -> dict:
    try:
        cur.execute("SHOW GLOBAL VARIABLES")
        rows = cur.fetchall()
        return {
            str(row.get("variable_name") or row.get("Variable_name") or row.get("VARIABLE_NAME") or "").lower():
            str(row.get("value") if row.get("value") is not None else row.get("Value") if row.get("Value") is not None else row.get("VARIABLE_VALUE") or "")
            for row in rows
        }
    except Exception as exc:
        logger.warning("load mysql variables failed: %s", exc)
        return {}


def _load_all_status(cur) -> dict:
    try:
        cur.execute("SHOW GLOBAL STATUS")
        rows = cur.fetchall()
        return {
            str(row.get("variable_name") or row.get("Variable_name") or row.get("VARIABLE_NAME") or "").lower():
            str(row.get("value") if row.get("value") is not None else row.get("Value") if row.get("Value") is not None else row.get("VARIABLE_VALUE") or "")
            for row in rows
        }
    except Exception as exc:
        logger.warning("load mysql status failed: %s", exc)
        return {}


def _fetch_one(cur, sql: str, params=None, default: int = 0) -> int:
    try:
        cur.execute(sql, params or ())
        row = cur.fetchone() or {}
        if not row:
            return default
        return _to_int(next(iter(row.values())), default)
    except Exception:
        return default


def _fetch_replica_rows(cur) -> list[dict]:
    for sql in ["SHOW REPLICA STATUS", "SHOW SLAVE STATUS"]:
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            if rows:
                return rows
        except Exception:
            continue
    return []


def _fetch_mgr_members(cur) -> list[dict]:
    try:
        cur.execute(
            """
            SELECT MEMBER_ID, MEMBER_HOST, MEMBER_PORT, MEMBER_STATE, MEMBER_ROLE
            FROM performance_schema.replication_group_members
            ORDER BY MEMBER_HOST, MEMBER_PORT
            """
        )
        return cur.fetchall()
    except Exception:
        return []


def _check_error_log_readable(instance: DBInstance, log_error: str) -> dict:
    if not log_error:
        return {"checked": False, "readable": False, "reason": "\u672a\u914d\u7f6e log_error"}
    if not instance.ssh_host:
        return {"checked": False, "readable": False, "reason": "\u672a\u914d\u7f6e SSH\uff0c\u65e0\u6cd5\u9a8c\u8bc1\u6587\u4ef6\u53ef\u8bfb\u6027"}
    try:
        from app.services.ssh_executor import execute_command

        is_windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", log_error))
        if is_windows_path:
            ps_path = "'" + log_error.replace("'", "''") + "'"
            command = f"powershell -NoProfile -Command \"if (Test-Path -LiteralPath {ps_path}) {{ 'READABLE' }} else {{ 'UNREADABLE' }}\""
        else:
            command = f"test -r {shlex.quote(log_error)} && echo READABLE || echo UNREADABLE"
        result = execute_command(instance, command, timeout=15)
        output = (result.get("stdout") or result.get("output") or "").upper()
        return {"checked": True, "readable": "READABLE" in output and "UNREADABLE" not in output, "reason": output.strip()}
    except Exception as exc:
        return {"checked": True, "readable": False, "reason": str(exc)}


def _build_risks(instance: DBInstance, basic: dict, status: dict, cur) -> tuple[list[dict], dict]:
    risks: list[dict] = []
    checks: dict = {}

    max_connections = _to_int(basic.get("max_connections"))
    threads_connected = _to_int(status.get("threads_connected"))
    max_used_connections = _to_int(status.get("max_used_connections"))
    conn_pct = _ratio(threads_connected, max_connections)
    max_used_pct = _ratio(max_used_connections, max_connections)
    checks["connection_usage_pct"] = conn_pct
    checks["max_used_connection_pct"] = max_used_pct
    if conn_pct >= 80:
        _add_risk(risks, "critical" if conn_pct >= 90 else "warning", "\u8fde\u63a5\u6570\u4f7f\u7528\u7387", f"\u5f53\u524d\u8fde\u63a5\u6570\u4f7f\u7528\u7387 {conn_pct}%", "\u68c0\u67e5\u8fde\u63a5\u6c60\u3001\u6162 SQL \u548c\u5e94\u7528\u8fde\u63a5\u91ca\u653e\uff0c\u5fc5\u8981\u65f6\u8c03\u6574 max_connections\u3002")
    if max_used_pct >= 80:
        _add_risk(risks, "critical" if max_used_pct >= 90 else "warning", "Max_used_connections", f"Max_used_connections \u5df2\u8fbe\u5230 max_connections \u7684 {max_used_pct}%", "\u8fde\u63a5\u5cf0\u503c\u63a5\u8fd1\u4e0a\u9650\uff0c\u9700\u8bc4\u4f30\u8fde\u63a5\u6c60\u4e0e max_connections \u5bb9\u91cf\u3002")

    if str(basic.get("log_bin", "")).upper() not in ("ON", "1", "YES"):
        _add_risk(risks, "high", "Binlog", "Binlog \u672a\u5f00\u542f", "\u5efa\u8bae\u5f00\u542f log_bin\uff0c\u5426\u5219\u590d\u5236\u3001\u70b9\u65f6\u6062\u590d\u548c\u5ba1\u8ba1\u80fd\u529b\u4f1a\u53d7\u9650\u3002")
    if str(basic.get("gtid_mode", "")).upper() != "ON":
        _add_risk(risks, "warning", "GTID", "GTID \u672a\u5f00\u542f", "\u5efa\u8bae\u5728\u590d\u5236\u6216\u9ad8\u53ef\u7528\u573a\u666f\u542f\u7528 GTID\uff0c\u964d\u4f4e\u5207\u6362\u548c\u6062\u590d\u590d\u6742\u5ea6\u3002")
    if str(basic.get("sync_binlog", "")) == "0":
        _add_risk(risks, "warning", "sync_binlog", "sync_binlog=0\uff0cBinlog \u6301\u4e45\u5316\u98ce\u9669\u8f83\u9ad8", "\u751f\u4ea7\u5e93\u5efa\u8bae\u8bc4\u4f30\u8bbe\u7f6e sync_binlog=1\uff0c\u5728\u6027\u80fd\u548c\u5b89\u5168\u95f4\u53d6\u5e73\u8861\u3002")
    if str(basic.get("innodb_flush_log_at_trx_commit", "")) != "1":
        _add_risk(risks, "high", "innodb_flush_log_at_trx_commit", "innodb_flush_log_at_trx_commit \u975e 1", "\u53ef\u80fd\u5728\u5b95\u673a\u65f6\u4e22\u5931\u5df2\u63d0\u4ea4\u4e8b\u52a1\uff0c\u6838\u5fc3\u751f\u4ea7\u5e93\u5efa\u8bae\u8bbe\u7f6e\u4e3a 1\u3002")

    read_requests = _to_int(status.get("innodb_buffer_pool_read_requests"))
    disk_reads = _to_int(status.get("innodb_buffer_pool_reads"))
    hit_pct = round((1 - disk_reads / read_requests) * 100, 4) if read_requests > 0 else 100.0
    checks["buffer_pool_hit_pct"] = hit_pct
    if hit_pct < 99:
        _add_risk(risks, "warning", "Buffer Pool \u547d\u4e2d\u7387", f"Buffer Pool \u547d\u4e2d\u7387 {hit_pct}%", "\u68c0\u67e5\u70ed\u6570\u636e\u96c6\u3001\u6162 SQL\u3001\u7d22\u5f15\u548c innodb_buffer_pool_size\u3002")

    tmp_tables = _to_int(status.get("created_tmp_tables"))
    tmp_disk = _to_int(status.get("created_tmp_disk_tables"))
    tmp_disk_pct = _ratio(tmp_disk, tmp_tables)
    checks["tmp_disk_table_pct"] = tmp_disk_pct
    if tmp_tables > 0 and tmp_disk_pct >= 25:
        _add_risk(risks, "warning", "\u4e34\u65f6\u78c1\u76d8\u8868", f"Created_tmp_disk_tables \u5360\u6bd4 {tmp_disk_pct}%", "\u68c0\u67e5\u6392\u5e8f\u3001GROUP BY\u3001\u5927\u5b57\u6bb5\u4e34\u65f6\u8868\uff0c\u8bc4\u4f30 tmp_table_size/max_heap_table_size\u3002")
    if _to_int(status.get("select_full_join")) > 0:
        _add_risk(risks, "warning", "Select_full_join", "Select_full_join \u5df2\u51fa\u73b0\u589e\u957f", "\u5b58\u5728\u65e0\u7d22\u5f15 JOIN \u98ce\u9669\uff0c\u5efa\u8bae\u7ed3\u5408\u6162 SQL \u548c\u8868\u8bbf\u95ee\u753b\u50cf\u68c0\u67e5 JOIN \u6761\u4ef6\u7d22\u5f15\u3002")

    root_wildcard = _fetch_one(cur, "SELECT COUNT(*) AS cnt FROM mysql.user WHERE user='root' AND host='%'")
    anonymous_users = _fetch_one(cur, "SELECT COUNT(*) AS cnt FROM mysql.user WHERE user=''")
    checks["root_wildcard_count"] = root_wildcard
    checks["anonymous_user_count"] = anonymous_users
    if root_wildcard > 0:
        _add_risk(risks, "critical", "root@%", "\u5b58\u5728 root@% \u8d26\u53f7", "\u7acb\u5373\u6536\u655b root \u8fdc\u7a0b\u767b\u5f55\u6765\u6e90\uff0c\u6539\u7528\u6700\u5c0f\u6743\u9650\u8fd0\u7ef4\u8d26\u53f7\u3002")
    if anonymous_users > 0:
        _add_risk(risks, "critical", "\u533f\u540d\u7528\u6237", "\u5b58\u5728\u533f\u540d\u7528\u6237", "\u5efa\u8bae\u5220\u9664\u533f\u540d\u7528\u6237\u5e76\u5237\u65b0\u6743\u9650\u3002")

    if str(basic.get("slow_query_log", "")).upper() not in ("ON", "1", "YES"):
        _add_risk(risks, "warning", "slow_query_log", "\u6162\u67e5\u8be2\u65e5\u5fd7\u672a\u5f00\u542f", "\u5efa\u8bae\u5f00\u542f slow_query_log\uff0c\u4fbf\u4e8e\u5b9a\u4f4d\u6162 SQL \u548c\u7d22\u5f15\u95ee\u9898\u3002")
    if str(basic.get("performance_schema", "")).upper() not in ("ON", "1", "YES"):
        _add_risk(risks, "high", "performance_schema", "performance_schema \u672a\u5f00\u542f", "\u5f88\u591a\u9501\u3001SQL\u3001MGR \u548c\u6027\u80fd\u8bca\u65ad\u4f9d\u8d56 performance_schema\uff0c\u5efa\u8bae\u7ef4\u62a4\u7a97\u53e3\u542f\u7528\u3002")
    if str(basic.get("lower_case_table_names", "")) != "1":
        _add_risk(risks, "info", "lower_case_table_names", "lower_case_table_names \u4e0e\u5e38\u89c4\u751f\u4ea7\u6807\u51c6 1 \u4e0d\u4e00\u81f4", "\u786e\u8ba4\u5ba2\u6237\u6807\u51c6\u503c\uff0c\u8de8 Windows/Linux \u8fc1\u79fb\u65f6\u8981\u7279\u522b\u6ce8\u610f\u8868\u540d\u5927\u5c0f\u5199\u3002")
    if str(basic.get("character_set_server", "")).lower() != "utf8mb4":
        _add_risk(risks, "warning", "\u5b57\u7b26\u96c6", f"\u5f53\u524d\u5b57\u7b26\u96c6\u4e3a {basic.get('character_set_server') or '-'}", "\u5efa\u8bae\u65b0\u7cfb\u7edf\u7edf\u4e00\u4f7f\u7528 utf8mb4\uff0c\u907f\u514d\u4e2d\u6587\u3001emoji \u548c\u591a\u8bed\u79cd\u6570\u636e\u95ee\u9898\u3002")

    non_innodb = _fetch_one(cur, "SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema NOT IN %s AND table_type='BASE TABLE' AND IFNULL(engine,'') <> 'InnoDB'", (SYSTEM_SCHEMAS,))
    no_pk = _fetch_one(cur, "SELECT COUNT(*) AS cnt FROM information_schema.tables t LEFT JOIN information_schema.table_constraints c ON t.table_schema=c.table_schema AND t.table_name=c.table_name AND c.constraint_type='PRIMARY KEY' WHERE t.table_schema NOT IN %s AND t.table_type='BASE TABLE' AND c.constraint_name IS NULL", (SYSTEM_SCHEMAS,))
    large_no_pk = _fetch_one(cur, "SELECT COUNT(*) AS cnt FROM information_schema.tables t LEFT JOIN information_schema.table_constraints c ON t.table_schema=c.table_schema AND t.table_name=c.table_name AND c.constraint_type='PRIMARY KEY' WHERE t.table_schema NOT IN %s AND t.table_type='BASE TABLE' AND c.constraint_name IS NULL AND IFNULL(t.table_rows,0) >= 1000000", (SYSTEM_SCHEMAS,))
    checks["non_innodb_table_count"] = non_innodb
    checks["no_primary_key_table_count"] = no_pk
    checks["large_no_primary_key_table_count"] = large_no_pk
    if non_innodb > 0:
        _add_risk(risks, "warning", "\u975e InnoDB \u8868", f"\u5b58\u5728 {non_innodb} \u5f20\u975e InnoDB \u8868", "\u68c0\u67e5 MyISAM/CSV/MEMORY \u7b49\u8868\uff0c\u6838\u5fc3\u4e1a\u52a1\u8868\u5efa\u8bae\u7edf\u4e00 InnoDB\u3002")
    if no_pk > 0:
        _add_risk(risks, "warning", "\u65e0\u4e3b\u952e\u8868", f"\u5b58\u5728 {no_pk} \u5f20\u65e0\u4e3b\u952e\u8868", "\u65e0\u4e3b\u952e\u8868\u4f1a\u5f71\u54cd\u590d\u5236\u3001\u884c\u5b9a\u4f4d\u548c\u6e05\u7406\u6548\u7387\uff0c\u5efa\u8bae\u8865\u5145\u4e1a\u52a1\u4e3b\u952e\u6216\u4ee3\u7406\u4e3b\u952e\u3002")
    if large_no_pk > 0:
        _add_risk(risks, "high", "\u5927\u8868\u65e0\u4e3b\u952e", f"\u5b58\u5728 {large_no_pk} \u5f20\u767e\u4e07\u884c\u4ee5\u4e0a\u65e0\u4e3b\u952e\u5927\u8868", "\u4f18\u5148\u6cbb\u7406\u5927\u8868\u4e3b\u952e\uff0c\u5426\u5219\u5f52\u6863\u3001\u590d\u5236\u548c\u6570\u636e\u6821\u9a8c\u98ce\u9669\u8f83\u9ad8\u3002")

    replicas = _fetch_replica_rows(cur)
    checks["replicas"] = replicas
    for replica in replicas:
        io_running = replica.get("Replica_IO_Running") or replica.get("Slave_IO_Running")
        sql_running = replica.get("Replica_SQL_Running") or replica.get("Slave_SQL_Running")
        lag = _to_int(replica.get("Seconds_Behind_Source") or replica.get("Seconds_Behind_Master"))
        relay_space = _to_int(replica.get("Relay_Log_Space"))
        if str(io_running).lower() != "yes" or str(sql_running).lower() != "yes":
            _add_risk(risks, "critical", "\u590d\u5236\u7ebf\u7a0b", "\u590d\u5236 IO/SQL \u7ebf\u7a0b\u5f02\u5e38", "\u68c0\u67e5 Last_IO_Error/Last_SQL_Error\uff0c\u786e\u8ba4\u4e3b\u5e93\u8fde\u901a\u6027\u3001\u4f4d\u70b9\u548c\u6743\u9650\u3002")
        if relay_space >= 1024 * 1024 * 1024 or lag >= 300:
            _add_risk(risks, "warning", "Relay Log", f"Relay Log \u53ef\u80fd\u5806\u79ef\uff0cRelay_Log_Space={relay_space}\uff0c\u5ef6\u8fdf={lag}s", "\u68c0\u67e5 SQL \u7ebf\u7a0b\u5e94\u7528\u901f\u5ea6\u3001\u5927\u4e8b\u52a1\u548c\u4ece\u5e93 I/O \u6027\u80fd\u3002")

    mgr_members = _fetch_mgr_members(cur)
    checks["mgr_members"] = mgr_members
    mgr_configured = bool(basic.get("group_replication_group_name") or mgr_members)
    if mgr_configured:
        offline = [m for m in mgr_members if str(m.get("MEMBER_STATE", "")).upper() != "ONLINE"]
        if offline:
            _add_risk(risks, "critical", "MGR", "MGR \u5b58\u5728\u975e ONLINE \u8282\u70b9", "\u68c0\u67e5 group_replication \u72b6\u6001\u3001\u7f51\u7edc\u3001\u8bc1\u4e66\u548c\u8282\u70b9\u9519\u8bef\u65e5\u5fd7\u3002")

    error_log_check = _check_error_log_readable(instance, str(basic.get("log_error", "")))
    checks["error_log_readable"] = error_log_check
    if error_log_check.get("checked") and not error_log_check.get("readable"):
        _add_risk(risks, "warning", "Error Log", "Error Log \u8def\u5f84\u4e0d\u53ef\u8bfb", "\u68c0\u67e5 log_error \u8def\u5f84\u3001SSH \u7528\u6237\u6743\u9650\u548c\u6587\u4ef6\u7cfb\u7edf\u6743\u9650\u3002")
    elif not error_log_check.get("checked"):
        _add_risk(risks, "info", "Error Log", error_log_check.get("reason", "\u672a\u9a8c\u8bc1 Error Log \u53ef\u8bfb\u6027"), "\u5efa\u8bae\u914d\u7f6e SSH\uff0c\u4fbf\u4e8e\u5e73\u53f0\u76f4\u63a5\u8bfb\u53d6\u9519\u8bef\u65e5\u5fd7\u5e76\u8fc7\u6ee4 ERROR/MY/InnoDB\u3002")

    open_files_limit = _to_int(basic.get("open_files_limit"))
    open_files = _to_int(status.get("open_files"))
    checks["open_files_usage_pct"] = _ratio(open_files, open_files_limit)
    if open_files_limit and open_files_limit < 65535:
        _add_risk(risks, "warning", "open_files_limit", f"open_files_limit={open_files_limit}\uff0c\u53ef\u80fd\u504f\u5c0f", "\u5927\u5e76\u53d1\u6216\u8868\u6570\u8f83\u591a\u65f6\u5efa\u8bae\u8c03\u5927 open_files_limit\u3002")

    hits = _to_int(status.get("table_open_cache_hits"))
    misses = _to_int(status.get("table_open_cache_misses"))
    hit_pct_table_cache = _ratio(hits, hits + misses)
    checks["table_open_cache_hit_pct"] = hit_pct_table_cache
    if hits + misses > 0 and hit_pct_table_cache < 95:
        _add_risk(risks, "warning", "table_open_cache", f"table_open_cache \u547d\u4e2d\u7387 {hit_pct_table_cache}%", "\u8bc4\u4f30\u8c03\u5927 table_open_cache/table_definition_cache\uff0c\u5e76\u68c0\u67e5\u8868\u6570\u548c\u8fde\u63a5\u6a21\u5f0f\u3002")

    level_order = {"critical": 0, "high": 1, "warning": 2, "info": 3, "normal": 4}
    risks.sort(key=lambda r: level_order.get(r.get("level", "info"), 9))
    return risks, checks



def _has_display_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _drop_empty(data: dict | None) -> dict:
    return {key: value for key, value in (data or {}).items() if _has_display_value(value)}


def _pick(source: dict, keys: list[str]) -> dict:
    return {key: source.get(key) for key in keys if _has_display_value(source.get(key))}


def _safe_query_rows(cur, sql: str, warnings: list[str], section: str) -> list[dict]:
    try:
        cur.execute(sql)
        return cur.fetchall()
    except Exception as exc:
        warnings.append(f"{section} \u91c7\u96c6\u5931\u8d25\uff1a{exc}")
        return []


def _safe_query_one(cur, sql: str, warnings: list[str], section: str) -> dict:
    rows = _safe_query_rows(cur, sql, warnings, section)
    return rows[0] if rows else {}


def _fetch_identity(cur, variables: dict, warnings: list[str]) -> dict:
    row = _safe_query_one(
        cur,
        """
        SELECT @@hostname AS hostname,
               @@server_id AS server_id,
               @@port AS port,
               DATABASE() AS current_database,
               CURRENT_USER() AS session_current_user,
               USER() AS session_login_user,
               CONNECTION_ID() AS connection_id,
               @@read_only AS read_only
        """,
        warnings,
        "\u5b9e\u4f8b\u8eab\u4efd",
    )
    fallback = _pick(
        variables,
        ["hostname", "server_id", "server_uuid", "port", "socket", "pid_file", "read_only", "super_read_only"],
    )
    return _drop_empty({**fallback, **(row or {})})


def _fetch_grants(cur, warnings: list[str]) -> dict:
    rows = _safe_query_rows(cur, "SHOW GRANTS FOR CURRENT_USER()", warnings, "\u5f53\u524d\u8d26\u53f7\u6388\u6743")
    grants = [next(iter(row.values())) for row in rows if row]
    grant_text = "\n".join(str(g).upper() for g in grants)
    return {
        "grants": grants,
        "has_process_privilege": "PROCESS" in grant_text or "ALL PRIVILEGES" in grant_text,
        "has_replication_privilege": "REPLICATION CLIENT" in grant_text or "ALL PRIVILEGES" in grant_text,
        "has_super_or_connection_admin": "SUPER" in grant_text or "CONNECTION_ADMIN" in grant_text or "ALL PRIVILEGES" in grant_text,
    }


def _fetch_binary_log_status(cur) -> dict:
    for sql in ["SHOW MASTER STATUS", "SHOW BINARY LOG STATUS"]:
        try:
            cur.execute(sql)
            row = cur.fetchone()
            if row:
                return row
        except Exception:
            continue
    return {}


def _fetch_binary_logs(cur) -> list[dict]:
    try:
        cur.execute("SHOW BINARY LOGS")
        return cur.fetchall()
    except Exception:
        return []


def _fetch_innodb_status(cur) -> dict:
    try:
        cur.execute("SHOW ENGINE INNODB STATUS")
        row = cur.fetchone() or {}
        text = row.get("Status") or row.get("status") or ""
        history = ""
        match = re.search(r"History list length\s+(\d+)", text)
        if match:
            history = match.group(1)
        return {"raw": text, "history_list_length": history, "has_deadlock": "LATEST DETECTED DEADLOCK" in text}
    except Exception:
        return {"raw": "", "history_list_length": "", "has_deadlock": False}


def _build_innodb_status(status: dict, variables: dict, engine_status: dict, checks: dict) -> dict:
    total_pages = _to_int(status.get("innodb_buffer_pool_pages_total"))
    free_pages = _to_int(status.get("innodb_buffer_pool_pages_free"))
    data_pages = _to_int(status.get("innodb_buffer_pool_pages_data"))
    misc_pages = _to_int(status.get("innodb_buffer_pool_pages_misc"))
    dirty_pages = _to_int(status.get("innodb_buffer_pool_pages_dirty"))
    used_pages = max(total_pages - free_pages, 0) if total_pages else 0
    page_size = _to_int(variables.get("innodb_page_size"), 16384) or 16384
    configured_bytes = _to_int(variables.get("innodb_buffer_pool_size"))
    read_requests = _to_int(status.get("innodb_buffer_pool_read_requests"))
    disk_reads = _to_int(status.get("innodb_buffer_pool_reads"))

    def gb_from_pages(pages: int) -> float:
        return round(pages * page_size / 1024 / 1024 / 1024, 2)

    return {
        **_pick(
            status,
            [
                "innodb_buffer_pool_read_requests",
                "innodb_buffer_pool_reads",
                "innodb_buffer_pool_write_requests",
                "innodb_buffer_pool_pages_total",
                "innodb_buffer_pool_pages_data",
                "innodb_buffer_pool_pages_free",
                "innodb_buffer_pool_pages_misc",
                "innodb_buffer_pool_pages_dirty",
                "innodb_buffer_pool_pages_flushed",
                "innodb_buffer_pool_bytes_data",
                "innodb_buffer_pool_bytes_dirty",
                "innodb_buffer_pool_wait_free",
                "innodb_buffer_pool_read_ahead",
                "innodb_buffer_pool_read_ahead_evicted",
                "innodb_buffer_pool_read_ahead_rnd",
                "innodb_row_lock_waits",
                "innodb_row_lock_time",
                "innodb_row_lock_time_avg",
                "innodb_log_waits",
                "innodb_os_log_written",
                "innodb_data_reads",
                "innodb_data_writes",
                "innodb_data_fsyncs",
            ],
        ),
        **engine_status,
        "innodb_page_size": page_size,
        "innodb_buffer_pool_size": variables.get("innodb_buffer_pool_size", ""),
        "buffer_pool_configured_gb": round(configured_bytes / 1024 / 1024 / 1024, 2) if configured_bytes else 0,
        "buffer_pool_total_gb": gb_from_pages(total_pages),
        "buffer_pool_used_pages": used_pages,
        "buffer_pool_used_gb": gb_from_pages(used_pages),
        "buffer_pool_free_gb": gb_from_pages(free_pages),
        "buffer_pool_data_gb": gb_from_pages(data_pages),
        "buffer_pool_dirty_gb": gb_from_pages(dirty_pages),
        "buffer_pool_used_pct": _ratio(used_pages, total_pages),
        "buffer_pool_free_pct": _ratio(free_pages, total_pages),
        "buffer_pool_data_pct": _ratio(data_pages, total_pages),
        "buffer_pool_dirty_pct": _ratio(dirty_pages, total_pages),
        "buffer_pool_misc_pct": _ratio(misc_pages, total_pages),
        "buffer_pool_hit_pct": checks.get("buffer_pool_hit_pct", 0),
        "buffer_pool_disk_read_pct": round(100 - checks.get("buffer_pool_hit_pct", 0), 2) if read_requests else 0,
        "buffer_pool_disk_reads_per_10k": round(disk_reads * 10000 / read_requests, 2) if read_requests else 0,
    }


def _fetch_setup_consumers(cur, warnings: list[str]) -> list[dict]:
    return _safe_query_rows(
        cur,
        "SELECT NAME, ENABLED FROM performance_schema.setup_consumers WHERE NAME LIKE '%statements%' ORDER BY NAME",
        warnings,
        "performance_schema statement consumer",
    )


def _fetch_engines(cur) -> list[dict]:
    try:
        cur.execute("SHOW ENGINES")
        return cur.fetchall()
    except Exception:
        return []


def _fetch_plugins(cur) -> list[dict]:
    try:
        cur.execute("SHOW PLUGINS")
        return cur.fetchall()
    except Exception:
        return []


def _fetch_schema_overview(cur, warnings: list[str]) -> dict:
    schemas = _safe_query_rows(
        cur,
        """
        SELECT table_schema,
               COUNT(*) AS table_count,
               SUM(CASE WHEN table_type='VIEW' THEN 1 ELSE 0 END) AS view_count,
               ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 2) AS total_gb,
               ROUND(SUM(data_length) / 1024 / 1024 / 1024, 2) AS data_gb,
               ROUND(SUM(index_length) / 1024 / 1024 / 1024, 2) AS index_gb
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
        GROUP BY table_schema
        ORDER BY total_gb DESC
        """,
        warnings,
        "Schema \u6982\u89c8",
    )
    return {
        "schemas": schemas,
        "schema_count": len(schemas),
        "table_count": sum(_to_int(row.get("table_count")) for row in schemas),
        "view_count": sum(_to_int(row.get("view_count")) for row in schemas),
        "total_size_gb": round(sum(float(row.get("total_gb") or 0) for row in schemas), 2),
        "top_schema_by_size": schemas[0] if schemas else {},
    }


def _fetch_program_objects(cur, warnings: list[str]) -> dict:
    routines = _safe_query_rows(
        cur,
        """
        SELECT ROUTINE_SCHEMA AS schema_name, ROUTINE_TYPE AS object_type, COUNT(*) AS cnt
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
        GROUP BY ROUTINE_SCHEMA, ROUTINE_TYPE
        """,
        warnings,
        "\u5b58\u50a8\u8fc7\u7a0b/\u51fd\u6570",
    )
    triggers = _safe_query_rows(
        cur,
        "SELECT TRIGGER_SCHEMA AS schema_name, COUNT(*) AS cnt FROM information_schema.TRIGGERS GROUP BY TRIGGER_SCHEMA",
        warnings,
        "\u89e6\u53d1\u5668",
    )
    events = _safe_query_rows(
        cur,
        "SELECT EVENT_SCHEMA AS schema_name, COUNT(*) AS cnt FROM information_schema.EVENTS GROUP BY EVENT_SCHEMA",
        warnings,
        "\u4e8b\u4ef6",
    )
    return {"routines": routines, "triggers": triggers, "events": events}


def _fetch_security(cur, warnings: list[str]) -> dict:
    users = _safe_query_rows(
        cur,
        """
        SELECT user, host, plugin, account_locked, password_expired
        FROM mysql.user
        ORDER BY user, host
        """,
        warnings,
        "\u7528\u6237\u5b89\u5168",
    )
    return {
        "users": users,
        "user_count": len(users),
        "anonymous_user_count": sum(1 for row in users if not row.get("user")),
        "root_remote_login": sum(1 for row in users if row.get("user") == "root" and row.get("host") == "%"),
        "locked_user_count": sum(1 for row in users if row.get("account_locked") == "Y"),
        "password_expired_count": sum(1 for row in users if row.get("password_expired") == "Y"),
        "mysql_native_password_count": sum(1 for row in users if row.get("plugin") == "mysql_native_password"),
        "caching_sha2_password_count": sum(1 for row in users if row.get("plugin") == "caching_sha2_password"),
    }


def _fetch_table_health(cur, checks: dict, warnings: list[str]) -> dict:
    partition_tables = _fetch_one(
        cur,
        """
        SELECT COUNT(*) AS cnt
        FROM (
          SELECT table_schema, table_name
          FROM information_schema.partitions
          WHERE partition_name IS NOT NULL
            AND table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
          GROUP BY table_schema, table_name
        ) x
        """,
    )
    large_tables = _fetch_one(
        cur,
        "SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema NOT IN %s AND table_type='BASE TABLE' AND IFNULL(table_rows,0) >= 5000000",
        (SYSTEM_SCHEMAS,),
    )
    fragmented_tables = _fetch_one(
        cur,
        "SELECT COUNT(*) AS cnt FROM information_schema.tables WHERE table_schema NOT IN %s AND table_type='BASE TABLE' AND data_free >= 1024*1024*100",
        (SYSTEM_SCHEMAS,),
    )
    return {
        "non_innodb_table_count": checks.get("non_innodb_table_count", 0),
        "no_primary_key_table_count": checks.get("no_primary_key_table_count", 0),
        "large_no_primary_key_table_count": checks.get("large_no_primary_key_table_count", 0),
        "partition_table_count": partition_tables,
        "large_table_count": large_tables,
        "fragmented_table_count": fragmented_tables,
    }


def _risk_level(risks: list[dict]) -> str:
    levels = {item.get("level") for item in risks}
    if "critical" in levels:
        return "critical"
    if "high" in levels:
        return "high"
    if "warning" in levels:
        return "warning"
    if "info" in levels:
        return "info"
    return "normal"


def _enabled_label(value) -> str:
    return "已开启" if str(value).upper() in ("ON", "1", "YES", "TRUE") else "未开启"


def _pct_status_label(value: float, warning: float, critical: float, reverse: bool = False) -> str:
    if reverse:
        if value < critical:
            return "严重"
        if value < warning:
            return "警告"
        return "正常"
    if value >= critical:
        return "严重"
    if value >= warning:
        return "警告"
    return "正常"


def _uptime_label(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


def _build_summary(instance: DBInstance, basic: dict, status: dict, checks: dict, risks: list[dict], basic_info: dict) -> dict:
    uptime = _to_int(status.get("uptime"))
    questions = _to_int(status.get("questions") or status.get("queries"))
    commits = _to_int(status.get("com_commit"))
    rollbacks = _to_int(status.get("com_rollback"))
    replicas = checks.get("replicas") or []
    mgr_members = checks.get("mgr_members") or []
    if mgr_members:
        role = "mgr_member"
    elif replicas:
        role = "replica"
    elif str(basic.get("read_only", "")).upper() in ("ON", "1"):
        role = "readonly"
    else:
        role = "primary_or_standalone"
    replication_status = "not_configured"
    if replicas:
        bad = [r for r in replicas if str(r.get("Replica_IO_Running") or r.get("Slave_IO_Running")).lower() != "yes" or str(r.get("Replica_SQL_Running") or r.get("Slave_SQL_Running")).lower() != "yes"]
        replication_status = "abnormal" if bad else "normal"
    mgr_status = "not_enabled"
    if mgr_members:
        mgr_status = "normal" if all(str(m.get("MEMBER_STATE", "")).upper() == "ONLINE" for m in mgr_members) else "abnormal"
    role_label_map = {
        "primary_or_standalone": "可写实例",
        "replica": "从库",
        "readonly": "只读实例",
        "mgr_member": "MGR 节点",
    }
    role_desc_map = {
        "primary_or_standalone": "当前实例 read_only=OFF，可能是单实例或主库",
        "replica": "检测到复制通道，当前实例作为从库或复制节点运行",
        "readonly": "当前实例开启只读，通常用于备库、只读节点或维护状态",
        "mgr_member": "检测到 Group Replication 成员信息",
    }
    replication_label_map = {
        "not_configured": "未配置复制",
        "normal": "复制正常",
        "abnormal": "复制异常",
    }
    risk_level = _risk_level(risks)
    risk_label_map = {"critical": "严重", "high": "高风险", "warning": "警告", "info": "提示", "normal": "正常"}
    connection_pct = checks.get("connection_usage_pct", 0)
    buffer_pool_pct = checks.get("buffer_pool_hit_pct", 0)
    binlog_label = _enabled_label(basic.get("log_bin"))
    gtid_label = _enabled_label(basic.get("gtid_mode"))
    slow_query_label = _enabled_label(basic.get("slow_query_log"))
    main_risks = [item.get("message") or item.get("item") for item in risks[:3] if item.get("message") or item.get("item")]
    stable_parts = [
        f"连接使用率 {_fmt_pct(connection_pct)}",
        f"Buffer Pool 命中率 {_fmt_pct(buffer_pool_pct)}",
        f"Binlog {binlog_label}",
        f"GTID {gtid_label}",
    ]
    health_summary = "实例当前运行稳定：" + "，".join(stable_parts) + "。"
    if main_risks:
        health_summary += " 需要关注：" + "；".join(main_risks) + "。"
    else:
        health_summary += " 暂未发现明显基础配置风险。"
    return {
        "mysql_version": basic.get("version", ""),
        "role": role,
        "role_label": role_label_map.get(role, role),
        "role_desc": role_desc_map.get(role, ""),
        "uptime_days": round(uptime / 86400, 2) if uptime else 0,
        "uptime_label": _uptime_label(uptime),
        "connection_used_pct": checks.get("connection_usage_pct", 0),
        "connection_status_label": _pct_status_label(connection_pct, 80, 95),
        "qps": round(questions / uptime, 2) if uptime else 0,
        "tps_estimated": round((commits + rollbacks) / uptime, 2) if uptime else 0,
        "binlog_enabled": str(basic.get("log_bin", "")).upper() in ("ON", "1", "YES"),
        "binlog_label": binlog_label,
        "gtid_mode": basic.get("gtid_mode", ""),
        "gtid_label": gtid_label,
        "replication_status": replication_status,
        "replication_status_label": replication_label_map.get(replication_status, replication_status),
        "mgr_status": mgr_status,
        "innodb_buffer_pool_size_gb": round(_to_int(basic.get("innodb_buffer_pool_size")) / 1024 / 1024 / 1024, 2),
        "buffer_pool_hit_pct": checks.get("buffer_pool_hit_pct", 0),
        "buffer_pool_status_label": _pct_status_label(buffer_pool_pct, 99, 95, reverse=True),
        "slow_query_log": basic.get("slow_query_log", ""),
        "slow_query_log_label": slow_query_label,
        "risk_level": risk_level,
        "risk_level_label": risk_label_map.get(risk_level, risk_level),
        "risk_count": len(risks),
        "health_summary_text": health_summary,
        "main_risk_text": "；".join(main_risks) if main_risks else "暂无明显基础配置风险",
    }


def get_basic_info(instance: DBInstance) -> dict:
    result = {"error": None, "basic": {}, "status": {}, "risks": [], "checks": {}, "warnings": [], "summary": {}, "basic_info": {}, "raw": {"variables": {}, "status": {}}}
    warnings: list[str] = []
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                raw_variables = _load_all_variables(cur)
                raw_status = _load_all_status(cur)
                variables = {key: raw_variables.get(key, "") for key in MYSQL_VARIABLE_KEYS}
                status = {
                    "uptime": raw_status.get("uptime", ""),
                    "threads_connected": raw_status.get("threads_connected", ""),
                    "threads_running": raw_status.get("threads_running", ""),
                    "queries": raw_status.get("queries", ""),
                    "qps": raw_status.get("queries", ""),
                    **{key.lower(): raw_status.get(key.lower(), "") for key in MYSQL_STATUS_KEYS},
                }
                cur.execute("SELECT VERSION() AS version")
                ver = cur.fetchone()
                variables["version"] = ver["version"] if ver else ""

                identity = _fetch_identity(cur, variables, warnings)
                grants = _fetch_grants(cur, warnings)
                binary_status = _fetch_binary_log_status(cur)
                binary_logs = _fetch_binary_logs(cur)
                replicas = _fetch_replica_rows(cur)
                mgr_members = _fetch_mgr_members(cur)
                innodb_status = _fetch_innodb_status(cur)
                consumers = _fetch_setup_consumers(cur, warnings)
                engines = _fetch_engines(cur)
                plugins = _fetch_plugins(cur)
                schema_overview = _fetch_schema_overview(cur, warnings)
                program_objects = _fetch_program_objects(cur, warnings)
                security = _fetch_security(cur, warnings)

                result["basic"] = variables
                result["status"] = status
                risks, checks = _build_risks(instance, variables, status, cur)
                checks["replicas"] = replicas or checks.get("replicas", [])
                checks["mgr_members"] = mgr_members or checks.get("mgr_members", [])
                table_health = _fetch_table_health(cur, checks, warnings)

                basic_info = {
                    "identity": identity,
                    "connection_context": {**grants, "account_type": instance.account_type, "instance_user": instance.username},
                    "version": _pick(variables, ["version", "version_comment", "version_compile_os", "version_compile_machine", "protocol_version", "mysqlx_port"]),
                    "runtime": {**_pick(status, ["uptime", "questions", "queries", "connections", "threads_connected", "threads_running", "max_used_connections", "aborted_connects", "aborted_clients", "com_commit", "com_rollback"]), "uptime_days": round(_to_int(status.get("uptime")) / 86400, 2) if _to_int(status.get("uptime")) else 0},
                    "paths": _pick(variables, ["basedir", "datadir", "tmpdir", "plugin_dir", "secure_file_priv", "socket", "pid_file", "log_error", "slow_query_log_file", "general_log_file", "relay_log", "relay_log_index", "log_bin_basename", "log_bin_index"]),
                    "variables_core": _pick(variables, ["max_connections", "connect_timeout", "wait_timeout", "interactive_timeout", "open_files_limit", "max_allowed_packet", "table_open_cache", "table_definition_cache", "thread_cache_size", "tmp_table_size", "max_heap_table_size", "sort_buffer_size", "join_buffer_size", "read_buffer_size", "read_rnd_buffer_size"]),
                    "charset_timezone": _pick(variables, ["character_set_server", "collation_server", "character_set_database", "collation_database", "character_set_client", "character_set_connection", "character_set_results", "lower_case_table_names", "sql_mode", "time_zone", "system_time_zone", "explicit_defaults_for_timestamp"]),
                    "binlog_gtid": {**_pick(variables, ["log_bin", "log_bin_basename", "log_bin_index", "binlog_format", "sync_binlog", "binlog_row_image", "binlog_expire_logs_seconds", "expire_logs_days", "gtid_mode", "enforce_gtid_consistency", "gtid_executed", "gtid_purged", "binlog_do_db", "binlog_ignore_db"]), "current_status": binary_status, "files": binary_logs},
                    "replication": {"replica_detected": bool(replicas), "replicas": replicas},
                    "mgr": {"mgr_enabled": bool(mgr_members or variables.get("group_replication_group_name")), "members": mgr_members, "variables": _pick(variables, ["group_replication_group_name", "group_replication_single_primary_mode", "group_replication_start_on_boot"])},
                    "innodb_config": _pick(variables, ["default_storage_engine", "innodb_version", "innodb_page_size", "innodb_buffer_pool_size", "innodb_buffer_pool_chunk_size", "innodb_buffer_pool_instances", "innodb_log_file_size", "innodb_log_files_in_group", "innodb_flush_log_at_trx_commit", "innodb_flush_method", "innodb_file_per_table", "innodb_data_file_path", "innodb_temp_data_file_path", "innodb_undo_directory", "innodb_undo_tablespaces", "innodb_max_dirty_pages_pct", "innodb_io_capacity", "innodb_io_capacity_max", "innodb_read_io_threads", "innodb_write_io_threads", "innodb_thread_concurrency", "innodb_lock_wait_timeout", "innodb_deadlock_detect"]),
                    "innodb_status": _build_innodb_status(status, variables, innodb_status, checks),
                    "thread_connection": {**_pick(status, ["threads_connected", "threads_running", "threads_cached", "threads_created", "connections", "max_used_connections"]), **_pick(variables, ["thread_cache_size", "max_connections"]), "connection_used_pct": checks.get("connection_usage_pct", 0), "max_used_connection_pct": checks.get("max_used_connection_pct", 0)},
                    "table_file_cache": {**_pick(status, ["open_tables", "opened_tables", "open_files", "opened_files", "table_open_cache_hits", "table_open_cache_misses", "table_open_cache_overflows"]), **_pick(variables, ["table_open_cache", "table_definition_cache", "open_files_limit"]), "table_open_cache_hit_pct": checks.get("table_open_cache_hit_pct", 0)},
                    "temp_sort_join": {**_pick(status, ["created_tmp_tables", "created_tmp_disk_tables", "created_tmp_files", "sort_merge_passes", "sort_rows", "select_full_join", "select_full_range_join", "select_scan", "handler_read_rnd_next"]), "tmp_disk_table_pct": checks.get("tmp_disk_table_pct", 0)},
                    "performance_switch": {**_pick(variables, ["slow_query_log", "slow_query_log_file", "long_query_time", "log_queries_not_using_indexes", "performance_schema"]), "statement_consumers": consumers},
                    "engines_plugins": {"engines": engines, "plugins": plugins},
                    "schema_overview": schema_overview,
                    "security": security,
                    "ssl_auth": _pick(variables, ["have_ssl", "ssl_ca", "ssl_cert", "ssl_key", "require_secure_transport", "default_authentication_plugin"]),
                    "program_objects": {**program_objects, "event_scheduler": variables.get("event_scheduler", "")},
                    "table_health": table_health,
                    "risk_items": risks,
                }

                result["risks"] = risks
                result["checks"] = checks
                result["warnings"] = warnings
                result["basic_info"] = basic_info
                result["summary"] = _build_summary(instance, variables, status, checks, risks, basic_info)
                result["raw"] = {"variables": raw_variables, "status": raw_status}
                result["instance"] = {
                    "id": instance.id,
                    "instance_name": instance.instance_name,
                    "db_type": instance.db_type,
                    "host": instance.host,
                    "port": instance.port,
                    "database_name": instance.database_name,
                    "account_type": instance.account_type,
                    "env_type": instance.env_type,
                    "factory_name": instance.factory_name,
                    "line_name": instance.line_name,
                }
                result["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    except Exception as e:
        result["error"] = str(e)
    return result

MYSQL_HIGH_RISK_VARIABLES = {
    "innodb_flush_log_at_trx_commit",
    "sync_binlog",
    "log_bin",
    "binlog_format",
    "gtid_mode",
    "enforce_gtid_consistency",
    "read_only",
    "super_read_only",
    "sql_mode",
    "max_connections",
    "innodb_buffer_pool_size",
    "innodb_log_file_size",
    "lower_case_table_names",
    "server_id",
}

# 只读 / 需重启（不可 SET GLOBAL）的系统变量。列表不全时宁可标静态，避免对只读参数
# 生成会在生产报错的 SET GLOBAL。注意 server_id/gtid_mode 在 8.0 实为动态，但改动需特定流程，
# 这里按高风险静态处理（仅生成 my.cnf 方案），避免误导直接 SET GLOBAL。
MYSQL_STATIC_VARIABLES = {
    # 路径 / 网络 / 启动
    "datadir", "basedir", "socket", "port", "bind_address", "pid_file", "plugin_dir",
    "tmpdir", "secure_file_priv", "mysqlx_port", "mysqlx_socket",
    "skip_name_resolve", "skip_networking", "skip_external_locking", "skip_show_database",
    "skip_grant_tables", "large_pages", "locked_in_memory",
    # 版本 / 只读信息
    "version", "version_comment", "version_compile_machine", "version_compile_os",
    "protocol_version", "character_set_system", "system_time_zone", "lower_case_file_system",
    "lower_case_table_names",
    # 复制 / 二进制日志（启动期固定）
    "server_id", "log_bin", "log_bin_basename", "log_bin_index", "relay_log_basename",
    "gtid_mode", "log_replica_updates", "log_slave_updates",
    # InnoDB 启动期参数
    "innodb_page_size", "innodb_data_file_path", "innodb_data_home_dir",
    "innodb_log_group_home_dir", "innodb_log_files_in_group", "innodb_log_file_size",
    "innodb_buffer_pool_instances", "innodb_doublewrite", "innodb_flush_method",
    "innodb_open_files", "innodb_autoinc_lock_mode", "innodb_numa_interleave",
    "innodb_read_io_threads", "innodb_write_io_threads", "innodb_undo_directory",
    "innodb_undo_tablespaces", "innodb_dedicated_server",
    # 安全 / SSL（多为启动期）
    "ssl_ca", "ssl_cert", "ssl_key", "ssl_capath", "ssl_cipher", "ssl_crl", "ssl_crlpath",
}


def _variable_category(name: str) -> str:
    name = name.lower()
    if name.startswith(("innodb_", "transaction_", "lock_")):
        return "InnoDB / 事务"
    if "binlog" in name or "gtid" in name or "replication" in name or name in ("server_id", "read_only", "super_read_only"):
        return "复制 / 高可用"
    if "log" in name:
        return "日志"
    if name.startswith(("character_", "collation_", "time_zone", "system_time_zone")) or name == "sql_mode":
        return "字符集 / SQL模式"
    if name in ("max_connections", "connect_timeout", "wait_timeout", "interactive_timeout") or name.endswith("_timeout"):
        return "连接 / 超时"
    if name.endswith("_buffer_size") or "cache" in name or name in ("tmp_table_size", "max_heap_table_size"):
        return "内存 / 缓存"
    if "ssl" in name or "auth" in name or "password" in name:
        return "安全"
    return "通用"


def _enrich_variable(name: str, value: str, version: str = "") -> dict:
    key = name.lower()
    is_static = key in MYSQL_STATIC_VARIABLES
    high_risk = key in MYSQL_HIGH_RISK_VARIABLES
    # MariaDB 没有 SET PERSIST；版本串形如 "10.6.5-MariaDB" 解析出的 major 会误判为 8+。
    is_mariadb = "mariadb" in str(version).lower()
    major_version = 0
    try:
        major_version = int(str(version).split(".", 1)[0])
    except Exception:
        major_version = 0
    persist_supported = major_version >= 8 and not is_static and not is_mariadb
    if is_static:
        change_method = "my.cnf + restart"
    elif persist_supported:
        change_method = "SET GLOBAL / SET PERSIST"
    else:
        change_method = "SET GLOBAL"
    return {
        "name": name,
        "value": str(value),
        "scope": "GLOBAL",
        "dynamic": not is_static,
        "persist_supported": persist_supported,
        "change_method": change_method,
        "category": _variable_category(key),
        "risk_level": "high" if high_risk else "normal",
        "plan_only": True,
    }


def get_variables(instance: DBInstance, search: str = "") -> list:
    items = []
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("SELECT VERSION() AS version")
                    version = _get_row_value(cur.fetchone(), "version")
                except Exception:
                    version = ""
                cur.execute("SHOW GLOBAL VARIABLES")
                rows = cur.fetchall()
                for row in rows:
                    name = _get_row_value(row, "Variable_name")
                    value = _get_row_value(row, "Value")
                    if not name:
                        continue
                    if search and search.lower() not in name.lower():
                        continue
                    items.append(_enrich_variable(name, value, version))
    except Exception as e:
        logger.error("get_variables failed: %s", e)
    return items


def get_status(instance: DBInstance, search: str = "") -> list:
    items = []
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW GLOBAL STATUS")
                rows = cur.fetchall()
                for row in rows:
                    name = _get_row_value(row, "Variable_name")
                    value = _get_row_value(row, "Value")
                    if not name:
                        continue
                    if search and search.lower() not in name.lower():
                        continue
                    items.append({"name": name, "value": str(value)})
    except Exception as e:
        logger.error("get_status failed: %s", e)
    return items
