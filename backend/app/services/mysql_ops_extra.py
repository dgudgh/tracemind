import logging
import re

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance
from app.services.mysql_lock import parse_deadlock_text
from app.services.mysql_basic import get_basic_info
from app.services.mysql_binlog import get_binlog_overview
from app.services.mysql_capacity import get_large_tables, get_schema_sizes
from app.services.mysql_innodb import get_innodb_status
from app.services.mysql_lock import get_lock_waits, get_transactions
from app.services.mysql_replication import get_replication_status

logger = logging.getLogger("app.services.mysql_ops_extra")


def get_mysql_overview(instance: DBInstance) -> dict:
    basic = get_basic_info(instance)
    replication = get_replication_status(instance)
    binlog = get_binlog_overview(instance)
    transactions = get_transactions(instance)
    waits = get_lock_waits(instance)
    schemas = get_schema_sizes(instance)
    largest_tables = get_large_tables(instance, 10)
    return {
        "basic": basic.get("basic", {}),
        "status": basic.get("status", {}),
        "replication": replication,
        "binlog": {
            "variables": binlog.get("variables", {}),
            "file_count": len(binlog.get("files", [])),
            "master_status": binlog.get("master_status", {}),
        },
        "transaction_count": len(transactions),
        "lock_wait_count": len(waits),
        "schema_count": len(schemas),
        "largest_tables": largest_tables,
    }


def get_mgr_status(instance: DBInstance) -> dict:
    result = {"variables": {}, "members": [], "member_stats": [], "configured": False, "error": None}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                for key in [
                    "group_replication_group_name",
                    "group_replication_start_on_boot",
                    "group_replication_local_address",
                    "group_replication_group_seeds",
                    "group_replication_single_primary_mode",
                ]:
                    try:
                        cur.execute("SHOW GLOBAL VARIABLES LIKE %s", (key,))
                        row = cur.fetchone()
                        value = row.get("Value") if row else ""
                        result["variables"][key] = value
                        if value:
                            result["configured"] = True
                    except Exception:
                        result["variables"][key] = ""

                try:
                    cur.execute("""
                        SELECT CHANNEL_NAME, MEMBER_ID, MEMBER_HOST, MEMBER_PORT,
                               MEMBER_STATE, MEMBER_ROLE, MEMBER_VERSION
                        FROM performance_schema.replication_group_members
                        ORDER BY MEMBER_HOST, MEMBER_PORT
                    """)
                    result["members"] = cur.fetchall()
                    if result["members"]:
                        result["configured"] = True
                except Exception:
                    result["members"] = []

                try:
                    cur.execute("""
                        SELECT CHANNEL_NAME, MEMBER_ID, COUNT_TRANSACTIONS_IN_QUEUE,
                               COUNT_TRANSACTIONS_CHECKED, COUNT_CONFLICTS_DETECTED,
                               COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE,
                               COUNT_TRANSACTIONS_REMOTE_APPLIED
                        FROM performance_schema.replication_group_member_stats
                    """)
                    result["member_stats"] = cur.fetchall()
                except Exception:
                    result["member_stats"] = []
    except Exception as exc:
        result["error"] = str(exc)
    return result


def get_relay_log_status(instance: DBInstance) -> dict:
    result = {"variables": {}, "replicas": [], "error": None}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                for key in ["relay_log", "relay_log_index", "relay_log_purge", "relay_log_recovery"]:
                    try:
                        cur.execute("SHOW GLOBAL VARIABLES LIKE %s", (key,))
                        row = cur.fetchone()
                        result["variables"][key] = row.get("Value") if row else ""
                    except Exception:
                        result["variables"][key] = ""

                for sql in ["SHOW REPLICA STATUS", "SHOW SLAVE STATUS"]:
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                        if rows:
                            result["replicas"] = rows
                            break
                    except Exception:
                        continue
    except Exception as exc:
        result["error"] = str(exc)
    return result


def get_error_log_info(instance: DBInstance) -> dict:
    result = {"variables": {}, "message": "", "error": None}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                for key in ["log_error", "log_error_services", "log_timestamps"]:
                    try:
                        cur.execute("SHOW GLOBAL VARIABLES LIKE %s", (key,))
                        row = cur.fetchone()
                        result["variables"][key] = row.get("Value") if row else ""
                    except Exception:
                        result["variables"][key] = ""
        result["message"] = "已获取错误日志路径。平台不直接读取服务器文件，请通过 SSH 或堡垒机查看并过滤 ERROR / MY- / InnoDB 等关键字。"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def get_latest_deadlock(instance: DBInstance) -> dict:
    status = get_innodb_status(instance)
    raw = status.get("raw") or ""
    match = re.search(
        r"LATEST DETECTED DEADLOCK\s*-+\s*(.*?)(?:\n-{10,}\n[A-Z ]+\n-{10,}|\Z)",
        raw,
        re.S,
    )
    latest = match.group(1).strip() if match else ""
    analysis = parse_deadlock_text(latest) if latest else {}
    return {
        "latest_deadlock": latest,
        "deadlock": analysis,
        "summary": analysis.get("summary", {}),
        "transactions": analysis.get("transactions", []),
        "lock_objects": analysis.get("lock_objects", []),
        "analysis": analysis.get("analysis", {}),
        "history": analysis.get("history", {
            "last_24h_count": 0,
            "last_7d_count": 0,
            "top_tables": [],
            "top_sql_fingerprints": [],
            "top_hosts": [],
            "message": "历史死锁需要开启 innodb_print_all_deadlocks，并通过 SSH/Error Log 定时采集后统计。",
        }),
        "warnings": analysis.get("warnings", []),
        "has_deadlock": bool(latest),
        "message": "检测到最近一次死锁信息。" if latest else "当前 InnoDB 状态中未发现最近死锁记录。",
    }


def get_table_statistics(instance: DBInstance, limit: int = 100) -> list:
    sql = """
    SELECT table_schema AS table_schema,
           table_name AS table_name,
           engine AS engine,
           table_rows AS table_rows,
           avg_row_length AS avg_row_length,
           data_length AS data_length,
           index_length AS index_length,
           data_free AS data_free,
           create_time AS create_time,
           update_time AS update_time,
           check_time AS check_time,
           table_collation AS table_collation
    FROM information_schema.tables
    WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
      AND table_type = 'BASE TABLE'
    ORDER BY update_time DESC, table_rows DESC
    LIMIT %s
    """
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (limit,))
                return cur.fetchall()
    except Exception as exc:
        logger.error("get_table_statistics failed: %s", exc)
        return []


def get_security_summary(instance: DBInstance) -> dict:
    result = {"users": [], "risks": [], "error": None}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user, host, plugin, account_locked, password_expired,
                           IF(authentication_string IS NULL OR authentication_string = '', 1, 0) AS empty_password
                    FROM mysql.user
                    ORDER BY user, host
                """)
                users = cur.fetchall()
                result["users"] = users
                for user in users:
                    name = f"{user.get('user')}@{user.get('host')}"
                    if user.get("empty_password"):
                        result["risks"].append({"level": "critical", "item": name, "message": "账号可能为空密码，请立即核查。"})
                    if user.get("user") == "root" and user.get("host") in ("%", "0.0.0.0"):
                        result["risks"].append({"level": "high", "item": name, "message": "root 账号允许远程访问，建议收敛来源。"})
                    if user.get("password_expired") == "Y":
                        result["risks"].append({"level": "warning", "item": name, "message": "账号密码已过期。"})
    except Exception as exc:
        result["error"] = str(exc)
    return result


def generate_mycnf_backup_plan(instance: DBInstance) -> dict:
    host = instance.host or "127.0.0.1"
    port = instance.port or 3306
    user = instance.username or "root"
    script = f"""# 仅生成备份方案，不自动执行
mkdir -p /backup/mysql/config_$(date +%Y%m%d)
cp -a /etc/my.cnf /backup/mysql/config_$(date +%Y%m%d)/my.cnf 2>/dev/null || true
cp -a /etc/mysql/my.cnf /backup/mysql/config_$(date +%Y%m%d)/my.cnf.mysql 2>/dev/null || true
mysql -h {host} -P {port} -u {user} -p -e "SHOW GLOBAL VARIABLES" > /backup/mysql/config_$(date +%Y%m%d)/global_variables.txt
mysql -h {host} -P {port} -u {user} -p -e "SHOW GLOBAL STATUS" > /backup/mysql/config_$(date +%Y%m%d)/global_status.txt"""
    return {
        "status": "plan_only",
        "message": "已生成 my.cnf 与运行参数备份方案，请人工确认路径和权限后执行。",
        "script": script,
    }


def generate_metadata_backup_plan(instance: DBInstance) -> dict:
    host = instance.host or "127.0.0.1"
    port = instance.port or 3306
    user = instance.username or "root"
    script = f"""# 仅生成元数据备份方案，不自动执行
mkdir -p /backup/mysql/metadata_$(date +%Y%m%d)
mysqldump -h {host} -P {port} -u {user} -p --no-data --routines --triggers --events --all-databases > /backup/mysql/metadata_$(date +%Y%m%d)/schema_metadata.sql
mysql -h {host} -P {port} -u {user} -p -e "SELECT @@server_uuid, @@server_id, @@gtid_executed" > /backup/mysql/metadata_$(date +%Y%m%d)/server_identity.txt
mysql -h {host} -P {port} -u {user} -p -e "SELECT user, host, plugin, account_locked, password_expired FROM mysql.user" > /backup/mysql/metadata_$(date +%Y%m%d)/users.txt"""
    return {
        "status": "plan_only",
        "message": "已生成 MySQL 元数据备份方案，请人工确认后执行。",
        "script": script,
    }


def generate_inspect_ai_plan(instance: DBInstance) -> dict:
    overview = get_mysql_overview(instance)
    warnings = []
    basic = overview.get("basic", {})
    status = overview.get("status", {})
    if str(basic.get("read_only", "")).upper() == "ON":
        warnings.append("当前实例 read_only=ON，请确认是否为备库或只读实例。")
    if int(status.get("threads_running") or 0) > 32:
        warnings.append("活跃线程数较高，建议结合慢 SQL 和锁等待进一步分析。")
    if overview.get("lock_wait_count", 0) > 0:
        warnings.append("存在锁等待，建议查看锁与事务页面。")
    if not warnings:
        warnings.append("未发现明确高风险项，可继续检查慢 SQL、容量和备份策略。")
    return {
        "status": "plan_only",
        "message": "已生成 MySQL 巡检诊断摘要。该结果基于规则，不直接执行任何变更。",
        "overview": overview,
        "warnings": warnings,
        "script": "\n".join(f"- {item}" for item in warnings),
    }
