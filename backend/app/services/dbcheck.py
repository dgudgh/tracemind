import logging
import os
import re

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.oracle.oracle_inspection_profile import SCRIPT_CATALOG, SCRIPT_DIR, detect_oracle_profile
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.dbcheck")

SQL_FILES = {
    key: os.path.join(str(SCRIPT_DIR), item["script_name"])
    for key, item in SCRIPT_CATALOG.items()
}
SQL_FILE = SQL_FILES["noncdb"]

CHECK_ITEMS = [
    {
        "key": "instance_info",
        "title": "实例信息",
        "sql": "SELECT d.DBID, d.NAME, d.DATABASE_ROLE, TO_CHAR(d.CREATED, 'yyyy-mm-dd HH24:mi:ss') CREATED, d.LOG_MODE, d.OPEN_MODE, (SELECT b.VERSION FROM v$instance b WHERE ROWNUM = 1) VERSION FROM v$database d",
    },
    {
        "key": "instance_host",
        "title": "主机信息",
        "sql": "SELECT host_name, instance_name, status, TO_CHAR(startup_time, 'yyyy-mm-dd HH24:mi:ss') startup_time, database_status FROM v$instance",
    },
    {
        "key": "recyclebin",
        "title": "回收站",
        "sql": "SELECT nvl(a.owner, 'SUM') owner, round(SUM(a.space * (SELECT value FROM v$parameter WHERE name = 'db_block_size')) / 1024 / 1024, 2) recyb_size_M, count(1) recyb_cnt FROM dba_recyclebin a GROUP BY ROLLUP(a.owner) ORDER BY 3",
    },
    {
        "key": "db_version",
        "title": "数据库版本",
        "sql": "SELECT banner FROM v$version WHERE rownum <= 5",
    },
    {
        "key": "db_components",
        "title": "已安装组件",
        "sql": "SELECT comp_id, comp_name, version, status FROM dba_registry ORDER BY comp_id",
    },
    {
        "key": "db_params",
        "title": "核心参数",
        "sql": "SELECT name, value, isdefault, isses_modifiable, issys_modifiable FROM v$parameter WHERE name IN ('sga_max_size','sga_target','pga_aggregate_target','memory_target','memory_max_target','db_block_size','db_files','processes','sessions','open_cursors','db_recovery_file_dest','db_recovery_file_dest_size','log_archive_dest_1','recyclebin','cursor_sharing','optimizer_mode','db_create_file_dest','spfile','cluster_database','compatible') ORDER BY name",
    },
    {
        "key": "spfile_check",
        "title": "SPFILE 检查",
        "sql": "SELECT name, value FROM v$parameter WHERE name = 'spfile'",
    },
    {
        "key": "memory_params",
        "title": "内存参数",
        "sql": "SELECT name, round(value/1024/1024/1024, 2) || 'G' AS value FROM v$parameter WHERE name IN ('memory_max_target','memory_target','sga_max_size','sga_target','pga_aggregate_target') AND value IS NOT NULL ORDER BY name",
    },
    {
        "key": "control_files",
        "title": "控制文件",
        "sql": "SELECT name, status, is_recovery_dest_file, block_size, file_size_blks FROM v$controlfile ORDER BY name",
    },
    {
        "key": "redo_logs",
        "title": "Redo Log",
        "sql": "SELECT a.group#, a.thread#, a.sequence#, a.members, a.archived, a.status, round(a.bytes/1024/1024) size_mb, b.member FROM v$log a, v$logfile b WHERE a.group# = b.group# ORDER BY a.group#, b.member",
    },
    {
        "key": "redo_switch",
        "title": "Redo 切换频率",
        "sql": "SELECT to_char(first_time, 'yyyy-mm-dd') log_date, thread#, count(*) switch_count FROM v$log_history WHERE first_time > sysdate - 7 GROUP BY to_char(first_time, 'yyyy-mm-dd'), thread# ORDER BY 1, 2",
    },
    {
        "key": "archive_rate",
        "title": "归档日志产生量",
        "sql": "SELECT to_char(first_time, 'yyyy-mm-dd') log_date, round(sum(blocks*block_size)/1024/1024, 2) size_mb FROM v$archived_log WHERE first_time > sysdate - 7 AND dest_id = 1 GROUP BY to_char(first_time, 'yyyy-mm-dd') ORDER BY 1",
    },
    {
        "key": "tablespaces",
        "title": "表空间使用率",
        "sql": "SELECT a.tablespace_name, a.status, a.contents, a.extent_management, a.segment_space_management, round(b.total_bytes/1024/1024, 2) total_mb, round((b.total_bytes - nvl(c.free_bytes, 0))/1024/1024, 2) used_mb, round(nvl(c.free_bytes, 0)/1024/1024, 2) free_mb, round((b.total_bytes - nvl(c.free_bytes, 0))/nullif(b.total_bytes, 0)*100, 2) used_pct FROM dba_tablespaces a, (SELECT tablespace_name, sum(bytes) total_bytes FROM dba_data_files GROUP BY tablespace_name) b, (SELECT tablespace_name, sum(bytes) free_bytes FROM dba_free_space GROUP BY tablespace_name) c WHERE a.tablespace_name = b.tablespace_name AND a.tablespace_name = c.tablespace_name(+) ORDER BY a.tablespace_name",
    },
    {
        "key": "datafiles",
        "title": "数据文件",
        "sql": "SELECT tablespace_name, file_name, status, autoextensible, round(bytes/1024/1024) size_mb, round(maxbytes/1024/1024) max_mb, round(increment_by * (SELECT value FROM v$parameter WHERE name = 'db_block_size')/1024/1024) increment_mb FROM dba_data_files ORDER BY tablespace_name, file_name",
    },
    {
        "key": "temp_files",
        "title": "临时文件",
        "sql": "SELECT tablespace_name, file_name, status, autoextensible, round(bytes/1024/1024) size_mb, round(maxbytes/1024/1024) max_mb FROM dba_temp_files ORDER BY tablespace_name, file_name",
    },
    {
        "key": "users",
        "title": "数据库用户",
        "sql": "SELECT username, account_status, default_tablespace, temporary_tablespace, created, expiry_date, profile FROM dba_users WHERE username NOT IN ('SYS','SYSTEM','DBSNMP','APPQOSSYS','DBSFWUSER','REMOTE_SCHEDULER_AGENT','GGSYS','AUDSYS','DIP','OUTLN','GSMADMIN_INTERNAL','ORACLE_OCM','XS$NULL','GSMCATUSER','MDDATA','SYSBACKUP','REMOTE_SCHEDULER_AGENT','GSMUSER','SYSKM','SYSDG','SYSRAC','WMSYS','OJVMSYS') ORDER BY username",
    },
    {
        "key": "profiles",
        "title": "Profile 配置",
        "sql": "SELECT profile, resource_name, resource_type, limit FROM dba_profiles WHERE profile IN (SELECT DISTINCT profile FROM dba_users) ORDER BY profile, resource_type, resource_name",
    },
    {
        "key": "resource_limit",
        "title": "资源限制",
        "sql": "SELECT resource_name, current_utilization, max_utilization, initial_allocation, limit_value FROM v$resource_limit ORDER BY resource_name",
    },
    {
        "key": "sga_info",
        "title": "SGA 信息",
        "sql": "SELECT name, round(bytes/1024/1024, 2) size_mb FROM v$sgastat WHERE bytes > 1024*1024 ORDER BY bytes DESC",
    },
    {
        "key": "pga_info",
        "title": "PGA 信息",
        "sql": "SELECT name, value, round(value/1024/1024, 2) value_mb FROM v$pgastat ORDER BY name",
    },
    {
        "key": "sessions",
        "title": "会话统计",
        "sql": "SELECT status, type, count(*) cnt FROM v$session GROUP BY status, type ORDER BY status, type",
    },
    {
        "key": "lock_info",
        "title": "当前锁等待",
        "sql": "SELECT a.sid, a.serial#, a.username, a.status, a.module, a.machine, a.seconds_in_wait, b.type, b.lmode, b.request, c.object_name FROM v$session a, v$lock b, dba_objects c WHERE a.sid = b.sid AND b.id1 = c.object_id(+) AND b.type IN ('TM','TX') AND a.seconds_in_wait > 10 ORDER BY a.seconds_in_wait DESC",
    },
    {
        "key": "invalid_objects",
        "title": "失效对象",
        "sql": "SELECT owner, object_type, object_name, status FROM dba_objects WHERE status = 'INVALID' AND owner NOT IN ('SYS','SYSTEM','PUBLIC') ORDER BY owner, object_type, object_name",
    },
    {
        "key": "top_sql",
        "title": "TOP SQL (按执行时间)",
        "sql": "SELECT * FROM (SELECT sql_id, parsing_schema_name, module, executions, round(elapsed_time/1000000, 2) elapsed_sec, round(cpu_time/1000000, 2) cpu_sec, round(buffer_gets/nullif(executions, 0), 0) avg_buffer_gets, round(disk_reads/nullif(executions, 0), 0) avg_disk_reads, substr(sql_text, 1, 200) sql_preview FROM v$sqlarea WHERE parsing_schema_name NOT IN ('SYS','SYSTEM','DBSNMP') AND elapsed_time > 1000000 ORDER BY elapsed_time DESC) WHERE ROWNUM <= 20",
    },
    {
        "key": "db_links",
        "title": "数据库链接",
        "sql": "SELECT owner, db_link, username, host, created FROM dba_db_links ORDER BY owner, db_link",
    },
    {
        "key": "jobs",
        "title": "定时任务",
        "sql": "SELECT job, schema_user, what, to_char(last_date, 'yyyy-mm-dd hh24:mi:ss') last_run, to_char(next_date, 'yyyy-mm-dd hh24:mi:ss') next_run, interval, failures, broken FROM dba_jobs WHERE failures > 0 OR broken = 'Y' ORDER BY job",
    },
    {
        "key": "dg_config",
        "title": "Data Guard 配置",
        "sql": "SELECT dest_id, dest_name, status, destination, error FROM v$archive_dest WHERE dest_id <= 5 ORDER BY dest_id",
    },
]


PROFILE_RESULT_ITEM = {
    "key": "oracle_inspection_profile",
    "title": "Oracle 版本识别与脚本选择",
}


CDB_CHECK_OVERRIDES = {
    "instance_info": """
        SELECT d.DBID,
               d.NAME,
               d.DATABASE_ROLE,
               TO_CHAR(d.CREATED, 'yyyy-mm-dd HH24:mi:ss') CREATED,
               d.LOG_MODE,
               d.OPEN_MODE,
               d.CDB,
               (SELECT b.VERSION FROM v$instance b WHERE ROWNUM = 1) VERSION
        FROM v$database d
    """,
    "recyclebin": """
        SELECT NVL(TO_CHAR(a.con_id), 'SUM') con_id,
               NVL(a.owner, 'SUM') owner,
               ROUND(SUM(a.space * (SELECT value FROM v$parameter WHERE name = 'db_block_size')) / 1024 / 1024, 2) recyb_size_M,
               COUNT(1) recyb_cnt
        FROM cdb_recyclebin a
        GROUP BY ROLLUP(a.con_id, a.owner)
        ORDER BY 4 DESC
    """,
    "tablespaces": """
        SELECT ts.con_id,
               CASE ts.con_id
                    WHEN 1 THEN 'CDB$ROOT'
                    WHEN 2 THEN 'PDB$SEED'
                    ELSE NVL(p.name, TO_CHAR(ts.con_id))
               END AS container_name,
               ts.tablespace_name,
               ts.status,
               ts.contents,
               ts.extent_management,
               ts.segment_space_management,
               ROUND(df.total_bytes / 1024 / 1024, 2) total_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / 1024 / 1024, 2) used_mb,
               ROUND(NVL(fs.free_bytes, 0) / 1024 / 1024, 2) free_mb,
               ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / NULLIF(df.total_bytes, 0) * 100, 2) used_pct
        FROM cdb_tablespaces ts
        JOIN (
            SELECT con_id, tablespace_name, SUM(bytes) total_bytes
            FROM cdb_data_files
            GROUP BY con_id, tablespace_name
        ) df ON df.con_id = ts.con_id AND df.tablespace_name = ts.tablespace_name
        LEFT JOIN (
            SELECT con_id, tablespace_name, SUM(bytes) free_bytes
            FROM cdb_free_space
            GROUP BY con_id, tablespace_name
        ) fs ON fs.con_id = ts.con_id AND fs.tablespace_name = ts.tablespace_name
        LEFT JOIN v$pdbs p ON p.con_id = ts.con_id
        ORDER BY used_pct DESC
    """,
    "datafiles": """
        SELECT con_id,
               tablespace_name,
               file_name,
               status,
               autoextensible,
               ROUND(bytes / 1024 / 1024) size_mb,
               ROUND(maxbytes / 1024 / 1024) max_mb,
               ROUND(increment_by * (SELECT value FROM v$parameter WHERE name = 'db_block_size') / 1024 / 1024) increment_mb
        FROM cdb_data_files
        ORDER BY con_id, tablespace_name, file_name
    """,
    "temp_files": """
        SELECT con_id,
               tablespace_name,
               file_name,
               status,
               autoextensible,
               ROUND(bytes / 1024 / 1024) size_mb,
               ROUND(maxbytes / 1024 / 1024) max_mb
        FROM cdb_temp_files
        ORDER BY con_id, tablespace_name, file_name
    """,
    "users": """
        SELECT con_id,
               username,
               account_status,
               default_tablespace,
               temporary_tablespace,
               created,
               expiry_date,
               profile,
               common
        FROM cdb_users
        WHERE username NOT IN ('SYS','SYSTEM','DBSNMP','APPQOSSYS','DBSFWUSER','REMOTE_SCHEDULER_AGENT','GGSYS','AUDSYS','DIP','OUTLN','GSMADMIN_INTERNAL','ORACLE_OCM','XS$NULL','GSMCATUSER','MDDATA','SYSBACKUP','REMOTE_SCHEDULER_AGENT','GSMUSER','SYSKM','SYSDG','SYSRAC','WMSYS','OJVMSYS')
        ORDER BY con_id, username
    """,
    "invalid_objects": """
        SELECT con_id, owner, object_type, object_name, status
        FROM cdb_objects
        WHERE status = 'INVALID'
          AND owner NOT IN ('SYS','SYSTEM','PUBLIC')
        ORDER BY con_id, owner, object_type, object_name
    """,
}


CDB_EXTRA_CHECK_ITEMS = [
    {
        "key": "pdb_status",
        "title": "PDB 状态",
        "sql": "SELECT con_id, name, open_mode, restricted, open_time FROM v$pdbs ORDER BY con_id",
    }
]


def _check_items_for_profile(profile) -> list[dict]:
    items = [dict(item) for item in CHECK_ITEMS]
    if profile.script_key == "cdb":
        for item in items:
            override = CDB_CHECK_OVERRIDES.get(item["key"])
            if override:
                item["sql"] = override
        items[2:2] = CDB_EXTRA_CHECK_ITEMS
    return items


def _profile_report_item(profile) -> dict:
    data = profile.to_dict()
    rows = [[
        data.get("version") or "",
        data.get("major_version") or "",
        data.get("cdb") or "",
        data.get("con_name") or "",
        data.get("script_name") or "",
        data.get("script_label") or "",
        data.get("selection_reason") or "",
        "yes" if data.get("script_exists") else "no",
        "; ".join(data.get("warnings") or []),
    ]]
    return {
        "key": PROFILE_RESULT_ITEM["key"],
        "title": PROFILE_RESULT_ITEM["title"],
        "columns": [
            "VERSION",
            "MAJOR_VERSION",
            "CDB",
            "CON_NAME",
            "SCRIPT_NAME",
            "SCRIPT_LABEL",
            "SELECTION_REASON",
            "SCRIPT_EXISTS",
            "WARNINGS",
        ],
        "rows": rows,
        "row_count": 1,
        "status": "warning" if data.get("warnings") else "ok",
        "raw_data": data,
    }


def run_dbcheck(instance: DBInstance) -> dict:
    results = []
    errors = []

    try:
        with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
            profile = detect_oracle_profile(conn)
            check_items = _check_items_for_profile(profile)
            results.append(_profile_report_item(profile))
            cursor = conn.cursor()

            for item in check_items:
                title = item["title"]
                sql = item["sql"]
                try:
                    cursor.execute(sql)
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    rows = []
                    for row in cursor.fetchall():
                        row_data = []
                        for val in row:
                            if hasattr(val, "isoformat"):
                                row_data.append(val.isoformat())
                            elif isinstance(val, (bytes, bytearray)):
                                row_data.append(val.hex())
                            else:
                                row_data.append(str(val) if val is not None else "")
                        rows.append(row_data)

                    results.append({
                        "key": item["key"],
                        "title": title,
                        "columns": columns,
                        "rows": rows,
                        "row_count": len(rows),
                        "status": "ok",
                    })
                except Exception as e:
                    err_msg = str(e).strip()[:500]
                    logger.warning("dbcheck 项失败 | title=%s | error=%s", title, err_msg)
                    results.append({
                        "key": item["key"],
                        "title": title,
                        "columns": [],
                        "rows": [],
                        "row_count": 0,
                        "status": "error",
                        "error": err_msg,
                    })
                    errors.append({"title": title, "error": err_msg})

            cursor.close()

    except Exception as e:
        logger.error("dbcheck 数据库连接失败 | error=%s", e, exc_info=True)
        return {
            "ok": False,
            "instance_name": instance.instance_name,
            "items": [],
            "errors": [{"title": "连接失败", "error": str(e)}],
            "total": len(CHECK_ITEMS) + 1,
            "success": 0,
            "failed": len(CHECK_ITEMS) + 1,
        }

    success_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "ok": True,
        "instance_name": instance.instance_name,
        "items": results,
        "errors": errors,
        "total": len(results),
        "success": success_count,
        "failed": len(results) - success_count,
    }
