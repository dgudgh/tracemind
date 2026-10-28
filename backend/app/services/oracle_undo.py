import logging
from datetime import datetime

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_undo")


def get_undo_overview(instance: DBInstance) -> dict:
    logger.info("Oracle UNDO overview | instance=%s", instance.instance_name)
    params = get_undo_parameters(instance)
    extents = get_undo_extents(instance)
    history = get_undo_history(instance)

    param_map = {item["name"]: item for item in params}
    undo_tablespace = (param_map.get("undo_tablespace") or {}).get("value") or ""
    undo_retention = _to_int((param_map.get("undo_retention") or {}).get("value"))
    latest = history[0] if history else {}
    tuned_retention = _to_int(latest.get("tuned_undoretention"))

    current_rows = [
        row for row in extents.get("items", [])
        if not undo_tablespace or row.get("tablespace_name") == undo_tablespace
    ]
    summary = _summarize_extent_rows(current_rows)

    ssolderrcnt = sum(_to_int(row.get("ssolderrcnt")) for row in history)
    nospaceerrcnt = sum(_to_int(row.get("nospaceerrcnt")) for row in history)
    unxpstealcnt = sum(_to_int(row.get("unxpstealcnt")) for row in history)
    maxquerylen = max([_to_int(row.get("maxquerylen")) for row in history], default=0)

    risk_level, suggestions = _build_risk_summary(
        summary=summary,
        undo_retention=undo_retention,
        ssolderrcnt=ssolderrcnt,
        nospaceerrcnt=nospaceerrcnt,
        unxpstealcnt=unxpstealcnt,
        maxquerylen=maxquerylen,
    )

    return {
        "undo_tablespace": undo_tablespace,
        "undo_retention": undo_retention,
        "tuned_undoretention": tuned_retention,
        "total_mb": summary["total_mb"],
        "active_mb": summary["active_mb"],
        "unexpired_mb": summary["unexpired_mb"],
        "expired_mb": summary["expired_mb"],
        "free_mb": summary["free_mb"],
        "active_pct": summary["active_pct"],
        "unexpired_pct": summary["unexpired_pct"],
        "expired_pct": summary["expired_pct"],
        "free_pct": summary["free_pct"],
        "ssolderrcnt": ssolderrcnt,
        "nospaceerrcnt": nospaceerrcnt,
        "unxpstealcnt": unxpstealcnt,
        "maxquerylen": maxquerylen,
        "risk_level": risk_level,
        "suggestions": suggestions,
        "parameters": params,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def get_undo_parameters(instance: DBInstance) -> list[dict]:
    sql = """
        SELECT name, value, display_value, isdefault, issys_modifiable
        FROM v$parameter
        WHERE name IN ('undo_management', 'undo_tablespace', 'undo_retention')
        ORDER BY name
    """
    return _query(instance, sql)


def get_undo_extents(instance: DBInstance) -> dict:
    logger.info("Oracle UNDO extents | instance=%s", instance.instance_name)
    extent_sql = """
        SELECT tablespace_name,
               status,
               COUNT(*) AS extent_count,
               ROUND(SUM(bytes) / 1024 / 1024, 2) AS size_mb
        FROM dba_undo_extents
        GROUP BY tablespace_name, status
        ORDER BY tablespace_name, status
    """
    total_sql = """
        SELECT tablespace_name,
               ROUND(SUM(bytes) / 1024 / 1024, 2) AS total_mb
        FROM dba_data_files
        WHERE tablespace_name IN (
            SELECT tablespace_name FROM dba_tablespaces WHERE contents = 'UNDO'
        )
        GROUP BY tablespace_name
    """
    free_sql = """
        SELECT tablespace_name,
               ROUND(SUM(bytes) / 1024 / 1024, 2) AS free_mb
        FROM dba_free_space
        WHERE tablespace_name IN (
            SELECT tablespace_name FROM dba_tablespaces WHERE contents = 'UNDO'
        )
        GROUP BY tablespace_name
    """

    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute(extent_sql)
            extents = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            cursor.execute(total_sql)
            totals = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            cursor.execute(free_sql)
            free_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        raise RuntimeError(
            "UNDO 空间明细查询失败，请确认当前账号具备 dba_undo_extents、dba_data_files、"
            "dba_free_space、dba_tablespaces 查询权限。原始错误：%s" % exc
        ) from exc

    total_map = {row["tablespace_name"]: float(row.get("total_mb") or 0) for row in totals}
    free_map = {row["tablespace_name"]: float(row.get("free_mb") or 0) for row in free_rows}
    by_tablespace: dict[str, dict] = {}

    for row in extents:
        tablespace_name = row.get("tablespace_name")
        if not tablespace_name:
            continue
        status = (row.get("status") or "").upper()
        size_mb = float(row.get("size_mb") or 0)
        total_mb = total_map.get(tablespace_name, 0)
        row["size_mb"] = round(size_mb, 2)
        row["total_mb"] = round(total_mb, 2)
        row["pct"] = round(size_mb / total_mb * 100, 2) if total_mb else 0
        by_tablespace.setdefault(
            tablespace_name,
            {
                "tablespace_name": tablespace_name,
                "total_mb": round(total_mb, 2),
                "active_mb": 0,
                "unexpired_mb": 0,
                "expired_mb": 0,
                "free_mb": round(free_map.get(tablespace_name, 0), 2),
            },
        )
        if status == "ACTIVE":
            by_tablespace[tablespace_name]["active_mb"] += size_mb
        elif status == "UNEXPIRED":
            by_tablespace[tablespace_name]["unexpired_mb"] += size_mb
        elif status == "EXPIRED":
            by_tablespace[tablespace_name]["expired_mb"] += size_mb

    for tablespace_name, total_mb in total_map.items():
        by_tablespace.setdefault(
            tablespace_name,
            {
                "tablespace_name": tablespace_name,
                "total_mb": round(total_mb, 2),
                "active_mb": 0,
                "unexpired_mb": 0,
                "expired_mb": 0,
                "free_mb": round(free_map.get(tablespace_name, 0), 2),
            },
        )

    summary = []
    for item in by_tablespace.values():
        total_mb = float(item.get("total_mb") or 0)
        for key in ["active_mb", "unexpired_mb", "expired_mb", "free_mb"]:
            item[key] = round(float(item.get(key) or 0), 2)
        item["active_pct"] = _pct(item["active_mb"], total_mb)
        item["unexpired_pct"] = _pct(item["unexpired_mb"], total_mb)
        item["expired_pct"] = _pct(item["expired_mb"], total_mb)
        item["free_pct"] = _pct(item["free_mb"], total_mb)
        summary.append(item)

    return {
        "items": extents,
        "summary": sorted(summary, key=lambda row: row["tablespace_name"]),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def get_undo_transactions(instance: DBInstance) -> dict:
    logger.info("Oracle UNDO active transactions | instance=%s", instance.instance_name)
    sql = """
        SELECT s.sid,
               s.serial# AS serial_num,
               s.username,
               s.machine,
               s.program,
               s.status,
               t.start_time,
               t.used_ublk,
               ROUND(t.used_ublk * TO_NUMBER(p.value) / 1024 / 1024, 2) AS undo_mb,
               s.sql_id
        FROM v$transaction t
        JOIN v$session s ON t.ses_addr = s.saddr
        CROSS JOIN (
            SELECT value FROM v$parameter WHERE name = 'db_block_size'
        ) p
        ORDER BY t.used_ublk DESC
    """
    return {"items": _query(instance, sql), "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds")}


def get_undo_history(instance: DBInstance) -> list[dict]:
    logger.info("Oracle UNDO history | instance=%s", instance.instance_name)
    sql = """
        SELECT *
        FROM (
            SELECT begin_time,
                   end_time,
                   undoblks,
                   txncount,
                   maxquerylen,
                   tuned_undoretention,
                   ssolderrcnt,
                   nospaceerrcnt,
                   unxpstealcnt,
                   expstealcnt
            FROM v$undostat
            ORDER BY begin_time DESC
        )
        WHERE ROWNUM <= 48
    """
    return _query(instance, sql)


def build_undo_retention_plan(retention_seconds: int, scope: str = "BOTH", rac_all_instances: bool = True) -> dict:
    scope = (scope or "BOTH").upper()
    if scope not in {"MEMORY", "SPFILE", "BOTH"}:
        raise ValueError("scope 只能是 MEMORY、SPFILE 或 BOTH")
    if retention_seconds < 60:
        raise ValueError("retention_seconds 不能小于 60 秒")

    sid_clause = " SID='*'" if rac_all_instances else ""
    sql = f"ALTER SYSTEM SET undo_retention = {int(retention_seconds)} SCOPE={scope}{sid_clause};"
    return {
        "status": "plan_only",
        "message": "已生成 UNDO_RETENTION 修改 SQL，请在维护窗口人工确认后执行。",
        "sql": sql,
        "warnings": [
            "该操作只生成 SQL，不会直接修改数据库参数。",
            "调整前请确认 UNDO 表空间容量、长查询窗口和闪回查询需求。",
        ],
    }


def _query(instance: DBInstance, sql: str, params: list | None = None) -> list[dict]:
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or [])
            return safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        raise RuntimeError(
            "UNDO 信息查询失败，请确认当前账号具备 v$parameter、v$undostat、v$transaction、v$session、"
            "dba_undo_extents、dba_data_files、dba_free_space 查询权限。原始错误：%s" % exc
        ) from exc


def _summarize_extent_rows(rows: list[dict]) -> dict:
    total_mb = max([float(row.get("total_mb") or 0) for row in rows], default=0)
    active_mb = sum(float(row.get("size_mb") or 0) for row in rows if (row.get("status") or "").upper() == "ACTIVE")
    unexpired_mb = sum(float(row.get("size_mb") or 0) for row in rows if (row.get("status") or "").upper() == "UNEXPIRED")
    expired_mb = sum(float(row.get("size_mb") or 0) for row in rows if (row.get("status") or "").upper() == "EXPIRED")
    free_mb = max(total_mb - active_mb - unexpired_mb - expired_mb, 0)
    return {
        "total_mb": round(total_mb, 2),
        "active_mb": round(active_mb, 2),
        "unexpired_mb": round(unexpired_mb, 2),
        "expired_mb": round(expired_mb, 2),
        "free_mb": round(free_mb, 2),
        "active_pct": _pct(active_mb, total_mb),
        "unexpired_pct": _pct(unexpired_mb, total_mb),
        "expired_pct": _pct(expired_mb, total_mb),
        "free_pct": _pct(free_mb, total_mb),
    }


def _build_risk_summary(summary: dict, undo_retention: int, ssolderrcnt: int, nospaceerrcnt: int,
                        unxpstealcnt: int, maxquerylen: int) -> tuple[str, list[str]]:
    risk_level = "normal"
    suggestions = []
    if ssolderrcnt > 0:
        risk_level = "critical"
        suggestions.append(f"最近 v$undostat 中出现 {ssolderrcnt} 次 ORA-01555，请检查长查询与 UNDO 保留窗口。")
    if nospaceerrcnt > 0:
        risk_level = "critical"
        suggestions.append(f"最近出现 {nospaceerrcnt} 次 UNDO 空间不足，建议优先评估 UNDO 表空间扩容。")
    if unxpstealcnt > 0:
        risk_level = "warning" if risk_level == "normal" else risk_level
        suggestions.append("存在未过期 UNDO 被抢占记录，说明 UNDO 空间或保留时间配置偏紧。")
    if summary.get("active_pct", 0) > 70:
        risk_level = "warning" if risk_level == "normal" else risk_level
        suggestions.append("ACTIVE UNDO 占比超过 70%，请检查活跃事务页中的大事务。")
    if undo_retention and maxquerylen > undo_retention:
        risk_level = "warning" if risk_level == "normal" else risk_level
        suggestions.append("最长查询时间超过 undo_retention，长查询或闪回查询可能失败。")
    if not suggestions:
        suggestions.append("当前 UNDO 空间与保留窗口未发现明显风险。")
        suggestions.append("如需增强数据恢复助手的闪回查询窗口，可在保留时间调整页生成 UNDO_RETENTION 修改 SQL。")
    return risk_level, suggestions


def _pct(value: float, total: float) -> float:
    return round(value / total * 100, 2) if total else 0


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
