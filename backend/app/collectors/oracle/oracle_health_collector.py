import logging
from typing import Any

from app.collectors.oracle.oracle_connection import (
    effective_oracle_account_type,
    oracle_connection,
    probe_oracle_inspection_capability,
)
from app.collectors.oracle.oracle_inspection_profile import detect_oracle_profile
from app.collectors.oracle.sql_text import materialize_sql_rows
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.oracle.health")


ORACLE_QUERIES = {
    "instance_info": """
        SELECT instance_name, host_name, version, status, database_status, startup_time
        FROM v$instance
    """,
    "database_info": """
        SELECT name, open_mode, database_role, log_mode, force_logging
        FROM v$database
    """,
    "tablespace_usage": """
        SELECT t.tablespace_name,
               ROUND((t.unalloc_size + t.used_size) / 1024 / 1024, 2) AS total_mb,
               ROUND((t.used_size - NVL(t.free_size, 0)) / 1024 / 1024, 2) AS used_mb,
               ROUND(NVL(t.free_size, 0) / 1024 / 1024, 2) AS free_mb,
               ROUND((t.used_size - NVL(t.free_size, 0)) / NULLIF(t.unalloc_size + t.used_size, 0) * 100, 2) AS used_pct
        FROM (
          SELECT a.tablespace_name,
                 a.unalloc_size,
                 f.free_size,
                 a.used_size
          FROM (
            SELECT tablespace_name, SUM(bytes) AS free_size
            FROM dba_free_space GROUP BY tablespace_name
          ) f, (
            SELECT tablespace_name,
              SUM(user_bytes) AS used_size,
              SUM(DECODE(autoextensible, 'YES', DECODE(SIGN(maxbytes - user_bytes), -1, 0, maxbytes - user_bytes), 0)) AS unalloc_size
            FROM dba_data_files GROUP BY tablespace_name
          ) a WHERE a.tablespace_name = f.tablespace_name(+)
        ) t ORDER BY used_pct DESC
    """,
    "temp_usage": """
        SELECT tablespace_name,
               ROUND(SUM(bytes_used + bytes_free) / 1024 / 1024, 2) AS total_mb,
               ROUND(SUM(bytes_used) / 1024 / 1024, 2) AS used_mb,
               ROUND(SUM(bytes_free) / 1024 / 1024, 2) AS free_mb,
               ROUND(SUM(bytes_used) / NULLIF(SUM(bytes_used + bytes_free), 0) * 100, 2) AS used_pct
        FROM v$temp_space_header
        GROUP BY tablespace_name
        ORDER BY used_pct DESC
    """,
    "undo_usage": """
        SELECT df.tablespace_name,
               ROUND(df.total_bytes / 1024 / 1024, 2) AS total_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / 1024 / 1024, 2) AS used_mb,
               ROUND(NVL(fs.free_bytes, 0) / 1024 / 1024, 2) AS free_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / NULLIF(df.total_bytes, 0) * 100, 2) AS used_pct
        FROM (
            SELECT tablespace_name, SUM(bytes) AS total_bytes
            FROM dba_data_files
            WHERE tablespace_name = (
                SELECT UPPER(value) FROM v$parameter WHERE name = 'undo_tablespace'
            )
            GROUP BY tablespace_name
        ) df
        LEFT JOIN (
            SELECT tablespace_name, SUM(bytes) AS free_bytes
            FROM dba_free_space
            GROUP BY tablespace_name
        ) fs ON fs.tablespace_name = df.tablespace_name
    """,
    "listener_parameters": """
        SELECT name, value
        FROM v$parameter
        WHERE name IN ('local_listener', 'remote_listener', 'service_names')
        ORDER BY name
    """,
    "asm_usage": """
        SELECT name, total_mb, free_mb,
               ROUND((total_mb - free_mb) / total_mb * 100, 2) AS used_pct
        FROM v$asm_diskgroup
        ORDER BY used_pct DESC
    """,
    "fra_usage": """
        SELECT name,
               space_limit / 1024 / 1024 AS space_limit_mb,
               space_used / 1024 / 1024 AS space_used_mb,
               ROUND(space_used / space_limit * 100, 2) AS used_pct
        FROM v$recovery_file_dest
    """,
    "archive_growth": """
        SELECT TRUNC(completion_time) AS log_date,
               COUNT(*) AS log_count,
               ROUND(SUM(blocks * block_size) / 1024 / 1024 / 1024, 2) AS size_gb
        FROM v$archived_log
        WHERE completion_time >= SYSDATE - 7
        GROUP BY TRUNC(completion_time)
        ORDER BY log_date DESC
    """,
    "invalid_objects": """
        SELECT owner, object_type, COUNT(*) AS invalid_count
        FROM dba_objects
        WHERE status = 'INVALID'
        GROUP BY owner, object_type
        ORDER BY invalid_count DESC
    """,
    "stale_statistics": """
        SELECT owner, table_name, stale_stats, last_analyzed, num_rows
        FROM dba_tab_statistics
        WHERE owner NOT IN ('SYS', 'SYSTEM')
          AND stale_stats = 'YES'
        ORDER BY last_analyzed NULLS FIRST
    """,
    "top_sql_elapsed": """
        SELECT *
        FROM (
            SELECT sql_id, child_number, parsing_schema_name AS schema_name,
                   plan_hash_value AS plan_hash, executions,
                   elapsed_time / 1000000 AS elapsed_seconds,
                   cpu_time / 1000000 AS cpu_seconds,
                   buffer_gets, disk_reads, rows_processed, sql_fulltext AS sql_text
            FROM v$sql
            WHERE executions > 0
            ORDER BY elapsed_time DESC
        )
        WHERE ROWNUM <= 20
    """,
    "top_sql_buffer_gets": """
        SELECT *
        FROM (
            SELECT sql_id, child_number, parsing_schema_name AS schema_name,
                   plan_hash_value AS plan_hash, executions,
                   elapsed_time / 1000000 AS elapsed_seconds,
                   cpu_time / 1000000 AS cpu_seconds,
                   buffer_gets, disk_reads, rows_processed, sql_fulltext AS sql_text
            FROM v$sql
            WHERE executions > 0
            ORDER BY buffer_gets DESC
        )
        WHERE ROWNUM <= 20
    """,
    "top_sql_disk_reads": """
        SELECT *
        FROM (
            SELECT sql_id, child_number, parsing_schema_name AS schema_name,
                   plan_hash_value AS plan_hash, executions,
                   elapsed_time / 1000000 AS elapsed_seconds,
                   cpu_time / 1000000 AS cpu_seconds,
                   buffer_gets, disk_reads, rows_processed, sql_fulltext AS sql_text
            FROM v$sql
            WHERE executions > 0
            ORDER BY disk_reads DESC
        )
        WHERE ROWNUM <= 20
    """,
    "locks": """
        SELECT s.sid, s.serial#, s.username, s.machine, s.program, s.event,
               s.blocking_session, s.sql_id, s.status, s.seconds_in_wait
        FROM v$session s
        WHERE s.blocking_session IS NOT NULL
        ORDER BY s.seconds_in_wait DESC
    """,
    "dataguard_stats": """
        SELECT name, value, unit, time_computed
        FROM v$dataguard_stats
    """,
}

BUSINESS_ORACLE_QUERIES = {
    "business_session": """
        SELECT SYS_CONTEXT('USERENV', 'SESSION_USER') AS session_user,
               SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS current_schema,
               SYS_CONTEXT('USERENV', 'DB_NAME') AS db_name,
               SYS_CONTEXT('USERENV', 'HOST') AS client_host
        FROM dual
    """,
    "business_objects": """
        SELECT object_type, status, COUNT(*) AS object_count
        FROM user_objects
        GROUP BY object_type, status
        ORDER BY object_count DESC
    """,
    "business_tables": """
        SELECT table_name, num_rows, last_analyzed
        FROM user_tables
        ORDER BY NVL(num_rows, 0) DESC
        FETCH FIRST 50 ROWS ONLY
    """,
    "business_segments": """
        SELECT segment_name, segment_type, ROUND(bytes / 1024 / 1024, 2) AS size_mb
        FROM user_segments
        ORDER BY bytes DESC
        FETCH FIRST 50 ROWS ONLY
    """,
}


CDB_ORACLE_QUERIES = {
    **ORACLE_QUERIES,
    "database_info": """
        SELECT name, open_mode, database_role, log_mode, force_logging, cdb
        FROM v$database
    """,
    "pdb_status": """
        SELECT con_id, name, open_mode, restricted, open_time
        FROM v$pdbs
        ORDER BY con_id
    """,
    "recyclebin": """
        SELECT con_id,
               owner,
               ROUND(SUM(space * (SELECT value FROM v$parameter WHERE name = 'db_block_size')) / 1024 / 1024, 2) AS recyclebin_mb,
               COUNT(*) AS object_count
        FROM cdb_recyclebin
        GROUP BY con_id, owner
        ORDER BY recyclebin_mb DESC
    """,
    "tablespace_usage": """
        SELECT ts.con_id,
               CASE ts.con_id
                    WHEN 1 THEN 'CDB$ROOT'
                    WHEN 2 THEN 'PDB$SEED'
                    ELSE NVL(p.name, TO_CHAR(ts.con_id))
               END AS container_name,
               ts.tablespace_name,
               ts.status,
               ts.contents,
               ROUND(df.total_bytes / 1024 / 1024, 2) AS total_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / 1024 / 1024, 2) AS used_mb,
               ROUND(NVL(fs.free_bytes, 0) / 1024 / 1024, 2) AS free_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / NULLIF(df.total_bytes, 0) * 100, 2) AS used_pct
        FROM cdb_tablespaces ts
        JOIN (
            SELECT con_id, tablespace_name, SUM(bytes) AS total_bytes
            FROM cdb_data_files
            GROUP BY con_id, tablespace_name
        ) df ON df.con_id = ts.con_id AND df.tablespace_name = ts.tablespace_name
        LEFT JOIN (
            SELECT con_id, tablespace_name, SUM(bytes) AS free_bytes
            FROM cdb_free_space
            GROUP BY con_id, tablespace_name
        ) fs ON fs.con_id = ts.con_id AND fs.tablespace_name = ts.tablespace_name
        LEFT JOIN v$pdbs p ON p.con_id = ts.con_id
        ORDER BY used_pct DESC
    """,
    "invalid_objects": """
        SELECT con_id, owner, object_type, COUNT(*) AS invalid_count
        FROM cdb_objects
        WHERE status = 'INVALID'
        GROUP BY con_id, owner, object_type
        ORDER BY invalid_count DESC
    """,
    "stale_statistics": """
        SELECT con_id, owner, table_name, stale_stats, last_analyzed, num_rows
        FROM cdb_tab_statistics
        WHERE owner NOT IN ('SYS', 'SYSTEM')
          AND stale_stats = 'YES'
        ORDER BY last_analyzed NULLS FIRST
    """,
}


LEGACY_ORACLE_QUERIES = {
    **ORACLE_QUERIES,
    "stale_statistics": """
        SELECT owner,
               table_name,
               'UNKNOWN' AS stale_stats,
               last_analyzed,
               num_rows
        FROM dba_tables
        WHERE owner NOT IN ('SYS', 'SYSTEM')
          AND (last_analyzed IS NULL OR last_analyzed < SYSDATE - 30)
        ORDER BY last_analyzed NULLS FIRST
    """,
}


LEGACY_BUSINESS_ORACLE_QUERIES = {
    **BUSINESS_ORACLE_QUERIES,
    "business_tables": """
        SELECT *
        FROM (
            SELECT table_name, num_rows, last_analyzed
            FROM user_tables
            ORDER BY NVL(num_rows, 0) DESC
        )
        WHERE ROWNUM <= 50
    """,
    "business_segments": """
        SELECT *
        FROM (
            SELECT segment_name, segment_type, ROUND(bytes / 1024 / 1024, 2) AS size_mb
            FROM user_segments
            ORDER BY bytes DESC
        )
        WHERE ROWNUM <= 50
    """,
}

ORACLE_QUERY_ITEM_KEYS = {
    "instance_info": {"oracle.basic.instance_rac"},
    "database_info": {"oracle.basic.database_info", "oracle.archive.mode"},
    "tablespace_usage": {"oracle.tablespace.used_pct"},
    "temp_usage": {"oracle.temp.used_pct"},
    "undo_usage": {"oracle.undo.used_pct"},
    "listener_parameters": {"oracle.listener.status"},
    "asm_usage": {"oracle.tablespace.used_pct", "oracle.basic.instance_rac"},
    "fra_usage": {"oracle.fra.used_pct"},
    "archive_growth": {"oracle.archive.generate_mb_per_hour"},
    "invalid_objects": {"oracle.objects.invalid_count"},
    "stale_statistics": {"oracle.objects.stats_stale"},
    "top_sql_elapsed": {"oracle.performance.top_sql_elapsed"},
    "top_sql_buffer_gets": {"oracle.performance.top_sql_buffer_gets"},
    "top_sql_disk_reads": {"oracle.performance.top_sql_disk_reads"},
    "locks": {"oracle.blocking.seconds"},
    "dataguard_stats": {"oracle.dataguard.lag"},
    "pdb_status": {"oracle.basic.instance_rac"},
    "recyclebin": {"oracle.space.recyclebin_usage"},
    "business_session": {"oracle.basic.database_info"},
    "business_objects": {"oracle.objects.invalid_count", "oracle.security.account_status"},
    "business_tables": {"oracle.objects.segment_fragmentation"},
    "business_segments": {"oracle.objects.segment_fragmentation"},
}


class OracleHealthCollector:
    def __init__(self, instance: DBInstance):
        self.instance = instance

    def collect(self, item_keys: set[str] | None = None, expected_profile: str | None = None) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        query_evidence: dict[str, dict] = {}
        account_type = effective_oracle_account_type(self.instance)
        with oracle_connection(self.instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
            profile = detect_oracle_profile(conn)
            if expected_profile:
                from app.services.inspection.template_profiles import require_matching_oracle_profile

                require_matching_oracle_profile(profile, expected_profile)
            metrics["account_capability"] = [
                probe_oracle_inspection_capability(conn, account_type)
            ]
            profile_data = profile.to_dict()
            profile_sql = ["SELECT version FROM v$instance"]
            if profile.major_version is not None and profile.major_version >= 18:
                profile_sql.append("SELECT version_full FROM v$instance")
            if profile.major_version is None or profile.major_version >= 12:
                profile_sql.append("SELECT cdb FROM v$database")
            if str(profile.cdb or "").upper() == "YES":
                profile_sql.append("SELECT sys_context('USERENV', 'CON_NAME') FROM dual")
            profile_data.update(
                {
                    "sql": profile_sql,
                    "columns": ["version", "major_version", "cdb", "con_name", "script_name"],
                    "rows": [
                        {
                            "version": profile.version,
                            "major_version": profile.major_version,
                            "cdb": profile.cdb,
                            "con_name": profile.con_name,
                            "script_name": profile.script_name,
                        }
                    ],
                    "row_count": 1,
                    "truncated": False,
                    "read_only": True,
                    "status": "warning" if profile.warnings else "normal",
                    "evidence_source": "live_profile_probe",
                }
            )
            metrics["inspection_profile"] = [profile_data]
            query_set = _query_set_for_profile(account_type, profile)
            query_set = {name: sql.replace("sql_id, child_number,", "sql_id, child_number, "
                         + ("con_id," if (profile.major_version or 0) >= 12 else "0 AS con_id,"))
                         if name.startswith("top_sql_") else sql for name, sql in query_set.items()}
            if item_keys is not None:
                query_set = {
                    name: sql
                    for name, sql in query_set.items()
                    if ORACLE_QUERY_ITEM_KEYS.get(name, set()) & item_keys
                }
            logger.info(
                "Oracle collection start | instance=%s | account_type=%s | script=%s | query_count=%d",
                self.instance.instance_name,
                account_type,
                profile.script_name,
                len(query_set),
            )
            cursor = conn.cursor()
            try:
                for name, sql in query_set.items():
                    try:
                        cursor.execute(sql)
                        columns = [
                            str(item[0]).lower()
                            for item in (cursor.description or [])
                        ]
                        rows = rows_to_dicts(cursor, cursor.fetchall())
                        if name.startswith("top_sql_"):
                            rows = materialize_sql_rows(rows, connection=conn)
                        rows = safe_rows(rows)
                        metrics[name] = rows
                        query_evidence[name] = {
                            "status": "normal",
                            "sql": sql.strip(),
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                            "truncated": False,
                            "read_only": True,
                            "evidence_source": "oracle_health_collector",
                            "query_name": name,
                        }
                        logger.debug("Oracle 采集项完成 | %s | 行数=%d", name, len(metrics[name]))
                    except Exception as exc:
                        metrics[name] = [{"error": str(exc)}]
                        query_evidence[name] = {
                            "status": "warning",
                            "sql": sql.strip(),
                            "columns": [],
                            "rows": [],
                            "row_count": 0,
                            "truncated": False,
                            "read_only": True,
                            "evidence_source": "oracle_health_collector",
                            "query_name": name,
                            "message": str(exc),
                        }
                        logger.warning("Oracle 采集项失败 | %s | error=%s", name, exc)
            finally:
                cursor.close()
        metrics["_query_evidence"] = query_evidence
        logger.info("Oracle 采集完成 | instance=%s | 采集项=%d", self.instance.instance_name, len(metrics))
        return metrics


def _query_set_for_profile(account_type: str, profile) -> dict[str, str]:
    if account_type == "business":
        if profile.major_version is not None and profile.major_version < 12:
            return LEGACY_BUSINESS_ORACLE_QUERIES
        return BUSINESS_ORACLE_QUERIES
    if profile.script_key == "cdb":
        return CDB_ORACLE_QUERIES
    if profile.script_key == "10g":
        return LEGACY_ORACLE_QUERIES
    return ORACLE_QUERIES
