import logging
import re

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance
from app.services import oracle_index

logger = logging.getLogger("app.services.oracle_table_usage")

_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_SYSTEM_OWNERS = "'SYS','SYSTEM','SYSMAN','DBSNMP','OUTLN','XDB','WMSYS'"


def list_hot_tables(
    instance: DBInstance,
    days: int = 30,
    source: str = "AWR",
    owner: str | None = None,
    table_name: str | None = None,
) -> dict:
    owner = _optional_identifier(owner)
    table_name = _optional_identifier(table_name)
    days = _days(days)
    source = _source(source)

    def awr():
        params = {"days": days}
        filters = [f"p.object_owner NOT IN ({_SYSTEM_OWNERS})", "p.object_name IS NOT NULL", "p.object_type LIKE 'TABLE%'"]
        if owner:
            filters.append("p.object_owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("p.object_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH plan_obj AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.object_owner, p.object_name,
                       p.operation, p.options
                FROM dba_hist_sql_plan p
                WHERE {' AND '.join(filters)}
            ),
            sql_stat AS (
                SELECT s.sql_id, s.plan_hash_value,
                       SUM(s.executions_delta) AS executions,
                       ROUND(SUM(s.elapsed_time_delta) / 1000000, 2) AS elapsed_sec,
                       SUM(s.buffer_gets_delta) AS buffer_gets,
                       SUM(s.disk_reads_delta) AS disk_reads,
                       SUM(s.rows_processed_delta) AS rows_processed
                FROM dba_hist_sqlstat s
                JOIN dba_hist_snapshot sn
                  ON s.snap_id = sn.snap_id
                 AND s.dbid = sn.dbid
                 AND s.instance_number = sn.instance_number
                WHERE sn.begin_interval_time >= SYSDATE - :days
                GROUP BY s.sql_id, s.plan_hash_value
            ),
            table_size AS (
                SELECT owner, segment_name AS table_name, ROUND(SUM(bytes) / 1024 / 1024, 2) AS table_size_mb
                FROM dba_segments
                WHERE segment_type LIKE 'TABLE%'
                GROUP BY owner, segment_name
            )
            SELECT p.object_owner AS owner,
                   p.object_name AS table_name,
                   COUNT(DISTINCT p.sql_id) AS sql_count,
                   NVL(SUM(st.executions), 0) AS executions,
                   ROUND(NVL(SUM(st.executions), 0) / :days, 2) AS avg_exec_per_day,
                   NVL(SUM(st.elapsed_sec), 0) AS elapsed_sec,
                   NVL(SUM(st.buffer_gets), 0) AS buffer_gets,
                   NVL(SUM(st.disk_reads), 0) AS disk_reads,
                   NVL(SUM(st.rows_processed), 0) AS rows_processed,
                   SUM(CASE WHEN p.operation = 'TABLE ACCESS' AND p.options LIKE '%FULL%' THEN 1 ELSE 0 END) AS full_scan_sql_count,
                   SUM(CASE WHEN p.operation LIKE 'INDEX%' OR p.options LIKE '%INDEX%' THEN 1 ELSE 0 END) AS index_access_sql_count,
                   NVL(ts.table_size_mb, 0) AS table_size_mb,
                   t.num_rows,
                   CASE
                     WHEN NVL(SUM(st.executions), 0) / :days > 100000 OR NVL(SUM(st.buffer_gets), 0) / :days > 100000000 THEN 'critical'
                     WHEN COUNT(DISTINCT p.sql_id) > 20 OR NVL(ts.table_size_mb, 0) > 51200 OR SUM(CASE WHEN p.operation = 'TABLE ACCESS' AND p.options LIKE '%FULL%' THEN 1 ELSE 0 END) > 5 THEN 'warning'
                     ELSE 'normal'
                   END AS risk_level
            FROM plan_obj p
            JOIN sql_stat st ON p.sql_id = st.sql_id AND p.plan_hash_value = st.plan_hash_value
            LEFT JOIN dba_tables t ON t.owner = p.object_owner AND t.table_name = p.object_name
            LEFT JOIN table_size ts ON ts.owner = p.object_owner AND ts.table_name = p.object_name
            GROUP BY p.object_owner, p.object_name, ts.table_size_mb, t.num_rows
            ORDER BY executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, params)

    def current():
        params = {}
        filters = [f"p.object_owner NOT IN ({_SYSTEM_OWNERS})", "p.object_name IS NOT NULL"]
        if owner:
            filters.append("p.object_owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("p.object_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH plan_obj AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.object_owner, p.object_name,
                       p.operation, p.options
                FROM v$sql_plan p
                WHERE {' AND '.join(filters)}
                  AND p.operation = 'TABLE ACCESS'
            ),
            sql_stat AS (
                SELECT sql_id, plan_hash_value,
                       SUM(executions) AS executions,
                       ROUND(SUM(elapsed_time) / 1000000, 2) AS elapsed_sec,
                       SUM(buffer_gets) AS buffer_gets,
                       SUM(disk_reads) AS disk_reads,
                       SUM(rows_processed) AS rows_processed
                FROM v$sql
                GROUP BY sql_id, plan_hash_value
            ),
            table_size AS (
                SELECT owner, segment_name AS table_name, ROUND(SUM(bytes) / 1024 / 1024, 2) AS table_size_mb
                FROM dba_segments
                WHERE segment_type LIKE 'TABLE%'
                GROUP BY owner, segment_name
            )
            SELECT p.object_owner AS owner,
                   p.object_name AS table_name,
                   COUNT(DISTINCT p.sql_id) AS sql_count,
                   NVL(SUM(st.executions), 0) AS executions,
                   ROUND(NVL(SUM(st.executions), 0) / 1, 2) AS avg_exec_per_day,
                   NVL(SUM(st.elapsed_sec), 0) AS elapsed_sec,
                   NVL(SUM(st.buffer_gets), 0) AS buffer_gets,
                   NVL(SUM(st.disk_reads), 0) AS disk_reads,
                   NVL(SUM(st.rows_processed), 0) AS rows_processed,
                   SUM(CASE WHEN p.options LIKE '%FULL%' THEN 1 ELSE 0 END) AS full_scan_sql_count,
                   0 AS index_access_sql_count,
                   NVL(ts.table_size_mb, 0) AS table_size_mb,
                   t.num_rows,
                   CASE
                     WHEN NVL(SUM(st.executions), 0) > 100000 OR NVL(SUM(st.buffer_gets), 0) > 100000000 THEN 'critical'
                     WHEN COUNT(DISTINCT p.sql_id) > 20 OR NVL(ts.table_size_mb, 0) > 51200 OR SUM(CASE WHEN p.options LIKE '%FULL%' THEN 1 ELSE 0 END) > 5 THEN 'warning'
                     ELSE 'normal'
                   END AS risk_level
            FROM plan_obj p
            LEFT JOIN sql_stat st ON p.sql_id = st.sql_id AND p.plan_hash_value = st.plan_hash_value
            LEFT JOIN dba_tables t ON t.owner = p.object_owner AND t.table_name = p.object_name
            LEFT JOIN table_size ts ON ts.owner = p.object_owner AND ts.table_name = p.object_name
            GROUP BY p.object_owner, p.object_name, ts.table_size_mb, t.num_rows
            ORDER BY executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, params)

    data = _with_fallback(source, awr, current)
    return {"source": data["source"], "days": days, "items": data["items"], "warnings": data["warnings"]}


def list_table_sqls(instance: DBInstance, owner: str, table_name: str, days: int = 30, source: str = "AWR") -> dict:
    owner = _identifier(owner, "owner")
    table_name = _identifier(table_name, "table_name")
    days = _days(days)
    source = _source(source)

    def awr():
        sql = """
            WITH table_sql AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.operation, p.options
                FROM dba_hist_sql_plan p
                WHERE p.object_owner = :owner AND p.object_name = :table_name
            ),
            sql_stat AS (
                SELECT s.sql_id, s.plan_hash_value,
                       SUM(s.executions_delta) AS executions,
                       ROUND(SUM(s.elapsed_time_delta) / 1000000, 2) AS elapsed_sec,
                       SUM(s.buffer_gets_delta) AS buffer_gets,
                       SUM(s.disk_reads_delta) AS disk_reads,
                       SUM(s.rows_processed_delta) AS rows_processed,
                       MAX(sn.end_interval_time) AS last_active_time
                FROM dba_hist_sqlstat s
                JOIN dba_hist_snapshot sn
                  ON s.snap_id = sn.snap_id AND s.dbid = sn.dbid AND s.instance_number = sn.instance_number
                WHERE sn.begin_interval_time >= SYSDATE - :days
                GROUP BY s.sql_id, s.plan_hash_value
            )
            SELECT t.sql_id,
                   t.plan_hash_value,
                   t.operation,
                   t.options,
                   NVL(st.executions, 0) AS executions,
                   ROUND(st.elapsed_sec / NULLIF(st.executions, 0), 4) AS avg_elapsed_sec,
                   NVL(st.elapsed_sec, 0) AS elapsed_sec,
                   NVL(st.buffer_gets, 0) AS buffer_gets,
                   NVL(st.disk_reads, 0) AS disk_reads,
                   NVL(st.rows_processed, 0) AS rows_processed,
                   st.last_active_time,
                   SUBSTR(txt.sql_text, 1, 1000) AS sql_text
            FROM table_sql t
            LEFT JOIN sql_stat st ON t.sql_id = st.sql_id AND t.plan_hash_value = st.plan_hash_value
            LEFT JOIN dba_hist_sqltext txt ON t.sql_id = txt.sql_id
            ORDER BY executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, {"owner": owner, "table_name": table_name, "days": days})

    def current():
        sql = """
            WITH table_sql AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.operation, p.options
                FROM v$sql_plan p
                WHERE p.object_owner = :owner AND p.object_name = :table_name
            )
            SELECT t.sql_id,
                   t.plan_hash_value,
                   t.operation,
                   t.options,
                   NVL(s.executions, 0) AS executions,
                   ROUND((s.elapsed_time / 1000000) / NULLIF(s.executions, 0), 4) AS avg_elapsed_sec,
                   ROUND(NVL(s.elapsed_time, 0) / 1000000, 2) AS elapsed_sec,
                   NVL(s.buffer_gets, 0) AS buffer_gets,
                   NVL(s.disk_reads, 0) AS disk_reads,
                   NVL(s.rows_processed, 0) AS rows_processed,
                   s.last_active_time,
                   SUBSTR(s.sql_fulltext, 1, 1000) AS sql_text
            FROM table_sql t
            LEFT JOIN v$sql s ON t.sql_id = s.sql_id AND t.plan_hash_value = s.plan_hash_value
            ORDER BY executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, {"owner": owner, "table_name": table_name})

    data = _with_fallback(source, awr, current)
    return {"source": data["source"], "days": days, "owner": owner, "table_name": table_name, "items": data["items"], "warnings": data["warnings"]}


def list_full_scan_sqls(
    instance: DBInstance,
    owner: str | None = None,
    table_name: str | None = None,
    days: int = 30,
    source: str = "AWR",
) -> dict:
    rows = list_table_sqls_for_scan(instance, owner, table_name, days, source, full_scan_only=True)
    return rows


def list_table_sqls_for_scan(
    instance: DBInstance,
    owner: str | None,
    table_name: str | None,
    days: int,
    source: str,
    full_scan_only: bool = False,
) -> dict:
    owner = _optional_identifier(owner)
    table_name = _optional_identifier(table_name)
    days = _days(days)
    source = _source(source)

    def awr():
        params = {"days": days}
        filters = [f"p.object_owner NOT IN ({_SYSTEM_OWNERS})", "p.object_name IS NOT NULL"]
        if full_scan_only:
            filters.append("p.operation = 'TABLE ACCESS'")
            filters.append("p.options LIKE '%FULL%'")
        if owner:
            filters.append("p.object_owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("p.object_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH table_sql AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.object_owner, p.object_name,
                       p.operation, p.options
                FROM dba_hist_sql_plan p
                WHERE {' AND '.join(filters)}
            ),
            sql_stat AS (
                SELECT s.sql_id, s.plan_hash_value,
                       SUM(s.executions_delta) AS executions,
                       ROUND(SUM(s.elapsed_time_delta) / 1000000, 2) AS elapsed_sec,
                       SUM(s.buffer_gets_delta) AS buffer_gets,
                       SUM(s.disk_reads_delta) AS disk_reads,
                       SUM(s.rows_processed_delta) AS rows_processed,
                       MAX(sn.end_interval_time) AS last_active_time
                FROM dba_hist_sqlstat s
                JOIN dba_hist_snapshot sn
                  ON s.snap_id = sn.snap_id AND s.dbid = sn.dbid AND s.instance_number = sn.instance_number
                WHERE sn.begin_interval_time >= SYSDATE - :days
                GROUP BY s.sql_id, s.plan_hash_value
            )
            SELECT t.object_owner AS owner,
                   t.object_name AS table_name,
                   t.sql_id,
                   t.plan_hash_value,
                   t.operation,
                   t.options,
                   NVL(st.executions, 0) AS executions,
                   NVL(st.elapsed_sec, 0) AS elapsed_sec,
                   NVL(st.buffer_gets, 0) AS buffer_gets,
                   NVL(st.disk_reads, 0) AS disk_reads,
                   NVL(st.rows_processed, 0) AS rows_processed,
                   st.last_active_time,
                   SUBSTR(txt.sql_text, 1, 1000) AS sql_text,
                   '建议评估谓词字段、现有索引和统计信息；高频大表全表扫描可进入索引创建向导。' AS suggestion
            FROM table_sql t
            LEFT JOIN sql_stat st ON t.sql_id = st.sql_id AND t.plan_hash_value = st.plan_hash_value
            LEFT JOIN dba_hist_sqltext txt ON t.sql_id = txt.sql_id
            ORDER BY buffer_gets DESC NULLS LAST, executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, params)

    def current():
        params = {}
        filters = [f"p.object_owner NOT IN ({_SYSTEM_OWNERS})", "p.object_name IS NOT NULL"]
        if full_scan_only:
            filters.append("p.operation = 'TABLE ACCESS'")
            filters.append("p.options LIKE '%FULL%'")
        if owner:
            filters.append("p.object_owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("p.object_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH table_sql AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value, p.object_owner, p.object_name,
                       p.operation, p.options
                FROM v$sql_plan p
                WHERE {' AND '.join(filters)}
            )
            SELECT t.object_owner AS owner,
                   t.object_name AS table_name,
                   t.sql_id,
                   t.plan_hash_value,
                   t.operation,
                   t.options,
                   NVL(s.executions, 0) AS executions,
                   ROUND(NVL(s.elapsed_time, 0) / 1000000, 2) AS elapsed_sec,
                   NVL(s.buffer_gets, 0) AS buffer_gets,
                   NVL(s.disk_reads, 0) AS disk_reads,
                   NVL(s.rows_processed, 0) AS rows_processed,
                   s.last_active_time,
                   SUBSTR(s.sql_fulltext, 1, 1000) AS sql_text,
                   '建议评估谓词字段、现有索引和统计信息；高频大表全表扫描可进入索引创建向导。' AS suggestion
            FROM table_sql t
            LEFT JOIN v$sql s ON t.sql_id = s.sql_id AND t.plan_hash_value = s.plan_hash_value
            ORDER BY buffer_gets DESC NULLS LAST, executions DESC NULLS LAST
            FETCH FIRST 200 ROWS ONLY
        """
        return _query(instance, sql, params)

    data = _with_fallback(source, awr, current)
    return {"source": data["source"], "days": days, "items": data["items"], "warnings": data["warnings"]}


def list_index_usage(
    instance: DBInstance,
    owner: str | None = None,
    table_name: str | None = None,
    days: int = 90,
    source: str = "AWR",
) -> dict:
    owner = _optional_identifier(owner)
    table_name = _optional_identifier(table_name)
    days = _days(days)
    source = _source(source)

    def awr():
        params = {"days": days}
        filters = [f"i.owner NOT IN ({_SYSTEM_OWNERS})"]
        if owner:
            filters.append("i.owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("i.table_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH index_plan AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value,
                       p.object_owner AS index_owner,
                       p.object_name AS index_name
                FROM dba_hist_sql_plan p
                WHERE p.operation LIKE 'INDEX%'
                  AND p.object_owner NOT IN ({_SYSTEM_OWNERS})
                  AND p.object_name IS NOT NULL
            ),
            sql_stat AS (
                SELECT s.sql_id, s.plan_hash_value,
                       SUM(s.executions_delta) AS executions,
                       ROUND(SUM(s.elapsed_time_delta) / 1000000, 2) AS elapsed_sec,
                       SUM(s.buffer_gets_delta) AS buffer_gets,
                       SUM(s.disk_reads_delta) AS disk_reads,
                       MAX(sn.end_interval_time) AS last_seen
                FROM dba_hist_sqlstat s
                JOIN dba_hist_snapshot sn
                  ON s.snap_id = sn.snap_id AND s.dbid = sn.dbid AND s.instance_number = sn.instance_number
                WHERE sn.begin_interval_time >= SYSDATE - :days
                GROUP BY s.sql_id, s.plan_hash_value
            ),
            idx_cols AS (
                SELECT index_owner, index_name,
                       LISTAGG(column_name, ',') WITHIN GROUP (ORDER BY column_position) AS columns_text
                FROM dba_ind_columns
                GROUP BY index_owner, index_name
            ),
            idx_size AS (
                SELECT owner, segment_name AS index_name, ROUND(SUM(bytes)/1024/1024, 2) AS index_size_mb
                FROM dba_segments
                WHERE segment_type LIKE 'INDEX%'
                GROUP BY owner, segment_name
            )
            SELECT i.owner,
                   i.table_name,
                   i.index_name,
                   i.index_type,
                   i.uniqueness,
                   c.columns_text,
                   COUNT(DISTINCT ip.sql_id) AS used_sql_count,
                   NVL(SUM(st.executions), 0) AS executions,
                   NVL(SUM(st.elapsed_sec), 0) AS elapsed_sec,
                   NVL(SUM(st.buffer_gets), 0) AS buffer_gets,
                   NVL(SUM(st.disk_reads), 0) AS disk_reads,
                   MAX(st.last_seen) AS last_seen,
                   NVL(sz.index_size_mb, 0) AS index_size_mb,
                   CASE WHEN cons.index_name IS NOT NULL THEN 1 ELSE 0 END AS is_constraint_index
            FROM dba_indexes i
            LEFT JOIN index_plan ip ON i.owner = ip.index_owner AND i.index_name = ip.index_name
            LEFT JOIN sql_stat st ON ip.sql_id = st.sql_id AND ip.plan_hash_value = st.plan_hash_value
            LEFT JOIN idx_cols c ON c.index_owner = i.owner AND c.index_name = i.index_name
            LEFT JOIN idx_size sz ON sz.owner = i.owner AND sz.index_name = i.index_name
            LEFT JOIN dba_constraints cons ON cons.owner = i.owner AND cons.index_name = i.index_name AND cons.constraint_type IN ('P','U')
            WHERE {' AND '.join(filters)}
            GROUP BY i.owner, i.table_name, i.index_name, i.index_type, i.uniqueness, c.columns_text, sz.index_size_mb, cons.index_name
            ORDER BY executions ASC, i.owner, i.table_name, i.index_name
            FETCH FIRST 300 ROWS ONLY
        """
        return _query(instance, sql, params)

    def current():
        params = {}
        filters = [f"i.owner NOT IN ({_SYSTEM_OWNERS})"]
        if owner:
            filters.append("i.owner = :owner")
            params["owner"] = owner
        if table_name:
            filters.append("i.table_name = :table_name")
            params["table_name"] = table_name
        sql = f"""
            WITH index_plan AS (
                SELECT DISTINCT p.sql_id, p.plan_hash_value,
                       p.object_owner AS index_owner,
                       p.object_name AS index_name
                FROM v$sql_plan p
                WHERE p.operation LIKE 'INDEX%'
                  AND p.object_owner NOT IN ({_SYSTEM_OWNERS})
                  AND p.object_name IS NOT NULL
            ),
            sql_stat AS (
                SELECT sql_id, plan_hash_value,
                       SUM(executions) AS executions,
                       ROUND(SUM(elapsed_time) / 1000000, 2) AS elapsed_sec,
                       SUM(buffer_gets) AS buffer_gets,
                       SUM(disk_reads) AS disk_reads,
                       MAX(last_active_time) AS last_seen
                FROM v$sql
                GROUP BY sql_id, plan_hash_value
            ),
            idx_cols AS (
                SELECT index_owner, index_name,
                       LISTAGG(column_name, ',') WITHIN GROUP (ORDER BY column_position) AS columns_text
                FROM dba_ind_columns
                GROUP BY index_owner, index_name
            )
            SELECT i.owner,
                   i.table_name,
                   i.index_name,
                   i.index_type,
                   i.uniqueness,
                   c.columns_text,
                   COUNT(DISTINCT ip.sql_id) AS used_sql_count,
                   NVL(SUM(st.executions), 0) AS executions,
                   NVL(SUM(st.elapsed_sec), 0) AS elapsed_sec,
                   NVL(SUM(st.buffer_gets), 0) AS buffer_gets,
                   NVL(SUM(st.disk_reads), 0) AS disk_reads,
                   MAX(st.last_seen) AS last_seen,
                   CASE WHEN cons.index_name IS NOT NULL THEN 1 ELSE 0 END AS is_constraint_index
            FROM dba_indexes i
            LEFT JOIN index_plan ip ON i.owner = ip.index_owner AND i.index_name = ip.index_name
            LEFT JOIN sql_stat st ON ip.sql_id = st.sql_id AND ip.plan_hash_value = st.plan_hash_value
            LEFT JOIN idx_cols c ON c.index_owner = i.owner AND c.index_name = i.index_name
            LEFT JOIN dba_constraints cons ON cons.owner = i.owner AND cons.index_name = i.index_name AND cons.constraint_type IN ('P','U')
            WHERE {' AND '.join(filters)}
            GROUP BY i.owner, i.table_name, i.index_name, i.index_type, i.uniqueness, c.columns_text, cons.index_name
            ORDER BY executions ASC, i.owner, i.table_name, i.index_name
            FETCH FIRST 300 ROWS ONLY
        """
        return _query(instance, sql, params)

    data = _with_fallback(source, awr, current)
    return {"source": data["source"], "days": days, "items": data["items"], "warnings": data["warnings"]}


def list_unused_indexes(instance: DBInstance, owner: str | None = None, table_name: str | None = None, days: int = 90, source: str = "AWR") -> dict:
    result = list_index_usage(instance, owner, table_name, days, source)
    items = []
    for row in result["items"]:
        used = int(row.get("used_sql_count") or 0)
        is_constraint = int(row.get("is_constraint_index") or 0) == 1
        if used == 0:
            row["advice_level"] = "warning" if not is_constraint else "info"
            row["suggestion"] = "疑似未使用，建议先改为 INVISIBLE 观察。" if not is_constraint else "约束索引，不建议删除。"
            items.append(row)
    result["items"] = items
    return result


def list_duplicate_indexes(instance: DBInstance, owner: str | None = None, table_name: str | None = None) -> dict:
    owner = _optional_identifier(owner)
    table_name = _optional_identifier(table_name)
    params = {}
    filters = [f"table_owner NOT IN ({_SYSTEM_OWNERS})"]
    if owner:
        filters.append("table_owner = :owner")
        params["owner"] = owner
    if table_name:
        filters.append("table_name = :table_name")
        params["table_name"] = table_name
    sql = f"""
        WITH idx_cols AS (
            SELECT index_owner,
                   index_name,
                   table_owner,
                   table_name,
                   LISTAGG(column_name, ',') WITHIN GROUP (ORDER BY column_position) AS columns_text,
                   COUNT(*) AS column_count
            FROM dba_ind_columns
            WHERE {' AND '.join(filters)}
            GROUP BY index_owner, index_name, table_owner, table_name
        ),
        cons AS (
            SELECT owner, index_name, constraint_type FROM dba_constraints WHERE constraint_type IN ('P','U')
        )
        SELECT a.table_owner,
               a.table_name,
               a.index_owner AS index_owner_1,
               a.index_name AS index_name_1,
               a.columns_text AS columns_1,
               b.index_owner AS index_owner_2,
               b.index_name AS index_name_2,
               b.columns_text AS columns_2,
               CASE
                 WHEN a.columns_text = b.columns_text THEN '完全重复'
                 WHEN b.columns_text LIKE a.columns_text || ',%' THEN '前缀覆盖'
                 WHEN a.columns_text LIKE b.columns_text || ',%' THEN '前缀覆盖'
                 ELSE '部分重叠'
               END AS relation_type,
               CASE WHEN c1.index_name IS NOT NULL OR c2.index_name IS NOT NULL THEN 1 ELSE 0 END AS has_constraint_index,
               CASE
                 WHEN c1.index_name IS NOT NULL OR c2.index_name IS NOT NULL THEN '涉及主键/唯一约束索引，只建议观察，不建议删除。'
                 WHEN a.columns_text = b.columns_text THEN '建议评估保留使用频率更高或更短的索引。'
                 ELSE '建议结合 SQL 使用频率评估是否合并。'
               END AS suggestion
        FROM idx_cols a
        JOIN idx_cols b
          ON a.table_owner = b.table_owner
         AND a.table_name = b.table_name
         AND a.index_name < b.index_name
        LEFT JOIN cons c1 ON c1.owner = a.index_owner AND c1.index_name = a.index_name
        LEFT JOIN cons c2 ON c2.owner = b.index_owner AND c2.index_name = b.index_name
        WHERE a.columns_text = b.columns_text
           OR b.columns_text LIKE a.columns_text || ',%'
           OR a.columns_text LIKE b.columns_text || ',%'
        ORDER BY a.table_owner, a.table_name
        FETCH FIRST 300 ROWS ONLY
    """
    return {"items": _query(instance, sql, params)}


def list_index_advice(instance: DBInstance, owner: str | None = None, table_name: str | None = None, days: int = 30, source: str = "AWR") -> dict:
    full_scan = list_table_sqls_for_scan(instance, owner, table_name, days, source, full_scan_only=True)
    unused = list_unused_indexes(instance, owner, table_name, 90, source)
    duplicate = list_duplicate_indexes(instance, owner, table_name)
    items = []
    for row in full_scan["items"][:50]:
        level = "critical" if int(row.get("executions") or 0) > 10000 or int(row.get("buffer_gets") or 0) > 10000000 else "warning"
        items.append({
            "advice_type": "missing_index_candidate",
            "advice_level": level,
            "owner": row.get("owner"),
            "table_name": row.get("table_name"),
            "sql_id": row.get("sql_id"),
            "evidence_summary": f"SQL {row.get('sql_id')} 存在 TABLE ACCESS FULL，执行 {row.get('executions') or 0} 次，逻辑读 {row.get('buffer_gets') or 0}。",
            "suggestion": "建议评估谓词字段、现有索引和统计信息；如查询频繁且选择性好，可进入索引创建向导生成 CREATE INDEX SQL。",
            "generated_sql": None,
        })
    for row in unused["items"][:50]:
        items.append({
            "advice_type": "unused_index_candidate",
            "advice_level": row.get("advice_level"),
            "owner": row.get("owner"),
            "table_name": row.get("table_name"),
            "index_name": row.get("index_name"),
            "evidence_summary": f"最近 {unused['days']} 天未在 {unused['source']} 执行计划中发现该索引。",
            "suggestion": row.get("suggestion"),
            "generated_sql": f"ALTER INDEX {row.get('owner')}.{row.get('index_name')} INVISIBLE;",
        })
    for row in duplicate["items"][:50]:
        items.append({
            "advice_type": "duplicate_index_candidate",
            "advice_level": "warning",
            "owner": row.get("table_owner"),
            "table_name": row.get("table_name"),
            "index_name": row.get("index_name_1"),
            "evidence_summary": f"{row.get('index_name_1')} ({row.get('columns_1')}) 与 {row.get('index_name_2')} ({row.get('columns_2')}) 关系：{row.get('relation_type')}。",
            "suggestion": row.get("suggestion"),
            "generated_sql": None,
        })
    return {
        "source": full_scan["source"],
        "days": days,
        "items": items,
        "warnings": full_scan.get("warnings", []) + unused.get("warnings", []),
    }


def collect_table_usage(instance: DBInstance, days: int = 30, source: str = "AWR") -> dict:
    hot = list_hot_tables(instance, days, source)
    return {
        "status": "collected",
        "source": hot["source"],
        "days": hot["days"],
        "message": "已完成表访问画像即时分析。本版本直接从 AWR/当前缓存读取，不落地写入本地画像表。",
        "hot_table_count": len(hot["items"]),
        "warnings": hot.get("warnings", []),
    }


def build_index_invisible_plan(payload: dict) -> dict:
    owner = _identifier(payload.get("owner"), "owner")
    index_name = _identifier(payload.get("index_name"), "index_name")
    return {
        "status": "plan_only",
        "message": "已生成索引 INVISIBLE 脚本。请先在测试库验证，并在生产低峰期观察执行计划变化。",
        "sql": f"ALTER INDEX {owner}.{index_name} INVISIBLE;",
        "warnings": [
            "不要直接删除疑似未使用索引，建议先设置 INVISIBLE 并观察一个业务周期。",
            "主键、唯一约束和业务强依赖索引不建议隐藏。",
        ],
    }


def build_drop_index_plan(payload: dict) -> dict:
    owner = _identifier(payload.get("owner"), "owner")
    index_name = _identifier(payload.get("index_name"), "index_name")
    return {
        "status": "plan_only",
        "message": "已生成删除索引脚本，仅供人工评审，不会自动执行。",
        "sql": f"DROP INDEX {owner}.{index_name};",
        "warnings": [
            "删除索引风险较高，请确认不是主键/唯一约束索引。",
            "建议先执行 ALTER INDEX ... INVISIBLE 并观察稳定后，再考虑 DROP INDEX。",
        ],
    }


def build_create_index_plan(instance: DBInstance, payload: dict) -> dict:
    return oracle_index.build_create_index_plan(instance, payload)


def generate_index_advice(instance: DBInstance, payload: dict) -> dict:
    owner = _identifier(payload.get("owner"), "owner")
    table_name = _identifier(payload.get("table_name"), "table_name")
    sql_id = payload.get("sql_id")
    if sql_id:
        _safe_sql_id(sql_id)
    result = list_index_advice(instance, owner, table_name, payload.get("days", 30), payload.get("source", "AWR"))
    result["status"] = "plan_only"
    result["message"] = "已生成索引治理建议。所有变更类动作只生成 SQL，不自动执行。"
    return result


def _query(instance: DBInstance, sql: str, params: dict | None = None) -> list[dict]:
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params or {})
        return safe_rows(rows_to_dicts(cursor, cursor.fetchall()))


def _with_fallback(source: str, awr_fn, current_fn) -> dict:
    if source == "CURRENT":
        return {"source": "CURRENT", "items": current_fn(), "warnings": []}
    try:
        return {"source": "AWR", "items": awr_fn(), "warnings": []}
    except Exception as exc:
        logger.warning("AWR table usage query failed, fallback to current cache: %s", exc)
        return {
            "source": "CURRENT",
            "items": current_fn(),
            "warnings": [f"AWR 查询不可用，已自动降级为当前缓存分析：{exc}"],
        }


def _source(value: str | None) -> str:
    source = (value or "AWR").upper()
    if source not in ("AWR", "CURRENT"):
        raise ValueError("source 只能是 AWR 或 CURRENT")
    return source


def _days(value: int | None) -> int:
    days = int(value or 30)
    if days < 1 or days > 365:
        raise ValueError("days 必须在 1 到 365 之间")
    return days


def _optional_identifier(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _identifier(value, "identifier")


def _identifier(value: str | None, field: str) -> str:
    name = str(value or "").strip().upper()
    if not _IDENT_RE.match(name):
        raise ValueError(f"{field} 非法：{value}")
    return name


def _safe_sql_id(value: str) -> str:
    sql_id = str(value).strip()
    if not re.match(r"^[A-Za-z0-9_.$#-]{1,64}$", sql_id):
        raise ValueError(f"sql_id 非法：{value}")
    return sql_id
