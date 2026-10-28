import logging
import re
from datetime import datetime
from pathlib import PurePosixPath

from dateutil.relativedelta import relativedelta

logger = logging.getLogger("app.services.oracle_archive_file")

SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
SAFE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./:\\-\\\\]+$")


def _check_safe(name: str, field: str):
    if not name or not SAFE_NAME_PATTERN.match(str(name)):
        raise ValueError(f"{field} 非法: {name}")


def _check_path(path: str):
    if not path or not SAFE_PATH_PATTERN.match(str(path)):
        raise ValueError(f"directory_path 非法: {path}")


def _upper(payload: dict, key: str, default: str | None = None) -> str:
    value = payload.get(key) or default
    if value is None:
        return ""
    return str(value).upper()


def _prefix(payload: dict, owner: str, table_name: str) -> str:
    prefix = payload.get("file_prefix") or f"{owner}_{table_name}"
    prefix = str(prefix).upper()
    if not re.match(r"^[A-Za-z0-9_$#-]+$", prefix):
        raise ValueError(f"file_prefix 非法: {prefix}")
    return prefix


def _file_path(payload: dict, file_name: str) -> str:
    directory_path = payload.get("directory_path") or "/backup/archive"
    _check_path(directory_path)
    return str(PurePosixPath(str(directory_path)) / file_name)


def _date_literal(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except Exception as exc:
        raise ValueError(f"日期格式必须为 YYYY-MM-DD: {value}") from exc
    return value


def _base_context(payload: dict) -> dict:
    owner = _upper(payload, "owner")
    table_name = _upper(payload, "table_name")
    archive_column = _upper(payload, "archive_column")
    directory_name = _upper(payload, "directory_name", "ARCHIVE_DIR")
    for value, field in [
        (owner, "owner"),
        (table_name, "table_name"),
        (archive_column, "archive_column"),
        (directory_name, "directory_name"),
    ]:
        _check_safe(value, field)
    return {
        "owner": owner,
        "table_name": table_name,
        "archive_column": archive_column,
        "directory_name": directory_name,
        "prefix": _prefix(payload, owner, table_name),
    }


def build_archive_condition(payload: dict) -> dict:
    ctx = _base_context(payload)
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    retention_months = payload.get("retention_months")
    col = ctx["archive_column"]

    if start_date or end_date:
        if not start_date or not end_date:
            raise ValueError("开始日期和结束日期必须同时填写")
        start_date = _date_literal(start_date)
        end_date = _date_literal(end_date)
        if datetime.strptime(start_date, "%Y-%m-%d") >= datetime.strptime(end_date, "%Y-%m-%d"):
            raise ValueError("开始日期必须早于结束日期")
        condition = f"{col} >= DATE '{start_date}' AND {col} < DATE '{end_date}'"
        label = f"{start_date.replace('-', '')}_{end_date.replace('-', '')}"
        return {**ctx, "condition": condition, "label": label, "start_date": start_date, "end_date": end_date}

    if not retention_months:
        raise ValueError("请填写起止日期，或填写保留月份")

    condition = f"{col} < ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -{int(retention_months)})"
    label = f"before_{int(retention_months)}m"
    return {**ctx, "condition": condition, "label": label, "start_date": None, "end_date": None}


def _monthly_ranges(payload: dict) -> list[dict]:
    condition = build_archive_condition(payload)
    if not payload.get("split_by_month") or not condition["start_date"] or not condition["end_date"]:
        return [condition]

    start = datetime.strptime(condition["start_date"], "%Y-%m-%d")
    end = datetime.strptime(condition["end_date"], "%Y-%m-%d")
    cur = start
    ranges = []
    while cur < end:
        next_dt = min(cur + relativedelta(months=1), end)
        item_payload = {
            **payload,
            "start_date": cur.strftime("%Y-%m-%d"),
            "end_date": next_dt.strftime("%Y-%m-%d"),
            "split_by_month": False,
        }
        ranges.append(build_archive_condition(item_payload))
        cur = next_dt
    return ranges


def _select_sql(ctx: dict) -> str:
    return f"""SELECT *
FROM {ctx['owner']}.{ctx['table_name']}
WHERE {ctx['condition']};"""


def build_csv_sqlcl_plan(payload: dict) -> dict:
    sections = []
    commands = []
    for ctx in _monthly_ranges(payload):
        csv_file = _file_path(payload, f"{ctx['prefix']}_{ctx['label']}.csv")
        script_name = f"export_{ctx['prefix']}_{ctx['label']}_sqlcl.sql"
        script = f"""SET SQLFORMAT csv
SET FEEDBACK OFF
SET HEADING ON
SPOOL {csv_file}

{_select_sql(ctx)}

SPOOL OFF
EXIT;"""
        command = f'sql {ctx["owner"]}/"<PASSWORD>"@TNS_ALIAS @{script_name}'
        if payload.get("compress"):
            command += f"\ngzip -f {csv_file}"
        sections.append({"title": f"SQLcl CSV 导出 {ctx['label']}", "script": script, "command": command})
        commands.append(command)
    return {
        "status": "plan_only",
        "message": "已生成 SQLcl CSV 归档脚本。",
        "sections": sections,
        "script": "\n\n".join(section["script"] for section in sections),
        "command": "\n\n".join(commands),
        "warnings": ["SQLcl 的 CSV 格式对逗号、引号、换行处理更稳，推荐优先使用。"],
    }


def build_csv_sqlplus_plan(payload: dict) -> dict:
    sections = []
    commands = []
    separator = payload.get("separator") or ","
    for ctx in _monthly_ranges(payload):
        csv_file = _file_path(payload, f"{ctx['prefix']}_{ctx['label']}.csv")
        script_name = f"export_{ctx['prefix']}_{ctx['label']}_sqlplus.sql"
        script = f"""SET ECHO OFF
SET FEEDBACK OFF
SET HEADING ON
SET PAGESIZE 0
SET LINESIZE 32767
SET TRIMSPOOL ON
SET TERMOUT OFF
SET COLSEP '{separator}'

SPOOL {csv_file}

{_select_sql(ctx)}

SPOOL OFF
EXIT;"""
        command = f'sqlplus {ctx["owner"]}/"<PASSWORD>"@TNS_ALIAS @{script_name}'
        if payload.get("compress"):
            command += f"\ngzip -f {csv_file}"
        sections.append({"title": f"SQL*Plus CSV 导出 {ctx['label']}", "script": script, "command": command})
        commands.append(command)
    return {
        "status": "plan_only",
        "message": "已生成 SQL*Plus CSV 归档脚本。",
        "sections": sections,
        "script": "\n\n".join(section["script"] for section in sections),
        "command": "\n\n".join(commands),
        "warnings": ["SQL*Plus 处理字段内逗号、换行、引号不如 SQLcl/Python 稳定。"],
    }


def build_csv_python_plan(payload: dict) -> dict:
    sections = []
    commands = []
    encoding = payload.get("encoding") or "UTF-8-SIG"
    separator = payload.get("separator") or ","
    batch_size = int(payload.get("batch_size") or 10000)
    for ctx in _monthly_ranges(payload):
        csv_file = _file_path(payload, f"{ctx['prefix']}_{ctx['label']}.csv")
        script_name = f"export_{ctx['prefix']}_{ctx['label']}.py"
        script = f'''import csv
import oracledb

conn = oracledb.connect(
    user="{ctx["owner"]}",
    password="<PASSWORD>",
    dsn="HOST:PORT/SERVICE_NAME"
)

sql = """
SELECT *
FROM {ctx["owner"]}.{ctx["table_name"]}
WHERE {ctx["condition"]}
"""

cursor = conn.cursor()
cursor.execute(sql)
columns = [d[0] for d in cursor.description]

with open(r"{csv_file}", "w", newline="", encoding="{encoding}") as f:
    writer = csv.writer(f, delimiter="{separator}")
    writer.writerow(columns)
    while True:
        rows = cursor.fetchmany({batch_size})
        if not rows:
            break
        writer.writerows(rows)

cursor.close()
conn.close()
print("Export complete: {csv_file}")
'''
        command = f"python {script_name}"
        if payload.get("compress"):
            command += f"\ngzip -f {csv_file}"
        sections.append({"title": f"Python CSV 导出 {ctx['label']}", "script": script, "command": command})
        commands.append(command)
    return {
        "status": "plan_only",
        "message": "已生成 Python CSV 归档脚本。",
        "sections": sections,
        "script": "\n\n".join(section["script"] for section in sections),
        "command": "\n\n".join(commands),
        "warnings": ["CSV 默认建议使用 UTF-8-SIG，便于 Excel 打开中文。"],
    }


def build_directory_plan(payload: dict) -> dict:
    directory_name = _upper(payload, "directory_name", "ARCHIVE_DIR")
    directory_path = payload.get("directory_path") or "/backup/archive"
    grant_to = _upper(payload, "grant_to", None) or _upper(payload, "owner", None)
    _check_safe(directory_name, "directory_name")
    _check_path(directory_path)
    if grant_to:
        _check_safe(grant_to, "grant_to")

    check_sql = f"""SELECT directory_name,
       directory_path
FROM dba_directories
WHERE directory_name = '{directory_name}';"""
    script = f"CREATE OR REPLACE DIRECTORY {directory_name} AS '{directory_path}';"
    if grant_to:
        script += f"\nGRANT READ, WRITE ON DIRECTORY {directory_name} TO {grant_to};"

    return {
        "status": "plan_only",
        "message": "已生成 Directory 检查和创建脚本。",
        "check_sql": check_sql,
        "script": script,
        "sections": [{"title": "Directory 检查/创建", "script": check_sql + "\n\n" + script}],
        "warnings": ["请确认数据库服务器上的 OS 路径已存在，并由 Oracle 用户可读写。"],
    }


def build_expdp_plan(payload: dict) -> dict:
    sections = []
    commands = []
    parallel = int(payload.get("parallel") or payload.get("parallel_degree") or 4)
    compression = (payload.get("compression") or "ALL").upper()
    for ctx in _monthly_ranges(payload):
        dumpfile = f"{ctx['prefix']}_{ctx['label']}_%U.dmp"
        logfile = f"{ctx['prefix']}_{ctx['label']}_expdp.log"
        query = f"{ctx['owner']}.{ctx['table_name']}:\\\"WHERE {ctx['condition']}\\\""
        command = f"""expdp {ctx['owner']}/"<PASSWORD>"@TNS_ALIAS \\
  directory={ctx['directory_name']} \\
  dumpfile={dumpfile} \\
  logfile={logfile} \\
  tables={ctx['owner']}.{ctx['table_name']} \\
  query={query} \\
  compression={compression} \\
  parallel={parallel}"""
        oneline = command.replace("\\\n  ", " ")
        sections.append({"title": f"EXPDP 文件归档 {ctx['label']}", "script": oneline, "command": command})
        commands.append(command)

    result = {
        "status": "plan_only",
        "message": "已生成 EXPDP 文件归档命令。",
        "sections": sections,
        "command": "\n\n".join(commands),
        "command_oneline": "\n\n".join(section["script"] for section in sections),
        "warnings": ["密码使用 <PASSWORD> 占位，执行前人工替换。", "大表建议低峰期导出并限制 parallel。"],
    }
    if payload.get("generate_impdp", True):
        impdp = build_impdp_plan(payload)
        result["impdp_command"] = impdp["command"]
        result["impdp_command_oneline"] = impdp["command_oneline"]
    return result


def build_impdp_plan(payload: dict) -> dict:
    sections = []
    commands = []
    remap_schema = _upper(payload, "remap_schema", None)
    remap_table = _upper(payload, "remap_table", None)
    for ctx in _monthly_ranges(payload):
        target_user = remap_schema or "HIS_DATA"
        dumpfile = f"{ctx['prefix']}_{ctx['label']}_%U.dmp"
        logfile = f"{ctx['prefix']}_{ctx['label']}_impdp.log"
        command = f"""impdp {target_user}/"<PASSWORD>"@HISDB_TNS \\
  directory={ctx['directory_name']} \\
  dumpfile={dumpfile} \\
  logfile={logfile}"""
        if remap_schema:
            command += f" \\\n  remap_schema={ctx['owner']}:{remap_schema}"
        if remap_table:
            command += f" \\\n  remap_table={ctx['table_name']}:{remap_table}"
        command += " \\\n  table_exists_action=append"
        oneline = command.replace("\\\n  ", " ")
        sections.append({"title": f"IMPDP 恢复命令 {ctx['label']}", "script": oneline, "command": command})
        commands.append(command)

    return {
        "status": "plan_only",
        "message": "已生成 IMPDP 恢复命令。",
        "sections": sections,
        "command": "\n\n".join(commands),
        "command_oneline": "\n\n".join(section["script"] for section in sections),
        "warnings": ["恢复命令默认 table_exists_action=append，请确认目标表结构和重复数据策略。"],
    }


def _pk_columns(payload: dict) -> list[str]:
    raw = payload.get("primary_key_columns")
    if not raw:
        return []
    cols = [item.strip().upper() for item in str(raw).split(",") if item.strip()]
    for col in cols:
        _check_safe(col, "primary_key_columns")
    return cols


def build_archive_file_verify_plan(payload: dict) -> dict:
    sections = []
    pk_cols = _pk_columns(payload)
    for ctx in _monthly_ranges(payload):
        count_sql = f"""SELECT COUNT(*) AS archive_count
FROM {ctx['owner']}.{ctx['table_name']}
WHERE {ctx['condition']};"""
        range_sql = f"""SELECT MIN({ctx['archive_column']}) AS min_archive_time,
       MAX({ctx['archive_column']}) AS max_archive_time
FROM {ctx['owner']}.{ctx['table_name']}
WHERE {ctx['condition']};"""
        scripts = [count_sql, range_sql]
        if pk_cols:
            cols = ", ".join(pk_cols)
            scripts.append(f"""SELECT {cols},
       COUNT(*) AS duplicate_count
FROM {ctx['owner']}.{ctx['table_name']}
WHERE {ctx['condition']}
GROUP BY {cols}
HAVING COUNT(*) > 1;""")
        sections.append({"title": f"归档校验 SQL {ctx['label']}", "script": "\n\n".join(scripts)})

    return {
        "status": "plan_only",
        "message": "已生成归档校验 SQL。",
        "sections": sections,
        "count_sql": sections[0]["script"] if sections else "",
        "warnings": ["请将导出文件行数、目标库导入行数与源库 COUNT 结果交叉校验。"],
    }


def build_cleanup_plan(payload: dict) -> dict:
    sections = []
    batch_size = int(payload.get("cleanup_batch_size") or 10000)
    for ctx in _monthly_ranges(payload):
        script = f"""DECLARE
  v_rows NUMBER;
BEGIN
  LOOP
    DELETE FROM {ctx['owner']}.{ctx['table_name']}
    WHERE {ctx['condition']}
      AND ROWNUM <= {batch_size};

    v_rows := SQL%ROWCOUNT;
    COMMIT;
    EXIT WHEN v_rows = 0;
  END LOOP;
END;
/"""
        sections.append({
            "title": f"生产库清理 SQL {ctx['label']}",
            "script": script,
            "warning": "该脚本会删除生产数据，必须在归档与恢复校验通过后执行。",
        })
    return {
        "status": "plan_only",
        "message": "已生成生产库清理 SQL。",
        "sections": sections,
        "script": "\n\n".join(section["script"] for section in sections),
        "warnings": ["生产清理默认按批 DELETE + COMMIT。分区表优先考虑 DROP/TRUNCATE PARTITION。"],
    }


def build_full_archive_file_plan(payload: dict) -> dict:
    sections = []
    warnings = [
        "所有内容只生成脚本或命令，不会自动执行。",
        "EXPDP/IMPDP 密码统一使用 <PASSWORD> 占位。",
        "生产清理必须在导出、恢复和校验通过后人工执行。",
    ]
    file_type = (payload.get("file_type") or "EXPDP").upper()

    if payload.get("generate_directory", True):
        directory = build_directory_plan(payload)
        sections.extend(directory["sections"])
        warnings.extend(directory.get("warnings", []))

    if file_type in ("CSV", "BOTH"):
        method = (payload.get("csv_method") or "SQLCL").upper()
        if method == "SQLPLUS":
            csv_plan = build_csv_sqlplus_plan(payload)
        elif method == "PYTHON":
            csv_plan = build_csv_python_plan(payload)
        else:
            csv_plan = build_csv_sqlcl_plan(payload)
        sections.extend(csv_plan["sections"])
        warnings.extend(csv_plan.get("warnings", []))

    if file_type in ("EXPDP", "BOTH"):
        expdp_plan = build_expdp_plan(payload)
        sections.extend(expdp_plan["sections"])
        warnings.extend(expdp_plan.get("warnings", []))
        if payload.get("generate_impdp", True):
            impdp_plan = build_impdp_plan(payload)
            sections.extend(impdp_plan["sections"])
            warnings.extend(impdp_plan.get("warnings", []))

    if payload.get("generate_verify", True):
        verify = build_archive_file_verify_plan(payload)
        sections.extend(verify["sections"])
        warnings.extend(verify.get("warnings", []))

    if payload.get("generate_cleanup", False):
        cleanup = build_cleanup_plan(payload)
        sections.extend(cleanup["sections"])
        warnings.extend(cleanup.get("warnings", []))

    return {
        "status": "plan_only",
        "message": "已生成完整历史归档方案，请人工确认后执行。",
        "sections": sections,
        "warnings": list(dict.fromkeys(warnings)),
    }
