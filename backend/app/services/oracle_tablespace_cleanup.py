import logging
from datetime import datetime

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_tablespace_cleanup")


def get_system_audit_summary(instance: DBInstance) -> dict:
    logger.info("SYSTEM audit summary | instance=%s", instance.instance_name)
    sql = """
        SELECT owner,
               segment_name,
               segment_type,
               tablespace_name,
               ROUND(bytes / 1024 / 1024, 2) AS size_mb
        FROM dba_segments
        WHERE tablespace_name = 'SYSTEM'
          AND owner = 'SYS'
          AND segment_name IN ('AUD$', 'FGA_LOG$')
        ORDER BY bytes DESC
    """
    items = _query(instance, sql, "SYSTEM 审计对象占用查询失败，请确认账号具备 dba_segments 查询权限。")
    total_mb = round(sum(float(row.get("size_mb") or 0) for row in items), 2)
    return {
        "tablespace_name": "SYSTEM",
        "items": items,
        "total_mb": total_mb,
        "suggestions": _audit_suggestions(items),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def build_system_audit_cleanup_plan(retention_days: int = 30, include_fga: bool = True,
                                    include_location_plan: bool = False,
                                    audit_trail_location: str = "SYSAUX") -> dict:
    blocks = [
        "-- SYSTEM 表空间专项治理：传统审计 AUD$ 清理",
        "-- 只生成 SQL，不直接执行。执行前请确认审计合规要求并完成必要备份。",
        "",
        "BEGIN",
        "  DBMS_AUDIT_MGMT.INIT_CLEANUP(",
        "    audit_trail_type         => DBMS_AUDIT_MGMT.AUDIT_TRAIL_AUD_STD,",
        "    default_cleanup_interval => 24",
        "  );",
        "END;",
        "/",
        "",
        "BEGIN",
        "  DBMS_AUDIT_MGMT.SET_LAST_ARCHIVE_TIMESTAMP(",
        "    audit_trail_type  => DBMS_AUDIT_MGMT.AUDIT_TRAIL_AUD_STD,",
        f"    last_archive_time => SYSTIMESTAMP - INTERVAL '{int(retention_days)}' DAY",
        "  );",
        "END;",
        "/",
        "",
        "BEGIN",
        "  DBMS_AUDIT_MGMT.CLEAN_AUDIT_TRAIL(",
        "    audit_trail_type        => DBMS_AUDIT_MGMT.AUDIT_TRAIL_AUD_STD,",
        "    use_last_arch_timestamp => TRUE",
        "  );",
        "END;",
        "/",
    ]

    if include_fga:
        blocks.extend([
            "",
            "-- FGA_LOG$ 精细审计清理",
            "BEGIN",
            "  DBMS_AUDIT_MGMT.INIT_CLEANUP(",
            "    audit_trail_type         => DBMS_AUDIT_MGMT.AUDIT_TRAIL_FGA_STD,",
            "    default_cleanup_interval => 24",
            "  );",
            "END;",
            "/",
            "",
            "BEGIN",
            "  DBMS_AUDIT_MGMT.SET_LAST_ARCHIVE_TIMESTAMP(",
            "    audit_trail_type  => DBMS_AUDIT_MGMT.AUDIT_TRAIL_FGA_STD,",
            f"    last_archive_time => SYSTIMESTAMP - INTERVAL '{int(retention_days)}' DAY",
            "  );",
            "END;",
            "/",
            "",
            "BEGIN",
            "  DBMS_AUDIT_MGMT.CLEAN_AUDIT_TRAIL(",
            "    audit_trail_type        => DBMS_AUDIT_MGMT.AUDIT_TRAIL_FGA_STD,",
            "    use_last_arch_timestamp => TRUE",
            "  );",
            "END;",
            "/",
        ])

    if include_location_plan:
        location = _safe_identifier(audit_trail_location or "SYSAUX")
        blocks.extend([
            "",
            "-- 审计表迁移建议：长期治理时可考虑将 AUD$ 移出 SYSTEM",
            "-- 请确认目标表空间容量和变更窗口后再执行。",
            "BEGIN",
            "  DBMS_AUDIT_MGMT.SET_AUDIT_TRAIL_LOCATION(",
            "    audit_trail_type           => DBMS_AUDIT_MGMT.AUDIT_TRAIL_AUD_STD,",
            f"    audit_trail_location_value => '{location}'",
            "  );",
            "END;",
            "/",
        ])

    return {
        "status": "plan_only",
        "message": "已生成 SYSTEM 审计清理 SQL，请人工确认后执行。",
        "sql": "\n".join(blocks),
        "warnings": [
            "不直接 DELETE SYS.AUD$ 或 SYS.FGA_LOG$。",
            "执行前请确认审计保留要求、备份情况和维护窗口。",
        ],
    }


def get_sysaux_awr_summary(instance: DBInstance) -> dict:
    logger.info("SYSAUX AWR summary | instance=%s", instance.instance_name)
    occupants_sql = """
        SELECT occupant_name,
               occupant_desc,
               schema_name,
               space_usage_kbytes,
               ROUND(space_usage_kbytes / 1024, 2) AS space_usage_mb
        FROM v$sysaux_occupants
        ORDER BY space_usage_kbytes DESC
    """
    snapshots_sql = """
        SELECT dbid,
               instance_number,
               MIN(snap_id) AS min_snap_id,
               MAX(snap_id) AS max_snap_id,
               MIN(begin_interval_time) AS min_time,
               MAX(end_interval_time) AS max_time,
               COUNT(*) AS snap_count
        FROM dba_hist_snapshot
        GROUP BY dbid, instance_number
        ORDER BY dbid, instance_number
    """
    occupants = _query(instance, occupants_sql, "SYSAUX 占用构成查询失败，请确认账号具备 v$sysaux_occupants 查询权限。")
    snapshots = _query(instance, snapshots_sql, "AWR 快照范围查询失败，请确认账号具备 dba_hist_snapshot 查询权限。")
    awr_mb = round(sum(float(row.get("space_usage_mb") or 0) for row in occupants if _is_awr_occupant(row)), 2)
    return {
        "tablespace_name": "SYSAUX",
        "occupants": occupants,
        "snapshots": snapshots,
        "awr_mb": awr_mb,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def build_sysaux_awr_cleanup_plan(instance: DBInstance, retention_days: int = 30) -> dict:
    candidates = _awr_cleanup_candidates(instance, retention_days)
    blocks = [
        "-- SYSAUX 表空间专项治理：AWR 快照清理",
        "-- 只生成 SQL，不直接执行。不要直接 DELETE WRH$_ / WRI$_ 表数据。",
        f"-- 保留最近 {int(retention_days)} 天快照，清理更早的快照范围。",
        "",
    ]
    if candidates:
        for row in candidates:
            blocks.extend([
                f"-- DBID {row.get('dbid')}，快照 {row.get('low_snap_id')} - {row.get('high_snap_id')}，共 {row.get('snap_count')} 条",
                "BEGIN",
                "  DBMS_WORKLOAD_REPOSITORY.DROP_SNAPSHOT_RANGE(",
                f"    low_snap_id  => {int(row.get('low_snap_id'))},",
                f"    high_snap_id => {int(row.get('high_snap_id'))},",
                f"    dbid         => {int(row.get('dbid'))}",
                "  );",
                "END;",
                "/",
                "",
            ])
    else:
        blocks.append("-- 未查询到超过保留天数的 AWR 快照，无需生成 DROP_SNAPSHOT_RANGE。")

    return {
        "status": "plan_only",
        "message": "已生成 AWR 快照清理 SQL，请人工确认后执行。",
        "sql": "\n".join(blocks).rstrip(),
        "candidates": candidates,
        "warnings": [
            "仅使用 DBMS_WORKLOAD_REPOSITORY.DROP_SNAPSHOT_RANGE。",
            "执行前请确认 AWR 报告保留要求和维护窗口。",
        ],
    }


def build_sysaux_awr_retention_plan(retention_days: int = 30, interval_minutes: int = 60) -> dict:
    retention_minutes = int(retention_days) * 24 * 60
    interval_minutes = int(interval_minutes)
    sql = "\n".join([
        "-- 修改 AWR 快照保留策略",
        "-- 只生成 SQL，不直接执行。",
        "BEGIN",
        "  DBMS_WORKLOAD_REPOSITORY.MODIFY_SNAPSHOT_SETTINGS(",
        f"    retention => {retention_minutes},",
        f"    interval  => {interval_minutes}",
        "  );",
        "END;",
        "/",
    ])
    return {
        "status": "plan_only",
        "message": "已生成 AWR 保留策略调整 SQL，请人工确认后执行。",
        "sql": sql,
        "warnings": ["调整 AWR 保留策略会影响历史性能数据保留窗口。"],
    }


def _awr_cleanup_candidates(instance: DBInstance, retention_days: int) -> list[dict]:
    sql = """
        SELECT dbid,
               MIN(snap_id) AS low_snap_id,
               MAX(snap_id) AS high_snap_id,
               COUNT(*) AS snap_count,
               MIN(begin_interval_time) AS begin_time,
               MAX(end_interval_time) AS end_time
        FROM dba_hist_snapshot
        WHERE begin_interval_time < SYSDATE - :1
        GROUP BY dbid
        ORDER BY dbid
    """
    return _query(instance, sql, "可清理 AWR 快照查询失败，请确认账号具备 dba_hist_snapshot 查询权限。", [retention_days])


def _query(instance: DBInstance, sql: str, error_prefix: str, params: list | None = None) -> list[dict]:
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or [])
            return safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        raise RuntimeError(f"{error_prefix} 原始错误：{exc}") from exc


def _audit_suggestions(items: list[dict]) -> list[str]:
    if not items:
        return ["未发现 SYS.AUD$ / SYS.FGA_LOG$ 在 SYSTEM 中占用空间。"]
    suggestions = ["建议优先使用 DBMS_AUDIT_MGMT 生成清理脚本，不要直接删除 SYS.AUD$。"]
    if sum(float(row.get("size_mb") or 0) for row in items) >= 1024:
        suggestions.append("审计对象占用超过 1GB，可评估迁移审计表位置或建立定期清理策略。")
    return suggestions


def _is_awr_occupant(row: dict) -> bool:
    name = str(row.get("occupant_name") or "").upper()
    desc = str(row.get("occupant_desc") or "").upper()
    return "AWR" in name or "WORKLOAD" in name or "ASH" in desc or "AWR" in desc


def _safe_identifier(value: str) -> str:
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum() or ch in "_#$")
    return cleaned or "SYSAUX"
