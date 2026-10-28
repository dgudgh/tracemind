import logging
from datetime import datetime
import re

from sqlalchemy.orm import Session

from app.collectors.oracle.oracle_health_collector import OracleHealthCollector
from app.collectors.oracle.oracle_connection import effective_oracle_account_type, oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_operations")


def list_oracle_instances(db: Session) -> list[DBInstance]:
    return (
        db.query(DBInstance)
        .filter(DBInstance.db_type == "oracle", DBInstance.enabled == 1)
        .order_by(DBInstance.instance_name.asc())
        .all()
    )


def collect_overview(instance: DBInstance) -> dict:
    logger.info("Oracle 健康检查 | instance=%s | host=%s:%s", instance.instance_name, instance.host, instance.port)
    metrics = OracleHealthCollector(instance).collect()
    account_type = effective_oracle_account_type(instance)
    tablespaces = [row for row in metrics.get("tablespace_usage", []) if "error" not in row]
    fra = [row for row in metrics.get("fra_usage", []) if "error" not in row]
    locks = [row for row in metrics.get("locks", []) if "error" not in row]
    invalid_objects = [row for row in metrics.get("invalid_objects", []) if "error" not in row]

    max_tablespace_pct = max([float(row.get("used_pct") or 0) for row in tablespaces], default=0)
    fra_pct = max([float(row.get("used_pct") or 0) for row in fra], default=0)
    risk_level = "critical" if max(max_tablespace_pct, fra_pct) >= 90 or locks else "warning" if account_type == "business" or max(max_tablespace_pct, fra_pct) >= 80 or invalid_objects else "normal"

    return {
        "instance": _instance_payload(instance),
        "risk_level": risk_level,
        "summary": {
            "account_type": account_type,
            "capability": "business_limited" if account_type == "business" else "full_inspection",
            "tablespace_count": len(tablespaces),
            "max_tablespace_used_pct": round(max_tablespace_pct, 2),
            "fra_used_pct": round(fra_pct, 2),
            "blocking_sessions": len(locks),
            "invalid_object_groups": len(invalid_objects),
        },
        "metrics": metrics,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def run_action(instance: DBInstance, action: str, sql_text: str | None = None) -> dict:
    logger.info("Oracle 运维操作 | instance=%s | action=%s", instance.instance_name, action)
    if action == "health":
        overview = collect_overview(instance)
        return {
            "status": "success",
            "title": "一键健康检查",
            "output": _format_health_output(overview),
            "data": overview,
        }
    if action == "tablespace":
        data = collect_tablespaces(instance)
        return {
            "status": "success",
            "title": "表空间查看",
            "output": _format_tablespace_output(data["tablespaces"]),
            "data": data,
        }
    if action == "archive":
        data = collect_archive(instance)
        return {
            "status": "success",
            "title": "归档日志空间",
            "output": _format_archive_output(data),
            "data": data,
        }
    if action == "trace":
        data = collect_alert_log(instance)
        return {
            "status": "success",
            "title": "一键查看 Trace",
            "output": data["content"],
            "data": data,
        }
    if action == "pfile":
        return _script_only_result(
            "一键备份 PFILE",
            "CREATE PFILE='/tmp/pfile_backup_YYYYMMDD_HH24MISS.ora' FROM SPFILE;",
            "该操作会在数据库服务器生成参数文件，当前 V1 仅生成人工执行脚本。",
        )
    if action == "controlfile":
        return _script_only_result(
            "一键备份控制文件",
            "ALTER DATABASE BACKUP CONTROLFILE TO TRACE;\n-- 或在 RMAN 中执行：BACKUP CURRENT CONTROLFILE;",
            "该操作属于备份变更动作，当前 V1 仅生成人工执行脚本。",
        )
    if action == "sql":
        return _script_only_result(
            "SQL 执行",
            sql_text or "-- 请在专用 SQL Runner 中执行经过审核的 SQL",
            "当前日常运维不开放任意 SQL 直连执行，避免误操作。",
        )
    raise ValueError(f"Unsupported action: {action}")


def collect_tablespaces(instance: DBInstance) -> dict:
    if effective_oracle_account_type(instance) == "business":
        return {
            "instance": _instance_payload(instance),
            "tablespaces": [],
            "summary": {"total": 0, "warning": 1, "critical": 0},
            "message": "业务用户模式无法可靠读取 DBA 表空间视图。请使用 System 用户或具备 DBA_DATA_FILES/DBA_FREE_SPACE 权限的巡检账号。",
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    metrics = OracleHealthCollector(instance).collect()
    tablespaces = [row for row in metrics.get("tablespace_usage", []) if "error" not in row]
    enriched = []
    for row in tablespaces:
        used_pct = float(row.get("used_pct") or 0)
        status = "critical" if used_pct >= 95 else "warning" if used_pct >= 85 else "normal"
        enriched.append({
            **row,
            "status": status,
            "recommendation": _tablespace_recommendation(row),
            "expansion_sql_autoextend_on": build_tablespace_expansion_sql(row.get("tablespace_name", ""), None, 1, 30, "on"),
            "expansion_sql_autoextend_off": build_tablespace_expansion_sql(row.get("tablespace_name", ""), None, 1, None, "off"),
        })
    return {
        "instance": _instance_payload(instance),
        "tablespaces": enriched,
        "summary": {
            "total": len(enriched),
            "warning": len([row for row in enriched if row["status"] == "warning"]),
            "critical": len([row for row in enriched if row["status"] == "critical"]),
        },
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def collect_archive(instance: DBInstance) -> dict:
    if effective_oracle_account_type(instance) == "business":
        return {
            "instance": _instance_payload(instance),
            "archive_usage": {
                "percent_used": 0,
                "space_limit_gb": 0,
                "space_used_gb": 0,
                "free_space_gb": 0,
                "file_count": 0,
                "status_level": "limited",
            },
            "archive_growth": [],
            "recommendations": ["业务用户模式无法读取 FRA/归档视图，请使用 System 用户或具备 v$recovery_file_dest、v$archived_log 权限的巡检账号。"],
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    metrics = OracleHealthCollector(instance).collect()
    fra_rows = [row for row in metrics.get("fra_usage", []) if "error" not in row]
    archive_rows = [row for row in metrics.get("archive_growth", []) if "error" not in row]
    fra = fra_rows[0] if fra_rows else {}
    used_pct = float(fra.get("used_pct") or 0)
    limit_mb = float(fra.get("space_limit_mb") or 0)
    used_mb = float(fra.get("space_used_mb") or 0)
    status_level = "urgent" if used_pct >= 90 else "warning" if used_pct >= 80 else "normal"
    recommendations = ["归档空间正常，保持当前备份与清理策略。"]
    if status_level == "warning":
        recommendations = ["FRA 使用率偏高，建议检查 RMAN 备份与归档保留策略。", "优先确认归档是否已备份，再制定清理窗口。"]
    if status_level == "urgent":
        recommendations = ["FRA 使用率已接近或超过高危阈值，存在归档写满风险。", "立即确认备份状态、业务峰值和可清理归档范围。"]
    return {
        "instance": _instance_payload(instance),
        "archive_usage": {
            "percent_used": round(used_pct, 2),
            "space_limit_gb": round(limit_mb / 1024, 2),
            "space_used_gb": round(used_mb / 1024, 2),
            "free_space_gb": round(max(limit_mb - used_mb, 0) / 1024, 2),
            "file_count": fra.get("number_of_files") or 0,
            "status_level": status_level,
        },
        "archive_growth": archive_rows,
        "recommendations": recommendations,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def collect_alert_log(instance: DBInstance) -> dict:
    sql = """
        SELECT originating_timestamp, message_text
        FROM v$diag_alert_ext
        WHERE originating_timestamp >= SYSTIMESTAMP - INTERVAL '3' DAY
        ORDER BY originating_timestamp DESC
        FETCH FIRST 300 ROWS ONLY
    """
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        return {
            "content": f"Alert Log 采集失败：{exc}\n请确认账号具备 v$diag_alert_ext 查询权限，或在服务器侧查看 alert log 文件。",
            "lines": 1,
            "errors": 1,
            "error_lines": [str(exc)],
        }
    lines = [
        f"{row.get('originating_timestamp')} {row.get('message_text')}"
        for row in rows
    ]
    error_pattern = re.compile(r"(ORA-\d+|ERROR)", re.IGNORECASE)
    error_lines = [line for line in lines if error_pattern.search(line)]
    return {
        "content": "\n".join(error_lines) if error_lines else "最近 3 天未发现 ORA- 或 ERROR 报错信息。",
        "lines": len(lines),
        "errors": len(error_lines),
        "error_lines": error_lines,
    }


def build_archive_cleanup_plan(retention_days: int, cleanup_mode: str, force_cleanup: bool) -> dict:
    aggressive_line = "DELETE NOPROMPT ARCHIVELOG ALL BACKED UP 1 TIMES TO DEVICE TYPE DISK;" if force_cleanup or cleanup_mode == "aggressive" else ""
    script = "\n".join([
        "RMAN TARGET /",
        "CROSSCHECK ARCHIVELOG ALL;",
        "DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;",
        f"DELETE NOPROMPT ARCHIVELOG UNTIL TIME 'SYSDATE-{retention_days}';",
        aggressive_line,
        "EXIT;",
    ]).strip()
    return {
        "status": "plan_only",
        "message": "已生成归档清理 RMAN 脚本。V1 不直接执行清理，请人工确认备份状态后在维护窗口执行。",
        "retention_days": retention_days,
        "cleanup_mode": cleanup_mode,
        "force_cleanup": force_cleanup,
        "rman_script": script,
    }


def build_tablespace_expansion_plan(
    tablespace_name: str,
    datafile_path: str | None,
    initial_size_gb: float,
    max_size_gb: float | None,
    autoextend: str,
) -> dict:
    sql = build_tablespace_expansion_sql(
        tablespace_name, datafile_path, initial_size_gb, max_size_gb, autoextend
    )
    return {
        "status": "plan_only",
        "message": "已生成表空间扩容 SQL。V1 不直接执行扩容，请人工确认路径、ASM/文件系统空间和回滚方案。",
        "sql": sql,
    }


def build_tablespace_expansion_sql(
    tablespace_name: str,
    datafile_path: str | None,
    initial_size_gb: float,
    max_size_gb: float | None,
    autoextend: str,
) -> str:
    safe_name = (tablespace_name or "USERS").upper()
    path = datafile_path or f"/oradata/ORCL/{safe_name.lower()}_02.dbf"
    size_mb = int(initial_size_gb * 1024)
    if autoextend == "on":
        max_clause = f" MAXSIZE {int((max_size_gb or 30) * 1024)}M"
        auto_clause = f"AUTOEXTEND ON NEXT 1024M{max_clause}"
    else:
        auto_clause = "AUTOEXTEND OFF"
    return f"ALTER TABLESPACE {safe_name} ADD DATAFILE '{path}' SIZE {size_mb}M {auto_clause};"


def _instance_payload(instance: DBInstance) -> dict:
    return {
        "id": instance.id,
        "instance_name": instance.instance_name,
        "host": instance.host,
        "port": instance.port,
        "service_name": instance.service_name,
        "sid": instance.sid,
        "account_type": effective_oracle_account_type(instance),
        "env_type": instance.env_type,
        "factory_name": instance.factory_name,
    }


def _tablespace_recommendation(row: dict) -> str:
    used_pct = float(row.get("used_pct") or 0)
    if used_pct >= 95:
        return "立即评估扩容、清理历史数据或迁移大对象，避免空间写满。"
    if used_pct >= 85:
        return "进入预警区间，建议生成扩容方案并确认数据文件路径。"
    return "使用率正常，保持趋势观察。"


def _format_health_output(overview: dict) -> str:
    summary = overview["summary"]
    return "\n".join([
        "=== Oracle 一键健康检查 ===",
        f"实例: {overview['instance']['instance_name']}",
        f"风险等级: {overview['risk_level']}",
        f"表空间数量: {summary['tablespace_count']}",
        f"最高表空间使用率: {summary['max_tablespace_used_pct']}%",
        f"FRA 使用率: {summary['fra_used_pct']}%",
        f"阻塞会话: {summary['blocking_sessions']}",
        f"无效对象分组: {summary['invalid_object_groups']}",
    ])


def _format_tablespace_output(rows: list[dict]) -> str:
    lines = ["=== Oracle 表空间使用率 ==="]
    for row in rows:
        lines.append(
            f"{row.get('tablespace_name')} total={row.get('total_mb')}MB used={row.get('used_mb')}MB free={row.get('free_mb')}MB used_pct={row.get('used_pct')}%"
        )
    return "\n".join(lines)


def _format_archive_output(data: dict) -> str:
    usage = data["archive_usage"]
    lines = [
        "=== Oracle 归档/FRA 空间 ===",
        f"使用率: {usage['percent_used']}%",
        f"总空间: {usage['space_limit_gb']}GB",
        f"已用空间: {usage['space_used_gb']}GB",
        f"可用空间: {usage['free_space_gb']}GB",
        f"状态: {usage['status_level']}",
        "",
        "=== 建议 ===",
        *data["recommendations"],
    ]
    return "\n".join(lines)


def _script_only_result(title: str, script: str, message: str) -> dict:
    return {
        "status": "plan_only",
        "title": title,
        "message": message,
        "output": f"=== {title} ===\n{message}\n\n{script}",
        "script": script,
    }
