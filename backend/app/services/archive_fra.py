import logging
from datetime import datetime

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.archive_fra")


def _archive_dest_risk(parameter_state: str, runtime_status: str) -> str:
    parameter_state = (parameter_state or "").upper()
    runtime_status = (runtime_status or "").upper()
    if parameter_state == "ENABLE" and runtime_status == "VALID":
        return "normal"
    if parameter_state == "ENABLE" and runtime_status == "ERROR":
        return "critical"
    if parameter_state == "ENABLE" and runtime_status in ("DEFERRED", "INACTIVE"):
        return "warning"
    if parameter_state in ("DEFER", "ALTERNATE", "RESET") and runtime_status in ("INACTIVE", "DEFERRED"):
        return "info"
    if runtime_status == "ERROR":
        return "critical"
    return "warning" if runtime_status not in ("", "VALID") else "normal"


def get_archive_config(instance: DBInstance) -> dict:
    logger.info("查询归档配置 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT name, value FROM v$parameter WHERE name LIKE 'log_archive_dest%' OR name IN ('log_archive_format','db_recovery_file_dest','db_recovery_file_dest_size')")
            params = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            params = []

        try:
            cursor.execute(
                """
                SELECT dest_id,
                       dest_name,
                       destination,
                       target,
                       binding,
                       status,
                       error,
                       valid_type,
                       valid_role,
                       db_unique_name
                FROM v$archive_dest
                WHERE dest_id <= 10
                ORDER BY dest_id
                """
            )
            dests = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            try:
                cursor.execute("SELECT dest_id, dest_name, destination, binding, status FROM v$archive_dest WHERE dest_id <= 10 ORDER BY dest_id")
                dests = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            except Exception:
                dests = []

        try:
            cursor.execute("SELECT archiver, log_mode FROM v$database, v$instance")
            mode = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            mode = []

    param_state_map = {}
    for row in params:
        name = str(row.get("name") or row.get("NAME") or "").lower()
        if name.startswith("log_archive_dest_state_"):
            dest_id = name.rsplit("_", 1)[-1]
            param_state_map[str(dest_id)] = str(row.get("value") or row.get("VALUE") or "").upper()

    enriched_dests = []
    for row in dests:
        dest_id = str(row.get("dest_id") or row.get("DEST_ID") or "")
        runtime_status = str(row.get("status") or row.get("STATUS") or "").upper()
        destination = row.get("destination") or row.get("DESTINATION")
        # 未配置目标（destination 为空）不应显示 ENABLE：Oracle 的 log_archive_dest_state_N
        # 对全部 31 个槽位默认返回 ENABLE，会把空目标也染成绿色 ENABLE，掩盖真实的 ENABLE/DEFER。
        if destination:
            parameter_state = param_state_map.get(dest_id, "ENABLE")
        else:
            parameter_state = ""
        enriched_dests.append({
            **row,
            "parameter_state": parameter_state,
            "path_state": parameter_state,
            "runtime_status": runtime_status,
            "risk_level": _archive_dest_risk(parameter_state, runtime_status),
        })

    return {"parameters": params, "destinations": enriched_dests, "mode": mode}


def get_flashback_status(instance: DBInstance) -> dict:
    logger.info("query oracle flashback status | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT flashback_on, log_mode FROM v$database")
            database = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            database = []
        try:
            cursor.execute("SELECT name, value FROM v$parameter WHERE name IN ('db_flashback_retention_target','db_recovery_file_dest','db_recovery_file_dest_size')")
            parameters = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            parameters = []
        try:
            cursor.execute(
                "SELECT name AS dest_name, SPACE_LIMIT/1024/1024/1024 AS limit_gb, "
                "SPACE_USED/1024/1024/1024 AS used_gb, "
                "ROUND(SPACE_USED/NULLIF(SPACE_LIMIT,0)*100, 2) AS used_pct "
                "FROM v$recovery_file_dest"
            )
            fra = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            fra = []
        try:
            cursor.execute(
                "SELECT file_type, PERCENT_SPACE_USED, PERCENT_SPACE_RECLAIMABLE, NUMBER_OF_FILES "
                "FROM v$flash_recovery_area_usage WHERE file_type = 'FLASHBACK LOG'"
            )
            flashback_usage = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            flashback_usage = []

    first_database = database[0] if database else {}
    flashback_on = str(first_database.get("flashback_on") or first_database.get("FLASHBACK_ON") or "").upper()
    fra_used_pct = float((fra[0] if fra else {}).get("used_pct") or (fra[0] if fra else {}).get("USED_PCT") or 0)
    fra_limit_gb = float((fra[0] if fra else {}).get("limit_gb") or (fra[0] if fra else {}).get("LIMIT_GB") or 0)
    flashback_space = []
    for row in flashback_usage:
        used_pct = float(row.get("percent_space_used") or row.get("PERCENT_SPACE_USED") or 0)
        reclaim_pct = float(row.get("percent_space_reclaimable") or row.get("PERCENT_SPACE_RECLAIMABLE") or 0)
        used_gb = round(fra_limit_gb * used_pct / 100, 2) if fra_limit_gb else 0
        reclaimable_gb = round(fra_limit_gb * reclaim_pct / 100, 2) if fra_limit_gb else 0
        flashback_space.append({
            **row,
            "used_gb": used_gb,
            "reclaimable_gb": reclaimable_gb,
            "risk_level": "critical" if used_pct >= 40 and fra_used_pct >= 85 else "warning" if used_pct >= 25 else "normal",
        })
    risk_level = "normal"
    if flashback_on != "YES":
        risk_level = "info"
    if fra_used_pct >= 95:
        risk_level = "critical"
    elif fra_used_pct >= 85:
        risk_level = "warning"
    return {
        "database": database,
        "parameters": parameters,
        "fra": fra,
        "flashback_usage": flashback_usage,
        "flashback_space": flashback_space,
        "flashback_on": flashback_on,
        "fra_used_pct": fra_used_pct,
        "risk_level": risk_level,
    }


def generate_flashback_plan(instance: DBInstance, action: str = "enable") -> dict:
    action = (action or "enable").lower()
    if action == "disable":
        sql = "ALTER DATABASE FLASHBACK OFF;\nSELECT flashback_on FROM v$database;"
        warnings = ["关闭 Flashback 会降低误操作快速恢复能力，请确认已有备份和恢复策略。"]
    elif action == "retention":
        sql = "ALTER SYSTEM SET db_flashback_retention_target = 1440 SCOPE=BOTH;\nSHOW PARAMETER db_flashback_retention_target;"
        warnings = ["提高保留时间会增加 FRA 空间压力，请同步评估 FRA 容量。"]
    elif action == "extend_fra":
        sql = "ALTER SYSTEM SET db_recovery_file_dest_size = 100G SCOPE=BOTH;\nSELECT name, space_limit, space_used FROM v$recovery_file_dest;"
        warnings = ["扩展 FRA 前请确认底层文件系统或 ASM 磁盘组容量。"]
    else:
        sql = "ALTER DATABASE FLASHBACK ON;\nSELECT flashback_on FROM v$database;"
        warnings = ["开启 Flashback 需要数据库处于归档模式，并会占用 FRA 空间。"]
    return {
        "status": "plan_only",
        "message": "Flashback 方案已生成，平台不直接执行数据库变更。",
        "sql": sql,
        "warnings": warnings,
    }


def generate_archive_enable_script(instance: DBInstance) -> dict:
    logger.info("生成归档开启脚本 | instance=%s", instance.instance_name)
    sql = (
        f"-- 开启归档日志模式 (需要停机维护窗口)\n"
        f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"SHUTDOWN IMMEDIATE;\n"
        f"STARTUP MOUNT;\n"
        f"ALTER DATABASE ARCHIVELOG;\n"
        f"ALTER DATABASE OPEN;\n"
        f"-- 验证:\n"
        f"SELECT log_mode FROM v$database;"
    )
    return {"status": "plan_only", "message": "已生成开启归档日志脚本。需要停机操作！", "sql": sql, "warnings": ["开启归档日志需要 SHUTDOWN + STARTUP MOUNT，请在维护窗口执行。", "建议先配置 log_archive_dest 参数。"]}


def generate_archive_disable_script(instance: DBInstance) -> dict:
    logger.info("生成归档关闭脚本 | instance=%s", instance.instance_name)
    sql = (
        f"-- 关闭归档日志模式 (需要停机维护窗口)\n"
        f"-- ⚠️ 生产环境强烈建议保持归档模式\n"
        f"SHUTDOWN IMMEDIATE;\n"
        f"STARTUP MOUNT;\n"
        f"ALTER DATABASE NOARCHIVELOG;\n"
        f"ALTER DATABASE OPEN;\n"
    )
    return {"status": "plan_only", "message": "已生成关闭归档日志脚本。⚠️ 生产环境不建议关闭归档！", "sql": sql, "warnings": ["关闭归档将无法进行在线备份和 RMAN 恢复！", "生产环境强烈建议保持 ARCHIVELOG 模式。", "需要停机操作。"]}


def generate_archive_path_script(instance: DBInstance, dest_id: int, path: str) -> dict:
    logger.info("生成归档路径修改脚本 | instance=%s | dest_id=%d", instance.instance_name, dest_id)
    param_name = f"log_archive_dest_{dest_id}"
    sql = f"ALTER SYSTEM SET {param_name}='LOCATION={path}' SCOPE=BOTH;\n-- 验证:\nSHOW PARAMETER {param_name};"
    return {"status": "plan_only", "message": "已生成归档路径修改脚本。", "sql": sql, "warnings": ["请确认目标路径存在且有写入权限。", "建议使用 LOCATION 或 SERVICE 参数。"]}


def get_fra_status(instance: DBInstance) -> dict:
    logger.info("查询 FRA 状态 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT name, SPACE_LIMIT/1024/1024/1024 AS limit_gb, "
                "SPACE_USED/1024/1024/1024 AS used_gb, "
                "SPACE_RECLAIMABLE/1024/1024/1024 AS reclaimable_gb, "
                "NUMBER_OF_FILES AS file_count "
                "FROM v$recovery_file_dest"
            )
            fra = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            fra = []

        try:
            cursor.execute(
                "SELECT file_type, PERCENT_SPACE_USED, PERCENT_SPACE_RECLAIMABLE, NUMBER_OF_FILES "
                "FROM v$flash_recovery_area_usage"
            )
            usage = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            usage = []

    return {"recovery_file_dest": fra, "usage_by_type": usage}


def get_archive_space_detail(instance: DBInstance) -> dict:
    logger.info("查询归档空间详情 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT name AS dest_name, SPACE_LIMIT/1024/1024/1024 AS limit_gb, "
                "SPACE_USED/1024/1024/1024 AS used_gb, "
                "SPACE_RECLAIMABLE/1024/1024/1024 AS reclaimable_gb, "
                "NUMBER_OF_FILES AS file_count, "
                "ROUND(SPACE_USED/NULLIF(SPACE_LIMIT,0)*100, 2) AS used_pct "
                "FROM v$recovery_file_dest"
            )
            fra = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            fra = []

        try:
            cursor.execute(
                "SELECT file_type, PERCENT_SPACE_USED, PERCENT_SPACE_RECLAIMABLE, NUMBER_OF_FILES "
                "FROM v$flash_recovery_area_usage ORDER BY PERCENT_SPACE_USED DESC"
            )
            usage = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            usage = []

        try:
            cursor.execute(
                "SELECT COUNT(*) AS log_count, "
                "ROUND(NVL(SUM(blocks * block_size)/1024/1024/1024, 0), 2) AS total_gb, "
                "ROUND(NVL(SUM(CASE WHEN archived='YES' THEN blocks * block_size END)/1024/1024/1024, 0), 2) AS archived_gb, "
                "ROUND(NVL(SUM(CASE WHEN archived='NO' THEN blocks * block_size END)/1024/1024/1024, 0), 2) AS not_archived_gb "
                "FROM v$archived_log"
            )
            log_summary = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            log_summary = []

        try:
            cursor.execute(
                "SELECT sequence#, name, TO_CHAR(completion_time,'YYYY-MM-DD HH24:MI:SS') AS completion_time, "
                "blocks * block_size/1024/1024 AS size_mb, archived, deleted "
                "FROM (SELECT * FROM v$archived_log ORDER BY sequence# DESC) WHERE ROWNUM <= 20"
            )
            recent_logs = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            recent_logs = []

        try:
            cursor.execute(
                "SELECT group#, sequence#, members, bytes/1024/1024 AS size_mb, archived, status "
                "FROM v$log ORDER BY group#"
            )
            redo_groups = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            current_group = next((g for g in redo_groups if g.get("status") == "CURRENT"), None)
            current_sequence = current_group.get("sequence#", 0) if current_group else 0
        except Exception:
            redo_groups = []
            current_sequence = 0

    fra_used_pct = 0
    if fra and fra[0].get("limit_gb", 0) > 0:
        fra_used_pct = round(fra[0]["used_gb"] / fra[0]["limit_gb"] * 100, 2)

    status = "normal"
    if fra_used_pct >= 95:
        status = "critical"
    elif fra_used_pct >= 85:
        status = "warning"

    return {
        "fra": fra,
        "fra_used_pct": fra_used_pct,
        "fra_status": status,
        "usage_by_type": usage,
        "log_summary": log_summary,
        "recent_logs": recent_logs,
        "redo_groups": redo_groups,
        "current_sequence": current_sequence,
    }


def generate_emergency_cleanup_script(instance: DBInstance, mode: str = "rman") -> dict:
    logger.info("生成 ORA-00257 应急处理脚本 | instance=%s | mode=%s", instance.instance_name, mode)

    if mode == "rman":
        sql = (
            f"-- ORA-00257 应急处理：RMAN 清理归档日志\n"
            f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"-- 适用场景: 归档日志占满 FRA 空间导致数据库挂起\n\n"
            f"-- 步骤1: 检查 FRA 使用率\n"
            f"SELECT name, SPACE_LIMIT/1024/1024/1024 AS limit_gb, SPACE_USED/1024/1024/1024 AS used_gb, "
            f"ROUND(SPACE_USED/SPACE_LIMIT*100,2) AS used_pct FROM v$recovery_file_dest;\n\n"
            f"-- 步骤2: RMAN 清理已备份的归档日志（保留最近1天）\n"
            f"rman target / <<EOF\n"
            f"CROSSCHECK ARCHIVELOG ALL;\n"
            f"DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;\n"
            f"DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-1';\n"
            f"EXIT;\n"
            f"EOF\n\n"
            f"-- 步骤3: 如果仍然空间不足，增加 FRA 大小\n"
            f"ALTER SYSTEM SET db_recovery_file_dest_size = <新的更大值> SCOPE=BOTH;\n\n"
            f"-- 步骤4: 验证\n"
            f"SELECT name, ROUND(SPACE_USED/SPACE_LIMIT*100,2) AS used_pct FROM v$recovery_file_dest;"
        )
        warnings = [
            "ORA-00257 表示归档日志空间已满，数据库将无法进行任何 DML 操作。",
            "RMAN DELETE 操作不可逆，请确保已有有效备份。",
            "保留最近1天的归档以确保可恢复性。",
            "如果不确定备份状态，建议先增加 FRA 大小而非删除归档。",
        ]
    elif mode == "force":
        sql = (
            f"-- ORA-00257 应急处理：强制删除归档日志文件\n"
            f"-- ⚠️ 极端紧急情况使用，会删除所有归档日志！\n"
            f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"-- 步骤1: 查看归档日志路径\n"
            f"SELECT value FROM v$parameter WHERE name = 'db_recovery_file_dest';\n\n"
            f"-- 步骤2: 先用 RMAN crosscheck 标记\n"
            f"rman target / <<EOF\n"
            f"CROSSCHECK ARCHIVELOG ALL;\n"
            f"DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;\n"
            f"DELETE NOPROMPT ARCHIVELOG ALL;\n"
            f"EXIT;\n"
            f"EOF\n\n"
            f"-- 步骤3: 增加 FRA 大小\n"
            f"ALTER SYSTEM SET db_recovery_file_dest_size = <更大的值> SCOPE=BOTH;\n"
        )
        warnings = [
            "⚠️ 强制删除所有归档日志，将无法进行时间点恢复！",
            "仅在数据库完全挂起且无法通过其他方式解决时使用。",
            "执行后建议立即执行全量备份。",
        ]
    else:
        sql = (
            f"-- ORA-00257 应急处理：增加 FRA 大小\n"
            f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"-- 步骤1: 查看当前 FRA 配置\n"
            f"SELECT name, value FROM v$parameter WHERE name IN ('db_recovery_file_dest', 'db_recovery_file_dest_size');\n\n"
            f"-- 步骤2: 增加 FRA 大小（根据磁盘空间调整）\n"
            f"ALTER SYSTEM SET db_recovery_file_dest_size = 50G SCOPE=BOTH;\n\n"
            f"-- 步骤3: 验证\n"
            f"SELECT name, SPACE_LIMIT/1024/1024/1024 AS limit_gb, SPACE_USED/1024/1024/1024 AS used_gb FROM v$recovery_file_dest;"
        )
        warnings = [
            "增加 FRA 大小是最安全的应急方式，不删除任何归档日志。",
            "请确保磁盘有足够空间。",
            "建议同时安排归档日志备份和清理策略。",
        ]

    return {
        "status": "plan_only",
        "message": f"已生成 ORA-00257 应急处理脚本（{mode} 模式）。",
        "sql": sql,
        "mode": mode,
        "warnings": warnings,
    }
