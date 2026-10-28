import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.table_index")


def get_table_fragmentation(instance: DBInstance, owner: str | None = None, min_size_mb: float = 10, limit: int = 100) -> dict:
    logger.info("表碎片分析 | instance=%s | owner=%s | min_size=%sMB", instance.instance_name, owner, min_size_mb)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT DISTINCT owner FROM dba_tables WHERE owner NOT IN ('SYS','SYSTEM','DBSNMP','APPQOSSYS','DBSFWUSER','REMOTE_SCHEDULER_AGENT','SYSBACKUP','GSMADMIN_INTERNAL','OUTLN','XDB') ORDER BY owner"
        )
        schemas = [r[0] for r in cursor.fetchall()]

        params = {}
        owner_filter = ""
        if owner:
            owner_filter = "AND d.owner = :owner"
            params["owner"] = owner.upper()

        min_filter = "AND d.blocks * 8 / 1024 > :min_mb"
        params["min_mb"] = min_size_mb

        sql = (
            "SELECT d.owner, "
            "  d.table_name, "
            "  ROUND((d.blocks * 8) / 1024, 2) AS allocated_mb, "
            "  ROUND(NVL(d.num_rows, 0) * NVL(d.avg_row_len, 0) / 1024 / 1024, 2) AS used_mb, "
            "  ROUND((d.blocks * 10 / 100) * 8 / 1024, 2) AS reserved_mb, "
            "  ROUND((d.blocks * 8 - NVL(d.num_rows * d.avg_row_len / 1024, 0) - d.blocks * 8 * 10 / 100) / 1024, 2) AS waste_mb, "
            "  CASE WHEN d.blocks * 8 / 1024 > 0 "
            "    THEN ROUND((d.blocks * 8 - NVL(d.num_rows * d.avg_row_len / 1024, 0) - d.blocks * 8 * 10 / 100) / (d.blocks * 8) * 100, 2) "
            "    ELSE 0 END AS waste_pct, "
            "  d.num_rows, "
            "  d.blocks, "
            "  d.avg_row_len, "
            "  d.last_analyzed "
            "FROM dba_tables d "
            f"WHERE d.blocks IS NOT NULL AND d.blocks > 0 {owner_filter} {min_filter} "
            "ORDER BY waste_mb DESC"
        )
        cursor.execute(sql, params)
        tables = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        tables = tables[:limit]

    total_allocated = sum(float(t.get("allocated_mb") or 0) for t in tables)
    total_used = sum(float(t.get("used_mb") or 0) for t in tables)
    total_waste = sum(float(t.get("waste_mb") or 0) for t in tables)
    total_reserved = sum(float(t.get("reserved_mb") or 0) for t in tables)

    return {
        "schemas": schemas,
        "tables": tables,
        "summary": {
            "total_tables": len(tables),
            "total_allocated_mb": round(total_allocated, 2),
            "total_used_mb": round(total_used, 2),
            "total_reserved_mb": round(total_reserved, 2),
            "total_waste_mb": round(total_waste, 2),
            "waste_ratio_pct": round(total_waste / total_allocated * 100, 2) if total_allocated > 0 else 0,
        },
    }


def generate_shrink_script(instance: DBInstance, owner: str, table_name: str) -> dict:
    logger.info("生成碎片整理脚本 | instance=%s | %s.%s", instance.instance_name, owner, table_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tablespace_name, blocks, num_rows, avg_row_len, "
            "  ROUND((blocks * 8) / 1024, 2) AS allocated_mb "
            "FROM dba_tables WHERE owner = :1 AND table_name = :2",
            [owner.upper(), table_name.upper()],
        )
        rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        if not rows:
            return {"status": "error", "message": f"表 {owner}.{table_name} 不存在"}

        t = rows[0]
        ts = t.get("tablespace_name", "USERS")
        allocated = t.get("allocated_mb", 0)

    sql = (
        f"-- 碎片整理: {owner}.{table_name}\n"
        f"-- 当前分配: {allocated} MB, 表空间: {ts}\n"
        f"-- 注意: ALTER TABLE ... SHRINK 需要启用 row movement\n\n"
        f"ALTER TABLE {owner}.\"{table_name}\" ENABLE ROW MOVEMENT;\n"
        f"ALTER TABLE {owner}.\"{table_name}\" SHRINK SPACE;\n"
        f"ALTER TABLE {owner}.\"{table_name}\" DISABLE ROW MOVEMENT;\n\n"
        f"-- 收集统计信息\n"
        f"EXEC DBMS_STATS.GATHER_TABLE_STATS('{owner}', '{table_name}');\n"
    )

    return {
        "status": "plan_only",
        "message": f"已生成 {owner}.{table_name} 碎片整理脚本",
        "sql": sql,
        "owner": owner,
        "table_name": table_name,
        "tablespace_name": ts,
        "allocated_mb": allocated,
        "warnings": [
            "碎片整理期间会产生锁，请在低峰期执行。",
            "SHRINK SPACE 会移动行数据，可能导致索引失效，建议执行后重建索引。",
            "请确认表已启用 ROW MOVEMENT。",
        ],
    }


def get_index_fragmentation(instance: DBInstance, owner: str) -> dict:
    logger.info("索引碎片分析 | instance=%s | owner=%s", instance.instance_name, owner)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT table_name FROM ("
            "  SELECT t.table_name, COUNT(*) idx_count "
            "  FROM dba_indexes i, dba_tables t "
            "  WHERE i.owner = :1 AND t.owner = i.owner AND i.table_name = t.table_name "
            "  AND t.num_rows > 0 AND t.blocks > 0 "
            "  GROUP BY t.table_name "
            "  ORDER BY idx_count DESC"
            ") WHERE ROWNUM <= 50",
            [owner.upper()],
        )
        tables = [r[0] for r in cursor.fetchall()]

        all_indexes = []
        for table_name in tables:
            sql = (
                "SELECT c.index_name, "
                "  ROUND(c.nmb, 2) AS expected_mb, "
                "  ROUND(NVL(d.smb, 0), 2) AS actual_mb, "
                "  CASE WHEN c.nmb > 0 THEN ROUND(NVL(d.smb, 0) / c.nmb * 100, 2) ELSE 0 END AS frag_pct "
                "FROM ("
                "  SELECT index_name, "
                "    ROUND((SELECT num_rows FROM dba_tables WHERE table_name = UPPER(:tname) AND owner = UPPER(:owner)) "
                "      / ((8192 - 819.2 - 4 - 20 - 72 - 32) / (SUM(AVG_COL_LEN) + 2 + 18)) * 8192 / 1024 / 1024) AS nmb "
                "  FROM ("
                "    SELECT b.index_name, a.AVG_COL_LEN "
                "    FROM dba_tab_columns a, "
                "      (SELECT index_name, column_name FROM dba_ind_columns "
                "       WHERE table_name = UPPER(:tname) AND index_owner = UPPER(:owner)) b "
                "    WHERE a.TABLE_NAME = UPPER(:tname) AND a.OWNER = UPPER(:owner) "
                "    AND a.column_name = b.column_name"
                "  ) GROUP BY index_name"
                ") c, ("
                "  SELECT segment_name, ROUND(SUM(bytes) / 1024 / 1024, 2) AS smb "
                "  FROM dba_segments WHERE owner = UPPER(:owner) GROUP BY segment_name"
                ") d "
                "WHERE c.index_name = d.segment_name(+)"
            )
            cursor.execute(sql, {"tname": table_name, "owner": owner.upper()})
            rows = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
            for r in rows:
                r["table_name"] = table_name
                r["owner"] = owner.upper()
                actual = float(r.get("actual_mb") or 0)
                expected = float(r.get("expected_mb") or 0)
                r["waste_mb"] = round(max(actual - expected, 0), 2)
            all_indexes.extend(rows)

    all_indexes.sort(key=lambda x: float(x.get("waste_mb") or 0), reverse=True)

    total_actual = sum(float(i.get("actual_mb") or 0) for i in all_indexes)
    total_expected = sum(float(i.get("expected_mb") or 0) for i in all_indexes)
    total_waste = sum(float(i.get("waste_mb") or 0) for i in all_indexes)

    return {
        "indexes": all_indexes,
        "summary": {
            "total_indexes": len(all_indexes),
            "total_tables_analyzed": len(tables),
            "total_actual_mb": round(total_actual, 2),
            "total_expected_mb": round(total_expected, 2),
            "total_waste_mb": round(total_waste, 2),
        },
    }


def generate_rebuild_index_script(instance: DBInstance, owner: str, index_name: str) -> dict:
    logger.info("生成索引重建脚本 | %s.%s", owner, index_name)
    return {
        "status": "plan_only",
        "message": f"已生成索引 {owner}.{index_name} 重建脚本",
        "sql": (
            f"-- 重建索引: {owner}.{index_name}\n"
            f"ALTER INDEX {owner}.\"{index_name}\" REBUILD ONLINE;\n\n"
            f"-- 收集统计信息\n"
            f"EXEC DBMS_STATS.GATHER_INDEX_STATS('{owner}', '{index_name}');\n"
        ),
        "warnings": [
            "ONLINE 重建允许并发 DML，但仍会消耗大量资源，请在低峰期执行。",
            "重建期间需要额外磁盘空间（约等于索引大小）。",
        ],
    }


def get_unused_indexes(instance: DBInstance, owner: str, days: int = 90) -> dict:
    logger.info("未使用索引分析 | instance=%s | owner=%s | days=%d", instance.instance_name, owner, days)
    owner_upper = owner.upper()
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()

        try:
            sql = (
                "SELECT i.owner, "
                "  i.table_name, "
                "  i.index_name, "
                "  i.index_type, "
                "  i.uniqueness, "
                "  i.status, "
                "  i.visibility, "
                "  i.num_rows, "
                "  i.leaf_blocks, "
                "  i.distinct_keys, "
                "  i.clustering_factor, "
                "  i.last_analyzed "
                "FROM dba_indexes i "
                "LEFT JOIN ( "
                "  SELECT DISTINCT object_owner, object_name "
                "  FROM dba_hist_sql_plan "
                "  WHERE object_type = 'INDEX' "
                "    AND timestamp > SYSDATE - :1 "
                ") p ON i.owner = p.object_owner "
                "   AND i.index_name = p.object_name "
                "LEFT JOIN dba_constraints c ON i.owner = c.owner "
                "   AND i.index_name = c.index_name "
                "WHERE i.owner = :2 "
                "  AND p.object_name IS NULL "
                "  AND i.index_type NOT IN ('LOB', 'DOMAIN') "
                "  AND c.index_name IS NULL "
                "ORDER BY i.leaf_blocks DESC, i.table_name, i.index_name"
            )
            cursor.execute(sql, [days, owner_upper])
        except Exception as e:
            logger.warning("dba_hist_sql_plan 查询失败，回退到 v$sql_plan | error=%s", e)
            sql = (
                "SELECT i.owner, "
                "  i.table_name, "
                "  i.index_name, "
                "  i.index_type, "
                "  i.uniqueness, "
                "  i.status, "
                "  i.visibility, "
                "  i.num_rows, "
                "  i.leaf_blocks, "
                "  i.distinct_keys, "
                "  i.clustering_factor, "
                "  i.last_analyzed "
                "FROM dba_indexes i "
                "LEFT JOIN ( "
                "  SELECT DISTINCT object_owner, object_name "
                "  FROM v$sql_plan "
                "  WHERE object_type = 'INDEX' "
                ") p ON i.owner = p.object_owner "
                "   AND i.index_name = p.object_name "
                "LEFT JOIN dba_constraints c ON i.owner = c.owner "
                "   AND i.index_name = c.index_name "
                "WHERE i.owner = :1 "
                "  AND p.object_name IS NULL "
                "  AND i.index_type NOT IN ('LOB', 'DOMAIN') "
                "  AND c.index_name IS NULL "
                "ORDER BY i.leaf_blocks DESC, i.table_name, i.index_name"
            )
            cursor.execute(sql, [owner_upper])

        indexes = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        total_index_mb = 0
        unused_size_mb = 0
        try:
            cursor.execute(
                "SELECT NVL(SUM(bytes) / 1024 / 1024, 0) "
                "FROM dba_segments WHERE owner = :1 AND segment_type = 'INDEX'",
                [owner_upper],
            )
            total_index_mb = round(float(cursor.fetchone()[0]), 2)
        except Exception:
            pass

        if indexes:
            try:
                names = [idx["index_name"] for idx in indexes]
                placeholders = ",".join([":" + str(i + 1) for i in range(len(names))])
                cursor.execute(
                    f"SELECT NVL(SUM(bytes) / 1024 / 1024, 0) FROM dba_segments "
                    f"WHERE owner = :{len(names) + 1} AND segment_type = 'INDEX' "
                    f"AND segment_name IN ({placeholders})",
                    names + [owner_upper],
                )
                unused_size_mb = round(float(cursor.fetchone()[0]), 2)
            except Exception:
                pass

    return {
        "indexes": indexes,
        "summary": {
            "total_unused": len(indexes),
            "total_index_mb": total_index_mb,
            "unused_size_mb": unused_size_mb,
            "days": days,
            "owner": owner_upper,
        },
    }


def generate_drop_index_script(instance: DBInstance, owner: str, index_name: str) -> dict:
    logger.info("生成删除索引脚本 | %s.%s", owner, index_name)
    return {
        "status": "plan_only",
        "message": f"已生成删除索引 {owner}.{index_name} 的脚本",
        "sql": (
            f"-- 删除未使用索引: {owner}.{index_name}\n"
            f"-- 请确认该索引确实未被使用后再执行删除\n"
            f"-- 建议先设置为 INVISIBLE 观察，确认无影响后再删除\n\n"
            f"-- Step 1: 设置为不可见（观察期）\n"
            f"ALTER INDEX {owner}.\"{index_name}\" INVISIBLE;\n\n"
            f"-- Step 2: 确认无影响后删除\n"
            f"-- DROP INDEX {owner}.\"{index_name}\";\n"
        ),
        "warnings": [
            "请先设置 INVISIBLE 观察一段时间，确认没有性能影响。",
            "删除前请确认该索引不是约束（主键/唯一键）所使用的索引。",
            "建议在删除前备份索引创建脚本。",
        ],
    }


def find_similar_tables(instance: DBInstance, owner: str, pattern: str, limit: int = 50) -> dict:
    logger.info("查找相似表 | instance=%s | owner=%s | pattern=%s", instance.instance_name, owner, pattern)
    owner_upper = owner.upper()
    like_pattern = f"%{pattern}%"
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT table_name, num_rows, blocks, last_analyzed "
            "FROM dba_tables WHERE owner = :1 AND table_name LIKE :2 "
            "ORDER BY table_name",
            [owner_upper, like_pattern],
        )
        tables = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))[:limit]

        for t in tables:
            tname = t["table_name"]
            cursor.execute(
                "SELECT column_name, data_type, data_length, data_precision, data_scale, "
                "  nullable, data_default, column_id "
                "FROM dba_tab_columns WHERE owner = :1 AND table_name = :2 "
                "ORDER BY column_id",
                [owner_upper, tname],
            )
            t["columns"] = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

    return {"tables": tables, "total": len(tables), "owner": owner_upper, "pattern": pattern}


def generate_batch_alter_sql(
    instance: DBInstance,
    owner: str,
    table_names: list[str],
    action: str,
    column_name: str,
    data_type: str,
    data_length: int | None = None,
    data_precision: int | None = None,
    data_scale: int | None = None,
    nullable: bool = True,
    default_value: str | None = None,
) -> dict:
    logger.info("生成批量ALTER脚本 | owner=%s | tables=%d | action=%s | col=%s", owner, len(table_names), action, column_name)
    owner_upper = owner.upper()
    type_str = data_type.upper()
    if data_type.upper() in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW") and data_length:
        type_str = f"{data_type.upper()}({data_length})"
    elif data_type.upper() in ("NUMBER", "FLOAT") and data_precision is not None:
        if data_scale is not None:
            type_str = f"{data_type.upper()}({data_precision},{data_scale})"
        else:
            type_str = f"{data_type.upper()}({data_precision})"

    lines = [f"-- 批量{action}字段脚本", f"-- Schema: {owner_upper}", f"-- 字段: {column_name}", f"-- 类型: {type_str}", f"-- 涉及表: {len(table_names)} 个", ""]

    for tname in table_names:
        tn = tname.strip().upper()
        if action == "ADD":
            null_str = "" if nullable else " NOT NULL"
            def_str = f" DEFAULT {default_value}" if default_value else ""
            lines.append(f"ALTER TABLE {owner_upper}.\"{tn}\" ADD ({column_name} {type_str}{def_str}{null_str});")
        elif action == "MODIFY":
            null_str = "" if nullable else " NOT NULL"
            lines.append(f"ALTER TABLE {owner_upper}.\"{tn}\" MODIFY ({column_name} {type_str}{null_str});")
        elif action == "DROP":
            lines.append(f"ALTER TABLE {owner_upper}.\"{tn}\" DROP COLUMN {column_name};")

    lines.append("")
    lines.append(f"-- 共生成 {len(table_names)} 条 SQL")

    return {
        "status": "plan_only",
        "message": f"已生成批量{action}字段脚本，共 {len(table_names)} 张表",
        "sql": "\n".join(lines),
        "table_count": len(table_names),
        "warnings": [
            "请仔细检查 SQL 再执行，建议先在测试环境验证。",
            "ADD 字段时如果有 NOT NULL 约束，需确保表为空或提供 DEFAULT 值。",
            "MODIFY 字段类型可能导致数据截断或转换错误。",
            "DROP COLUMN 不可逆，请确保已备份。",
        ],
    }


def generate_batch_create_index_sql(
    owner: str,
    table_names: list[str],
    index_suffix: str,
    columns: str,
    tablespace: str | None = None,
    unique: bool = False,
    parallel: int | None = None,
    online: bool = False,
) -> dict:
    logger.info("生成批量创建索引脚本 | owner=%s | tables=%d | cols=%s", owner, len(table_names), columns)
    owner_upper = owner.upper()
    col_list = [c.strip().upper() for c in columns.split(",") if c.strip()]
    if not col_list:
        return {"status": "error", "message": "未指定索引列", "sql": "", "table_count": 0, "warnings": []}

    col_suffix = "_".join(col_list)
    raw_name = f"IDX_{col_suffix}_{index_suffix}"
    idx_name = raw_name[:30]

    lines = [
        f"-- 批量创建索引脚本",
        f"-- Schema: {owner_upper}",
        f"-- 索引列: {', '.join(col_list)}",
        f"-- 索引名模式: {idx_name}",
        f"-- 唯一: {'是' if unique else '否'}",
        f"-- 涉及表: {len(table_names)} 个",
        "",
    ]

    unique_kw = "UNIQUE " if unique else ""
    online_kw = " ONLINE" if online else ""
    parallel_clause = f" PARALLEL {parallel}" if parallel else ""
    tablespace_clause = f" TABLESPACE {tablespace.upper()}" if tablespace else ""

    for tname in table_names:
        tn = tname.strip().upper()
        full_idx = f"{idx_name}_{tn[-6:]}" if len(table_names) > 1 else idx_name
        full_idx = full_idx[:30]
        lines.append(
            f"CREATE {unique_kw}INDEX {owner_upper}.\"{full_idx}\" ON {owner_upper}.\"{tn}\" ({', '.join(col_list)}){tablespace_clause}{online_kw}{parallel_clause};"
        )

    lines.append("")
    lines.append(f"-- 共生成 {len(table_names)} 条 SQL")

    return {
        "status": "plan_only",
        "message": f"已生成批量创建索引脚本，共 {len(table_names)} 张表",
        "sql": "\n".join(lines),
        "table_count": len(table_names),
        "warnings": [
            "创建索引会锁表（除非使用 ONLINE），请在低峰期执行。",
            "ONLINE 创建允许并发 DML，但仍会消耗大量资源。",
            "请确保索引名不超过 30 字节（Oracle 限制）。",
            "建议先检查这些列上是否已存在索引，避免重复创建。",
        ],
    }
