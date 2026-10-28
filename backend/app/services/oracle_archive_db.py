import logging
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta

logger = logging.getLogger("app.services.oracle_archive_db")

SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def _check_safe(name: str, field: str):
    if not name or not SAFE_NAME_PATTERN.match(name):
        raise ValueError(f"{field} invalid: {name}")


def _resolve_target_table(payload: dict) -> str:
    return payload.get("target_table") or f"{payload['source_table'].upper()}_HIS"


def _build_archive_condition(payload: dict) -> str:
    col = payload["archive_column"]
    retention = payload.get("retention_months", 6)
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")

    if start_date and end_date:
        return f"{col} >= DATE '{start_date}' AND {col} < DATE '{end_date}'"

    return f"{col} < ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -{retention})"


def _build_monthly_conditions(payload: dict) -> list:
    col = payload["archive_column"]
    retention = payload.get("retention_months", 6)
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")

    if start_date and end_date:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
    else:
        e = datetime.now() - relativedelta(months=retention)
        e = e.replace(day=1)
        s = e - relativedelta(months=1)

    conditions = []
    cur = s
    while cur < e:
        next_month = cur + relativedelta(months=1)
        conditions.append({
            "label": cur.strftime("%Y-%m"),
            "where": f"{col} >= DATE '{cur.strftime('%Y-%m-%d')}' AND {col} < DATE '{next_month.strftime('%Y-%m-%d')}'",
        })
        cur = next_month
    return conditions


def build_db_link_plan(payload: dict) -> dict:
    link_name = payload.get("db_link_name", "PROD_MES_LINK").upper()
    source_owner = payload["source_owner"].upper()
    _check_safe(link_name, "db_link_name")
    _check_safe(source_owner, "source_owner")

    sql = f"""CREATE DATABASE LINK {link_name}
CONNECT TO {source_owner} IDENTIFIED BY "<PROD_PASSWORD>"
USING '
  (DESCRIPTION=
    (ADDRESS=(PROTOCOL=TCP)(HOST=<PROD_HOST>)(PORT=<PROD_PORT>))
    (CONNECT_DATA=(SERVICE_NAME=<PROD_SERVICE>))
  )';"""

    return {
        "status": "plan_only",
        "message": "已生成 DB Link 创建脚本，请在历史库执行。",
        "sql": sql,
        "warnings": ["执行前请替换 <PROD_PASSWORD>、<PROD_HOST>、<PROD_PORT>、<PROD_SERVICE>。"],
    }


def build_create_history_table_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    target_owner = payload.get("target_owner", "HIS_DATA").upper()
    target_table = _resolve_target_table(payload)
    archive_col = payload["archive_column"]
    partitioned = payload.get("history_partitioned", True)
    interval = payload.get("partition_interval", "MONTH")
    tbs = payload.get("history_tablespace", "HIS_DATA_TS")
    db_link = payload.get("db_link_name", "PROD_MES_LINK").upper()

    for name, field in [(source_owner, "source_owner"), (source_table, "source_table"),
                        (target_owner, "target_owner"), (target_table, "target_table"),
                        (archive_col, "archive_column"), (tbs, "tablespace")]:
        _check_safe(name, field)

    base = f"""CREATE TABLE {target_owner}.{target_table}
TABLESPACE {tbs}"""

    if partitioned:
        if interval == "MONTH":
            interval_expr = "NUMTOYMINTERVAL(1, 'MONTH')"
        elif interval == "DAY":
            interval_expr = "NUMTODSINTERVAL(1, 'DAY')"
        else:
            interval_expr = "NUMTOYMINTERVAL(1, 'YEAR')"

        base += f"""
PARTITION BY RANGE ({archive_col})
INTERVAL ({interval_expr})
(
  PARTITION P_BEFORE_2025 VALUES LESS THAN (DATE '2025-01-01')
)"""

    base += f"""
AS
SELECT *
FROM {source_owner}.{source_table}@{db_link}
WHERE 1 = 2;"""

    return {
        "status": "plan_only",
        "message": "已生成历史表创建脚本，请在历史库执行。",
        "sql": base,
    }


def build_insert_select_cross_db_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    target_owner = payload.get("target_owner", "HIS_DATA").upper()
    target_table = _resolve_target_table(payload)
    db_link = payload.get("db_link_name", "PROD_MES_LINK").upper()
    parallel = payload.get("parallel_degree", 4)

    condition = _build_archive_condition(payload)
    monthly = _build_monthly_conditions(payload)

    if monthly:
        parts = []
        for batch in monthly:
            parts.append(f"""-- Batch: {batch['label']}
INSERT /*+ APPEND PARALLEL({parallel}) */
INTO {target_owner}.{target_table}
SELECT /*+ PARALLEL(t {parallel}) */
       *
FROM {source_owner}.{source_table}@{db_link} t
WHERE t.{batch['where']};

COMMIT;""")
        sql = "\n\n".join(parts)
    else:
        sql = f"""INSERT /*+ APPEND PARALLEL({parallel}) */
INTO {target_owner}.{target_table}
SELECT /*+ PARALLEL(t {parallel}) */
       *
FROM {source_owner}.{source_table}@{db_link} t
WHERE t.{condition};

COMMIT;"""

    return {
        "status": "plan_only",
        "message": "已生成 INSERT SELECT 跨库迁移脚本，请在历史库执行。",
        "sql": sql,
        "warnings": ["请在历史库执行，不要在生产库执行。", "脚本使用 APPEND hint 进行直接路径插入。"],
    }


def build_datapump_network_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    target_owner = payload.get("target_owner", "HIS_DATA").upper()
    target_table = _resolve_target_table(payload)
    db_link = payload.get("db_link_name", "PROD_MES_LINK").upper()
    parallel = payload.get("parallel_degree", 4)

    condition = _build_archive_condition(payload)
    condition_escaped = condition.replace('"', '\\"')

    cmd = f"""impdp {target_owner}/"<HIS_PASSWORD>"@HISDB_TNS \\
  network_link={db_link} \\
  tables={source_owner}.{source_table} \\
  remap_schema={source_owner}:{target_owner}"""
    if target_table != source_table:
        cmd += f" \\\n  remap_table={source_table}:{target_table}"
    cmd += f""" \\
  table_exists_action=append \\
  query={source_owner}.{source_table}:"{condition_escaped}" \\
  directory=DATA_PUMP_DIR \\
  logfile=imp_{source_table}_{target_table}.log \\
  parallel={parallel}"""

    cmd_oneline = cmd.replace("\\\n  ", " ")

    return {
        "status": "plan_only",
        "message": "已生成 Data Pump NETWORK_LINK 导入命令，请在历史库服务器执行。",
        "command": cmd,
        "command_oneline": cmd_oneline,
    }


def build_verify_cross_db_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    target_owner = payload.get("target_owner", "HIS_DATA").upper()
    target_table = _resolve_target_table(payload)
    archive_col = payload["archive_column"]
    db_link = payload.get("db_link_name", "PROD_MES_LINK").upper()

    condition = _build_archive_condition(payload)

    count_sql = f"""SELECT 'Source (pending archive)' AS check_item,
       COUNT(*) AS cnt
FROM {source_owner}.{source_table}@{db_link}
WHERE {condition}

UNION ALL

SELECT 'Target (archived)' AS check_item,
       COUNT(*) AS cnt
FROM {target_owner}.{target_table}
WHERE {condition};"""

    range_sql = f"""SELECT MIN({archive_col}) AS min_val,
       MAX({archive_col}) AS max_val,
       COUNT(*) AS cnt
FROM {target_owner}.{target_table}
WHERE {condition};"""

    return {
        "status": "plan_only",
        "message": "已生成跨库归档校验 SQL，请在历史库执行。",
        "count_sql": count_sql,
        "range_sql": range_sql,
    }


def build_delete_source_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    archive_col = payload["archive_column"]
    batch_size = payload.get("batch_size", 10000)

    condition = _build_archive_condition(payload)

    sql = f"""DECLARE
  v_rows NUMBER;
BEGIN
  LOOP
    DELETE FROM {source_owner}.{source_table}
    WHERE {condition}
      AND ROWNUM <= {batch_size};

    v_rows := SQL%ROWCOUNT;
    COMMIT;

    EXIT WHEN v_rows = 0;
  END LOOP;
END;
/"""

    return {
        "status": "plan_only",
        "message": "已生成生产库清理脚本，仅在归档校验通过后执行。",
        "sql": sql,
        "warnings": [
            "该脚本会删除生产库数据，必须确认归档完整后再执行。",
            "如果源表是分区表，优先评估 TRUNCATE/DROP PARTITION 方案。",
        ],
    }


def build_gather_stats_plan(payload: dict) -> dict:
    source_owner = payload["source_owner"].upper()
    source_table = payload["source_table"].upper()
    target_owner = payload.get("target_owner", "HIS_DATA").upper()
    target_table = _resolve_target_table(payload)
    parallel = payload.get("parallel_degree", 4)

    source_stats = f"""BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(
    ownname          => '{source_owner}',
    tabname          => '{source_table}',
    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,
    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',
    cascade          => TRUE,
    degree           => {parallel},
    no_invalidate    => DBMS_STATS.AUTO_INVALIDATE
  );
END;
/"""

    target_stats = f"""BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(
    ownname          => '{target_owner}',
    tabname          => '{target_table}',
    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,
    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',
    cascade          => TRUE,
    degree           => {parallel},
    no_invalidate    => DBMS_STATS.AUTO_INVALIDATE
  );
END;
/"""

    return {
        "status": "plan_only",
        "message": "已生成统计信息收集脚本。",
        "source_stats": source_stats,
        "target_stats": target_stats,
    }


def build_full_archive_plan(payload: dict) -> dict:
    sections = []

    db_link = build_db_link_plan(payload)
    sections.append({"title": "1. 创建 DB Link", "sql": db_link["sql"]})

    create_table = build_create_history_table_plan(payload)
    sections.append({"title": "2. 创建历史表", "sql": create_table["sql"]})

    if payload.get("method", "DB_LINK") == "DB_LINK":
        migrate = build_insert_select_cross_db_plan(payload)
        sections.append({"title": "3. 数据迁移 (INSERT SELECT)", "sql": migrate["sql"]})
    else:
        dp = build_datapump_network_plan(payload)
        sections.append({"title": "3. 数据迁移 (Data Pump)", "command": dp["command"]})

    if payload.get("generate_verify_script", True):
        verify = build_verify_cross_db_plan(payload)
        sections.append({"title": "4. 数据校验 SQL", "sql": verify["count_sql"] + "\n\n" + verify["range_sql"]})

    if payload.get("generate_stats_script", True):
        stats = build_gather_stats_plan(payload)
        sections.append({"title": "5. 收集统计信息", "sql": stats["target_stats"] + "\n\n" + stats["source_stats"]})

    if payload.get("generate_delete_script", True):
        delete_plan = build_delete_source_plan(payload)
        sections.append({
            "title": "6. 生产库清理（高风险）",
            "sql": delete_plan["sql"],
            "warning": "仅在归档校验通过后执行。该脚本会删除生产库数据。",
        })

    return {
        "status": "plan_only",
        "message": "已生成完整跨库历史归档方案，请逐段确认后再执行。",
        "sections": sections,
        "warnings": [
            "DB Link 和历史表创建脚本请在历史库执行。",
            "生产库清理脚本必须在归档校验通过后执行。",
            "大表建议按月分批迁移，避免长事务和生产压力。",
        ],
    }
