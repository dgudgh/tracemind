import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.parameters")


def get_parameters(instance: DBInstance, keyword: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    logger.info("查询参数 | instance=%s | keyword=%s", instance.instance_name, keyword)
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        base_sql = "SELECT name, value, description, isdefault, isses_modifiable, issys_modifiable, isinstance_modifiable FROM v$parameter"
        count_sql = "SELECT COUNT(*) AS cnt FROM v$parameter"
        params = []
        if keyword:
            safe_kw = keyword.replace("'", "''").replace("%", "").replace("_", "")
            base_sql += " WHERE LOWER(name) LIKE :kw"
            count_sql += " WHERE LOWER(name) LIKE :kw"
            params = [{"kw": f"%{safe_kw.lower()}%"}]

        base_sql += " ORDER BY name"

        if params:
            cursor.execute(f"{count_sql}", params[0])
        else:
            cursor.execute(count_sql)
        total = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))[0].get("cnt", 0)

        offset = (page - 1) * page_size
        if params:
            cursor.execute(f"{base_sql} OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY", params[0])
        else:
            cursor.execute(f"{base_sql} OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY")
        items = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_sga_parameters(instance: DBInstance) -> dict:
    logger.info("查询 SGA 参数 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, [
        "sga_target", "sga_max_size", "sga_min_size",
        "shared_pool_size", "db_cache_size", "log_buffer",
        "large_pool_size", "java_pool_size", "streams_pool_size",
        "shared_io_pool_size", "memory_target", "memory_max_target",
    ])
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM v$sgainfo")
            params["sga_info"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            params["sga_info"] = []
    return params


def get_pga_parameters(instance: DBInstance) -> dict:
    logger.info("查询 PGA 参数 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, [
        "pga_aggregate_target", "pga_aggregate_limit",
        "workarea_size_policy", "sort_area_size", "hash_area_size",
    ])
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT name, value, unit FROM v$pgastat")
            params["pga_stat"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            params["pga_stat"] = []
    return params


def get_process_parameters(instance: DBInstance) -> dict:
    logger.info("查询进程参数 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, ["processes", "sessions", "transactions"])
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) AS current_processes FROM v$process")
            params["current_processes"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))[0].get("current_processes", 0)
        except Exception:
            params["current_processes"] = 0
    return params


def get_session_parameters(instance: DBInstance) -> dict:
    logger.info("查询会话参数 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, ["sessions", "processes", "license_max_sessions", "license_sessions_warning"])
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) AS current_sessions FROM v$session")
            params["current_sessions"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))[0].get("current_sessions", 0)
            cursor.execute("SELECT status, COUNT(*) AS cnt FROM v$session GROUP BY status")
            params["session_by_status"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            params["current_sessions"] = 0
            params["session_by_status"] = []
    return params


def get_password_policy(instance: DBInstance) -> dict:
    logger.info("查询密码策略 | instance=%s", instance.instance_name)
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT profile, resource_name, resource_type, limit "
            "FROM dba_profiles WHERE resource_type = 'PASSWORD' ORDER BY profile, resource_name"
        )
        profiles = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    return {"profiles": profiles}


def get_audit_config(instance: DBInstance) -> dict:
    logger.info("查询审计配置 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, [
        "audit_trail", "audit_sys_operations", "audit_file_dest",
        "unified_audit_sga_queue_size", "enable_ddl_logging",
    ])
    return params


def get_nls_parameters(instance: DBInstance) -> dict:
    logger.info("查询 NLS 参数 | instance=%s", instance.instance_name)
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT parameter, value FROM nls_database_parameters ORDER BY parameter")
        db_params = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        try:
            cursor.execute("SELECT parameter, value FROM nls_instance_parameters ORDER BY parameter")
            inst_params = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            inst_params = []
    return {"database_parameters": db_params, "instance_parameters": inst_params}


def get_dblinks(instance: DBInstance) -> dict:
    logger.info("查询 DB Link | instance=%s", instance.instance_name)
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT owner, db_link, username, host, created FROM dba_db_links ORDER BY owner, db_link")
            links = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            try:
                cursor.execute("SELECT owner, db_link, username, host, created FROM all_db_links ORDER BY owner, db_link")
                links = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            except Exception as exc:
                logger.warning("DB Link 查询失败 | error=%s", exc)
                links = []
    return {"links": links}


def get_datafile_parameters(instance: DBInstance) -> dict:
    logger.info("查询数据文件参数 | instance=%s", instance.instance_name)
    params = _query_named_params(instance, [
        "db_files", "db_create_file_dest", "db_create_online_log_dest_1",
        "db_create_online_log_dest_2", "db_recovery_file_dest",
        "db_recovery_file_dest_size", "db_block_size",
    ])
    return params


def get_supplemental_log(instance: DBInstance) -> dict:
    logger.info("查询补充日志 | instance=%s", instance.instance_name)
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        result = {}
        try:
            cursor.execute("SELECT supplemental_log_data_min, supplemental_log_data_pk, supplemental_log_data_ui, supplemental_log_data_fk, supplemental_log_data_all FROM v$database")
            rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            result["supplemental_log"] = rows[0] if rows else {}
        except Exception:
            result["supplemental_log"] = {}
    return result


def check_ransomware(instance: DBInstance) -> dict:
    logger.info("勒索病毒检查 | instance=%s", instance.instance_name)
    findings = []
    # 五个检查项各自是独立的只读查询，失败必须被记下来。
    # 原实现逐个 `except Exception: pass`，于是"没有发现"与"没能检查"合并成同一结果：
    # 数据库账号缺少 dba_* 权限时五个查询全部失败，输出却是
    # "勒索病毒检查通过 / 未发现明显勒索病毒痕迹"——安全类检查的假阴性。
    # 连接本身失败仍然向上抛出（oracle_connection 会抛），这里处理的是"连上了但查不动"。
    unavailable: list[tuple[str, str]] = []
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT COUNT(*) AS cnt FROM dba_tables WHERE last_analyzed IS NOT NULL AND last_analyzed > SYSDATE - 1 AND owner NOT IN ('SYS','SYSTEM')")
            suspicious_tables = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            cnt = suspicious_tables[0].get("cnt", 0) if suspicious_tables else 0
            if cnt > 50:
                findings.append({"check": "大量表近期被分析", "count": cnt, "risk": "warning", "detail": f"近1天有 {cnt} 张非系统表被分析，可能存在异常批量操作"})
        except Exception as exc:
            unavailable.append(("大量表近期被分析", str(exc)))

        try:
            cursor.execute("SELECT COUNT(*) AS cnt FROM dba_objects WHERE created > SYSDATE - 1 AND object_type = 'TABLE' AND owner NOT IN ('SYS','SYSTEM')")
            new_tables = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            cnt = new_tables[0].get("cnt", 0) if new_tables else 0
            if cnt > 20:
                findings.append({"check": "大量新表创建", "count": cnt, "risk": "warning", "detail": f"近1天创建了 {cnt} 张新表，请确认是否为正常业务操作"})
        except Exception as exc:
            unavailable.append(("大量新表创建", str(exc)))

        try:
            cursor.execute("SELECT COUNT(*) AS cnt FROM dba_objects WHERE status = 'INVALID' AND owner NOT IN ('SYS','SYSTEM')")
            invalid = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            cnt = invalid[0].get("cnt", 0) if invalid else 0
            if cnt > 0:
                findings.append({"check": "无效对象", "count": cnt, "risk": "info", "detail": f"存在 {cnt} 个无效对象，需确认是否被恶意篡改"})
        except Exception as exc:
            unavailable.append(("无效对象", str(exc)))

        try:
            cursor.execute("SELECT username, account_status, created FROM dba_users WHERE account_status = 'OPEN' AND created > SYSDATE - 7")
            new_users = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            if new_users:
                findings.append({"check": "近期新建活跃用户", "count": len(new_users), "risk": "warning", "detail": f"近7天新建且活跃的用户: {', '.join(u.get('username','') for u in new_users)}", "users": new_users})
        except Exception as exc:
            unavailable.append(("近期新建活跃用户", str(exc)))

        try:
            cursor.execute("SELECT tablespace_name, used_pct FROM (SELECT tablespace_name, ROUND((used_bytes/total_bytes)*100,2) AS used_pct FROM (SELECT tablespace_name, SUM(bytes) AS total_bytes FROM dba_data_files GROUP BY tablespace_name) t, (SELECT tablespace_name, SUM(bytes) AS used_bytes FROM dba_segments GROUP BY tablespace_name) s WHERE t.tablespace_name = s.tablespace_name(+)) WHERE used_pct > 95")
            full_ts = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            if full_ts:
                findings.append({"check": "表空间使用率>95%", "count": len(full_ts), "risk": "critical", "detail": "以下表空间使用率超过95%，可能是勒索软件加密导致数据暴涨", "tablespaces": full_ts})
        except Exception as exc:
            unavailable.append(("表空间使用率>95%", str(exc)))

    if unavailable:
        labels = "、".join(label for label, _ in unavailable)
        for label, error in unavailable:
            logger.warning(
                "勒索病毒检查项未能执行 | instance=%s | check=%s | error=%s",
                instance.instance_name, label, error,
            )
        findings.append({
            "check": "勒索病毒检查存在未执行项",
            "count": len(unavailable),
            "risk": "warning",
            "detail": (
                f"{len(unavailable)} 项检查未能执行（通常为账号缺少 dba_* 权限或查询被中断）：{labels}。"
                f"未执行的检查不构成无风险结论。首个错误：{unavailable[0][1]}"
            ),
            "unavailable_checks": [{"check": label, "error": error} for label, error in unavailable],
        })

    if not findings:
        findings.append({"check": "勒索病毒检查通过", "count": 0, "risk": "normal", "detail": "未发现明显勒索病毒痕迹"})

    return {"instance": instance.instance_name, "findings": findings}


def generate_alter_system_sql(instance: DBInstance, param_name: str, value: str, scope: str = "both") -> dict:
    scope = scope.lower()
    if scope not in ("memory", "spfile", "both"):
        scope = "both"
    sql = f"ALTER SYSTEM SET {param_name} = {value} SCOPE = {scope.upper()};"
    logger.info("生成参数修改 SQL | instance=%s | param=%s | value=%s | scope=%s", instance.instance_name, param_name, value, scope)
    return {
        "status": "plan_only",
        "message": "已生成参数修改 SQL。请在维护窗口通过 sqlplus 手工执行。",
        "sql": sql,
        "param_name": param_name,
        "value": value,
        "scope": scope,
        "warnings": [
            "修改系统参数属于 DBA 变更操作，请在维护窗口执行。",
            "SCOPE=SPFILE 修改需要重启实例才能生效。",
            "SCOPE=MEMORY 仅修改内存，重启后失效。",
            "SCOPE=BOTH 同时修改内存和 SPFILE（默认）。",
        ],
    }


def _query_named_params(instance: DBInstance, names: list[str]) -> dict:
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        placeholders = ", ".join([f":p{i}" for i in range(len(names))])
        cursor.execute(
            f"SELECT name, value, description, isdefault, issys_modifiable "
            f"FROM v$parameter WHERE name IN ({placeholders}) ORDER BY name",
            {f"p{i}": n for i, n in enumerate(names)}
        )
        rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    return {row["name"]: row for row in rows}
