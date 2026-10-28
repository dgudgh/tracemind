import logging
import re

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.oracle_temp")


def _safe_cursor_query(cursor, sql, label="", params=None):
    try:
        cursor.execute(sql, params or {})
        return safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as e:
        logger.warning("%s query failed: %s", label, e)
        return []


def _validate_tablespace_name(name: str) -> str:
    value = (name or "TEMP").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", value):
        raise ValueError("Invalid tablespace name")
    return value


def get_temp_overview(instance: DBInstance) -> dict:
    with oracle_connection(instance) as conn:
        c = conn.cursor()

        role_row = _safe_cursor_query(c, "SELECT database_role, open_mode FROM v$database", "DB role")
        database_role = role_row[0].get("database_role", "") if role_row else ""
        open_mode = role_row[0].get("open_mode", "") if role_row else ""

        items = _safe_cursor_query(c, """
            SELECT h.tablespace_name,
                   ROUND(SUM(h.bytes_used + h.bytes_free) / 1024 / 1024, 2) AS total_mb,
                   ROUND(SUM(h.bytes_used) / 1024 / 1024, 2) AS used_mb,
                   ROUND(SUM(h.bytes_free) / 1024 / 1024, 2) AS free_mb,
                   ROUND(SUM(h.bytes_used) / NULLIF(SUM(h.bytes_used + h.bytes_free), 0) * 100, 2) AS used_pct
            FROM v$temp_space_header h
            GROUP BY h.tablespace_name
            ORDER BY h.tablespace_name
        """, "temp overview")

        risks = []
        for t in items:
            pct = t.get("used_pct") or 0
            if pct > 90:
                risks.append({"level": "critical", "tablespace": t["tablespace_name"], "message": f"TEMP {t['tablespace_name']} \u4f7f\u7528\u7387 {pct}%\uff0c\u8d85\u8fc7 90% \u4e34\u754c\u503c"})
            elif pct > 80:
                risks.append({"level": "warning", "tablespace": t["tablespace_name"], "message": f"TEMP {t['tablespace_name']} \u4f7f\u7528\u7387 {pct}%\uff0c\u8d85\u8fc7 80% \u8b66\u544a\u503c"})

    return {"database_role": database_role, "open_mode": open_mode, "items": items, "risks": risks}


def get_temp_files(instance: DBInstance) -> dict:
    with oracle_connection(instance) as conn:
        c = conn.cursor()
        items = _safe_cursor_query(c, """
            SELECT tablespace_name, file_id, file_name,
                   ROUND(bytes / 1024 / 1024, 2) AS size_mb,
                   autoextensible,
                   ROUND(maxbytes / 1024 / 1024, 2) AS max_mb,
                   status
            FROM dba_temp_files
            ORDER BY tablespace_name, file_id
        """, "temp files")

        risks = []
        for f in items:
            if (f.get("autoextensible") or "").upper() == "NO":
                risks.append({"level": "warning", "file_name": f.get("file_name", ""), "message": f"{f['file_name']} \u672a\u5f00\u542f\u81ea\u52a8\u6269\u5c55\uff0c\u5b58\u5728\u7a7a\u95f4\u4e0d\u8db3\u98ce\u9669"})
            size_mb = f.get("size_mb") or 0
            max_mb = f.get("max_mb") or 0
            if max_mb > 0 and size_mb >= max_mb * 0.95:
                risks.append({"level": "critical", "file_name": f.get("file_name", ""), "message": f"{f['file_name']} \u5df2\u63a5\u8fd1 maxsize ({size_mb}/{max_mb} MB)"})

    return {"items": items, "risks": risks}


def get_temp_sql_usage(instance: DBInstance) -> dict:
    with oracle_connection(instance) as conn:
        c = conn.cursor()
        items = _safe_cursor_query(c, """
            SELECT u.sql_id,
                   COUNT(DISTINCT s.sid || ',' || s.serial#) AS session_count,
                   ROUND(SUM(u.blocks * ts.block_size) / 1024 / 1024, 2) AS temp_mb,
                   SUBSTR(MAX(q.sql_text), 1, 4000) AS sql_text
            FROM v$tempseg_usage u
            JOIN v$session s ON u.session_addr = s.saddr
            JOIN dba_tablespaces ts ON u.tablespace = ts.tablespace_name
            LEFT JOIN v$sql q ON u.sql_id = q.sql_id
            WHERE s.username IS NOT NULL
            GROUP BY u.sql_id
            ORDER BY temp_mb DESC
        """, "temp sql usage")

        risks = []
        for r in items:
            mb = r.get("temp_mb") or 0
            if mb > 51200:
                risks.append({"level": "critical", "sql_id": r.get("sql_id", ""), "message": f"SQL {r.get('sql_id', '')} \u5360\u7528 TEMP {mb} MB\uff0c\u8d85\u8fc7 50GB"})
            elif mb > 10240:
                risks.append({"level": "warning", "sql_id": r.get("sql_id", ""), "message": f"SQL {r.get('sql_id', '')} \u5360\u7528 TEMP {mb} MB\uff0c\u8d85\u8fc7 10GB"})

    return {"items": items, "risks": risks}


def get_temp_session_usage(instance: DBInstance) -> dict:
    with oracle_connection(instance) as conn:
        c = conn.cursor()
        items = _safe_cursor_query(c, """
            SELECT s.sid, s.serial#, s.username, s.machine, s.program, s.module, s.status,
                   u.tablespace, u.segtype, u.sql_id,
                   ROUND(SUM(u.blocks * ts.block_size) / 1024 / 1024, 2) AS temp_mb,
                   s.last_call_et AS last_call_seconds,
                   SUBSTR(q.sql_text, 1, 4000) AS sql_text
            FROM v$tempseg_usage u
            JOIN v$session s ON u.session_addr = s.saddr
            JOIN dba_tablespaces ts ON u.tablespace = ts.tablespace_name
            LEFT JOIN v$sql q ON u.sql_id = q.sql_id
            WHERE s.username IS NOT NULL
            GROUP BY s.sid, s.serial#, s.username, s.machine, s.program, s.module, s.status,
                     u.tablespace, u.segtype, u.sql_id, s.last_call_et, q.sql_text
            ORDER BY temp_mb DESC
        """, "temp session usage")

    return {"items": items}


def get_temp_extend_plan(instance: DBInstance, tablespace_name: str = "TEMP", size_mb: int = 20480, autoextend: bool = True) -> dict:
    tablespace_name = _validate_tablespace_name(tablespace_name)
    size_mb = int(size_mb or 20480)
    if size_mb < 100 or size_mb > 102400:
        raise ValueError("size_mb must be between 100 and 102400")

    with oracle_connection(instance) as conn:
        c = conn.cursor()

        existing = _safe_cursor_query(c, """
            SELECT COUNT(*) AS cnt FROM dba_temp_files WHERE tablespace_name = :ts
        """, "count temp files", {"ts": tablespace_name})
        file_count = existing[0].get("cnt", 0) if existing else 0

        is_asm_row = _safe_cursor_query(c, """
            SELECT name FROM v$datafile WHERE ROWNUM = 1
        """, "check asm")
        is_asm = False
        if is_asm_row:
            path = is_asm_row[0].get("name", "")
            if path.startswith("+"):
                is_asm = True

        if is_asm:
            path_hint = "+DATA"
        else:
            path_row = _safe_cursor_query(c, """
                SELECT file_name FROM dba_temp_files WHERE tablespace_name = :ts AND ROWNUM = 1
            """, "get temp path", {"ts": tablespace_name})
            if path_row:
                import os
                base = path_row[0].get("file_name", "/oradata/TEMP01.dbf")
                dir_name = os.path.dirname(base)
                ext = os.path.splitext(base)[1]
                new_name = f"{tablespace_name}{file_count + 1:02d}{ext}"
                path_hint = os.path.join(dir_name, new_name)
            else:
                path_hint = f"/oradata/{tablespace_name}{file_count + 1:02d}.dbf"

        auto_clause = "AUTOEXTEND ON NEXT 1024M MAXSIZE 102400M" if autoextend else ""
        sql = f"ALTER TABLESPACE {tablespace_name}\nADD TEMPFILE '{path_hint}'\nSIZE {size_mb}M\n{auto_clause};"

        return {
            "status": "plan_only",
            "message": "\u5df2\u751f\u6210 TEMP \u6269\u5bb9 SQL\uff0c\u8bf7\u786e\u8ba4\u78c1\u76d8\u7a7a\u95f4\u540e\u6267\u884c\u3002",
            "sql": sql,
            "tablespace_name": tablespace_name,
            "file_path": path_hint,
            "size_mb": size_mb,
            "autoextend": autoextend,
            "is_asm": is_asm,
        }
