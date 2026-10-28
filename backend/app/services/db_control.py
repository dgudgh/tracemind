import logging
import re
import socket
from datetime import datetime

from app.collectors.oracle.oracle_connection import effective_oracle_account_type, oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.db_control")

BOOT_PREREQUISITE_SOCKET_TIMEOUT_SECONDS = 2
BOOT_PREREQUISITE_SSH_TIMEOUT_SECONDS = 2


def _archive_dest_id_from_parameter(name: str) -> int | None:
    match = re.fullmatch(r"log_archive_dest_(\d+)", (name or "").lower())
    return int(match.group(1)) if match else None


def _normalize_db_unique_name(value: str) -> str:
    value = (value or "").strip().upper()
    return "" if value in {"", "NONE", "UNKNOWN", "NULL"} else value


def _is_remote_archive_dest_param(value: str, current_db_unique_name: str = "") -> bool:
    return bool((value or "").strip())


def _remote_archive_dest_ids(params: list[dict], current_db_unique_name: str = "") -> set[int]:
    dest_ids: set[int] = set()
    for row in params:
        dest_id = _archive_dest_id_from_parameter(row.get("name", ""))
        param_value = row.get("value") or row.get("display_value") or ""
        if dest_id is not None and 2 <= dest_id <= 10 and _is_remote_archive_dest_param(param_value, current_db_unique_name):
            dest_ids.add(dest_id)
    return dest_ids


def check_db_status(instance: DBInstance) -> dict:
    logger.info("检查数据库状态 | instance=%s", instance.instance_name)
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, instance_name FROM v$instance")
            rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            if rows:
                inst_status = rows[0].get("status", "UNKNOWN")
                inst_name = rows[0].get("instance_name", "")
            else:
                return {"status": "UNKNOWN", "instance_name": "", "database_name": "", "open_mode": ""}

            try:
                cursor.execute("SELECT open_mode, name FROM v$database")
                db_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                if db_rows:
                    return {
                        "status": inst_status,
                        "instance_name": inst_name,
                        "database_name": db_rows[0].get("name", ""),
                        "open_mode": db_rows[0].get("open_mode", ""),
                    }
            except Exception:
                pass

            return {"status": inst_status, "instance_name": inst_name, "database_name": "", "open_mode": ""}
    except Exception as exc:
        logger.warning("数据库连接失败，判断为 DOWN | error=%s", exc)
        return {"status": "DOWN", "instance_name": instance.service_name or "", "database_name": "", "open_mode": ""}


def _is_dataguard_destination(row: dict, current_db_unique_name: str = "") -> bool:
    """Return True only for real Data Guard transport targets, not local archive dests."""
    dest_type = (row.get("dest_type") or "").upper()
    db_unique_name = _normalize_db_unique_name(row.get("db_unique_name") or "")
    current_db_unique_name = _normalize_db_unique_name(current_db_unique_name)
    destination = (row.get("destination") or "").upper()

    if dest_type == "LOCAL":
        return False
    if not db_unique_name and not any(token in dest_type for token in ("PHYSICAL", "LOGICAL", "STANDBY", "FAR SYNC")) and "SERVICE=" not in destination:
        return False
    if current_db_unique_name and db_unique_name and db_unique_name == current_db_unique_name:
        return False
    if any(token in dest_type for token in ("PHYSICAL", "LOGICAL", "STANDBY", "FAR SYNC")):
        return True
    if db_unique_name and db_unique_name != current_db_unique_name:
        return True
    if "SERVICE=" in destination:
        return True
    return False


def _check_dataguard_status(cursor) -> dict:
    """Check whether Data Guard is configured and whether transport/apply is active."""
    result = {
        "inspectable": True,
        "configured": False,
        "running": False,
        "db_unique_name": "",
        "database_role": "",
        "open_mode": "",
        "protection_mode": "",
        "protection_level": "",
        "switchover_status": "",
        "guard_status": "",
        "message": "未配置 DG",
        "destinations": [],
        "all_destinations": [],
        "archive_dest_params": [],
        "remote_dest_ids": [],
        "archive_dest_params_inspectable": True,
        "managed_standby": [],
        "stats": [],
        "errors": [],
    }

    try:
        cursor.execute(
            """
            SELECT name,
                   db_unique_name,
                   database_role,
                   open_mode,
                   protection_mode,
                   protection_level,
                   switchover_status,
                   guard_status
            FROM v$database
            """
        )
        rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        if rows:
            db_row = rows[0]
            result.update({
                "database_name": db_row.get("name", ""),
                "db_unique_name": db_row.get("db_unique_name", ""),
                "database_role": db_row.get("database_role", ""),
                "open_mode": db_row.get("open_mode", ""),
                "protection_mode": db_row.get("protection_mode", ""),
                "protection_level": db_row.get("protection_level", ""),
                "switchover_status": db_row.get("switchover_status", ""),
                "guard_status": db_row.get("guard_status", ""),
            })
    except Exception as exc:
        result["inspectable"] = False
        result["errors"].append(f"v$database 检查失败: {exc}")

    try:
        cursor.execute(
            """
            SELECT name, value, display_value
            FROM v$parameter
            WHERE name LIKE 'log_archive_dest_%'
              AND (value IS NOT NULL OR display_value IS NOT NULL)
            ORDER BY name
            """
        )
        archive_dest_params = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        result["archive_dest_params"] = archive_dest_params
        result["remote_dest_ids"] = sorted(_remote_archive_dest_ids(archive_dest_params, result.get("db_unique_name", "")))
        import logging
        _logger = logging.getLogger("app.db_control")
        for p in archive_dest_params:
            _logger.info("ARCHIVE_DEST_PARAM | name=%s | value=%s", p.get("name"), (p.get("value") or "")[:200])
        _logger.info("REMOTE_DEST_IDS | %s | db_unique_name=%s", result["remote_dest_ids"], result.get("db_unique_name", ""))
    except Exception as exc:
        result["archive_dest_params_inspectable"] = False
        result["errors"].append(f"log_archive_dest 参数检查失败: {exc}")

    try:
        cursor.execute(
            """
            SELECT dest_id,
                   status,
                   type AS dest_type,
                   database_mode,
                   recovery_mode,
                   protection_mode,
                   destination,
                   db_unique_name,
                   gap_status,
                   error
            FROM v$archive_dest_status
            WHERE status <> 'INACTIVE'
            ORDER BY dest_id
            """
        )
        archive_destinations = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        remote_dest_ids = set(result.get("remote_dest_ids") or [])
        result["all_destinations"] = archive_destinations
        result["destinations"] = [
            row for row in archive_destinations
            if (
                int(row.get("dest_id") or -1) in remote_dest_ids
                or (
                    not result.get("archive_dest_params_inspectable", True)
                    and _is_dataguard_destination(row, result.get("db_unique_name", ""))
                )
            )
        ]
    except Exception as exc:
        result["errors"].append(f"v$archive_dest_status 检查失败: {exc}")

    try:
        cursor.execute(
            """
            SELECT process,
                   status,
                   thread# AS thread_no,
                   sequence# AS sequence_no,
                   client_process
            FROM v$managed_standby
            WHERE process LIKE 'MRP%'
               OR process LIKE 'RFS%'
               OR process LIKE 'LSP%'
            ORDER BY process, thread#
            """
        )
        result["managed_standby"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        result["errors"].append(f"v$managed_standby 检查失败: {exc}")

    try:
        cursor.execute(
            """
            SELECT name, value, unit
            FROM v$dataguard_stats
            WHERE name IN ('transport lag', 'apply lag', 'apply finish time')
            ORDER BY name
            """
        )
        result["stats"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        result["errors"].append(f"v$dataguard_stats 检查失败: {exc}")

    role = (result.get("database_role") or "").upper()
    if "STANDBY" in role:
        result["configured"] = True
    else:
        try:
            cursor.execute(
                """
                SELECT name, value FROM v$parameter
                WHERE name IN (
                    'log_archive_dest_2','log_archive_dest_3','log_archive_dest_4',
                    'log_archive_dest_5','log_archive_dest_6','log_archive_dest_7',
                    'log_archive_dest_8','log_archive_dest_9','log_archive_dest_10'
                )
                """
            )
            dg_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            for r in dg_rows:
                v = (r.get("value") or "").strip()
                if v:
                    result["configured"] = True
                    result["message"] = f"检测到 {r['name']} 已配置"
                    break
        except Exception:
            pass
        if not result["configured"]:
            result["configured"] = bool(result["destinations"])

    if not result["configured"]:
        if not result["inspectable"]:
            result["message"] = "当前账号无法完整检查 Data Guard 视图，未确认是否存在 DG 配置"
        else:
            result["message"] = "未配置 DG"
        return result

    if "STANDBY" in role:
        apply_statuses = {"APPLYING_LOG", "WAIT_FOR_LOG", "WAIT_FOR_GAP", "WAIT_FOR_UNARCHIVED_LOG", "IDLE"}
        mrp_rows = [
            row for row in result["managed_standby"]
            if (row.get("process") or "").upper().startswith(("MRP", "LSP"))
        ]
        result["running"] = any((row.get("status") or "").upper() in apply_statuses for row in mrp_rows)
        if result["running"]:
            result["message"] = "检测到备库 Data Guard 配置，日志应用进程正在运行"
        else:
            result["message"] = "检测到备库 Data Guard 配置，但未发现运行中的 MRP/LSP 日志应用进程"
    else:
        valid_destinations = [
            row for row in result["destinations"]
            if (row.get("status") or "").upper() == "VALID"
        ]
        error_destinations = [
            row for row in result["destinations"]
            if row.get("error") or (row.get("status") or "").upper() in {"ERROR", "BAD PARAM"}
        ]
        remote_ids = result.get("remote_dest_ids") or []
        has_service = any(
            "SERVICE=" in ((p.get("value") or p.get("display_value") or "").upper())
            for p in result.get("archive_dest_params", [])
        )
        result["running"] = bool(valid_destinations) and not bool(error_destinations)
        if result["running"]:
            result["message"] = "DG 已启动并应用"
        elif has_service and not result["destinations"]:
            result["message"] = "检测到 LOG_ARCHIVE_DEST 配置 SERVICE 目标，但 v$archive_dest_status 未返回对应状态，请确认备库是否可达"
        elif bool(error_destinations):
            result["message"] = "检测到 Data Guard 配置，但归档传输目标存在异常或未处于 VALID"
        else:
            result["message"] = "检测到 Data Guard 配置（LOG_ARCHIVE_DEST 含 SERVICE/DB_UNIQUE_NAME），请确认传输状态"

    return result


def generate_startup_script(instance: DBInstance, mode: str = "open", pfile_path: str | None = None) -> dict:
    mode = mode.lower()
    if mode not in ("nomount", "mount", "open"):
        mode = "open"

    role_clause = ""
    if effective_oracle_account_type(instance) in ("system", "sysdba"):
        role_clause = " AS SYSDBA"

    connect_str = f"{instance.username}/<密码>@{instance.host}:{instance.port}/{instance.service_name or ''}"

    sql_lines = [f"-- Oracle 数据库启动脚本"]
    sql_lines.append(f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    sql_lines.append(f"-- 实例: {instance.instance_name} ({instance.host}:{instance.port})")
    sql_lines.append(f"-- 模式: {mode.upper()}")
    sql_lines.append("")

    sql_lines.append(f"-- 方式一: sqlplus /nolog + bequeath (推荐，适用于 SHUTDOWN 后重新启动)")
    sql_lines.append(f"sqlplus /nolog <<EOF")
    sql_lines.append(f"CONNECT {instance.username}/<密码>{role_clause}")
    if mode == "open":
        startup_cmd = "STARTUP"
    else:
        startup_cmd = f"STARTUP {mode.upper()}"
    if pfile_path:
        startup_cmd += f" PFILE='{pfile_path}'"
    sql_lines.append(startup_cmd)
    sql_lines.append("EXIT")
    sql_lines.append("EOF")

    sql_lines.append("")
    sql_lines.append(f"-- 方式二: sqlplus 直连")
    sql_lines.append(f"sqlplus -S \"{connect_str}{role_clause}\" <<EOF")
    sql_lines.append(startup_cmd)
    sql_lines.append("EXIT")
    sql_lines.append("EOF")

    script = "\n".join(sql_lines)
    logger.info("生成启动脚本 | instance=%s | mode=%s", instance.instance_name, mode)

    return {
        "status": "plan_only",
        "message": "已生成数据库启动脚本。V1 不直接执行启停操作，请在维护窗口通过 sqlplus 手工执行。",
        "mode": mode,
        "command": startup_cmd,
        "script": script,
        "warnings": [
            "STARTUP/SHUTDOWN 属于 DBA 变更操作，请在维护窗口执行。",
            "执行前请确认无人正在使用数据库。",
            "SHUTDOWN IMMEDIATE 会等待当前事务完成。",
            "SHUTDOWN ABORT 会立即终止，下次启动需要实例恢复。",
        ],
    }


def generate_shutdown_script(instance: DBInstance, mode: str = "immediate") -> dict:
    mode = mode.lower()
    if mode not in ("normal", "transactional", "immediate", "abort"):
        mode = "immediate"

    role_clause = ""
    if effective_oracle_account_type(instance) in ("system", "sysdba"):
        role_clause = " AS SYSDBA"

    shutdown_cmd = f"SHUTDOWN {mode.upper()}" if mode != "abort" else "SHUTDOWN ABORT"

    sql_lines = [f"-- Oracle 数据库关闭脚本"]
    sql_lines.append(f"-- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    sql_lines.append(f"-- 实例: {instance.instance_name} ({instance.host}:{instance.port})")
    sql_lines.append(f"-- 模式: {mode.upper()}")
    sql_lines.append("")

    sql_lines.append(f"-- 方式一: sqlplus /nolog + bequeath (推荐)")
    sql_lines.append(f"sqlplus /nolog <<EOF")
    sql_lines.append(f"CONNECT {instance.username}/<密码>{role_clause}")
    sql_lines.append(shutdown_cmd)
    sql_lines.append("EXIT")
    sql_lines.append("EOF")

    sql_lines.append("")
    mode_desc = {
        "normal": "等待所有用户断开连接后关闭（可能长时间等待）",
        "transactional": "等待当前事务完成后关闭",
        "immediate": "回滚未提交事务后立即关闭（推荐）",
        "abort": "立即终止所有操作（不推荐，下次启动需实例恢复）",
    }
    sql_lines.append(f"-- 说明: {mode_desc.get(mode, '')}")

    script = "\n".join(sql_lines)
    logger.info("生成关闭脚本 | instance=%s | mode=%s", instance.instance_name, mode)

    return {
        "status": "plan_only",
        "message": "已生成数据库关闭脚本。V1 不直接执行启停操作，请在维护窗口通过 sqlplus 手工执行。",
        "mode": mode,
        "command": shutdown_cmd,
        "script": script,
        "warnings": [
            "SHUTDOWN 属于 DBA 变更操作，请在维护窗口执行。",
            "SHUTDOWN IMMEDIATE 为推荐方式，会回滚未提交事务。",
            "SHUTDOWN ABORT 仅用于紧急情况，下次启动需要实例恢复。",
            f"当前模式 [{mode.upper()}]: {mode_desc.get(mode, '')}",
        ],
    }


def check_boot_prerequisites(instance: DBInstance, deep: bool = True) -> dict:
    logger.info("引导前置检查 | instance=%s", instance.instance_name)

    steps = []

    server_online = False
    try:
        sock = socket.create_connection((instance.host, instance.port), timeout=BOOT_PREREQUISITE_SOCKET_TIMEOUT_SECONDS)
        sock.close()
        server_online = True
    except Exception:
        pass
    steps.append({
        "step": 1,
        "name": "服务器在线检查",
        "key": "server_online",
        "status": "success" if server_online else "failed",
        "message": f"{instance.host}:{instance.port} 可达" if server_online else f"{instance.host}:{instance.port} 不可达",
    })

    is_rac = False
    cluster_mode = "单机"
    db_version = ""
    db_status = "DOWN"
    dataguard = {
        "inspectable": False,
        "configured": False,
        "running": False,
        "message": "数据库未连接，无法检查 Data Guard",
        "destinations": [],
        "managed_standby": [],
        "stats": [],
        "errors": [],
    }
    if server_online and deep:
        try:
            with oracle_connection(instance) as conn:
                cursor = conn.cursor()

                try:
                    cursor.execute(
                        "SELECT value FROM v$parameter WHERE name = 'cluster_database'"
                    )
                    rac_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                    if rac_rows and rac_rows[0].get("value", "").upper() == "TRUE":
                        is_rac = True
                        cluster_mode = "RAC"
                except Exception:
                    pass

                try:
                    cursor.execute("SELECT version_full FROM v$instance")
                    ver_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                    if ver_rows:
                        db_version = ver_rows[0].get("version_full", "")
                except Exception:
                    pass

                cursor.execute("SELECT status, instance_name FROM v$instance")
                inst_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
                if inst_rows:
                    db_status = inst_rows[0].get("status", "UNKNOWN")

                dataguard = _check_dataguard_status(cursor)
        except Exception as exc:
            logger.warning("数据库连接失败 | error=%s", exc)
            db_status = "DOWN"
    elif server_online:
        db_status = "UNKNOWN"
        dataguard["message"] = "快速模式未执行数据库动态视图检查"

    steps.append({
        "step": 2,
        "name": "实例类型判断",
        "key": "cluster_mode",
        "status": "success",
        "message": f"数据库类型: {cluster_mode}" + (f", 版本: {db_version}" if db_version else ""),
        "is_rac": is_rac,
        "cluster_mode": cluster_mode,
        "db_version": db_version,
    })

    dg_role = (dataguard.get("database_role") or "").upper()

    if dataguard.get("configured"):
        dg_status = "success" if dataguard.get("running") else "warning"
    elif dataguard.get("inspectable"):
        dg_status = "success"
    else:
        dg_status = "warning" if server_online else "failed"
    steps.append({
        "step": 3,
        "name": "Data Guard 检查",
        "key": "dataguard_status",
        "status": dg_status,
        "message": dataguard.get("message") or "Data Guard 状态未知",
        "dataguard": dataguard,
    })

    auto_start = "未知"
    ssh_available = False
    ssh_info = "未配置"
    if instance.ssh_host:
        try:
            from app.services.ssh_executor import execute_command
            chk_result = execute_command(
                instance,
                "grep -q '^ORACLE_HOME' /etc/oratab && echo 'Y' || echo 'N'",
                timeout=BOOT_PREREQUISITE_SSH_TIMEOUT_SECONDS,
                connect_timeout=BOOT_PREREQUISITE_SSH_TIMEOUT_SECONDS,
            )
            ssh_available = chk_result.get("ok", False)
            if chk_result.get("ok") and "Y" in chk_result.get("stdout", ""):
                ssh_info = "SSH 可用"
                auto_start = "可能已配置 oratab 自动启动"
            elif chk_result.get("ok"):
                ssh_info = "SSH 可用"
                auto_start = "未配置自动启动"
            else:
                reason = chk_result.get("stderr") or chk_result.get("error") or chk_result.get("message") or ""
                ssh_info = f"SSH 不可用: {reason}".strip()
                auto_start = "SSH 不可用，无法检查自动启动"
        except Exception as exc:
            ssh_info = f"SSH 检查失败: {exc}"
            auto_start = "SSH 未配置或不可用，无法检查"
    else:
        auto_start = "未配置 SSH，无法远程检查"

    steps.append({
        "step": 4,
        "name": "数据库启动状态",
        "key": "db_status",
        "status": "success" if db_status == "OPEN" else "warning" if db_status != "DOWN" else "failed",
        "message": f"数据库状态: {db_status}",
        "db_status": db_status,
        "auto_start": auto_start,
    })

    steps.append({
        "step": 5,
        "name": "SSH 连接检查",
        "key": "ssh_available",
        "status": "success" if ssh_available else "failed",
        "message": ssh_info,
        "ssh_available": ssh_available,
    })

    dg_pass = (not dataguard.get("configured")) or dataguard.get("running")
    all_pass = server_online and ssh_available and dg_pass
    need_startup = db_status != "OPEN"

    return {
        "steps": steps,
        "summary": {
            "server_online": server_online,
            "is_rac": is_rac,
            "cluster_mode": cluster_mode,
            "db_status": db_status,
            "db_version": db_version,
            "ssh_available": ssh_available,
            "auto_start": auto_start,
            "dataguard": dataguard,
            "all_pass": all_pass,
            "need_startup": need_startup,
        },
    }


def generate_boot_script(instance: DBInstance, mode: str = "open") -> dict:
    logger.info("生成引导启动脚本 | instance=%s | mode=%s", instance.instance_name, mode)

    mode = (mode or "open").lower()
    prereq = check_boot_prerequisites(instance, deep=True)
    summary = prereq["summary"]
    is_rac = summary["is_rac"]
    dataguard = summary.get("dataguard") or {}
    dg_mode = mode == "dg"
    dg_role = (dataguard.get("database_role") or "").upper()
    is_standby = "STANDBY" in dg_role
    remote_dest_ids = [
        row.get("dest_id")
        for row in dataguard.get("destinations", [])
        if row.get("dest_id") is not None and (row.get("status") or "").upper() != "VALID"
    ]

    ora_home_env = "$ORACLE_HOME"
    ora_sid_env = "$ORACLE_SID"

    lines = [
        f"#!/bin/bash",
        f"# Oracle 引导启动脚本",
        f"# 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"# 实例: {instance.instance_name} ({instance.host}:{instance.port})",
        f"# 类型: {'RAC' if is_rac else '单机'}",
        f"# 启动模式: {'Data Guard 引导' if dg_mode else mode.upper()}",
        f"",
        f"set -e",
        f"",
    ]

    lines.append("# ====== Step 1: 环境变量设置 ======")
    lines.append(f'export ORACLE_SID={instance.sid or instance.service_name or instance.instance_name}')
    lines.append(f"export ORACLE_HOME={ora_home_env}")
    lines.append(f"export PATH=$ORACLE_HOME/bin:$PATH")
    lines.append("")

    if is_rac:
        lines.append("# ====== RAC 模式: 使用 crsctl 启动集群 ======")
        lines.append("# 以 root 用户执行（或拥有 sudo 权限的 oracle 用户）:")
        lines.append("echo '>>> RAC 模式: 启动 CRS 集群服务'")
        lines.append("sudo crsctl start crs")
        lines.append("")
        lines.append("echo '>>> 等待 CRS 资源上线...'")
        lines.append("sleep 30")
        lines.append("crsctl stat res -t")
        lines.append("")
        lines.append("# 检查数据库实例是否自动启动")
        lines.append("echo '>>> 检查数据库实例状态...'")
        lines.append(f'srvctl status instance -db {instance.database_name or instance.instance_name} -instance {instance.sid or instance.instance_name}')
        if dg_mode:
            lines.append("")
            lines.append("# ====== Data Guard 引导: 启动或恢复归档传输/应用 ======")
            lines.append("# RAC 环境建议优先使用 Data Guard Broker；如未启用 Broker，可在对应实例上执行下方 SQL。")
            lines.append("dgmgrl / <<EODGMGRL")
            lines.append("SHOW CONFIGURATION;")
            lines.append(f"SHOW DATABASE VERBOSE '{dataguard.get('database_name') or instance.database_name or instance.instance_name}';")
            lines.append("EODGMGRL")
            lines.append("")
            lines.append("sqlplus / as sysdba <<EOSQL")
            lines.append("SET ECHO ON")
            if is_standby:
                lines.append("WHENEVER SQLERROR CONTINUE")
                lines.append("ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;")
                lines.append("ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION;")
                lines.append("SELECT process, status, thread#, sequence# FROM v$managed_standby WHERE process LIKE 'MRP%' OR process LIKE 'RFS%' ORDER BY process, thread#;")
                lines.append("SELECT thread#, MAX(sequence#) received_seq, MAX(CASE WHEN applied='YES' THEN sequence# END) applied_seq FROM v$archived_log GROUP BY thread# ORDER BY thread#;")
            else:
                lines.append("ALTER SYSTEM ARCHIVE LOG CURRENT;")
                for dest_id in remote_dest_ids:
                    lines.append(f"ALTER SYSTEM SET LOG_ARCHIVE_DEST_STATE_{dest_id}=ENABLE SCOPE=BOTH;")
                lines.append("SELECT dest_id, status, database_mode, recovery_mode, gap_status, error FROM v$archive_dest_status WHERE status <> 'INACTIVE' ORDER BY dest_id;")
            lines.append("EXIT;")
            lines.append("EOSQL")
    else:
        lines.append("# ====== Step 2: 启动监听 ======")
        lines.append("echo '>>> 启动 Oracle 监听...'")
        lines.append("lsnrctl start")
        lines.append("")
        lines.append("echo '>>> 等待监听启动完成...'")
        lines.append("sleep 3")
        lines.append("lsnrctl status")
        lines.append("")

        lines.append("# ====== Step 3: 启动数据库 ======")
        lines.append(f"echo '>>> 启动数据库 ({'MOUNT，适用于备库归档应用' if dg_mode and is_standby else 'OPEN，适用于主库归档传输' if dg_mode else mode.upper()})...'")
        lines.append("sqlplus / as sysdba <<EOSQL")
        if dg_mode and is_standby:
            lines.append("WHENEVER SQLERROR CONTINUE")
            lines.append("STARTUP MOUNT;")
        elif dg_mode:
            lines.append("WHENEVER SQLERROR CONTINUE")
            lines.append("STARTUP;")
        elif mode == "open":
            lines.append("STARTUP;")
        else:
            lines.append(f"STARTUP {mode.upper()};")
        lines.append("SELECT instance_name, status, database_status FROM v$instance;")
        lines.append("EXIT;")
        lines.append("EOSQL")

        if dg_mode:
            lines.append("")
            lines.append("# ====== Step 4: 启动 Data Guard 归档传输 / 应用 ======")
            lines.append("sqlplus / as sysdba <<EOSQL")
            lines.append("SET ECHO ON")
            lines.append("WHENEVER SQLERROR CONTINUE")
            if is_standby:
                lines.append("-- 备库：启动实时日志应用。若未购买 Active Data Guard，建议保持 MOUNT 状态执行。")
                lines.append("ALTER DATABASE RECOVER MANAGED STANDBY DATABASE CANCEL;")
                lines.append("ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION;")
                lines.append("")
                lines.append("-- 验证 MRP/RFS 进程和归档应用进度")
                lines.append("SELECT process, status, thread#, sequence# FROM v$managed_standby WHERE process LIKE 'MRP%' OR process LIKE 'RFS%' ORDER BY process, thread#;")
                lines.append("SELECT thread#, MAX(sequence#) received_seq, MAX(CASE WHEN applied='YES' THEN sequence# END) applied_seq FROM v$archived_log GROUP BY thread# ORDER BY thread#;")
            else:
                lines.append("-- 主库：重新启用异常归档传输目标，并手工切一次归档触发传输。")
                for dest_id in remote_dest_ids:
                    lines.append(f"ALTER SYSTEM SET LOG_ARCHIVE_DEST_STATE_{dest_id}=ENABLE SCOPE=BOTH;")
                lines.append("ALTER SYSTEM ARCHIVE LOG CURRENT;")
                lines.append("")
                lines.append("-- 验证归档传输目标状态")
                lines.append("SELECT dest_id, status, database_mode, recovery_mode, gap_status, error FROM v$archive_dest_status WHERE status <> 'INACTIVE' ORDER BY dest_id;")
            lines.append("EXIT;")
            lines.append("EOSQL")

    lines.append("")
    lines.append("echo '>>> 引导启动流程完成'")

    script = "\n".join(lines)

    boot_steps = []
    if is_rac:
        boot_steps = [
            {"step": 1, "action": "SSH 登录服务器", "desc": f"ssh {instance.ssh_username or 'oracle'}@{instance.ssh_host or instance.host}"},
            {"step": 2, "action": "切换到 oracle 用户", "desc": "su - oracle（如已是 oracle 用户可跳过）"},
            {"step": 3, "action": "启动 CRS 集群", "desc": "sudo crsctl start crs"},
            {"step": 4, "action": "等待资源上线", "desc": "crsctl stat res -t 检查状态"},
        ]
        if dg_mode:
            boot_steps.extend([
                {"step": 5, "action": "检查 DG Broker / 数据库资源", "desc": "dgmgrl / → SHOW CONFIGURATION"},
                {"step": 6, "action": "恢复 DG 传输或应用", "desc": "主库启用 LOG_ARCHIVE_DEST_STATE，备库启动 MRP 实时应用"},
            ])
    else:
        boot_steps = [
            {"step": 1, "action": "SSH 登录服务器", "desc": f"ssh {instance.ssh_username or 'oracle'}@{instance.ssh_host or instance.host}"},
            {"step": 2, "action": "切换到 oracle 用户", "desc": "su - oracle（如已是 oracle 用户可跳过）"},
            {"step": 3, "action": "启动监听", "desc": "lsnrctl start"},
            {"step": 4, "action": "启动数据库", "desc": "sqlplus / as sysdba → STARTUP MOUNT" if dg_mode and is_standby else "sqlplus / as sysdba → STARTUP" if dg_mode else f"sqlplus / as sysdba → STARTUP{'' if mode == 'open' else ' ' + mode.upper()}"},
        ]
        if dg_mode:
            boot_steps.append({
                "step": 5,
                "action": "启动归档应用/传输",
                "desc": "备库执行 ALTER DATABASE RECOVER MANAGED STANDBY DATABASE USING CURRENT LOGFILE DISCONNECT FROM SESSION；主库启用归档传输目标并 ARCHIVE LOG CURRENT",
            })

    warnings = [
        "引导启动属于重大运维操作，请在维护窗口执行。",
        "请确认服务器已正常启动、存储已挂载。",
    ]
    if is_rac:
        warnings.append("RAC 模式: crsctl start crs 需要 root 权限或 sudo。")
    else:
        warnings.append("单机模式: 确保监听和数据库使用正确的 ORACLE_HOME/ORACLE_SID。")
    if dg_mode:
        warnings.extend([
            "Data Guard 引导脚本只生成方案，不会自动执行。",
            "执行前请确认当前连接的是主库还是备库，主库侧处理归档传输，备库侧处理 MRP 日志应用。",
            "备库如未购买 Active Data Guard，建议保持 MOUNT 状态启动归档应用，不要直接 OPEN READ ONLY。",
            "如果存在归档 GAP，请先补齐缺失归档，再启动 MRP 应用。",
        ])
    if not summary["ssh_available"]:
        warnings.append("SSH 未配置或不可用，脚本仅可参考，无法远程执行。")

    return {
        "status": "plan_only",
        "message": f"已生成{'Data Guard ' if dg_mode else ''}引导启动脚本（{'RAC' if is_rac else '单机'}模式，{'DG' if dg_mode else mode.upper()}）。",
        "mode": mode,
        "is_rac": is_rac,
        "cluster_mode": summary["cluster_mode"],
        "boot_steps": boot_steps,
        "prerequisites": prereq,
        "script": script,
        "warnings": warnings,
    }
