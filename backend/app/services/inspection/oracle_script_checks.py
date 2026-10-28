from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from html import unescape
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from app.analyzers.rules import Finding
from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.oracle.oracle_inspection_profile import detect_oracle_profile
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, json_safe
from app.models.db_instance import DBInstance
from app.services.inspection.custom_metric_service import metric_source_key


MAX_ROWS_PER_SECTION = 200
QUERY_SOURCE_PREFIX = "oracle.query:"
SCRIPT_SOURCE_PREFIX = "oracle.script:"

ANCHOR_RE = re.compile(r'^\s*prompt\s+<a\s+name="?([^">]+)"?', re.IGNORECASE)
TITLE_RE = re.compile(r"^\s*prompt\s+<font[^>]*><b>(.*?)</b>", re.IGNORECASE)
SQL_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
PLSQL_START_RE = re.compile(r"^\s*(declare|begin)\b", re.IGNORECASE)
PLSQL_END_RE = re.compile(r"^\s*/\s*$")
SQLPLUS_VAR_RE = re.compile(r"&[A-Za-z_][A-Za-z0-9_]*")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SYS_INTERNAL_OBJECT_RE = re.compile(
    r"(?:\bsys\s*\.\s*)?(?:tabpart\$|tabcompart\$|obj\$|user\$|tab\$)(?!\w)",
    re.IGNORECASE,
)

ORACLE_SCRIPT_ITEM_KEYS = {
    "instance_info": "oracle.basic.instance_rac",
    "database_version": "oracle.basic.database_info",
    "database_component": "oracle.basic.database_info",
    "database_patch": "oracle.basic.database_info",
    "database_parameter": "oracle.config.parameter_baseline",
    "resource_limit": "oracle.session.used_pct",
    "control_file": "oracle.controlfile.count",
    "redolog_file": "oracle.redo.member_count",
    "redo_switch": "oracle.archive.generate_mb_per_hour",
    "archivelog_size": "oracle.archive.generate_mb_per_hour",
    "invalid_object": "oracle.objects.invalid_count",
    "tablespace_usage": "oracle.tablespace.used_pct",
    "tablespace_total": "oracle.tablespace.used_pct",
    "tablespace_perday": "oracle.tablespace.used_pct",
    "recyclebin_info": "oracle.space.recyclebin_usage",
    "top10_table": "oracle.objects.segment_fragmentation",
    "top10_index": "oracle.objects.index_unusable",
    "range_par_extend": "oracle.objects.segment_fragmentation",
    "foreigno_index": "oracle.objects.index_unusable",
    "object_insystem": "oracle.objects.segment_fragmentation",
    "bitcoin_check": "oracle.security.default_password",
    "sysaux_obj": "oracle.objects.segment_fragmentation",
    "sysaux_app": "oracle.objects.segment_fragmentation",
    "hwmstat_info": "oracle.objects.segment_fragmentation",
    "flashback_parameters": "oracle.backup.age_hours",
    "flashback_status": "oracle.backup.age_hours",
    "userdefault_passwd": "oracle.security.default_password",
    "system_role": "oracle.security.account_status",
    "schema_info": "oracle.security.account_status",
    "schema_info_2": "oracle.security.account_status",
    "profile": "oracle.security.account_status",
    "directory": "oracle.security.account_status",
    "job": "oracle.config.parameter_baseline",
    "scheduler_jobs": "oracle.config.parameter_baseline",
    "jobs_info_errors": "oracle.config.parameter_baseline",
    "dblink": "oracle.security.account_status",
    "autotask": "oracle.config.parameter_baseline",
    "autotask_windows": "oracle.config.parameter_baseline",
    "autotask_jobhis": "oracle.config.parameter_baseline",
    "dg_para": "oracle.dataguard.lag",
    "dgapply_info": "oracle.dataguard.lag",
    "dg_stats": "oracle.dataguard.lag",
    "rmanbackup_info": "oracle.backup.age_hours",
    "datapump_info": "oracle.objects.invalid_count",
    "alertlog_check": "oracle.archive.mode",
    "asm_instance": "oracle.basic.instance_rac",
    "asmdisk_attr": "oracle.tablespace.used_pct",
    "asmdiskgroup_usage": "oracle.tablespace.used_pct",
    "awraux_check": "oracle.awr.snapshot_coverage",
    "awrsnap_rate": "oracle.awr.snapshot_coverage",
    "awrsnap_info": "oracle.awr.snapshot_coverage",
    "load_profile": "oracle.performance.wait_events",
    "insteffi": "oracle.performance.wait_events",
    "top10_event": "oracle.performance.wait_events",
    "timemodel": "oracle.performance.wait_events",
    "top10_sql": "oracle.performance.top_sql_elapsed",
    "top10_sqltext": "oracle.performance.top_sql_elapsed",
    "awrcrt": "oracle.performance.wait_events",
}

GENERIC_ORACLE_ITEM_KEYS = {
    "connection.basic": {
        "oracle.basic.database_info",
        "oracle.basic.instance_rac",
        "oracle.listener.status",
    },
    "space.capacity": {
        "oracle.tablespace.used_pct",
        "oracle.temp.used_pct",
        "oracle.fra.used_pct",
        "oracle.undo.used_pct",
        "oracle.space.recyclebin_usage",
        "oracle.objects.segment_fragmentation",
    },
    "performance.slow_sql": {
        "oracle.session.used_pct",
        "oracle.blocking.seconds",
        "oracle.awr.snapshot_coverage",
        "oracle.performance.top_sql_elapsed",
        "oracle.performance.top_sql_buffer_gets",
        "oracle.performance.top_sql_disk_reads",
        "oracle.performance.wait_events",
        "oracle.objects.stats_stale",
    },
    "backup.recovery": {
        "oracle.backup.age_hours",
        "oracle.archive.mode",
        "oracle.archive.generate_mb_per_hour",
        "oracle.controlfile.count",
        "oracle.redo.member_count",
        "oracle.dataguard.lag",
    },
    "security.account": {
        "oracle.security.account_status",
        "oracle.security.default_password",
        "oracle.objects.invalid_count",
        "oracle.objects.index_unusable",
        "oracle.config.parameter_baseline",
    },
}


SQLPLUS_SUBSTITUTIONS = {
    "&_dbid": "(SELECT dbid FROM v$database)",
    "&_blocksize": "(SELECT TO_NUMBER(value) FROM v$parameter WHERE name='db_block_size')",
    "&_snap_beg": "(SELECT MIN(snap_id) FROM dba_hist_snapshot WHERE begin_interval_time >= SYSDATE - 1)",
    "&_snap_end": "(SELECT MAX(snap_id) FROM dba_hist_snapshot)",
    "&_crt_snap_beg": "(SELECT MIN(snap_id) FROM dba_hist_snapshot WHERE begin_interval_time >= SYSDATE - 7)",
    "&_crt_snap_end": "(SELECT MAX(snap_id) FROM dba_hist_snapshot)",
    "&_snap_int_min": "(SELECT EXTRACT(HOUR FROM snap_interval) * 60 + EXTRACT(MINUTE FROM snap_interval) FROM dba_hist_wr_control)",
    "&_cpus": "NULLIF((SELECT value FROM v$osstat WHERE stat_name = 'NUM_CPUS'), 0)",
    "&_iv": "3600",
}


@dataclass
class ScriptSqlSection:
    order: int
    key: str
    title: str
    sql: str
    line_number: int
    item_key: str
    template_sha256: str = ""


def collect_oracle_script_findings(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
    expected_profile: str | None = None,
) -> list[Finding]:
    allowed_item_keys = enabled_oracle_item_keys(policy_items)
    if allowed_item_keys == set():
        return []
    try:
        with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
            profile = detect_oracle_profile(conn)
            if expected_profile:
                from .template_profiles import require_matching_oracle_profile

                require_matching_oracle_profile(profile, expected_profile)
            sections = parse_script_sections(profile.script_path)
            if allowed_item_keys is not None:
                sections = select_script_sections(sections, allowed_item_keys)
            if not sections:
                return []
            findings = _execute_sections(
                conn,
                profile.to_dict(),
                sections,
                filtered_by_template=allowed_item_keys is not None,
            )
            return findings
    except Exception as exc:
        return [
            Finding(
                "Oracle DBCheck Script",
                "Script execution degraded",
                "warning",
                "collector_status",
                "degraded",
                "ok",
                f"Oracle DBCheck script inspection failed: {exc}",
                "Check Oracle connection, driver availability, script file path, and read-only catalog view permissions.",
                {"error": str(exc), "read_only": True},
            )
        ]


def enabled_oracle_item_keys(policy_items: list[dict] | None) -> set[str] | None:
    if policy_items is None:
        return None
    selected: set[str] = set()
    for item in policy_items:
        if not _is_enabled(item.get("enabled", True)):
            continue
        key = metric_source_key(item)
        if not key:
            continue
        selected.add(key)
        selected.update(GENERIC_ORACLE_ITEM_KEYS.get(key, set()))
    return selected


def enabled_oracle_query_keys(policy_items: list[dict] | None) -> set[str] | None:
    selected = enabled_oracle_item_keys(policy_items)
    if selected is None:
        return None
    return {
        key.removeprefix(QUERY_SOURCE_PREFIX)
        for key in selected
        if not key.startswith(SCRIPT_SOURCE_PREFIX)
    }


def select_script_sections(sections, allowed_item_keys):
    selected = []
    for section in sections:
        source = SCRIPT_SOURCE_PREFIX + section.key
        if source in allowed_item_keys:
            selected.append(replace(section, item_key=source))
        elif section.item_key in allowed_item_keys:
            # Old templates retain their grouped metric bindings.
            selected.append(section)
    return selected


def parse_script_sections(script_path: str | Path) -> list[ScriptSqlSection]:
    path = Path(script_path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    sections: list[ScriptSqlSection] = []
    current_anchor = ""
    current_title = ""
    in_report_body = False
    in_sql = False
    in_plsql = False
    sql_lines: list[str] = []
    sql_start_line = 0
    seen_keys: dict[str, int] = {}

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        anchor_match = ANCHOR_RE.match(line)
        if anchor_match:
            anchor = _slug(anchor_match.group(1))
            if anchor == "sqlscripts_errors":
                break
            current_anchor = anchor
            in_report_body = True

        if in_report_body:
            title_match = TITLE_RE.match(line)
            if title_match:
                title = _clean_title(title_match.group(1))
                if title:
                    current_title = title

        if not in_report_body:
            continue

        if not in_sql and PLSQL_START_RE.match(line):
            in_plsql = True
            continue

        if in_plsql:
            if PLSQL_END_RE.match(line):
                in_plsql = False
            continue

        if not in_sql and SQL_START_RE.match(line):
            in_sql = True
            sql_lines = [line]
            sql_start_line = index
            if stripped.endswith(";"):
                section = _build_section(
                    sections,
                    seen_keys,
                    current_anchor,
                    current_title,
                    sql_lines,
                    sql_start_line,
                )
                if section:
                    sections.append(section)
                in_sql = False
            continue

        if in_sql:
            sql_lines.append(line)
            if stripped.endswith(";"):
                section = _build_section(
                    sections,
                    seen_keys,
                    current_anchor,
                    current_title,
                    sql_lines,
                    sql_start_line,
                )
                if section:
                    sections.append(section)
                in_sql = False

    template_sha256 = sha256(path.read_bytes()).hexdigest()
    return [replace(section, template_sha256=template_sha256) for section in sections]


def _execute_sections(
    conn,
    profile: dict[str, Any],
    sections: list[ScriptSqlSection],
    *,
    filtered_by_template: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    executed = 0
    skipped = 0
    failed = 0
    details: list[dict[str, Any]] = []
    substitutions = _resolve_sqlplus_substitutions(conn, version=profile.get("version")) if any(
        SQLPLUS_VAR_RE.search(section.sql) for section in sections
    ) else {}
    cursor = conn.cursor()
    try:
        for section in sections:
            sql = _prepare_sql(section.sql, substitutions)
            skip_reason = _skip_reason(sql)
            if skip_reason:
                skipped += 1
                detail = _section_detail(section, sql, [], [], 0, False, skip_reason, "skipped")
                details.append(detail)
                continue
            try:
                compatibility = None
                if profile.get("script_name") == "dbcheck_cdb.sql" and section.key in {
                    "database_patch", "range_par_extend", "autotask_windows",
                }:
                    from .oracle_cdb_compat import execute_connected_pdb_section

                    compatibility = execute_connected_pdb_section(
                        section, conn, max_rows=MAX_ROWS_PER_SECTION,
                        expected_container=profile.get("con_name"),
                    )
                    columns, rows = compatibility["columns"], compatibility["rows"]
                    truncated = compatibility["truncated"]
                    sql = ";\n\n".join(entry["sql"] for entry in compatibility["actual_statements"])
                else:
                    cursor.execute(sql)
                    columns = [desc[0] for desc in (cursor.description or [])]
                    rows_raw = cursor.fetchmany(MAX_ROWS_PER_SECTION + 1)
                    truncated = len(rows_raw) > MAX_ROWS_PER_SECTION
                    rows = [_row_to_dict(columns, row) for row in rows_raw[:MAX_ROWS_PER_SECTION]]
                row_count = len(rows)
                executed += 1
                detail = _section_detail(section, sql, columns, rows, row_count, truncated, None, "normal")
                if compatibility is not None:
                    detail["compatibility"] = compatibility
                    detail["scope"] = compatibility["scope"]
                details.append(detail)
                findings.append(
                    _section_finding(
                        section,
                        "normal",
                        f"{row_count} rows" + (" (truncated)" if truncated else ""),
                        f"仅采集当前 PDB {compatibility['original_container']['name']}；未检查其他容器。" if compatibility else None,
                        detail,
                    )
                )
            except Exception as exc:
                failed += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
                detail = _section_detail(section, sql, [], [], 0, False, str(exc), "warning")
                if getattr(exc, "evidence", None):
                    detail["compatibility"] = exc.evidence
                details.append(detail)
                findings.append(_section_finding(section, "warning", "error", str(exc), detail))
    finally:
        cursor.close()

    findings.insert(
        0,
        Finding(
            "Oracle DBCheck Script",
            "模板 SQL 执行覆盖",
            "warning" if failed else "info" if skipped else "normal",
            "executed_sections",
            f"{executed}/{len(sections)}",
            "failed=0",
            (
                f"使用 {profile.get('script_name')}，模板选中 {len(sections)} 个数据采集 SQL；"
                f"成功 {executed}，跳过 {skipped}，失败 {failed}。"
            ),
            "跳过项不计入健康项；失败项通常表示目录/AWR 权限不足或可选组件不可用。",
            json_safe(
                {
                    "profile": profile,
                    "parsed_count": len(sections),
                    "executed_count": executed,
                    "skipped_count": skipped,
                    "failed_count": failed,
                    "matched_item_count": len(
                        {
                            section.item_key
                            for section in sections
                            if section.item_key
                        }
                    ),
                    "max_rows_per_section": MAX_ROWS_PER_SECTION,
                    "read_only": True,
                    "filtered_by_template": filtered_by_template,
                    "evidence_kind": "execution_summary",
                    "columns": [
                        "order",
                        "key",
                        "title",
                        "template_item_key",
                        "line_number",
                        "status",
                        "row_count",
                        "message",
                    ],
                    "rows": [
                        {
                            "order": item["order"],
                            "key": item["key"],
                            "title": item["title"],
                            "template_item_key": item.get("template_item_key"),
                            "line_number": item["line_number"],
                            "status": item["status"],
                            "row_count": item["row_count"],
                            "message": item.get("message"),
                        }
                        for item in details
                    ],
                    "row_count": len(details),
                    "truncated": False,
                    "sections": [
                        {
                            "order": item["order"],
                            "key": item["key"],
                            "title": item["title"],
                            "template_item_key": item.get("template_item_key"),
                            "line_number": item["line_number"],
                            "status": item["status"],
                            "row_count": item["row_count"],
                            "message": item.get("message"),
                        }
                        for item in details
                    ],
                }
            ),
        ),
    )
    return findings


def _build_section(
    sections: list[ScriptSqlSection],
    seen_keys: dict[str, int],
    current_anchor: str,
    current_title: str,
    sql_lines: list[str],
    line_number: int,
) -> ScriptSqlSection | None:
    sql = "\n".join(sql_lines).strip()
    if not SQL_START_RE.match(sql):
        return None
    if _is_presentation_sql(sql):
        return None
    title = current_title or current_anchor or f"SQL section {len(sections) + 1}"
    base_key = current_anchor or _slug(title)
    if not base_key:
        base_key = f"section_{len(sections) + 1}"
    seen = seen_keys.get(base_key, 0) + 1
    seen_keys[base_key] = seen
    key = base_key if seen == 1 else f"{base_key}_{seen}"
    display_title = title if seen == 1 else f"{title} #{seen}"
    item_key = ORACLE_SCRIPT_ITEM_KEYS.get(key) or ORACLE_SCRIPT_ITEM_KEYS.get(base_key) or ""
    return ScriptSqlSection(len(sections) + 1, key, display_title, sql, line_number, item_key)


def _prepare_sql(sql: str, substitutions: dict[str, str] | None = None) -> str:
    prepared = sql.strip().rstrip(";").strip()
    replacement_values = SQLPLUS_SUBSTITUTIONS if substitutions is None else substitutions
    for token in sorted(replacement_values, key=len, reverse=True):
        prepared = prepared.replace(token, replacement_values[token])
    if re.search(r"\bfrom\s+dba_autotask_window_clients\b", prepared, re.IGNORECASE):
        prepared = re.sub(
            r"\bw\.next_start_date\b",
            "TO_CHAR(w.next_start_date, 'YYYY-MM-DD HH24:MI:SS TZH:TZM') AS next_start_date",
            prepared,
            count=1,
            flags=re.IGNORECASE,
        )
        prepared = re.sub(
            r"\bw\.last_start_date\b",
            "TO_CHAR(w.last_start_date, 'YYYY-MM-DD HH24:MI:SS TZH:TZM') AS last_start_date",
            prepared,
            count=1,
            flags=re.IGNORECASE,
        )
    return prepared


def _dg_sqlplus_substitutions(version: str | None) -> dict[str, str]:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)*", str(version or "").strip())
    if not match:
        return {}
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < (11, 1):
        return {}
    legacy_sync = (major, minor) == (11, 1)
    # The column is an identifier; MODE/OK already occur inside SQL string quotes.
    # Only fixed values may replace them, never database-returned SQL fragments.
    return {
        "&_DG_GAP_COLUMN": "synchronization_status" if legacy_sync else "gap_status",
        "&_DG_GAP_MODE": "SYNC" if legacy_sync else "GAP",
        "&_DG_GAP_OK": "OK" if legacy_sync else "NO GAP",
    }


def _resolve_sqlplus_substitutions(conn, *, version: str | None = None) -> dict[str, str]:
    resolved = {"&_iv": "3600", **_dg_sqlplus_substitutions(version)}
    cursor = conn.cursor()
    try:
        for token, expression in SQLPLUS_SUBSTITUTIONS.items():
            if token in resolved:
                continue
            try:
                cursor.execute(f"SELECT {expression} FROM dual")
                row = cursor.fetchone()
                value = row[0] if row else None
                if value is not None:
                    resolved[token] = _sql_literal(value)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
    finally:
        cursor.close()
    return resolved


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _skip_reason(sql: str) -> str | None:
    if SQLPLUS_VAR_RE.search(sql):
        return "contains SQL*Plus substitution variables that cannot be safely bound in API mode"
    lowered = sql.lower()
    if SYS_INTERNAL_OBJECT_RE.search(sql):
        return "queries SYS internal dictionary base tables and is skipped to avoid requiring SYS/SYSDBA privileges"
    if "instinfo_cur." in lowered:
        return "references a PL/SQL loop variable and cannot run as an independent SQL statement"
    if _is_presentation_sql(lowered):
        return "HTML/JavaScript chart generation SQL is skipped in structured API mode"
    if not SQL_START_RE.match(sql):
        return "not a read-only SELECT/WITH statement"
    return None


def _section_finding(section: ScriptSqlSection, status: str, metric_value: str, message: str | None, detail: dict) -> Finding:
    return Finding(
        f"{section.order:02d}. {section.title}",
        section.title,
        status,
        section.item_key or section.key,
        metric_value,
        "只读 SQL 成功",
        message or f"{section.title} collected from DBCheck script.",
        "将本项作为只读巡检证据；结果被截断时可在报告中查看 SQL，并在维护窗口导出完整结果。",
        json_safe(detail),
    )


def _section_detail(
    section: ScriptSqlSection,
    sql: str,
    columns: list[str],
    rows: list[dict],
    row_count: int,
    truncated: bool,
    message: str | None,
    status: str,
) -> dict:
    return {
        "order": section.order,
        "key": section.key,
        "title": section.title,
        "line_number": section.line_number,
        "template_sha256": section.template_sha256,
        "template_item_key": section.item_key,
        "status": status,
        "message": message,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "truncated": truncated,
        "max_rows": MAX_ROWS_PER_SECTION,
        "read_only": True,
    }


def _row_to_dict(columns: list[str], row) -> dict:
    return {
        columns[index] if index < len(columns) else f"COL_{index + 1}": _safe_value(value)
        for index, value in enumerate(row)
    }


def _safe_value(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _clean_title(value: str) -> str:
    text = HTML_TAG_RE.sub("", value)
    text = unescape(text).replace("●", "").strip()
    return re.sub(r"\s+", " ", text)


def _slug(value: str) -> str:
    value = HTML_TAG_RE.sub("", value or "")
    value = value.strip().strip('"').strip("'").lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    return value.strip("_")


def _is_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "disabled", "停用"}
    return bool(value)


def _is_presentation_sql(sql: str) -> bool:
    lowered = str(sql or "").lower()
    return (
        "document.getelementbyid" in lowered
        or "new chart" in lowered
        or "canvas_" in lowered
    )
