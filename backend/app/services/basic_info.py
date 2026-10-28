import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance
from app.services import ssh_executor

logger = logging.getLogger("app.services.basic_info")


def _format_patch_info(patches: list[dict], manual_patch_info: str | None) -> str | None:
    if manual_patch_info:
        return manual_patch_info
    if not patches:
        return None

    latest_patch = patches[0]
    patch_id = latest_patch.get("patch_id") or latest_patch.get("patch_uid") or "未知补丁"
    status = latest_patch.get("status") or "未知状态"
    action_time = latest_patch.get("action_time") or "未知时间"
    description = latest_patch.get("description") or ""
    return f"最新补丁: {patch_id} | {status} | {action_time}\n{description}".strip()


def get_basic_info(instance: DBInstance) -> dict:
    logger.info("获取基础信息 | instance=%s | host=%s:%s", instance.instance_name, instance.host, instance.port)
    info: dict = {}
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name, dbid, TO_CHAR(created,'YYYY-MM-DD HH24:MI:SS') AS created, "
            "log_mode, open_mode, database_role, platform_name, force_logging "
            "FROM v$database"
        )
        db_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        if db_rows:
            info["database"] = db_rows[0]

        cursor.execute(
            "SELECT instance_name, host_name, version, status, "
            "TO_CHAR(startup_time,'YYYY-MM-DD HH24:MI:SS') AS startup_time, "
            "archiver, database_status, instance_role "
            "FROM v$instance"
        )
        inst_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        if inst_rows:
            info["instance"] = inst_rows[0]
            if info.get("database"):
                info["database"]["startup_time"] = inst_rows[0].get("startup_time")

        try:
            cursor.execute(
                "SELECT parameter, value FROM nls_database_parameters "
                "WHERE parameter IN ('NLS_CHARACTERSET', 'NLS_NCHAR_CHARACTERSET', 'NLS_LANGUAGE', 'NLS_TERRITORY')"
            )
            info["nls"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception as exc:
            logger.warning("NLS 参数查询失败 | error=%s", exc)
            info["nls"] = []

        try:
            cursor.execute("SELECT banner_full FROM v$version WHERE ROWNUM = 1")
            ver_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            info["version_full"] = ver_rows[0]["banner_full"] if ver_rows else None
        except Exception:
            info["version_full"] = info.get("instance", {}).get("version")

        info["os"] = {
            "database_host_name": info.get("instance", {}).get("host_name"),
            "database_platform": info.get("database", {}).get("platform_name"),
            "db_connect_host": instance.host,
            "ssh_configured": bool(instance.ssh_host),
            "ssh_host": instance.ssh_host,
            "ssh_port": instance.ssh_port or 22,
            "ssh_username": instance.ssh_username,
            "database_os_stats": [],
            "ssh_system_info": None,
        }

        try:
            cursor.execute(
                "SELECT stat_name, value FROM v$osstat "
                "WHERE stat_name IN ("
                "'NUM_CPUS','NUM_CPU_CORES','NUM_CPU_SOCKETS','PHYSICAL_MEMORY_BYTES',"
                "'LOAD','IDLE_TIME','BUSY_TIME','USER_TIME','SYS_TIME','IOWAIT_TIME'"
                ") ORDER BY stat_name"
            )
            info["os"]["database_os_stats"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception as exc:
            logger.warning("OS 统计信息查询失败 | error=%s", exc)
            info["os"]["database_os_stats"] = []

        if instance.ssh_host:
            try:
                info["os"]["ssh_system_info"] = ssh_executor.get_system_info(instance)
            except Exception as exc:
                logger.warning("SSH 操作系统信息获取失败 | host=%s | error=%s", instance.ssh_host, exc)
                info["os"]["ssh_system_info"] = {"ok": False, "message": str(exc), "data": {}}

        try:
            cursor.execute(
                "SELECT patch_id, patch_uid, description, status, "
                "TO_CHAR(action_time, 'YYYY-MM-DD HH24:MI:SS') AS action_time "
                "FROM dba_registry_sqlpatch ORDER BY action_time DESC"
            )
            info["patches"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            try:
                cursor.execute(
                    "SELECT patch_id, patch_uid, description, status, "
                    "TO_CHAR(action_time, 'YYYY-MM-DD HH24:MI:SS') AS action_time "
                    "FROM sys.registry$sqlpatch ORDER BY action_time DESC"
                )
                info["patches"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            except Exception as exc:
                logger.warning("补丁信息查询失败 | error=%s", exc)
                info["patches"] = []

        if instance.patch_info:
            info["patch_info_manual"] = instance.patch_info
        if info.get("instance"):
            info["instance"]["patch_info"] = _format_patch_info(info.get("patches", []), instance.patch_info)

    logger.info("基础信息获取完成 | db=%s | instance=%s", info.get("database", {}).get("name"), info.get("instance", {}).get("instance_name"))
    return info
