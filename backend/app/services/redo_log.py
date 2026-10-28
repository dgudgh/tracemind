import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.redo_log")


def get_redo_logs(instance: DBInstance) -> dict:
    logger.info("查询 Redo Log | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT group#, sequence#, members, bytes/1024/1024 AS size_mb, "
            "archived, status, first_change#, "
            "TO_CHAR(first_time,'YYYY-MM-DD HH24:MI:SS') AS first_time "
            "FROM v$log ORDER BY group#"
        )
        groups = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            "SELECT group#, member, type, is_recovery_dest_file "
            "FROM v$logfile ORDER BY group#, member"
        )
        members = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        try:
            cursor.execute("SELECT name, value FROM v$parameter WHERE name IN ('db_block_size', 'db_writer_processes', 'log_checkpoint_interval', 'log_checkpoint_timeout')")
            params = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            params = []

        try:
            cursor.execute(
                "SELECT event, total_waits, time_waited, "
                "ROUND(time_waited / 100, 2) AS time_waited_seconds, "
                "ROUND(average_wait / 100, 4) AS avg_wait_seconds "
                "FROM v$system_event "
                "WHERE event IN ("
                "'log file sync', 'log file parallel write', 'log file switch completion', "
                "'log file switch (checkpoint incomplete)', 'log buffer space'"
                ") "
                "ORDER BY time_waited DESC"
            )
            wait_events = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            wait_events = []

    return {"groups": groups, "members": members, "parameters": params, "wait_events": wait_events}


def get_redo_switch_frequency(instance: DBInstance, thread_num: int | None = None, limit: int = 50) -> dict:
    logger.info("查询 Redo 日志切换频率 | instance=%s | thread=%s", instance.instance_name, thread_num)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        if thread_num is None:
            cursor.execute("SELECT MAX(thread#) FROM v$log")
            row = cursor.fetchone()
            thread_num = row[0] if row and row[0] else 1

        cursor.execute(
            "SELECT b.SEQUENCE# AS prev_sequence#, "
            "TO_CHAR(b.FIRST_TIME, 'YYYY-MM-DD HH24:MI:SS') AS prev_first_time, "
            "a.SEQUENCE# AS curr_sequence#, "
            "TO_CHAR(a.FIRST_TIME, 'YYYY-MM-DD HH24:MI:SS') AS curr_first_time, "
            "ROUND(((a.FIRST_TIME - b.FIRST_TIME) * 24) * 60, 2) AS interval_minutes "
            "FROM v$log_history a, v$log_history b "
            "WHERE a.SEQUENCE# = b.SEQUENCE# + 1 AND b.THREAD# = :thread "
            "ORDER BY a.SEQUENCE# DESC",
            {"thread": thread_num},
        )
        rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        rows = rows[:limit]

        cursor.execute(
            "SELECT COUNT(*) FROM v$log_history "
            "WHERE THREAD# = :thread AND FIRST_TIME >= SYSDATE - 1/24",
            {"thread": thread_num},
        )
        switch_count_1h = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM v$log_history "
            "WHERE THREAD# = :thread AND FIRST_TIME >= SYSDATE - 5/1440",
            {"thread": thread_num},
        )
        switch_count_5min = cursor.fetchone()[0]

        total = len(rows)
        if total > 0:
            intervals = [r["interval_minutes"] for r in rows if r.get("interval_minutes") is not None]
            avg_interval = round(sum(intervals) / len(intervals), 2) if intervals else 0
            min_interval = round(min(intervals), 2) if intervals else 0
            max_interval = round(max(intervals), 2) if intervals else 0
        else:
            avg_interval = min_interval = max_interval = 0

    return {
        "thread": thread_num,
        "records": rows,
        "summary": {
            "total_records": total,
            "avg_interval_minutes": avg_interval,
            "min_interval_minutes": min_interval,
            "max_interval_minutes": max_interval,
            "switch_count_within_1h": switch_count_1h,
            "switch_count_within_5min": switch_count_5min,
        },
    }


def generate_add_group_script(instance: DBInstance, size_mb: int = 1024, member_path: str | None = None) -> dict:
    if size_mb < 50 or size_mb > 102400:
        raise ValueError("size_mb must be between 50 and 102400")
    logger.info("生成添加 Redo Log 组脚本 | instance=%s | size=%dMB", instance.instance_name, size_mb)
    logfile_path = member_path or "/oradata/redolog/redo_N.log"
    sql_corrected = f"-- 添加 Redo Log 组（请确认组号）\nALTER DATABASE ADD LOGFILE GROUP <N> '{logfile_path}' SIZE {size_mb}M;"

    return {
        "status": "plan_only",
        "message": "已生成添加 Redo Log 组脚本。请在维护窗口执行。",
        "sql": sql_corrected,
        "warnings": [
            "添加 Redo Log 组属于 DDL 操作，请在维护窗口执行。",
            "请确认磁盘空间充足。",
            "建议组号接续当前最大组号。",
            "建议与现有 Redo Log 大小保持一致。",
        ],
    }


def generate_add_member_script(instance: DBInstance, group_num: int, member_path: str) -> dict:
    if group_num < 1:
        raise ValueError("group_num must be greater than 0")
    if not member_path or len(member_path) > 512:
        raise ValueError("member_path is required and must be shorter than 512 characters")
    logger.info("生成添加 Redo Log 成员脚本 | instance=%s | group=%d", instance.instance_name, group_num)
    sql = f"-- 添加 Redo Log 成员\nALTER DATABASE ADD LOGFILE MEMBER '{member_path}' TO GROUP {group_num};"
    return {
        "status": "plan_only",
        "message": "已生成添加 Redo Log 成员脚本。",
        "sql": sql,
        "warnings": ["请确认磁盘空间和路径正确。", "建议 multiplex 每组至少2个成员。"],
    }


def generate_switch_log_script(instance: DBInstance) -> dict:
    logger.info("生成日志切换脚本 | instance=%s", instance.instance_name)
    sql = "-- 强制日志切换\nALTER SYSTEM SWITCH LOGFILE;"
    return {"status": "plan_only", "message": "已生成日志切换脚本。", "sql": sql, "warnings": ["日志切换会导致检查点，可能短暂影响性能。"]}


def generate_drop_group_script(instance: DBInstance, group_num: int) -> dict:
    if group_num < 1:
        raise ValueError("group_num must be greater than 0")
    logger.info("生成删除 Redo Log 组脚本 | instance=%s | group=%d", instance.instance_name, group_num)
    sql = f"-- 删除 Redo Log 组（确保该组状态为 INACTIVE）\n-- 先确认状态:\nSELECT group#, status FROM v$log WHERE group# = {group_num};\n\nALTER DATABASE DROP LOGFILE GROUP {group_num};"
    return {
        "status": "plan_only",
        "message": "已生成删除 Redo Log 组脚本。",
        "sql": sql,
        "warnings": [
            "只能删除 INACTIVE 状态的组。",
            "Oracle 至少需要 2 组 Redo Log。",
            "删除前确保已归档（ARCHIVED=YES）。",
        ],
    }
