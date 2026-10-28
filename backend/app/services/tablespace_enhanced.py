import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.tablespace_enhanced")


def _dictionary_columns(cursor, table_name: str) -> set[str]:
    try:
        cursor.execute(
            "SELECT column_name FROM all_tab_columns WHERE owner = 'SYS' AND table_name = :1",
            [table_name.upper()],
        )
        return {row[0].upper() for row in cursor.fetchall()}
    except Exception:
        return set()


def _tablespace_basic_sql(cursor) -> str:
    columns = _dictionary_columns(cursor, "DBA_TABLESPACES")
    expressions = [
        "tablespace_name",
        "contents",
        "status",
        "extent_management",
        "segment_space_management",
    ]

    expressions.append("bigfile" if "BIGFILE" in columns else "'NO' AS bigfile")

    if "ENCRYPTED" in columns:
        expressions.append("encrypted AS encryption")
    elif "ENCRYPTION" in columns:
        expressions.append("encryption")
    else:
        expressions.append("'NO' AS encryption")

    if "DEF_TAB_COMPRESSION" in columns:
        expressions.append("def_tab_compression AS compressed")
    elif "COMPRESS_FOR" in columns:
        expressions.append("compress_for AS compressed")
    elif "COMPRESSED" in columns:
        expressions.append("compressed")
    else:
        expressions.append("NULL AS compressed")

    return f"SELECT {', '.join(expressions)} FROM dba_tablespaces ORDER BY tablespace_name"


def get_tablespace_overview(instance: DBInstance) -> dict:
    logger.info("增强表空间概览 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        cursor.execute(_tablespace_basic_sql(cursor))
        ts_basic = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            "SELECT a.tablespace_name, "
            "COUNT(a.file_id) AS datafile_count, "
            "ROUND(SUM(a.user_bytes) / 1024 / 1024 / 1024, 2) AS used_gb, "
            "ROUND(SUM(DECODE(a.autoextensible, 'YES', "
            "  DECODE(SIGN(a.maxbytes - a.user_bytes), -1, 0, a.maxbytes - a.user_bytes), 0)) "
            "  / 1024 / 1024 / 1024, 2) AS unalloc_gb, "
            "SUM(CASE WHEN a.autoextensible = 'YES' THEN 1 ELSE 0 END) AS autoextensible_count "
            "FROM dba_data_files a GROUP BY a.tablespace_name"
        )
        df_info = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            "SELECT tablespace_name, ROUND(SUM(bytes) / 1024 / 1024 / 1024, 2) AS free_gb "
            "FROM dba_free_space GROUP BY tablespace_name"
        )
        free_info = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        free_map = {f["tablespace_name"]: f["free_gb"] for f in free_info}

        try:
            cursor.execute(
                "SELECT tablespace_name, COUNT(*) AS tempfile_count, "
                "ROUND(SUM(bytes)/1024/1024/1024, 2) AS temp_total_gb "
                "FROM dba_temp_files GROUP BY tablespace_name"
            )
            temp_info = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            temp_map = {t["tablespace_name"]: t for t in temp_info}
        except Exception:
            temp_map = {}

        omf_enabled = False
        db_create_file = ""
        try:
            cursor.execute("SELECT value FROM v$parameter WHERE name = 'db_create_file_dest'")
            omf_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            if omf_rows and omf_rows[0].get("value"):
                db_create_file = omf_rows[0]["value"]
                omf_enabled = True
        except Exception:
            pass

    tablespaces = []
    total_gb_sum = 0
    used_gb_sum = 0

    for ts in ts_basic:
        name = ts["tablespace_name"]
        contents = ts.get("contents", "PERMANENT")
        df = next((d for d in df_info if d["tablespace_name"] == name), None)
        tmp = temp_map.get(name)

        if contents == "TEMPORARY":
            total_gb = tmp.get("temp_total_gb", 0) if tmp else 0
            used_gb = 0
            free_gb = total_gb
            datafile_count = tmp.get("tempfile_count", 0) if tmp else 0
            max_total_gb = total_gb
            autoextensible_count = 0
        else:
            user_gb = float(df.get("used_gb") or 0) if df else 0
            unalloc_gb = float(df.get("unalloc_gb") or 0) if df else 0
            free_gb = float(free_map.get(name, 0) or 0)
            used_gb = max(user_gb - free_gb, 0)
            total_gb = unalloc_gb + user_gb
            datafile_count = df.get("datafile_count", 0) if df else 0
            max_total_gb = total_gb
            autoextensible_count = df.get("autoextensible_count", 0) if df else 0

        if total_gb == 0:
            total_gb = used_gb + free_gb
        if total_gb == 0:
            total_gb = 0.01

        usage_pct = round(used_gb / total_gb * 100, 2) if total_gb > 0 else 0

        status = "normal"
        if usage_pct >= 95:
            status = "critical"
        elif usage_pct >= 85:
            status = "warning"

        recommendation = ""
        if status == "critical":
            recommendation = "空间严重不足，建议立即扩容或清理历史数据"
        elif status == "warning":
            recommendation = "进入预警区间，建议生成扩容方案并确认数据文件路径"
        elif usage_pct >= 70:
            recommendation = "使用率偏高，建议关注增长趋势"

        total_gb_sum += total_gb
        used_gb_sum += used_gb

        tablespaces.append({
            "tablespace_name": name,
            "type": contents,
            "status": ts.get("status", ""),
            "extent_management": ts.get("extent_management", ""),
            "segment_space_management": ts.get("segment_space_management", ""),
            "bigfile": ts.get("bigfile", ""),
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "usage_percent": usage_pct,
            "max_total_gb": max_total_gb,
            "datafile_count": datafile_count,
            "autoextensible_count": autoextensible_count,
            "risk_status": status,
            "recommendation": recommendation,
        })

    warning_count = len([t for t in tablespaces if t["risk_status"] == "warning"])
    critical_count = len([t for t in tablespaces if t["risk_status"] == "critical"])

    return {
        "instance_name": instance.instance_name,
        "omf_enabled": omf_enabled,
        "db_create_file_dest": db_create_file,
        "tablespaces": tablespaces,
        "summary": {
            "total_count": len(tablespaces),
            "total_gb": round(total_gb_sum, 2),
            "used_gb": round(used_gb_sum, 2),
            "free_gb": round(total_gb_sum - used_gb_sum, 2),
            "warning_count": warning_count,
            "critical_count": critical_count,
        },
    }


def get_tablespace_datafiles(instance: DBInstance, tablespace_name: str) -> dict:
    logger.info("查询数据文件 | instance=%s | ts=%s", instance.instance_name, tablespace_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_id, file_name, bytes/1024/1024/1024 AS size_gb, "
            "maxbytes/1024/1024/1024 AS maxsize_gb, user_bytes/1024/1024/1024 AS user_gb, "
            "autoextensible, status, "
            "increment_by * (SELECT value FROM v$parameter WHERE name='db_block_size')/1024/1024 AS next_mb "
            "FROM dba_data_files WHERE tablespace_name = :1 ORDER BY file_id",
            [tablespace_name.upper()]
        )
        datafiles = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    return {"tablespace_name": tablespace_name, "datafiles": datafiles}


def generate_expand_script(instance: DBInstance, tablespace_name: str, datafile_path: str,
                           file_count: int = 1, size_mb: int = 1024, autoextend: bool = True,
                           next_mb: int = 1024, maxsize_mb: int = 30720) -> dict:
    logger.info("生成扩容脚本 | instance=%s | ts=%s | files=%d", instance.instance_name, tablespace_name, file_count)

    import os
    import re

    resolved_path = datafile_path
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT file_name FROM dba_data_files WHERE tablespace_name = :1 ORDER BY file_id",
            [tablespace_name.upper()]
        )
        existing_rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

    existing_files = [r["file_name"] for r in existing_rows]

    is_asm = False
    if existing_files:
        is_asm = existing_files[0].strip().startswith("+")

    if not resolved_path:
        if is_asm:
            asm_file = existing_files[-1].strip()
            disk_group = asm_file.split("/")[0].split(".")[0]
            if not disk_group.startswith("+"):
                disk_group = "+" + disk_group
            resolved_path = disk_group
        elif existing_files:
            last_file = existing_files[-1]
            match = re.match(r"^(.+?)[-_]?(\d+)?(\.\w+)$", last_file)
            if match:
                prefix = match.group(1)
                ext = match.group(3) or ".dbf"
                next_num = len(existing_files) + 1
                resolved_path = f"{prefix}{next_num:02d}{ext}"
            else:
                resolved_path = last_file
        else:
            resolved_path = f"/oradata/{instance.instance_name or 'ORCL'}/{tablespace_name.lower()}_01.dbf"
    elif resolved_path.startswith("+"):
        is_asm = True

    if is_asm:
        disk_group = resolved_path.strip().split("/")[0].split(".")[0]
        if not disk_group.startswith("+"):
            disk_group = "+" + disk_group
        start_num = len(existing_files) + 1

        auto_clause = ""
        if autoextend:
            auto_clause = f" AUTOEXTEND ON NEXT {next_mb}M MAXSIZE {maxsize_mb}M"

        lines = [f"-- 表空间扩容: {tablespace_name}", f"-- 文件数: {file_count}", f"-- 每文件大小: {size_mb}M", ""]
        for i in range(file_count):
            lines.append(f"ALTER TABLESPACE {tablespace_name} ADD DATAFILE '{disk_group}' SIZE {size_mb}M{auto_clause};")
        sql = "\n".join(lines)

        total_size_mb = file_count * size_mb
        total_size_gb = round(total_size_mb / 1024, 2)

        return {
            "status": "plan_only",
            "message": f"已生成表空间扩容 SQL（RAC/ASM，{file_count} 个数据文件，共 {total_size_gb} GB）。",
            "sql": sql,
            "tablespace_name": tablespace_name,
            "datafile_path": disk_group,
            "existing_datafiles": existing_files,
            "file_count": file_count,
            "size_mb": size_mb,
            "total_size_gb": total_size_gb,
            "autoextend": autoextend,
            "warnings": [
                "扩容属于 DDL 操作，请在维护窗口执行。",
                f"共 {file_count} 个数据文件，总计 {total_size_gb} GB，请确认磁盘组空间充足。",
                f"ASM 磁盘组: {disk_group}，Oracle 将自动管理文件名。",
            ],
        }

    base, ext = os.path.splitext(resolved_path)
    number_match = re.search(r"^(.+?)(\d+)$", base)
    if number_match:
        base_prefix = number_match.group(1)
    else:
        base_prefix = base + "_"

    start_num = len(existing_files) + 1

    auto_clause = ""
    if autoextend:
        auto_clause = f" AUTOEXTEND ON NEXT {next_mb}M MAXSIZE {maxsize_mb}M"

    lines = [f"-- 表空间扩容: {tablespace_name}", f"-- 文件数: {file_count}", f"-- 每文件大小: {size_mb}M", ""]
    for i in range(file_count):
        if file_count == 1:
            path = resolved_path
        else:
            path = f"{base_prefix}{start_num + i:02d}{ext}"
        lines.append(f"ALTER TABLESPACE {tablespace_name} ADD DATAFILE '{path}' SIZE {size_mb}M{auto_clause};")
    sql = "\n".join(lines)

    total_size_mb = file_count * size_mb
    total_size_gb = round(total_size_mb / 1024, 2)

    return {
        "status": "plan_only",
        "message": f"已生成表空间扩容 SQL（{file_count} 个数据文件，共 {total_size_gb} GB）。",
        "sql": sql,
        "tablespace_name": tablespace_name,
        "datafile_path": resolved_path,
        "existing_datafiles": existing_files,
        "file_count": file_count,
        "size_mb": size_mb,
        "total_size_gb": total_size_gb,
        "autoextend": autoextend,
        "warnings": [
            "扩容属于 DDL 操作，请在维护窗口执行。",
            f"共 {file_count} 个数据文件，总计 {total_size_gb} GB，请确认磁盘空间充足。",
            "请确认数据文件路径正确。",
        ],
    }
