"""Generic operations support for PostgreSQL and SQL Server."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable

from app.analyzers.rules import Finding
from app.collectors.postgresql.postgresql_connection import postgresql_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, json_safe
from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.generic_db_ops")

ENGINE_LABELS = {"postgresql": "PostgreSQL", "sqlserver": "SQL Server"}

SQLSERVER_SECTION_ALIASES = {
    "database_size": ["sqlserver.database.used_pct"],
    "log_space": ["sqlserver.log.used_pct"],
    "blocking": ["sqlserver.blocking.seconds"],
    "cpu_usage": ["sqlserver.cpu.used_pct"],
    "backup_info": ["sqlserver.backup.age_hours"],
    "database_status": ["sqlserver.availability.sync_state"],
}

SQLSERVER_SECTION_KEYS = (
    "version_info",
    "connection_info",
    "active_sessions",
    "database_size",
    "database_status",
    "config_parameters",
    "wait_stats",
    "cpu_usage",
    "blocking",
    "lock_info",
    "deadlock_info",
    "backup_info",
    "log_space",
    "memory_usage",
    "linked_servers",
    "file_groups",
    "system_info",
)

GENERIC_SQLSERVER_SECTION_KEYS = {
    "connection.basic": {
        "version_info",
        "connection_info",
    },
    "space.capacity": {
        "database_size",
        "log_space",
        "file_groups",
    },
    "performance.slow_sql": {
        "active_sessions",
        "wait_stats",
        "cpu_usage",
        "blocking",
        "lock_info",
        "deadlock_info",
        "memory_usage",
    },
    "backup.recovery": {
        "database_status",
        "backup_info",
    },
    "security.account": {
        "config_parameters",
        "linked_servers",
        "system_info",
    },
}

MODULES = {
    "postgresql": [
        {"key": "overview", "title": "运行概览", "description": "版本、连接、实例状态和数据库大小"},
        {"key": "space", "title": "空间管理", "description": "数据库、表空间、表/索引大小和膨胀线索"},
        {"key": "wal", "title": "WAL 日志", "description": "WAL 参数、归档模式、复制槽和 PITR 风险"},
        {"key": "sessionTransaction", "title": "会话事务", "description": "活动会话、锁等待、长事务和阻塞线索"},
        {"key": "objectStats", "title": "对象统计", "description": "表统计、索引统计、Vacuum 和统计信息建议"},
        {"key": "ha", "title": "复制高可用", "description": "主备状态、复制延迟和恢复模式"},
        {"key": "governance", "title": "数据治理", "description": "归档、分区、VACUUM 和统计信息治理"},
    ],
    "sqlserver": [
        {"key": "overview", "title": "运行概览", "description": "版本、连接、实例状态和数据库信息"},
        {"key": "space", "title": "空间管理", "description": "数据库文件、TempDB、增长配置和容量风险"},
        {"key": "logs", "title": "日志管理", "description": "Error Log、事务日志、VLF 和日志备份状态"},
        {"key": "sessionTransaction", "title": "会话事务", "description": "活动请求、锁等待、阻塞链和长事务"},
        {"key": "objectStats", "title": "对象统计", "description": "大表、索引碎片、缺失索引和统计信息"},
        {"key": "ha", "title": "高可用", "description": "Always On、镜像、复制和恢复模式检查"},
        {"key": "governance", "title": "数据治理", "description": "历史归档、分区、压缩和清理方案"},
    ],
}

SECTION_ALIASES = {
    "postgresql": {
        "storage": "space",
        "sessions": "sessionTransaction",
        "tables": "objectStats",
        "security": "objectStats",
        "backup": "backupRecovery",
    },
    "sqlserver": {
        "storage": "space",
        "sessions": "sessionTransaction",
        "tables": "objectStats",
        "security": "objectStats",
        "backup": "backupRecovery",
        "log": "logs",
    },
}


@contextmanager
def _connection(instance: DBInstance):
    """Open one bounded connection for the unattended reads this module performs.

    Only PostgreSQL and SQL Server are served here. SQL Server was already bounded by the
    connector's 10s default; PostgreSQL passed no budget at all, so a single stalled catalog
    read could pin a request thread for as long as the source database felt like it. The
    budget comes from the shared collection constant so the two engines cannot drift, and
    ``test_collection_query_timeout.py`` pins the SQL Server default to the same value.
    """
    if instance.db_type == "postgresql":
        with postgresql_connection(
            instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000,
        ) as conn:
            yield conn
    elif instance.db_type == "sqlserver":
        with sqlserver_connection(instance) as conn:
            yield conn
    else:
        raise ValueError(f"Unsupported engine: {instance.db_type}")


def _fetch(conn, sql: str, params=None) -> list[dict]:
    cursor = None
    try:
        try:
            cursor = conn.cursor(as_dict=True)
        except TypeError:
            cursor = conn.cursor()
        cursor.execute(sql, params or ())
        rows = cursor.fetchall() or []
        columns = [col[0] for col in (cursor.description or [])]
        items = []
        for row in rows:
            if isinstance(row, dict):
                items.append(dict(row))
            else:
                items.append({columns[index]: value for index, value in enumerate(row)})
        return items
    finally:
        if cursor is not None:
            cursor.close()


def _one(conn, sql: str, params=None) -> dict:
    rows = _fetch(conn, sql, params)
    return rows[0] if rows else {}


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits: int = 2):
    if value is None:
        return None
    return round(_num(value), digits)


def _safe_collect(instance: DBInstance, collector: Callable[[object], dict], section: str) -> dict:
    try:
        with _connection(instance) as conn:
            payload = collector(conn)
            payload.setdefault("engine", instance.db_type)
            payload.setdefault("section", section)
            payload.setdefault("warnings", [])
            if section == "backupRecovery":
                payload["plan"] = build_plan(instance, "backup")
            return json_safe(payload)
    except Exception as exc:
        logger.warning("%s %s collection degraded: %s", instance.db_type, section, exc)
        payload = {
            "engine": instance.db_type,
            "section": section,
            "status": "degraded",
            "warnings": [str(exc)],
            "summary": {
                "instance": instance.instance_name,
                "host": instance.host,
                "port": instance.port,
                "database": instance.database_name,
                "message": "采集失败，页面保留 plan_only 方案能力。",
            },
            "items": [],
        }
        if section == "backupRecovery":
            payload["plan"] = build_plan(instance, "backup")
        return payload


def get_modules(engine: str) -> dict:
    return {"items": MODULES.get(engine, [])}


def collect_section(instance: DBInstance, section: str) -> dict:
    section = SECTION_ALIASES.get(instance.db_type, {}).get(section, section)
    collectors = _PG_COLLECTORS if instance.db_type == "postgresql" else _SQLSERVER_COLLECTORS
    collector = collectors.get(section)
    if not collector:
        return {"engine": instance.db_type, "section": section, "status": "unknown_section", "warnings": [f"Unknown section: {section}"], "items": []}
    return _safe_collect(instance, collector, section)


def collect_all(instance: DBInstance) -> dict:
    return {
        "engine": instance.db_type,
        "label": ENGINE_LABELS.get(instance.db_type, instance.db_type),
        "modules": MODULES.get(instance.db_type, []),
        "sections": {item["key"]: collect_section(instance, item["key"]) for item in MODULES.get(instance.db_type, [])},
    }


def build_plan(instance: DBInstance, plan_type: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    engine = instance.db_type
    database = payload.get("database") or instance.database_name or ("postgres" if engine == "postgresql" else "master")
    if engine == "postgresql":
        plans = {
            "backup": f"pg_dump -h {instance.host} -p {instance.port} -U {instance.username} -Fc -d {database} -f {database}_$(date +%F).dump",
            "pgdump": f"pg_dump -h {instance.host} -p {instance.port} -U {instance.username} -Fc -d {database} -f {database}_$(date +%F).dump",
            "basebackup": f"pg_basebackup -h {instance.host} -p {instance.port} -U {instance.username} -D /backup/pg_basebackup/{database}_$(date +%F) -Fp -Xs -P",
            "restore": f"pg_restore -h {instance.host} -p {instance.port} -U {instance.username} -d {database}_restore --clean --if-exists {database}.dump",
            "pitr": "# PostgreSQL PITR 恢复方案\n# 1. 停库并备份当前数据目录\n# 2. 还原 basebackup\n# 3. 配置 restore_command 和 recovery_target_time\n# 4. 启动实例进入恢复\nrestore_command = 'cp /archive/%f %p'\nrecovery_target_time = '<YYYY-MM-DD HH:MI:SS>'",
            "switchover_check": "-- PostgreSQL 高可用切换检查方案\n-- 1. 检查主备角色、流复制状态、复制延迟、WAL 归档和复制槽积压。\n-- 2. 确认应用连接串、只读流量、备份状态和回退窗口。\n-- 3. 记录当前主库 LSN，确认备库 replay_lsn 已追平后再提交人工审批。\n-- 4. 审批通过后按 Patroni/repmgr/pg_ctl 流程执行 switchover，并保留原主库回退方案。",
            "governance": "-- PostgreSQL 数据治理方案\nVACUUM (ANALYZE);\n-- 按时间列创建分区，并用 INSERT INTO history SELECT ... WHERE ... 迁移历史数据",
            "kill_session": "SELECT pg_terminate_backend(<pid>);",
            "analyze": "ANALYZE VERBOSE <schema>.<table>;",
            "vacuum": "VACUUM (VERBOSE, ANALYZE) <schema>.<table>;",
            "partition": "-- PostgreSQL 分区治理方案\nCREATE TABLE <table>_new PARTITION BY RANGE (<time_column>);\n-- 创建分区后分批迁移数据并校验行数",
            "csv_export": f"\\copy (SELECT * FROM <schema>.<table> WHERE <condition>) TO '/backup/{database}_archive.csv' WITH CSV HEADER",
            "archive": "-- PostgreSQL 历史归档方案\nINSERT INTO <history_table> SELECT * FROM <source_table> WHERE <archive_condition>;\n-- 校验后由人工审批清理生产数据",
        }
    else:
        plans = {
            "backup": f"BACKUP DATABASE [{database}] TO DISK = N'D:\\\\backup\\\\{database}_full.bak' WITH INIT, COMPRESSION, CHECKSUM;",
            "full": f"BACKUP DATABASE [{database}] TO DISK = N'D:\\\\backup\\\\{database}_full.bak' WITH INIT, COMPRESSION, CHECKSUM;",
            "diff": f"BACKUP DATABASE [{database}] TO DISK = N'D:\\\\backup\\\\{database}_diff.bak' WITH DIFFERENTIAL, INIT, COMPRESSION, CHECKSUM;",
            "log_backup": f"BACKUP LOG [{database}] TO DISK = N'D:\\\\backup\\\\{database}_log.trn' WITH INIT, COMPRESSION, CHECKSUM;",
            "restore": f"RESTORE VERIFYONLY FROM DISK = N'D:\\\\backup\\\\{database}_full.bak' WITH CHECKSUM;",
            "growth": "-- SQL Server 文件增长方案\nALTER DATABASE [<database>] MODIFY FILE (NAME = N'<logical_name>', FILEGROWTH = 1024MB);",
            "governance": "-- SQL Server 数据治理方案\nEXEC sp_updatestats;\n-- 设计分区函数/分区方案，并用 INSERT INTO history SELECT ... WHERE ... 迁移历史数据",
            "kill_session": "KILL <session_id>;",
            "rebuild_index": "ALTER INDEX [<index_name>] ON [<schema>].[<table>] REBUILD WITH (ONLINE = ON);",
            "update_stats": "UPDATE STATISTICS [<schema>].[<table>] WITH FULLSCAN;",
            "failover_check": "-- SQL Server AlwaysOn Failover 检查方案\n-- 检查同步状态、备份链、应用连接串和回退方案，审批后人工执行故障转移。",
            "partition": "-- SQL Server 分区治理方案\nCREATE PARTITION FUNCTION pf_<table> (datetime2) AS RANGE RIGHT FOR VALUES (...);",
            "bcp_export": f"bcp \"SELECT * FROM {database}.<schema>.<table> WHERE <condition>\" queryout D:\\\\backup\\\\{database}_archive.csv -c -t, -T",
            "archive": "-- SQL Server 历史归档方案\nINSERT INTO <history_table> SELECT * FROM <source_table> WHERE <archive_condition>;\n-- 校验后由人工审批清理生产数据",
        }
    return {
        "plan_only": True,
        "status": "plan_only",
        "engine": engine,
        "plan_type": plan_type,
        "title": f"{ENGINE_LABELS.get(engine, engine)} {plan_type} 方案",
        "script": plans.get(plan_type, "-- 未知方案类型"),
        "warnings": ["仅生成方案，不直接执行。", "生产执行前必须确认备份、窗口期、回退方案和审批记录。"],
    }


def inspection_findings(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    if instance.db_type == "sqlserver":
        return _mssql_script_inspection_findings(instance, policy_items)

    data = collect_all(instance)
    findings: list[Finding] = []
    for section_key, section in (data.get("sections") or {}).items():
        title = next((m["title"] for m in MODULES.get(instance.db_type, []) if m["key"] == section_key), section_key)
        warnings = section.get("warnings") or []
        status = section.get("status")
        if warnings or status == "degraded":
            findings.append(Finding(
                check_category=title,
                check_item=f"{title}采集",
                check_status="warning",
                metric_name="collector_status",
                metric_value=status or "warning",
                threshold_value="ok",
                finding="采集存在异常或权限不足：" + "；".join(str(x) for x in warnings[:3]),
                suggestion="检查数据库连接、驱动依赖、账号权限，并按最小权限补充监控视图访问授权。",
                raw_data=section,
            ))
        else:
            findings.append(Finding(
                check_category=title,
                check_item=f"{title}基础检查",
                check_status="ok",
                metric_name="collector_status",
                metric_value="ok",
                threshold_value="ok",
                finding=f"{title}采集完成，未发现采集级异常。",
                suggestion="保持定期巡检，并结合业务高峰期做趋势分析。",
                raw_data=section,
            ))
    return findings


def _mssql_script_inspection_findings(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    """SQL Server inspection aligned with mssql_check.sh 17-section output."""
    from app.services.inspection.custom_metric_service import enabled_metric_source_keys

    allowed_item_keys = enabled_metric_source_keys(policy_items)
    if allowed_item_keys == set():
        return []
    try:
        with _connection(instance) as conn:
            sections = _mssql_script_sections(conn, allowed_item_keys)
    except Exception as exc:
        logger.warning("sqlserver full inspection degraded: %s", exc, exc_info=True)
        return [
            Finding(
                check_category="SQL Server 巡检运行",
                check_item="mssql_check.sh 兼容巡检",
                check_status="warning",
                metric_name="collector_status",
                metric_value="degraded",
                threshold_value="ok",
                finding=f"SQL Server 17 项巡检采集失败：{exc}",
                suggestion="检查 SQL Server 连接、账号权限、pymssql 驱动和网络连通性。",
                raw_data={"script": "mssql_check.sh", "script_version": "1.0", "error": str(exc)},
            )
        ]

    findings: list[Finding] = []
    for section in sections:
        warnings = section.get("warnings") or []
        status = section.get("status") or ("warning" if warnings else "ok")
        title = section["title"]
        template_item_keys = section.get("_template_item_keys") or [None]
        for template_item_key in template_item_keys:
            metric_override = _mssql_template_metric_override(section, template_item_key)
            findings.append(
                Finding(
                    check_category=f"{section['order']:02d}. {title}",
                    check_item=title,
                    check_status=status,
                    metric_name=(
                        metric_override["metric_name"]
                        if metric_override
                        else section.get("metric_name") or section["key"]
                    ),
                    metric_value=str(
                        metric_override["metric_value"]
                        if metric_override
                        else section.get("metric_value", "")
                    ),
                    threshold_value=str(section.get("threshold_value") or "ok"),
                    finding=section.get("finding") or _mssql_section_finding(title, section),
                    suggestion=section.get("suggestion") or _mssql_section_suggestion(title, status),
                    raw_data={
                        "script": "mssql_check.sh",
                        "script_version": "1.0",
                        "section_key": section["key"],
                        "section_order": section["order"],
                        "title": title,
                        "summary": section.get("summary") or {},
                        "items": section.get("items") or [],
                        "sql": section.get("sql") or [],
                        "warnings": warnings,
                        "html_section_id": section.get("html_section_id"),
                        "template_item_key": template_item_key,
                        "template_threshold_eligible": bool(metric_override),
                    },
                )
            )
    return findings


def _mssql_template_metric_override(section: dict, item_key: str | None) -> dict | None:
    if not item_key:
        return None
    summary = section.get("summary") or {}
    rows = section.get("items") or []
    value = None
    if item_key == "sqlserver.log.used_pct":
        percentages = [
            _num(row.get("LogSpaceUsedPercent"))
            for row in rows
            if row.get("LogSpaceUsedPercent") not in (None, "")
        ]
        value = max(percentages) if percentages else None
    elif item_key == "sqlserver.blocking.seconds":
        wait_ms = summary.get("max_wait_duration_ms")
        value = round(_num(wait_ms) / 1000, 3) if wait_ms not in (None, "") else None
    elif item_key == "sqlserver.backup.age_hours":
        ages = [
            _num(row.get("backup_age_hours"))
            for row in rows
            if row.get("backup_age_hours") not in (None, "")
        ]
        value = max(ages) if ages else None
    elif item_key == "sqlserver.cpu.used_pct":
        busy = _num(summary.get("cpu_busy_ms"))
        idle = _num(summary.get("idle_ms"))
        value = round(busy * 100 / (busy + idle), 2) if busy + idle > 0 else None
    if value is None:
        return None
    return {"metric_name": item_key, "metric_value": value}


def _mssql_script_sections(conn, allowed_item_keys: set[str] | None = None) -> list[dict]:
    collectors = {
        "version_info": _mssql_script_version_info,
        "connection_info": _mssql_script_connection_info,
        "active_sessions": _mssql_script_active_sessions,
        "database_size": _mssql_script_database_size,
        "database_status": _mssql_script_database_status,
        "config_parameters": _mssql_script_config_parameters,
        "wait_stats": _mssql_script_wait_stats,
        "cpu_usage": _mssql_script_cpu_usage,
        "blocking": _mssql_script_blocking,
        "lock_info": _mssql_script_lock_info,
        "deadlock_info": _mssql_script_deadlock_info,
        "backup_info": _mssql_script_backup_info,
        "log_space": _mssql_script_log_space,
        "memory_usage": _mssql_script_memory_usage,
        "linked_servers": _mssql_script_linked_servers,
        "file_groups": _mssql_script_file_groups,
        "system_info": _mssql_script_system_info,
    }
    section_collectors = [(key, collectors[key]) for key in SQLSERVER_SECTION_KEYS]
    sections = []
    for section_key, collector in section_collectors:
        matches = _mssql_matched_item_keys(section_key, allowed_item_keys)
        if allowed_item_keys is not None and not matches:
            continue
        section = collector(conn)
        if matches:
            section["_template_item_keys"] = matches
        sections.append(section)
    return [json_safe(section) for section in sections]


def _mssql_matched_item_keys(
    section_key: str,
    allowed_item_keys: set[str] | None,
) -> list[str]:
    if allowed_item_keys is None:
        return []
    candidates = [
        f"sqlserver.script.{section_key}",
        *SQLSERVER_SECTION_ALIASES.get(section_key, []),
    ]
    matches = [key for key in candidates if key in allowed_item_keys]
    for generic_key, section_keys in GENERIC_SQLSERVER_SECTION_KEYS.items():
        if generic_key in allowed_item_keys and section_key in section_keys:
            matches.append(generic_key)
    return matches


def _mssql_section(
    order: int,
    key: str,
    title: str,
    *,
    summary: dict | None = None,
    items: list[dict] | None = None,
    sql: str | list[str] | None = None,
    status: str = "ok",
    metric_name: str | None = None,
    metric_value=None,
    threshold_value: str = "ok",
    finding: str | None = None,
    suggestion: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    sql_items = [sql] if isinstance(sql, str) else (sql or [])
    rows = items or []
    return {
        "order": order,
        "key": key,
        "html_section_id": key,
        "title": title,
        "summary": summary or {},
        "items": rows,
        "sql": sql_items,
        "status": status,
        "metric_name": metric_name or key,
        "metric_value": len(rows) if metric_value is None else metric_value,
        "threshold_value": threshold_value,
        "finding": finding,
        "suggestion": suggestion,
        "warnings": warnings or [],
    }


def _mssql_safe_section(
    order: int,
    key: str,
    title: str,
    collector: Callable[[], dict],
    sql: str | list[str],
) -> dict:
    try:
        section = collector()
        section.setdefault("sql", [sql] if isinstance(sql, str) else sql)
        return section
    except Exception as exc:
        logger.warning("SQL Server script section degraded | section=%s | error=%s", key, exc)
        return _mssql_section(
            order,
            key,
            title,
            sql=sql,
            status="warning",
            metric_name="collector_status",
            metric_value="degraded",
            finding=f"{title}采集失败：{exc}",
            suggestion="检查 SQL Server 账号是否具备相关 DMV、msdb、扩展事件或系统视图读取权限。",
            warnings=[str(exc)],
        )


def _mssql_script_version_info(conn) -> dict:
    sql = [
        "SELECT @@VERSION AS sqlserver_version;",
        "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS product_version;",
        "SELECT CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition;",
        "SELECT CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(128)) AS product_level;",
    ]

    def collect():
        row = _one(conn, """
            SELECT
                @@VERSION AS sqlserver_version,
                CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS product_version,
                CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition,
                CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(128)) AS product_level
        """)
        items = [
            {"key": "SQL Server 版本", "value": row.get("sqlserver_version")},
            {"key": "产品版本", "value": row.get("product_version")},
            {"key": "版本", "value": row.get("edition")},
            {"key": "Service Pack", "value": row.get("product_level")},
        ]
        return _mssql_section(
            1,
            "version_info",
            "版本信息",
            summary=row,
            items=items,
            sql=sql,
            metric_name="sqlserver.version",
            metric_value=row.get("product_version") or "unknown",
            finding="已采集 SQL Server 版本、Edition、ProductVersion 和 ProductLevel。",
            suggestion="确认版本和补丁级别是否符合厂商支持周期，生产环境建议定期评估 CU/SP。",
        )

    return _mssql_safe_section(1, "version_info", "版本信息", collect, sql)


def _mssql_script_connection_info(conn) -> dict:
    sql = "SELECT @@MAX_CONNECTIONS AS max_connections;"

    def collect():
        row = _one(conn, sql)
        max_conn = row.get("max_connections")
        return _mssql_section(
            2,
            "connection_info",
            "连接信息",
            summary={"max_connections": max_conn},
            items=[{"key": "max_connections", "value": max_conn}],
            sql=sql,
            metric_name="sqlserver.max_connections",
            metric_value=max_conn,
            finding="已采集 SQL Server 最大连接数。",
            suggestion="结合当前连接数、连接池配置和业务并发评估连接容量。",
        )

    return _mssql_safe_section(2, "connection_info", "连接信息", collect, sql)


def _mssql_script_active_sessions(conn) -> dict:
    sql = "SELECT session_id, login_name, host_name, program_name, status FROM sys.dm_exec_sessions WHERE is_user_process = 1"

    def collect():
        rows = _fetch(conn, sql)
        active_count = len([row for row in rows if row.get("status") in {"running", "runnable", "suspended"}])
        return _mssql_section(
            3,
            "active_sessions",
            "活动会话",
            summary={"session_count": len(rows), "active_count": active_count},
            items=rows,
            sql=sql,
            metric_name="sqlserver.sessions",
            metric_value=len(rows),
            finding=f"已采集 {len(rows)} 个用户会话，其中活动/等待会话 {active_count} 个。",
            suggestion="如活动会话或等待会话异常升高，请进入会话事务页查看阻塞链和 SQL 文本。",
        )

    return _mssql_safe_section(3, "active_sessions", "活动会话", collect, sql)


def _mssql_script_database_size(conn) -> dict:
    sql = """
        SELECT
            d.name AS database_name,
            CAST(SUM(mf.size) * 8 / 1024.0 AS DECIMAL(18,2)) AS size_mb,
            d.state_desc,
            d.recovery_model_desc
        FROM sys.databases d
        JOIN sys.master_files mf ON d.database_id = mf.database_id
        GROUP BY d.name, d.state_desc, d.recovery_model_desc
        ORDER BY size_mb DESC
    """

    def collect():
        rows = _fetch(conn, sql)
        total_mb = round(sum(_num(row.get("size_mb")) for row in rows), 2)
        return _mssql_section(
            4,
            "database_size",
            "数据库大小",
            summary={"database_count": len(rows), "total_size_mb": total_mb},
            items=rows,
            sql=sql,
            metric_name="sqlserver.database.total_size_mb",
            metric_value=total_mb,
            finding=f"已采集 {len(rows)} 个数据库容量，总大小 {total_mb} MB。",
            suggestion="关注 TOP 数据库增长趋势，并与磁盘余量、备份窗口和归档策略联动分析。",
        )

    return _mssql_safe_section(4, "database_size", "数据库大小", collect, sql)


def _mssql_script_database_status(conn) -> dict:
    sql = "SELECT name, state_desc, recovery_model_desc, compatibility_level FROM sys.databases ORDER BY name"

    def collect():
        rows = _fetch(conn, sql)
        abnormal = [row for row in rows if row.get("state_desc") != "ONLINE"]
        status = "critical" if abnormal else "ok"
        finding = (
            f"发现 {len(abnormal)} 个数据库不是 ONLINE 状态。"
            if abnormal
            else f"已采集 {len(rows)} 个数据库状态，均为 ONLINE。"
        )
        return _mssql_section(
            5,
            "database_status",
            "数据库状态",
            summary={"database_count": len(rows), "abnormal_count": len(abnormal)},
            items=rows,
            sql=sql,
            status=status,
            metric_name="sqlserver.database.abnormal_count",
            metric_value=len(abnormal),
            finding=finding,
            suggestion="异常数据库需检查恢复状态、磁盘空间、最近恢复/附加操作和 SQL Server Error Log。",
        )

    return _mssql_safe_section(5, "database_status", "数据库状态", collect, sql)


def _mssql_script_config_parameters(conn) -> dict:
    sql = """
        SELECT
            name,
            CAST(value AS NVARCHAR(100)) AS value,
            CAST(value_in_use AS NVARCHAR(100)) AS value_in_use
        FROM sys.configurations
        WHERE name IN (
            'max server memory (MB)',
            'min server memory (MB)',
            'max degree of parallelism',
            'cost threshold for parallelism',
            'backup compression default'
        )
        ORDER BY name
    """

    def collect():
        rows = _fetch(conn, sql)
        warnings = []
        config = {row.get("name"): row.get("value_in_use") for row in rows}
        if _num(config.get("cost threshold for parallelism")) <= 5:
            warnings.append("cost threshold for parallelism 为默认或偏低。")
        if _num(config.get("backup compression default")) == 0:
            warnings.append("backup compression default 未开启。")
        status = "warning" if warnings else "ok"
        return _mssql_section(
            6,
            "config_parameters",
            "关键配置",
            summary={"parameter_count": len(rows), "warning_count": len(warnings)},
            items=rows,
            sql=sql,
            status=status,
            metric_name="sqlserver.config.warning_count",
            metric_value=len(warnings),
            finding=("；".join(warnings) if warnings else "关键配置采集完成，未发现明显配置风险。"),
            suggestion="结合 CPU、内存、并行等待和备份窗口评估 max memory、MAXDOP、并行成本阈值和备份压缩。",
            warnings=warnings,
        )

    return _mssql_safe_section(6, "config_parameters", "关键配置", collect, sql)


def _mssql_script_wait_stats(conn) -> dict:
    sql = """
        SELECT TOP 10
            wait_type,
            wait_time_ms,
            waiting_tasks_count
        FROM sys.dm_os_wait_stats
        WHERE wait_time_ms > 0
        ORDER BY wait_time_ms DESC
    """

    def collect():
        rows = _fetch(conn, sql)
        top_wait = rows[0].get("wait_type") if rows else "-"
        return _mssql_section(
            7,
            "wait_stats",
            "等待统计 TOP 10",
            summary={"wait_count": len(rows), "top_wait": top_wait},
            items=rows,
            sql=sql,
            metric_name="sqlserver.wait_stats.count",
            metric_value=len(rows),
            finding=f"已采集 TOP {len(rows)} 等待事件，最高等待为 {top_wait}。",
            suggestion="排除系统背景等待后，结合 AWR/SQL 画像、磁盘 IO 和阻塞链分析等待根因。",
        )

    return _mssql_safe_section(7, "wait_stats", "等待统计 TOP 10", collect, sql)


def _mssql_script_cpu_usage(conn) -> dict:
    sql = "SELECT @@CPU_BUSY AS cpu_busy_ms, @@IDLE AS idle_ms, @@TIMETICKS AS time_ticks;"

    def collect():
        row = _one(conn, sql)
        items = [{"key": key, "value": value} for key, value in row.items()]
        return _mssql_section(
            8,
            "cpu_usage",
            "CPU 信息",
            summary=row,
            items=items,
            sql=sql,
            metric_name="sqlserver.cpu_busy_ms",
            metric_value=row.get("cpu_busy_ms") or 0,
            finding="已采集 SQL Server CPU_BUSY、IDLE 和 TIMETICKS。",
            suggestion="该值为实例累计计数，建议结合 Prometheus/Zabbix 主机 CPU 指标做趋势判断。",
        )

    return _mssql_safe_section(8, "cpu_usage", "CPU 信息", collect, sql)


def _mssql_script_blocking(conn) -> dict:
    sql = "SELECT session_id, blocking_session_id, wait_duration_ms, wait_type FROM sys.dm_os_waiting_tasks WHERE blocking_session_id <> 0 ORDER BY wait_duration_ms DESC"

    def collect():
        rows = _fetch(conn, sql)
        status = "warning" if rows else "ok"
        return _mssql_section(
            9,
            "blocking",
            "阻塞进程",
            summary={"blocking_count": len(rows), "max_wait_duration_ms": max((_num(row.get("wait_duration_ms")) for row in rows), default=0)},
            items=rows,
            sql=sql,
            status=status,
            metric_name="sqlserver.blocking.count",
            metric_value=len(rows),
            finding=(f"发现 {len(rows)} 条阻塞等待。" if rows else "无阻塞进程。"),
            suggestion="发现阻塞时先定位 blocking_session_id、SQL 文本和事务来源，KILL 必须人工审批。",
        )

    return _mssql_safe_section(9, "blocking", "阻塞进程", collect, sql)


def _mssql_script_lock_info(conn) -> dict:
    sql = """
        SELECT
            CAST((SELECT COUNT(*) FROM sys.dm_tran_locks) AS BIGINT) AS lock_count,
            CAST(ISNULL((
                SELECT MAX(wt.wait_duration_ms)
                FROM sys.dm_os_waiting_tasks wt
                JOIN sys.dm_tran_locks tl ON wt.resource_address = tl.lock_owner_address
                WHERE wt.wait_type LIKE 'LCK%'
            ), 0) AS BIGINT) AS max_wait_time
    """

    def collect():
        row = _one(conn, sql)
        max_wait = _num(row.get("max_wait_time"))
        status = "warning" if max_wait >= 30000 else "ok"
        items = [{"key": key, "value": value} for key, value in row.items()]
        return _mssql_section(
            10,
            "lock_info",
            "锁信息",
            summary=row,
            items=items,
            sql=sql,
            status=status,
            metric_name="sqlserver.lock.max_wait_ms",
            metric_value=max_wait,
            finding=f"当前锁数量 {row.get('lock_count') or 0}，最大锁等待 {max_wait} ms。",
            suggestion="锁等待超过阈值时应进入会话事务页分析阻塞链、长事务和热点对象。",
        )

    return _mssql_safe_section(10, "lock_info", "锁信息", collect, sql)


def _mssql_script_deadlock_info(conn) -> dict:
    sql = """
        BEGIN TRY
            SELECT COUNT(*) AS deadlock_count
            FROM sys.fn_xe_file_target_read_file('DeadlockTracking*.xel', NULL, NULL, NULL)
            WHERE object_name = 'xml_deadlock_report'
        END TRY
        BEGIN CATCH
            SELECT 0 AS deadlock_count
        END CATCH
    """

    def collect():
        row = _one(conn, sql)
        deadlocks = int(_num(row.get("deadlock_count")))
        status = "warning" if deadlocks else "ok"
        return _mssql_section(
            11,
            "deadlock_info",
            "死锁信息",
            summary=row,
            items=[{"key": "deadlock_count", "value": deadlocks}],
            sql=sql,
            status=status,
            metric_name="sqlserver.deadlock.count",
            metric_value=deadlocks,
            finding=(f"扩展事件文件中发现 {deadlocks} 条死锁记录。" if deadlocks else "未发现 DeadlockTracking*.xel 死锁记录。"),
            suggestion="如存在死锁，解析 deadlock graph，定位对象、索引和访问顺序，优先从 SQL 和索引治理处理。",
        )

    return _mssql_safe_section(11, "deadlock_info", "死锁信息", collect, sql)


def _mssql_script_backup_info(conn) -> dict:
    sql = """
        SELECT
            database_name,
            CONVERT(NVARCHAR(20), MAX(backup_finish_date), 120) AS last_backup_time,
            DATEDIFF(HOUR, MAX(backup_finish_date), GETDATE()) AS backup_age_hours,
            CASE MAX(type)
                WHEN 'D' THEN N'完整'
                WHEN 'I' THEN N'差异'
                WHEN 'L' THEN N'日志'
                ELSE N'其他'
            END AS backup_type,
            CAST(MAX(backup_size) / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS backup_size_mb
        FROM msdb.dbo.backupset
        GROUP BY database_name
        ORDER BY database_name
    """

    def collect():
        rows = _fetch(conn, sql)
        return _mssql_section(
            12,
            "backup_info",
            "备份信息",
            summary={"database_count": len(rows)},
            items=rows,
            sql=sql,
            metric_name="sqlserver.backup.database_count",
            metric_value=len(rows),
            finding=f"已采集 {len(rows)} 个数据库的最近备份记录。",
            suggestion="生产库需确认完整、差异、日志备份链连续，并定期做恢复演练和 VERIFYONLY 校验。",
        )

    return _mssql_safe_section(12, "backup_info", "备份信息", collect, sql)


def _mssql_script_log_space(conn) -> dict:
    sql = """
        CREATE TABLE #logspace (
            DatabaseName sysname,
            LogSizeMB float,
            LogSpaceUsedPercent float,
            Status int
        );
        INSERT #logspace EXEC ('DBCC SQLPERF(LOGSPACE)');
        SELECT
            DatabaseName,
            CAST(LogSizeMB AS DECIMAL(18,2)) AS LogSizeMB,
            CAST(LogSpaceUsedPercent AS DECIMAL(10,2)) AS LogSpaceUsedPercent,
            Status
        FROM #logspace;
        DROP TABLE #logspace;
    """

    def collect():
        rows = _fetch(conn, sql)
        high = [row for row in rows if _num(row.get("LogSpaceUsedPercent")) >= 80]
        status = "critical" if any(_num(row.get("LogSpaceUsedPercent")) >= 95 for row in high) else ("warning" if high else "ok")
        return _mssql_section(
            13,
            "log_space",
            "日志空间",
            summary={"database_count": len(rows), "high_usage_count": len(high)},
            items=rows,
            sql=sql,
            status=status,
            metric_name="sqlserver.log.high_usage_count",
            metric_value=len(high),
            finding=(f"发现 {len(high)} 个数据库日志使用率超过 80%。" if high else "事务日志空间使用率未发现明显异常。"),
            suggestion="日志使用率高时检查长事务、复制/AG 延迟和日志备份链，FULL 模式需及时备份日志。",
        )

    return _mssql_safe_section(13, "log_space", "日志空间", collect, sql)


def _mssql_script_memory_usage(conn) -> dict:
    sql = """
        SELECT
            CAST(physical_memory_in_use_kb / 1024.0 AS DECIMAL(18,2)) AS memory_used_mb,
            CAST(available_commit_limit_kb / 1024.0 AS DECIMAL(18,2)) AS available_mb
        FROM sys.dm_os_process_memory
    """

    def collect():
        row = _one(conn, sql)
        items = [{"key": key, "value": value} for key, value in row.items()]
        return _mssql_section(
            14,
            "memory_usage",
            "SQL Server 内存",
            summary=row,
            items=items,
            sql=sql,
            metric_name="sqlserver.memory.used_mb",
            metric_value=row.get("memory_used_mb") or 0,
            finding=f"SQL Server 当前进程内存使用 {row.get('memory_used_mb') or 0} MB。",
            suggestion="结合 max server memory、OS 可用内存和 PLE/等待事件判断是否存在内存压力。",
        )

    return _mssql_safe_section(14, "memory_usage", "SQL Server 内存", collect, sql)


def _mssql_script_linked_servers(conn) -> dict:
    sql = "EXEC sp_linkedservers"

    def collect():
        rows = _fetch(conn, sql)
        status = "info" if rows else "ok"
        return _mssql_section(
            15,
            "linked_servers",
            "链接服务器",
            summary={"linked_server_count": len(rows)},
            items=rows,
            sql=sql,
            status=status,
            metric_name="sqlserver.linked_server.count",
            metric_value=len(rows),
            finding=(f"发现 {len(rows)} 个链接服务器配置。" if rows else "未发现链接服务器。"),
            suggestion="链接服务器需定期检查账号权限、远端连通性、超时设置和跨库调用风险。",
        )

    return _mssql_safe_section(15, "linked_servers", "链接服务器", collect, sql)


def _mssql_script_file_groups(conn) -> dict:
    sql = "SELECT data_space_id, name, type_desc, is_default FROM sys.filegroups ORDER BY data_space_id"

    def collect():
        rows = _fetch(conn, sql)
        return _mssql_section(
            16,
            "file_groups",
            "文件组",
            summary={"filegroup_count": len(rows)},
            items=rows,
            sql=sql,
            metric_name="sqlserver.filegroup.count",
            metric_value=len(rows),
            finding=f"已采集当前数据库 {len(rows)} 个文件组。",
            suggestion="大表、分区表和历史归档场景建议结合文件组设计隔离冷热数据。",
        )

    return _mssql_safe_section(16, "file_groups", "文件组", collect, sql)


def _mssql_script_system_info(conn) -> dict:
    sql = [
        "SELECT DISTINCT volume_mount_point, total_bytes / 1024 / 1024 / 1024 AS total_gb, available_bytes / 1024 / 1024 / 1024 AS available_gb FROM sys.master_files CROSS APPLY sys.dm_os_volume_stats(database_id, file_id);",
        "SELECT total_physical_memory_kb / 1024.0 / 1024 AS total_gb, available_physical_memory_kb / 1024.0 / 1024 AS available_gb FROM sys.dm_os_sys_memory;",
        "SELECT cpu_count, scheduler_count, hyperthread_ratio, physical_memory_kb / 1024.0 / 1024 AS physical_memory_gb FROM sys.dm_os_sys_info;",
    ]

    def collect():
        warnings = []
        disks = []
        memory = {}
        cpu = {}
        try:
            disks = _fetch(conn, """
                SELECT DISTINCT
                    volume_mount_point AS mount_point,
                    CAST(total_bytes / 1024.0 / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS total_gb,
                    CAST(available_bytes / 1024.0 / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS available_gb,
                    CAST((total_bytes - available_bytes) * 100.0 / NULLIF(total_bytes, 0) AS DECIMAL(10,2)) AS used_pct
                FROM sys.master_files
                CROSS APPLY sys.dm_os_volume_stats(database_id, file_id)
                ORDER BY mount_point
            """)
        except Exception as exc:
            warnings.append(f"磁盘卷信息采集失败：{exc}")
        try:
            memory = _one(conn, """
                SELECT
                    CAST(total_physical_memory_kb / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS total_gb,
                    CAST((total_physical_memory_kb - available_physical_memory_kb) / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS used_gb,
                    CAST(available_physical_memory_kb / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS available_gb,
                    CAST((total_physical_memory_kb - available_physical_memory_kb) * 100.0 / NULLIF(total_physical_memory_kb, 0) AS DECIMAL(10,2)) AS used_pct
                FROM sys.dm_os_sys_memory
            """)
        except Exception as exc:
            warnings.append(f"主机内存信息采集失败：{exc}")
        try:
            cpu = _one(conn, """
                SELECT
                    cpu_count,
                    scheduler_count,
                    hyperthread_ratio,
                    CAST(physical_memory_kb / 1024.0 / 1024.0 AS DECIMAL(18,2)) AS physical_memory_gb
                FROM sys.dm_os_sys_info
            """)
        except Exception as exc:
            warnings.append(f"主机 CPU 信息采集失败：{exc}")
        high_disk = [row for row in disks if _num(row.get("used_pct")) >= 85]
        status = "critical" if any(_num(row.get("used_pct")) >= 95 for row in high_disk) else ("warning" if high_disk or warnings else "ok")
        return _mssql_section(
            17,
            "system_info",
            "主机资源",
            summary={
                "disk_count": len(disks),
                "high_disk_count": len(high_disk),
                "memory": memory,
                "cpu": cpu,
            },
            items=[
                {"resource_type": "disk", **row} for row in disks
            ] + (
                [{"resource_type": "memory", **memory}] if memory else []
            ) + (
                [{"resource_type": "cpu", **cpu}] if cpu else []
            ),
            sql=sql,
            status=status,
            metric_name="host.disk.high_usage_count",
            metric_value=len(high_disk),
            finding=(f"发现 {len(high_disk)} 个磁盘卷使用率超过 85%。" if high_disk else "主机磁盘、内存、CPU 资源采集完成。"),
            suggestion="主机资源应与 Zabbix/Prometheus 指标绑定，磁盘或内存风险需要联动容量预测和告警事件。",
            warnings=warnings,
        )

    return _mssql_safe_section(17, "system_info", "主机资源", collect, sql)


def _mssql_section_finding(title: str, section: dict) -> str:
    rows = section.get("items") or []
    warnings = section.get("warnings") or []
    if warnings:
        return f"{title}采集存在异常：" + "；".join(str(item) for item in warnings[:3])
    return f"{title}采集完成，返回 {len(rows)} 行明细。"


def _mssql_section_suggestion(title: str, status: str) -> str:
    if status in {"critical", "warning"}:
        return f"请根据 {title} 原始明细生成 plan_only 方案，人工确认后在维护窗口处理。"
    if status == "info":
        return f"{title}为提示类信息，请纳入巡检报告和基线管理。"
    return f"保持定期巡检，并持续跟踪 {title} 趋势。"


def _pg_overview(conn) -> dict:
    summary = _one(conn, "SELECT version() AS version, current_database() AS database_name, current_user AS current_user, pg_postmaster_start_time() AS start_time, pg_database_size(current_database()) AS database_bytes")
    activity = _one(conn, "SELECT COUNT(*) AS total_sessions, COUNT(*) FILTER (WHERE state = 'active') AS active_sessions, COUNT(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting_sessions FROM pg_stat_activity")
    return {"summary": {**summary, **activity}, "items": []}


def _pg_storage(conn) -> dict:
    dbs = _fetch(conn, "SELECT datname, pg_database_size(datname) AS size_bytes FROM pg_database ORDER BY size_bytes DESC")
    tables = _fetch(conn, "SELECT schemaname, relname AS table_name, pg_total_relation_size(format('%I.%I', schemaname, relname)) AS total_bytes, n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze FROM pg_stat_user_tables ORDER BY pg_total_relation_size(format('%I.%I', schemaname, relname)) DESC LIMIT 50")
    return {"summary": {"database_count": len(dbs)}, "databases": dbs, "items": tables}


def _pg_wal(conn) -> dict:
    settings = _fetch(conn, "SELECT name, setting, unit, context FROM pg_settings WHERE name IN ('wal_level','archive_mode','archive_command','max_wal_size','min_wal_size','wal_keep_size','max_wal_senders') ORDER BY name")
    slots = _fetch(conn, "SELECT slot_name, plugin, slot_type, active, restart_lsn, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes FROM pg_replication_slots")
    return {"summary": {"setting_count": len(settings), "slot_count": len(slots)}, "items": settings, "replication_slots": slots}


def _pg_sessions(conn) -> dict:
    rows = _fetch(conn, "SELECT pid, usename, datname, state, wait_event_type, wait_event, now() - xact_start AS xact_age, now() - query_start AS query_age, left(query, 500) AS query FROM pg_stat_activity WHERE pid <> pg_backend_pid() ORDER BY query_start NULLS LAST LIMIT 100")
    return {"summary": {"session_count": len(rows)}, "items": rows}


def _pg_tables(conn) -> dict:
    rows = _fetch(conn, "SELECT schemaname, relname AS table_name, seq_scan, idx_scan, n_live_tup, n_dead_tup, CASE WHEN (seq_scan + idx_scan) > 0 THEN ROUND(seq_scan::numeric * 100 / (seq_scan + idx_scan), 2) ELSE 0 END AS seq_scan_pct FROM pg_stat_user_tables ORDER BY n_dead_tup DESC, seq_scan DESC LIMIT 100")
    return {"summary": {"table_count": len(rows)}, "items": rows}


def _pg_security(conn) -> dict:
    rows = _fetch(conn, "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolvaliduntil FROM pg_roles ORDER BY rolsuper DESC, rolcanlogin DESC, rolname")
    return {"summary": {"role_count": len(rows), "superuser_count": len([r for r in rows if r.get("rolsuper")])}, "items": rows}


def _pg_ha(conn) -> dict:
    recovery = _one(conn, "SELECT pg_is_in_recovery() AS in_recovery")
    replication = _fetch(conn, "SELECT pid, usename, application_name, client_addr, state, sync_state, sent_lsn, write_lsn, flush_lsn, replay_lsn FROM pg_stat_replication")
    return {"summary": recovery, "items": replication}


def _pg_backup(conn) -> dict:
    rows = _fetch(conn, "SELECT name, setting, unit, short_desc FROM pg_settings WHERE name IN ('archive_mode','archive_command','wal_level','max_wal_senders','wal_keep_size') ORDER BY name")
    return {"summary": {"settings_count": len(rows)}, "items": rows}


def _pg_governance(conn) -> dict:
    rows = _fetch(conn, "SELECT schemaname, relname AS table_name, n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE n_live_tup > 1000000 OR n_dead_tup > 100000 ORDER BY n_dead_tup DESC, n_live_tup DESC LIMIT 100")
    return {"summary": {"candidate_count": len(rows)}, "items": rows}


def _mssql_overview(conn) -> dict:
    warnings: list[str] = []
    server = _one(conn, """
        SELECT
            @@SERVERNAME AS server_name,
            @@SERVICENAME AS service_name,
            @@VERSION AS version,
            SERVERPROPERTY('MachineName') AS machine_name,
            SERVERPROPERTY('InstanceName') AS instance_name,
            SERVERPROPERTY('Edition') AS edition,
            SERVERPROPERTY('ProductVersion') AS product_version,
            SERVERPROPERTY('ProductLevel') AS product_level,
            SERVERPROPERTY('EngineEdition') AS engine_edition,
            CAST(SERVERPROPERTY('IsClustered') AS INT) AS is_clustered,
            CAST(SERVERPROPERTY('IsHadrEnabled') AS INT) AS is_hadr_enabled,
            DB_NAME() AS database_name,
            SUSER_SNAME() AS current_login
    """)
    sessions = _one(conn, """
        SELECT
            COUNT(*) AS total_sessions,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_sessions,
            SUM(CASE WHEN status = 'sleeping' THEN 1 ELSE 0 END) AS sleeping_sessions,
            SUM(CASE WHEN open_transaction_count > 0 THEN 1 ELSE 0 END) AS open_transaction_sessions
        FROM sys.dm_exec_sessions
        WHERE is_user_process = 1
    """)
    databases = _fetch(conn, """
        SELECT
            database_id,
            name AS database_name,
            state_desc,
            user_access_desc,
            recovery_model_desc,
            compatibility_level,
            is_read_only,
            is_auto_close_on,
            is_auto_shrink_on,
            page_verify_option_desc,
            create_date
        FROM sys.databases
        ORDER BY database_id
    """)

    runtime = {}
    try:
        runtime = _one(conn, """
            SELECT
                sqlserver_start_time,
                cpu_count,
                physical_memory_kb,
                committed_kb
            FROM sys.dm_os_sys_info
        """)
    except Exception as exc:
        warnings.append(f"运行时信息采集失败：{exc}")

    configurations = []
    try:
        configurations = _fetch(conn, """
            SELECT
                name,
                CONVERT(BIGINT, value) AS value,
                CONVERT(BIGINT, value_in_use) AS value_in_use,
                description,
                is_dynamic,
                is_advanced
            FROM sys.configurations
            WHERE name IN (
                'backup compression default',
                'cost threshold for parallelism',
                'max degree of parallelism',
                'max server memory (MB)',
                'min server memory (MB)',
                'optimize for ad hoc workloads',
                'remote admin connections'
            )
            ORDER BY name
        """)
    except Exception as exc:
        warnings.append(f"核心参数采集失败：{exc}")

    server = {**server, **runtime}
    online_count = len([row for row in databases if row.get("state_desc") == "ONLINE"])
    offline_count = len(databases) - online_count
    risk_items = _mssql_overview_risks(server, sessions, databases, configurations)
    summary = {
        **server,
        **sessions,
        "database_count": len(databases),
        "online_database_count": online_count,
        "offline_database_count": offline_count,
        "risk_count": len(risk_items),
    }
    return {
        "summary": summary,
        "server": server,
        "items": databases,
        "databases": databases,
        "configurations": configurations,
        "risk_items": risk_items,
        "warnings": warnings,
    }


def _mssql_config_value(configurations: list[dict], name: str):
    for row in configurations:
        if row.get("name") == name:
            return row.get("value_in_use")
    return None


def _mssql_overview_risks(server: dict, sessions: dict, databases: list[dict], configurations: list[dict]) -> list[dict]:
    risks: list[dict] = []
    offline_databases = [row for row in databases if row.get("state_desc") != "ONLINE"]
    if offline_databases:
        names = ", ".join(str(row.get("database_name")) for row in offline_databases[:5])
        risks.append({
            "level": "critical",
            "category": "数据库状态",
            "object_name": names,
            "message": f"{len(offline_databases)} 个数据库不是 ONLINE 状态。",
            "suggestion": "检查数据库恢复状态、磁盘空间、最近恢复/附加操作和 SQL Server Error Log。",
        })

    simple_recovery = [
        row for row in databases
        if str(row.get("database_name") or "").lower() not in {"master", "model", "msdb", "tempdb"}
        and row.get("recovery_model_desc") == "SIMPLE"
    ]
    if simple_recovery:
        names = ", ".join(str(row.get("database_name")) for row in simple_recovery[:5])
        risks.append({
            "level": "warning",
            "category": "恢复模型",
            "object_name": names,
            "message": f"{len(simple_recovery)} 个业务库为 SIMPLE 恢复模型，无法形成日志备份链。",
            "suggestion": "如需时间点恢复，评估切换为 FULL 恢复模型并配置日志备份。",
        })

    auto_close = [row for row in databases if row.get("is_auto_close_on")]
    if auto_close:
        risks.append({
            "level": "warning",
            "category": "数据库选项",
            "object_name": ", ".join(str(row.get("database_name")) for row in auto_close[:5]),
            "message": "发现 AUTO_CLOSE 开启，可能导致频繁打开数据库和计划缓存抖动。",
            "suggestion": "生产库建议关闭 AUTO_CLOSE。",
        })

    auto_shrink = [row for row in databases if row.get("is_auto_shrink_on")]
    if auto_shrink:
        risks.append({
            "level": "warning",
            "category": "数据库选项",
            "object_name": ", ".join(str(row.get("database_name")) for row in auto_shrink[:5]),
            "message": "发现 AUTO_SHRINK 开启，可能导致文件频繁收缩和索引碎片。",
            "suggestion": "生产库建议关闭 AUTO_SHRINK，并通过容量治理处理空间回收。",
        })

    max_memory = _num(_mssql_config_value(configurations, "max server memory (MB)"))
    physical_memory_mb = _num(server.get("physical_memory_kb")) / 1024 if server.get("physical_memory_kb") else 0
    if max_memory >= 2147483647 or (physical_memory_mb and max_memory > physical_memory_mb * 0.9):
        risks.append({
            "level": "warning",
            "category": "内存配置",
            "object_name": "max server memory (MB)",
            "message": "SQL Server 最大内存接近默认值或超过物理内存 90%，可能挤压操作系统内存。",
            "suggestion": "根据实例角色、主机内存和并发量设置合理 max server memory。",
        })

    cost_threshold = _num(_mssql_config_value(configurations, "cost threshold for parallelism"))
    if cost_threshold and cost_threshold <= 5:
        risks.append({
            "level": "warning",
            "category": "并行配置",
            "object_name": "cost threshold for parallelism",
            "message": "并行成本阈值仍为默认或偏低，可能产生过多并行计划。",
            "suggestion": "结合 CPU、等待事件和业务 SQL，评估调高并行成本阈值。",
        })

    backup_compression = _num(_mssql_config_value(configurations, "backup compression default"))
    if backup_compression == 0:
        risks.append({
            "level": "info",
            "category": "备份配置",
            "object_name": "backup compression default",
            "message": "默认备份压缩未开启，备份耗时和容量可能偏高。",
            "suggestion": "评估 CPU 余量后开启备份压缩，或在备份脚本中显式 WITH COMPRESSION。",
        })

    open_transactions = _num(sessions.get("open_transaction_sessions"))
    if open_transactions >= 10:
        risks.append({
            "level": "warning",
            "category": "会话事务",
            "object_name": "open_transaction_sessions",
            "message": f"当前存在 {int(open_transactions)} 个打开事务会话。",
            "suggestion": "进入会话事务页查看长事务、阻塞链和来源程序。",
        })
    return risks


def _mssql_storage(conn) -> dict:
    warnings = []
    volume_sql = """
        SELECT
            mf.database_id,
            DB_NAME(mf.database_id) AS database_name,
            mf.file_id,
            mf.name AS logical_name,
            mf.physical_name,
            mf.type_desc,
            mf.state_desc,
            CAST(mf.size AS FLOAT) * 8 / 1024 AS size_mb,
            mf.growth,
            mf.is_percent_growth,
            CASE WHEN mf.is_percent_growth = 1 THEN NULL ELSE CAST(mf.growth AS FLOAT) * 8 / 1024 END AS growth_mb,
            CASE WHEN mf.max_size IN (-1, 268435456) THEN NULL ELSE CAST(mf.max_size AS FLOAT) * 8 / 1024 END AS max_size_mb,
            vs.volume_mount_point,
            CAST(vs.total_bytes AS FLOAT) / 1024 / 1024 AS volume_total_mb,
            CAST(vs.available_bytes AS FLOAT) / 1024 / 1024 AS volume_free_mb
        FROM sys.master_files mf
        OUTER APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
        ORDER BY mf.database_id, mf.type_desc, mf.file_id
    """
    basic_sql = """
        SELECT
            mf.database_id,
            DB_NAME(mf.database_id) AS database_name,
            mf.file_id,
            mf.name AS logical_name,
            mf.physical_name,
            mf.type_desc,
            mf.state_desc,
            CAST(mf.size AS FLOAT) * 8 / 1024 AS size_mb,
            mf.growth,
            mf.is_percent_growth,
            CASE WHEN mf.is_percent_growth = 1 THEN NULL ELSE CAST(mf.growth AS FLOAT) * 8 / 1024 END AS growth_mb,
            CASE WHEN mf.max_size IN (-1, 268435456) THEN NULL ELSE CAST(mf.max_size AS FLOAT) * 8 / 1024 END AS max_size_mb
        FROM sys.master_files mf
        ORDER BY mf.database_id, mf.type_desc, mf.file_id
    """
    try:
        files = _fetch(conn, volume_sql)
    except Exception as exc:
        warnings.append(f"卷剩余空间采集失败：{exc}")
        files = _fetch(conn, basic_sql)

    database_map: dict[str, dict] = {}
    for row in files:
        size_mb = _round(row.get("size_mb"))
        growth_mb = _round(row.get("growth_mb"))
        max_size_mb = _round(row.get("max_size_mb"))
        volume_total_mb = _round(row.get("volume_total_mb"))
        volume_free_mb = _round(row.get("volume_free_mb"))
        is_percent_growth = bool(row.get("is_percent_growth"))
        row["size_mb"] = size_mb
        row["growth_mb"] = growth_mb
        row["max_size_mb"] = max_size_mb
        row["volume_total_mb"] = volume_total_mb
        row["volume_free_mb"] = volume_free_mb
        row["growth_desc"] = f"{_num(row.get('growth')):g}%" if is_percent_growth else f"{growth_mb or 0:g} MB"
        row["max_size_desc"] = f"{max_size_mb:g} MB" if max_size_mb else "UNLIMITED"
        if volume_total_mb:
            row["volume_free_pct"] = round((_num(volume_free_mb) / _num(volume_total_mb)) * 100, 2)

        name = row.get("database_name") or "UNKNOWN"
        bucket = database_map.setdefault(name, {
            "database_name": name,
            "file_count": 0,
            "data_file_count": 0,
            "log_file_count": 0,
            "total_size_mb": 0.0,
            "data_size_mb": 0.0,
            "log_size_mb": 0.0,
        })
        bucket["file_count"] += 1
        bucket["total_size_mb"] += _num(size_mb)
        if row.get("type_desc") == "LOG":
            bucket["log_file_count"] += 1
            bucket["log_size_mb"] += _num(size_mb)
        else:
            bucket["data_file_count"] += 1
            bucket["data_size_mb"] += _num(size_mb)

    databases = []
    for item in database_map.values():
        for key in ("total_size_mb", "data_size_mb", "log_size_mb"):
            item[key] = round(item[key], 2)
        databases.append(item)
    databases.sort(key=lambda item: item.get("total_size_mb") or 0, reverse=True)

    tempdb_files = [row for row in files if str(row.get("database_name") or "").lower() == "tempdb"]
    risks = _mssql_space_risks(files)
    total_size_mb = sum(_num(row.get("size_mb")) for row in files)
    data_size_mb = sum(_num(row.get("size_mb")) for row in files if row.get("type_desc") != "LOG")
    log_size_mb = total_size_mb - data_size_mb
    tempdb_size_mb = sum(_num(row.get("size_mb")) for row in tempdb_files)
    percent_growth_count = len([row for row in files if row.get("is_percent_growth")])

    return {
        "summary": {
            "database_count": len(databases),
            "file_count": len(files),
            "total_size_mb": round(total_size_mb, 2),
            "data_size_mb": round(data_size_mb, 2),
            "log_size_mb": round(log_size_mb, 2),
            "tempdb_size_mb": round(tempdb_size_mb, 2),
            "tempdb_file_count": len(tempdb_files),
            "percent_growth_count": percent_growth_count,
            "risk_count": len(risks),
        },
        "items": databases,
        "databases": databases,
        "files": files,
        "tempdb_files": tempdb_files,
        "growth_risks": risks,
        "warnings": warnings,
    }


def _mssql_space_risks(files: list[dict]) -> list[dict]:
    risks: list[dict] = []
    seen_volumes = set()
    for row in files:
        database_name = row.get("database_name") or "-"
        logical_name = row.get("logical_name") or "-"
        object_name = f"{database_name}.{logical_name}"
        growth = _num(row.get("growth"))
        growth_mb = _num(row.get("growth_mb"))
        size_mb = _num(row.get("size_mb"))
        max_size_mb = _num(row.get("max_size_mb"))
        volume_free_mb = row.get("volume_free_mb")
        volume_total_mb = row.get("volume_total_mb")

        if growth <= 0:
            risks.append({
                "level": "warning",
                "category": "文件增长",
                "object_name": object_name,
                "message": "文件未配置自动增长，容量耗尽时可能导致写入失败。",
                "suggestion": "评估业务峰值后设置固定 MB 增长，并保留人工审批扩容流程。",
            })
        elif row.get("is_percent_growth"):
            risks.append({
                "level": "warning",
                "category": "文件增长",
                "object_name": object_name,
                "message": f"文件使用百分比增长（{growth:g}%），大文件增长时可能出现长时间阻塞。",
                "suggestion": "建议改为固定 MB 增长，例如数据文件 512MB/1024MB，日志文件按写入峰值评估。",
            })
        elif 0 < growth_mb < 64:
            risks.append({
                "level": "warning",
                "category": "文件增长",
                "object_name": object_name,
                "message": f"文件增长步长较小（{growth_mb:g} MB），可能产生频繁 autogrow。",
                "suggestion": "建议按容量和增长速度调整为固定较大步长，减少频繁扩展。",
            })

        if max_size_mb and size_mb:
            used_pct = round(size_mb / max_size_mb * 100, 2)
            if used_pct >= 85:
                risks.append({
                    "level": "critical" if used_pct >= 95 else "warning",
                    "category": "最大容量",
                    "object_name": object_name,
                    "message": f"文件已接近 MAXSIZE，当前占比 {used_pct}%。",
                    "suggestion": "检查磁盘/NFS/存储池余量，生成 MODIFY FILE 扩容方案。",
                })

        volume_key = row.get("volume_mount_point") or row.get("physical_name")
        if volume_key and volume_key not in seen_volumes and volume_free_mb is not None and volume_total_mb:
            seen_volumes.add(volume_key)
            free_mb = _num(volume_free_mb)
            free_pct = round(free_mb / _num(volume_total_mb) * 100, 2)
            if free_pct < 10 or free_mb < 10240:
                risks.append({
                    "level": "critical" if free_pct < 5 or free_mb < 2048 else "warning",
                    "category": "磁盘余量",
                    "object_name": volume_key,
                    "message": f"卷剩余空间 {round(free_mb, 2)} MB（{free_pct}%）。",
                    "suggestion": "优先清理无效备份/归档，或扩展底层卷后再调整数据库文件。",
                })
    return risks


def _mssql_logs(conn) -> dict:
    logspace = _fetch(conn, "DBCC SQLPERF(LOGSPACE)")
    files = _fetch(conn, "SELECT DB_NAME(database_id) AS database_name, name, type_desc, size * 8 / 1024 AS size_mb, growth, is_percent_growth FROM sys.master_files WHERE type_desc = 'LOG' ORDER BY database_id")
    return {"summary": {"database_count": len(logspace), "log_file_count": len(files)}, "items": logspace, "files": files}


def _mssql_sessions(conn) -> dict:
    requests = _fetch(conn, "SELECT TOP 100 r.session_id, s.login_name, s.host_name, r.status, r.command, r.wait_type, r.blocking_session_id, r.cpu_time, r.total_elapsed_time, SUBSTRING(t.text, 1, 500) AS sql_text FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t WHERE s.is_user_process = 1 ORDER BY r.total_elapsed_time DESC")
    sessions = _fetch(conn, "SELECT TOP 100 session_id, login_name, host_name, program_name, status, open_transaction_count, last_request_start_time, last_request_end_time FROM sys.dm_exec_sessions WHERE is_user_process = 1 ORDER BY last_request_start_time DESC")
    return {
        "summary": {"request_count": len(requests), "session_count": len(sessions)},
        "items": requests or sessions,
        "requests": requests,
        "sessions": sessions,
    }


def _mssql_tables(conn) -> dict:
    rows = _fetch(conn, "SELECT TOP 100 SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count, SUM(a.total_pages) * 8 / 1024 AS total_mb FROM sys.tables t JOIN sys.indexes i ON t.object_id = i.object_id JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id JOIN sys.allocation_units a ON p.partition_id = a.container_id GROUP BY t.schema_id, t.name ORDER BY total_mb DESC")
    return {"summary": {"table_count": len(rows)}, "items": rows}


def _mssql_security(conn) -> dict:
    rows = _fetch(conn, "SELECT name, type_desc, is_disabled, create_date, modify_date, default_database_name FROM sys.server_principals WHERE type IN ('S','U','G') ORDER BY is_disabled, name")
    return {"summary": {"principal_count": len(rows)}, "items": rows}


def _mssql_ha(conn) -> dict:
    rows = _fetch(conn, "SELECT d.name AS database_name, d.recovery_model_desc, d.state_desc, ars.synchronization_state_desc, ars.synchronization_health_desc FROM sys.databases d LEFT JOIN sys.dm_hadr_database_replica_states ars ON d.database_id = ars.database_id ORDER BY d.name")
    return {"summary": {"database_count": len(rows)}, "items": rows}


def _mssql_backup(conn) -> dict:
    rows = _fetch(conn, "SELECT d.name AS database_name, MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END) AS last_full_backup, MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END) AS last_log_backup FROM sys.databases d LEFT JOIN msdb.dbo.backupset b ON d.name = b.database_name GROUP BY d.name ORDER BY d.name")
    return {"summary": {"database_count": len(rows)}, "items": rows}


def _mssql_governance(conn) -> dict:
    rows = _fetch(conn, "SELECT TOP 100 SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count FROM sys.tables t JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1) GROUP BY t.schema_id, t.name HAVING SUM(p.rows) > 5000000 ORDER BY row_count DESC")
    return {
        "summary": {"candidate_count": len(rows), "threshold_rows": 5000000},
        "items": rows,
        "empty_reason": "当前没有超过 500 万行阈值的 SQL Server 治理候选表。",
    }


_PG_COLLECTORS = {
    "overview": _pg_overview,
    "space": _pg_storage,
    "wal": _pg_wal,
    "sessionTransaction": _pg_sessions,
    "objectStats": _pg_tables,
    "roles": _pg_security,
    "ha": _pg_ha,
    "backupRecovery": _pg_backup,
    "governance": _pg_governance,
}

_SQLSERVER_COLLECTORS = {
    "overview": _mssql_overview,
    "space": _mssql_storage,
    "logs": _mssql_logs,
    "sessionTransaction": _mssql_sessions,
    "objectStats": _mssql_tables,
    "logins": _mssql_security,
    "ha": _mssql_ha,
    "backupRecovery": _mssql_backup,
    "governance": _mssql_governance,
}
