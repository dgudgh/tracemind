import base64
import logging
import posixpath
import re
import shlex
from datetime import datetime

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_binlog")

BINLOG_VARIABLES = [
    "log_bin",
    "log_bin_basename",
    "log_bin_index",
    "datadir",
    "binlog_format",
    "binlog_row_image",
    "binlog_row_metadata",
    "binlog_checksum",
    "sync_binlog",
    "max_binlog_size",
    "binlog_cache_size",
    "binlog_stmt_cache_size",
    "binlog_expire_logs_seconds",
    "expire_logs_days",
    "gtid_mode",
    "enforce_gtid_consistency",
    "gtid_executed",
    "gtid_purged",
    "binlog_do_db",
    "binlog_ignore_db",
    "log_slave_updates",
    "log_replica_updates",
    "binlog_transaction_compression",
    "binlog_transaction_compression_level_zstd",
    "binlog_encryption",
]

BINLOG_STATUS_KEYS = [
    "Binlog_cache_use",
    "Binlog_cache_disk_use",
    "Binlog_stmt_cache_use",
    "Binlog_stmt_cache_disk_use",
]


def _mysql_value(row: dict | None, key: str, default=""):
    if not row:
        return default
    return row.get(key) if row.get(key) is not None else row.get(key.lower(), row.get(key.upper(), default))


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _bool_on(value) -> bool:
    return str(value or "").upper() in {"ON", "1", "TRUE", "YES"}


def _format_bytes(value) -> str:
    size = _num(value)
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.2f} KB"
    return f"{int(size)} B"


def _retention_label(variables: dict) -> tuple[int, str]:
    seconds = int(_num(variables.get("binlog_expire_logs_seconds")))
    if seconds <= 0:
        days = int(_num(variables.get("expire_logs_days")))
        seconds = days * 86400
    if seconds <= 0:
        return 0, "未配置自动过期"
    days = seconds / 86400
    if days.is_integer():
        return seconds, f"{int(days)} 天 / {seconds} 秒"
    return seconds, f"{days:.1f} 天 / {seconds} 秒"


def _join_binlog_path(log_name: str, basename: str = "", datadir: str = "") -> str:
    if not log_name:
        return ""
    base = (basename or "").strip()
    data_dir = (datadir or "").strip()
    if base:
        normalized = base.replace("\\", "/")
        if "/" in normalized:
            return posixpath.join(posixpath.dirname(normalized), log_name)
        if data_dir:
            return posixpath.join(data_dir.replace("\\", "/"), log_name)
    if data_dir:
        return posixpath.join(data_dir.replace("\\", "/"), log_name)
    return log_name


def _is_remote_file_path(path: str) -> bool:
    return bool(path and (path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path)))


def _quote_powershell_single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parse_linux_stat_output(output: str) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for line in (output or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        path, size, modify_time = parts
        items[path] = {"size": size, "mtime": modify_time.split(".", 1)[0]}
    return items


def _parse_windows_time_output(output: str) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for line in (output or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            items[parts[0]] = {"size": parts[1], "mtime": parts[2].strip()}
    return items


def _load_file_times_by_ssh(instance: DBInstance, paths: list[str], warnings: list, notices: list | None = None) -> dict[str, dict]:
    remote_paths = [p for p in paths if _is_remote_file_path(p)]
    if not remote_paths:
        return {}
    if not getattr(instance, "ssh_host", None):
        if notices is not None:
            notices.append("未配置 SSH，Binlog 文件创建时间、修改时间和磁盘路径暂不采集。")
        return {}
    try:
        from app.services.ssh_executor import execute_command

        linux_paths = [p for p in remote_paths if p.startswith("/")]
        if linux_paths:
            command = "stat -c '%n|%s|%y' -- " + " ".join(shlex.quote(p) for p in linux_paths[:150])
            result = execute_command(instance, command, timeout=30)
            if result.get("ok"):
                return _parse_linux_stat_output(result.get("stdout") or "")

        ps_paths = ", ".join(_quote_powershell_single(p) for p in remote_paths[:150])
        ps_script = (
            f"$paths=@({ps_paths}); "
            "foreach($p in $paths){ if(Test-Path -LiteralPath $p){ "
            "$i=Get-Item -LiteralPath $p; "
            "Write-Output ($p + '|' + $i.Length + '|' + $i.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')) } }"
        )
        encoded_script = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        result = execute_command(instance, f"powershell -NoProfile -EncodedCommand {encoded_script}", timeout=30)
        if result.get("ok"):
            return _parse_windows_time_output(result.get("stdout") or "")
    except Exception as exc:
        logger.warning("获取 Binlog 文件时间失败 | host=%s | error=%s", getattr(instance, "ssh_host", ""), exc)
        warnings.append(f"SSH 读取 Binlog 文件时间失败：{exc}")
    return {}


def _safe_query(cur, sql: str, warnings: list, label: str):
    try:
        cur.execute(sql)
        return list(cur.fetchall() or [])
    except Exception as exc:
        warnings.append(f"{label}采集失败：{exc}")
        return []


def _collect_variables(cur, warnings: list) -> dict:
    variables = {}
    for key in BINLOG_VARIABLES:
        try:
            cur.execute("SHOW GLOBAL VARIABLES LIKE %s", (key,))
            row = cur.fetchone()
            variables[key] = _mysql_value(row, "Value", "")
        except Exception as exc:
            variables[key] = ""
            warnings.append(f"变量 {key} 不存在或无法读取：{exc}")
    return variables


def _collect_current_status(cur, warnings: list) -> tuple[dict, dict]:
    rows = []
    raw = {}
    errors = []
    source_sql = ""
    for sql in ("SHOW MASTER STATUS", "SHOW BINARY LOG STATUS"):
        try:
            cur.execute(sql)
            rows = list(cur.fetchall() or [])
            source_sql = sql
            if rows:
                break
        except Exception as exc:
            errors.append(f"{sql}: {exc}")
    if not rows and errors:
        warnings.append(f"Binlog 位点采集失败：{errors[-1]}")
    row = rows[0] if rows else {}
    raw = dict(row or {})
    if raw:
        raw["_source_sql"] = source_sql
    return {
        "file": _mysql_value(row, "File", ""),
        "position": int(_num(_mysql_value(row, "Position", 0))),
        "binlog_do_db": _mysql_value(row, "Binlog_Do_DB", ""),
        "binlog_ignore_db": _mysql_value(row, "Binlog_Ignore_DB", ""),
        "executed_gtid_set": _mysql_value(row, "Executed_Gtid_Set", ""),
    }, raw


def _collect_cache_status(cur, warnings: list) -> dict:
    rows = _safe_query(
        cur,
        "SHOW GLOBAL STATUS WHERE Variable_name IN ('Binlog_cache_use','Binlog_cache_disk_use','Binlog_stmt_cache_use','Binlog_stmt_cache_disk_use')",
        warnings,
        "Binlog Cache 状态",
    )
    values = {(_mysql_value(r, "Variable_name") or "").lower(): _mysql_value(r, "Value", 0) for r in rows}
    cache_use = _num(values.get("binlog_cache_use"))
    cache_disk = _num(values.get("binlog_cache_disk_use"))
    stmt_use = _num(values.get("binlog_stmt_cache_use"))
    stmt_disk = _num(values.get("binlog_stmt_cache_disk_use"))
    return {
        "binlog_cache_use": int(cache_use),
        "binlog_cache_disk_use": int(cache_disk),
        "binlog_cache_disk_pct": round(cache_disk / cache_use * 100, 4) if cache_use else 0,
        "binlog_stmt_cache_use": int(stmt_use),
        "binlog_stmt_cache_disk_use": int(stmt_disk),
        "binlog_stmt_cache_disk_pct": round(stmt_disk / stmt_use * 100, 4) if stmt_use else 0,
    }


def _build_files(
    instance: DBInstance,
    rows: list[dict],
    variables: dict,
    current_file: str,
    warnings: list,
    notices: list | None = None,
) -> tuple[list, dict]:
    basename = variables.get("log_bin_basename", "")
    datadir = variables.get("datadir", "")
    total_size = sum(int(_num(_mysql_value(r, "File_size", 0))) for r in rows)
    largest = None
    files = []
    for idx, row in enumerate(rows or [], start=1):
        log_name = _mysql_value(row, "Log_name")
        size = int(_num(_mysql_value(row, "File_size", 0)))
        encrypted = _mysql_value(row, "Encrypted", "unknown")
        path = _join_binlog_path(str(log_name), basename, datadir)
        item = {
            "seq": idx,
            "log_name": log_name,
            "file_name": log_name,
            "file_size_bytes": size,
            "file_size": size,
            "file_size_label": _format_bytes(size),
            "file_size_mb": round(size / 1024 / 1024, 2),
            "file_size_gb": round(size / 1024 / 1024 / 1024, 2),
            "size_pct": round(size / total_size * 100, 2) if total_size else 0,
            "encrypted": encrypted or "unknown",
            "is_current": log_name == current_file,
            "is_first": idx == 1,
            "is_last": idx == len(rows),
            "file_path": path,
            "mtime": "",
            "age_days": None,
            "generated_at": "",
            "generated_at_note": "",
        }
        item["status_label"] = "当前写入文件" if item["is_current"] else "历史文件"
        item["suggestion"] = "当前文件不可直接清理" if item["is_current"] else "需结合备份、GTID 和复制位点评估是否可清理"
        files.append(item)
        if not largest or size > largest["file_size_bytes"]:
            largest = item

    file_times = _load_file_times_by_ssh(instance, [f["file_path"] for f in files], warnings, notices)
    now = datetime.now()
    for item in files:
        meta = file_times.get(item["file_path"], {})
        item["mtime"] = meta.get("mtime", "")
        item["generated_at"] = item["mtime"]
        if item["mtime"]:
            try:
                item["age_days"] = (now - datetime.strptime(item["mtime"][:19], "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                item["age_days"] = None
        else:
            item["generated_at_note"] = "需配置 SSH"

    summary = {
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_gb": round(total_size / 1024 / 1024 / 1024, 2),
        "first_file": files[0]["log_name"] if files else "",
        "last_file": files[-1]["log_name"] if files else "",
        "current_file": current_file,
        "largest_file": largest["log_name"] if largest else "",
        "largest_file_size_gb": largest["file_size_gb"] if largest else 0,
    }
    return files, summary


def _build_risks(variables: dict, summary: dict, current_status: dict, cache_status: dict, warnings: list) -> list:
    risks = []

    def add(level, item, message):
        risks.append({"level": level, "item": item, "message": message})

    if not _bool_on(variables.get("log_bin")):
        add("critical", "log_bin", "Binlog 未开启，无法做基于 Binlog 的恢复和复制。")
    if variables.get("binlog_format") and variables.get("binlog_format") != "ROW":
        add("warning", "binlog_format", "生产建议使用 ROW，降低复制不一致风险。")
    if str(variables.get("sync_binlog", "")) == "0":
        add("warning", "sync_binlog", "sync_binlog=0，宕机时可能丢失 Binlog。")
    if str(variables.get("gtid_mode", "")).upper() == "OFF":
        add("warning", "GTID", "GTID 未开启，复制和故障切换复杂度更高。")
    if int(_num(variables.get("binlog_expire_logs_seconds"))) == 0 and int(_num(variables.get("expire_logs_days"))) == 0:
        add("warning", "保留策略", "未配置 Binlog 自动过期，可能无限增长。")
    if summary.get("total_size_gb", 0) > 500:
        add("critical", "Binlog 总大小", f"Binlog 总大小 {summary['total_size_gb']} GB，磁盘风险较高。")
    elif summary.get("total_size_gb", 0) > 100:
        add("warning", "Binlog 总大小", f"Binlog 总大小 {summary['total_size_gb']} GB，建议评估清理策略。")
    current_size = 0
    max_size = _num(variables.get("max_binlog_size"))
    for file in summary.get("_files", []):
        if file.get("is_current"):
            current_size = file.get("file_size_bytes", 0)
    if max_size and current_size / max_size >= 0.9:
        add("info", "当前文件接近上限", "当前 Binlog 文件接近 max_binlog_size，即将滚动到新文件。")
    if cache_status.get("binlog_cache_disk_pct", 0) > 1:
        add("warning", "Binlog Cache 落盘", "Binlog Cache 落盘比例较高，可能存在大事务或 binlog_cache_size 偏小。")
    if not current_status.get("executed_gtid_set") and str(variables.get("gtid_mode", "")).upper() == "ON":
        add("warning", "GTID 位点", "GTID 已开启但未采集到 executed_gtid_set，请确认权限或版本兼容。")
    return risks


def get_binlog_overview(instance: DBInstance) -> dict:
    warnings = []
    notices = []
    raw = {"variables": {}, "binary_log_status": {}, "master_status": {}, "binary_logs": []}
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                variables = _collect_variables(cur, warnings)
                raw["variables"] = dict(variables)
                current_status, status_raw = _collect_current_status(cur, warnings)
                raw["binary_log_status"] = status_raw
                binary_logs = _safe_query(cur, "SHOW BINARY LOGS", warnings, "SHOW BINARY LOGS")
                raw["binary_logs"] = binary_logs
                files, file_summary = _build_files(instance, binary_logs, variables, current_status.get("file"), warnings, notices)
                cache_status = _collect_cache_status(cur, warnings)

        retention_seconds, retention_text = _retention_label(variables)
        file_summary_for_risk = {**file_summary, "_files": files}
        risks = _build_risks(variables, file_summary_for_risk, current_status, cache_status, warnings)
        risk_level = "critical" if any(r["level"] == "critical" for r in risks) else ("warning" if any(r["level"] == "warning" for r in risks) else "normal")
        summary = {
            "enabled": _bool_on(variables.get("log_bin")),
            "enabled_label": "已开启" if _bool_on(variables.get("log_bin")) else "未开启",
            "format": variables.get("binlog_format", ""),
            "gtid_mode": variables.get("gtid_mode", ""),
            "gtid_label": "已开启" if str(variables.get("gtid_mode", "")).upper() == "ON" else "未开启",
            "current_file": current_status.get("file", ""),
            "current_position": current_status.get("position", 0),
            "file_count": file_summary.get("file_count", 0),
            "total_size_gb": file_summary.get("total_size_gb", 0),
            "retention_seconds": retention_seconds,
            "retention_label": retention_text,
            "max_binlog_size_gb": round(_num(variables.get("max_binlog_size")) / 1024 / 1024 / 1024, 2),
            "sync_binlog": variables.get("sync_binlog", ""),
            "risk_level": risk_level,
        }
        return {
            "status": "ok",
            "summary": summary,
            "variables": variables,
            "current_status": current_status,
            "files": files,
            "file_summary": file_summary,
            "cache_status": cache_status,
            "replication_dependency": {
                "safe_boundary_confirmed": False,
                "message": "当前未采集下游复制位点，清理前需确认备份、复制延迟和 GTID 安全边界。",
            },
            "risk_items": risks,
            "warnings": warnings,
            "notices": notices,
            "raw": raw,
            "master_status": current_status,
            "error": None,
        }
    except Exception as exc:
        logger.exception("get_binlog_overview failed")
        return {"status": "error", "error": str(exc), "warnings": warnings, "variables": {}, "files": []}


def generate_purge_plan(instance: DBInstance, before_file: str = "", before_date: str = "", retention_days: int | None = None) -> dict:
    sql = ""
    if before_file:
        safe_file = before_file.replace("'", "''")
        sql = f"PURGE BINARY LOGS TO '{safe_file}';"
    elif before_date:
        safe_date = before_date.replace("'", "''")
        sql = f"PURGE BINARY LOGS BEFORE '{safe_date}';"
    else:
        days = int(retention_days or 7)
        sql = f"PURGE BINARY LOGS BEFORE DATE_SUB(NOW(), INTERVAL {days} DAY);"
    return {
        "status": "plan_only",
        "message": "已生成 Binlog 清理方案，不直接执行。",
        "safe_to_purge": False,
        "reason": [
            "当前未采集到从库复制位点，无法确认下游是否仍依赖历史 Binlog。",
            "建议先确认备份完成、复制无延迟、GTID 位点安全后再执行。",
        ],
        "sql": sql,
        "script": sql,
        "warnings": [
            "不要清理当前正在写入的 Binlog 文件。",
            "不要使用 rm 直接删除 Binlog 文件。",
            "清理前确认备份、复制和恢复需求。",
        ],
    }
