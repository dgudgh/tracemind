from __future__ import annotations

import hashlib
import json
import logging
import re
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.analyzers.oracle_rule_engine import analyze_oracle_metrics
from app.analyzers.rules import Finding
from app.collectors.mysql.mysql_connection import mysql_connection
from app.collectors.mysql.mysql_health_collector import MYSQL_QUERIES
from app.collectors.oracle.oracle_health_collector import OracleHealthCollector
from app.models.db_instance import DBInstance
from app.models.slow_sql import DBSlowSQL
from app.services.slow_sql import _number as _slow_sql_number
from . import report_builder, result_writer

logger = logging.getLogger("app.services.inspection.runner")


@dataclass(frozen=True)
class StagedSlowSQLWrite:
    engine: str
    instance_id: int
    metrics: dict
    source_keys: tuple[str, ...]
    finding: Finding
    sql: str | None = None


class PreparedFindings(list):
    """A normal findings list with unpublished, collection-owned write inputs."""

    def __init__(self, findings, staged_writes=()):
        super().__init__(findings)
        identities = {id(finding) for finding in self}
        self.staged_writes = tuple(write for write in staged_writes if id(write.finding) in identities)


_STAGED_SLOW_SQL: ContextVar[list[StagedSlowSQLWrite] | None] = ContextVar(
    "inspection_staged_slow_sql", default=None,
)

INSPECTION_DB_TYPES = {"oracle", "mysql", "postgresql", "sqlserver", "host"}
POLICY_THRESHOLD_BINDINGS = {
    "oracle.tablespace.used_pct": ("space_usage_pct", "critical_space_usage_pct"),
    "oracle.temp.used_pct": (None, None),
    "oracle.undo.used_pct": (None, None),
    "oracle.blocking.seconds": (None, "blocking_seconds"),
    "mysql.connection.used_pct": ("connection_usage_pct", None),
    "mysql.replication.lag_seconds": ("replication_lag_seconds", None),
    "mysql.backup.age_hours": ("backup_age_hours", None),
    "postgresql.connection.used_pct": ("connection_usage_pct", None),
    "postgresql.replication.lag_seconds": ("replication_lag_seconds", None),
    "postgresql.deadlocks.count": ("deadlocks_per_day", None),
    "postgresql.backup.age_hours": ("backup_age_hours", None),
    "sqlserver.database.used_pct": ("database_used_pct", None),
    "sqlserver.log.used_pct": ("log_used_pct", None),
    "sqlserver.blocking.seconds": (None, "blocking_seconds"),
    "sqlserver.backup.age_hours": ("backup_age_hours", None),
    "host.windows.cpuasge": ("cpu_pct", "critical_cpu_pct"),
    "host.windows.memusage": ("memory_pct", "critical_memory_pct"),
    "host.windows.swapusage": ("swap_pct", "critical_swap_pct"),
    "host.windows.diskusage": ("filesystem_pct", "critical_filesystem_pct"),
}


def prepare_findings(db: Session, instance: DBInstance, policy=None, *, frozen: bool = False) -> tuple[DBInstance, list[Finding]]:
    """Collect and post-process findings **without persisting them**.

    Shared by the direct path (`run_inspection`) and the queued path
    (`jobs.commit_results`). The steps after collection are easy to lose when a
    caller switches paths - threshold binding and policy context change the
    findings themselves, not just their metadata - so they belong here, once.

    Returns the (possibly rewritten) instance, because a host-scoped policy
    replaces the target and callers must snapshot the same one that ran.
    """
    from .execution_policy import resolve_execution_policy
    from app.core.database_options import redact_database_error

    if getattr(policy, "db_types_json", None) == ["host"] and instance.db_type != "host":
        instance = host_inspection_instance(instance)
    try:
        policy = resolve_execution_policy(db, instance, policy, frozen=frozen)
    except Exception as exc:
        # A failed preflight produces a failed report, never an untemplated fallback.
        findings = [_degraded(instance, redact_database_error(exc))]
    else:
        staged_writes = []
        token = _STAGED_SLOW_SQL.set(staged_writes)
        try:
            findings = collect_findings(db, instance, policy=policy)
        finally:
            _STAGED_SLOW_SQL.reset(token)
        findings = PreparedFindings(findings or [], staged_writes)
    if not findings:
        findings = [_degraded(instance, "巡检没有返回检查结果")]
    _apply_policy_thresholds(findings, policy)
    _attach_policy_context(findings, policy)
    return instance, findings


def run_inspection(db: Session, instance: DBInstance, policy=None) -> tuple[str, list]:
    recorded_source, findings = prepare_findings(db, instance, policy)
    recorded_target = SimpleNamespace(**{
        name: getattr(recorded_source, name, None) for name in ("id", "db_type", "instance_name", "host", "port")
    })
    task_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    try:
        published = materialize_staged_writes(db, findings, instance_id=recorded_target.id)
        rows = result_writer.save_findings(db, recorded_target, task_id, published)
    except BaseException:
        db.rollback()
        raise
    return task_id, rows


def host_inspection_instance(instance):
    fields = (
        "id", "instance_name", "host", "port", "database_name", "username",
        "ssh_host", "ssh_port", "ssh_username", "ssh_password_cipher",
    )
    return SimpleNamespace(
        **{field: getattr(instance, field, None) for field in fields},
        db_type="host", source_db_type=instance.db_type,
    )


def run_unified_inspection(db: Session, instance: DBInstance, policy=None) -> dict:
    task_id, _rows = run_inspection(db, instance, policy=policy)
    result = report_builder.build_report_payload(db, task_id, instance.id)
    return result or {"task": None, "modules": [], "findings": [], "report": None}


def collect_findings(db: Session, instance: DBInstance, policy=None) -> list[Finding]:
    try:
        if instance.db_type == "oracle":
            findings = _collect_oracle(db, instance, policy=policy)
        elif instance.db_type == "mysql":
            findings = _collect_mysql(db, instance, policy=policy)
        elif instance.db_type == "postgresql":
            from app.services.inspection.collectors import postgresql_inspection_collector

            policy_items = getattr(policy, "item_config_json", None) if policy is not None else None
            findings = postgresql_inspection_collector.collect(instance, policy_items=policy_items)
        elif instance.db_type == "sqlserver":
            from app.services.inspection.collectors import sqlserver_inspection_collector

            policy_items = getattr(policy, "item_config_json", None) if policy is not None else None
            findings = sqlserver_inspection_collector.collect(instance, policy_items=policy_items)
        elif instance.db_type == "host":
            findings = _collect_host(instance, policy=policy)
        else:
            findings = [_degraded(instance, f"Unsupported db_type: {instance.db_type}")]
    except Exception as exc:
        logger.warning(
            "Inspection degraded | instance_id=%s | db_type=%s | error=%s",
            instance.id,
            instance.db_type,
            exc,
            exc_info=True,
        )
        findings = [_degraded(instance, str(exc))]

    if policy is not None and instance.db_type in {"oracle", "mysql", "postgresql", "sqlserver"}:
        from app.services.inspection import custom_metric_service

        try:
            findings.extend(custom_metric_service.collect_policy_metrics(instance, policy.item_config_json or []))
        except Exception as exc:
            from app.core.database_options import redact_database_error

            findings.append(_degraded(instance, redact_database_error(exc)))
    return findings


def _attach_policy_context(findings: list[Finding], policy) -> None:
    if policy is None:
        return
    context = {
        "id": getattr(policy, "id", None),
        "policy_key": getattr(policy, "policy_key", None),
        "name": getattr(policy, "name", None),
        "revision": _policy_revision(policy),
        "updated_at": _time_text(getattr(policy, "update_time", None)),
        "item_count": len(getattr(policy, "item_config_json", None) or []),
        "oracle_profile": getattr(policy, "resolved_oracle_profile", None),
        "host_platform": getattr(policy, "resolved_host_platform", None),
        "script_name": (getattr(policy, "source_script", None) or {}).get("name"),
        "selection_mode": getattr(policy, "selection_mode", "explicit"),
        "source_script": getattr(policy, "source_script", None),
    }
    from app.services.inspection.custom_metric_service import metric_source_key
    from app.services.inspection.oracle_script_checks import (
        GENERIC_ORACLE_ITEM_KEYS,
    )

    policy_items = getattr(policy, "item_config_json", None) or []
    item_by_key = {}
    for item in policy_items:
        alias_key = str(item.get("key") or "").strip()
        source_key = metric_source_key(item)
        if alias_key:
            item_by_key[alias_key] = item
        if source_key:
            item_by_key[source_key] = item
            if source_key.startswith("oracle.query:"):
                item_by_key[source_key.removeprefix("oracle.query:")] = item
        if source_key:
            for expanded_key in GENERIC_ORACLE_ITEM_KEYS.get(source_key, set()):
                item_by_key.setdefault(expanded_key, item)
    for finding in findings:
        raw_data = finding.raw_data
        if isinstance(raw_data, dict):
            raw_data = dict(raw_data)
        elif raw_data is None:
            raw_data = {}
        else:
            raw_data = {"collected_data": raw_data}
        evidence_key = str(raw_data.get("template_item_key") or "").strip()
        if not evidence_key and str(finding.metric_name or "") in item_by_key:
            evidence_key = str(finding.metric_name)
        item = item_by_key.get(evidence_key)
        if item:
            item_key = str(item.get("key") or evidence_key).strip()
            source_key = metric_source_key(item)
            item_name = str(item.get("name") or item_key)
            raw_data["template_item_key"] = item_key
            raw_data["template_source_key"] = source_key
            raw_data["inspection_item"] = {
                "key": item_key,
                "source_key": source_key,
                "name": item_name,
                "category": item.get("category"),
            }
            finding.check_category = item_name[:50]
        raw_data["inspection_template"] = context
        finding.raw_data = raw_data


def _apply_policy_thresholds(findings: list[Finding], policy) -> None:
    if policy is None:
        return
    from app.services.inspection.custom_metric_service import (
        is_custom_sql_metric,
        metric_source_key,
    )

    items = getattr(policy, "item_config_json", None) or []
    thresholds = getattr(policy, "threshold_config_json", None) or {}
    item_by_key: dict[str, dict] = {}
    for item in items:
        alias_key = str(item.get("key") or "").strip()
        source_key = metric_source_key(item)
        if alias_key:
            item_by_key[alias_key] = item
        if source_key:
            item_by_key[source_key] = item
            if source_key.startswith("oracle.query:"):
                item_by_key[source_key.removeprefix("oracle.query:")] = item
    for finding in findings:
        raw_data = dict(finding.raw_data) if isinstance(finding.raw_data, dict) else {}
        if result_writer.finding_execution_status(finding) != "success":
            continue
        if raw_data.get("template_threshold_eligible") is False:
            continue
        evidence_key = str(raw_data.get("template_item_key") or "").strip()
        if not evidence_key and str(finding.metric_name or "") in item_by_key:
            evidence_key = str(finding.metric_name)
        item = item_by_key.get(evidence_key)
        if not item or is_custom_sql_metric(item):
            continue
        source_key = metric_source_key(item).removeprefix("oracle.query:")
        if (
            raw_data.get("template_threshold_eligible") is not True
            and source_key not in POLICY_THRESHOLD_BINDINGS
        ):
            continue
        metric_value = _number_from_text(finding.metric_value)
        if metric_value is None:
            continue
        warning_key, critical_key = POLICY_THRESHOLD_BINDINGS.get(
            source_key,
            (None, None),
        )
        direction = _threshold_direction(item)
        warning = _threshold_boundary(
            item.get("warning_value"),
            item.get("warning_range"),
            thresholds.get(warning_key) if warning_key else None,
            direction=direction,
            boundary="warning",
        )
        critical = _threshold_boundary(
            item.get("critical_value"),
            item.get("critical_range"),
            thresholds.get(critical_key) if critical_key else None,
            direction=direction,
            boundary="critical",
        )
        if warning is None and critical is None:
            continue
        original_status = str(finding.check_status or "normal").lower()
        status = _threshold_status(
            metric_value,
            warning,
            critical,
            direction,
            original_status,
            normal_range=str(item.get("normal_range") or ""),
            critical_range=str(item.get("critical_range") or ""),
        )
        finding.check_status = status
        item_name = str(item.get("name") or finding.check_item or source_key)
        unit = str(item.get("unit") or "").strip()
        status_range = str(
            item.get(
                {
                    "critical": "critical_range",
                    "warning": "warning_range",
                }.get(status, "normal_range")
            )
            or ""
        ).strip()
        current_value = f"{metric_value:g}{unit}"
        finding.finding = (
            f"{item_name}当前值 {current_value}"
            + (f"，判定范围：{status_range}。" if status_range else "。")
        )
        if item.get("advice"):
            finding.suggestion = str(item["advice"])
        range_text = "；".join(
            text
            for text in [
                str(item.get("normal_range") or "").strip(),
                str(item.get("warning_range") or "").strip(),
                str(item.get("critical_range") or "").strip(),
            ]
            if text
        )
        if range_text:
            finding.threshold_value = range_text
        raw_data["template_threshold"] = {
            "source_key": source_key,
            "value": metric_value,
            "warning": warning,
            "critical": critical,
            "direction": direction,
            "original_status": original_status,
            "evaluated_status": status,
        }
        finding.raw_data = raw_data


def _threshold_status(
    value: float,
    warning: float | None,
    critical: float | None,
    direction: str,
    original_status: str,
    *,
    normal_range: str = "",
    critical_range: str = "",
) -> str:
    if critical is not None and _critical_threshold_crossed(
        value,
        critical,
        direction,
        critical_range,
    ):
        return "critical"
    if warning is not None and _warning_threshold_crossed(
        value,
        warning,
        direction,
        normal_range,
    ):
        return "critical" if original_status == "critical" and critical is None else "warning"
    if critical is not None and warning is None:
        return original_status
    return "normal"


def _critical_threshold_crossed(
    value: float,
    threshold: float,
    direction: str,
    range_text: str,
) -> bool:
    comparator = _leading_comparator(range_text)
    if comparator == "<":
        return value < threshold
    if comparator == "<=":
        return value <= threshold
    if comparator == ">":
        return value > threshold
    if comparator == ">=":
        return value >= threshold
    if direction == "lower_is_worse":
        return value <= threshold
    return value >= threshold


def _warning_threshold_crossed(
    value: float,
    threshold: float,
    direction: str,
    normal_range: str,
) -> bool:
    normal_comparator = _leading_comparator(normal_range)
    if direction == "lower_is_worse":
        if normal_comparator == ">=":
            return value < threshold
        if normal_comparator == ">":
            return value <= threshold
        return value <= threshold
    if normal_comparator == "<":
        return value >= threshold
    if normal_comparator == "<=":
        return value > threshold
    return value >= threshold


def _leading_comparator(range_text: str) -> str | None:
    matched = re.match(r"^\s*(<=|>=|<|>)", str(range_text or ""))
    return matched.group(1) if matched else None


def _threshold_direction(item: dict) -> str:
    configured = str(item.get("threshold_direction") or "").strip().lower()
    if configured in {"higher_is_worse", "lower_is_worse"}:
        return configured
    critical_range = str(item.get("critical_range") or "").strip()
    if re.match(r"^(?:<=|<)", critical_range):
        return "lower_is_worse"
    return "higher_is_worse"


def _threshold_boundary(
    explicit_value,
    range_value,
    fallback_value,
    *,
    direction: str,
    boundary: str,
) -> float | None:
    explicit = _number_from_text(explicit_value)
    if explicit is not None:
        return explicit

    numbers = [
        float(value)
        for value in re.findall(r"[-+]?\d+(?:\.\d+)?", str(range_value or ""))
    ]
    if numbers:
        if direction == "lower_is_worse" and boundary == "warning":
            return max(numbers)
        return numbers[0]
    return _number_from_text(fallback_value)


def _number_from_text(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    matched = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    if not matched:
        return None
    try:
        return float(matched.group(0))
    except (TypeError, ValueError, OverflowError):
        return None


def _collect_oracle(db: Session, instance: DBInstance, policy=None) -> list[Finding]:
    from app.services.inspection import oracle_script_checks

    policy_items = getattr(policy, "item_config_json", None) if policy is not None else None
    allowed_item_keys = oracle_script_checks.enabled_oracle_query_keys(policy_items)
    expected_profile = getattr(policy, "resolved_oracle_profile", None)
    profile_options = {"expected_profile": expected_profile} if expected_profile else {}
    metrics = OracleHealthCollector(instance).collect(item_keys=allowed_item_keys, **profile_options)
    findings = _filter_oracle_analyzer_findings(
        analyze_oracle_metrics(metrics),
        policy_items,
    )
    _attach_oracle_query_evidence(findings, metrics)
    findings.extend(
        oracle_script_checks.collect_oracle_script_findings(
            instance,
            policy_items,
            **profile_options,
        )
    )
    _ensure_oracle_query_findings(findings, metrics, allowed_item_keys)
    slow_sources = tuple(
        source for source in ("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads")
        if allowed_item_keys is None or f"oracle.performance.{source}" in allowed_item_keys
    )
    if slow_sources:
        findings.append(_stage_or_save_slow_sql(
            db, instance, metrics, engine="oracle", source_keys=slow_sources,
        ))
    return findings


ORACLE_QUERY_SUMMARIES = {
    "oracle.blocking.seconds": (
        "会话与锁等待",
        "阻塞会话",
        "未发现阻塞会话",
        "继续关注阻塞链路和长事务趋势。",
    ),
    "oracle.objects.stats_stale": (
        "统计信息状态",
        "统计信息过期",
        "未发现统计信息过期对象",
        "按业务变化率定期复核统计信息收集策略。",
    ),
    "oracle.performance.top_sql_elapsed": (
        "SQL 性能",
        "Top SQL（执行耗时）",
        "已采集按执行耗时排序的 Top SQL",
        "结合执行次数、执行计划和业务时段分析高耗时 SQL。",
    ),
    "oracle.performance.top_sql_buffer_gets": (
        "SQL 性能",
        "Top SQL（逻辑读）",
        "已采集按逻辑读排序的 Top SQL",
        "关注高逻辑读 SQL 的访问路径、过滤条件和索引有效性。",
    ),
    "oracle.performance.top_sql_disk_reads": (
        "SQL 性能",
        "Top SQL（物理读）",
        "已采集按物理读排序的 Top SQL",
        "关注高物理读 SQL 的缓存命中、访问路径和存储影响。",
    ),
}


def _ensure_oracle_query_findings(
    findings: list[Finding],
    metrics: dict,
    allowed_item_keys: set[str] | None,
) -> None:
    from app.collectors.oracle.oracle_health_collector import ORACLE_QUERY_ITEM_KEYS

    evidence_by_query = metrics.get("_query_evidence")
    if not isinstance(evidence_by_query, dict):
        return
    covered = {
        (
            str((finding.raw_data or {}).get("query_name") or _oracle_finding_query_name(finding) or ""),
            key,
        )
        for finding in findings
        for key in [
            str(
                ((finding.raw_data or {}).get("template_item_key") or "")
                if isinstance(finding.raw_data, dict)
                else ""
            ).strip()
            or _oracle_analyzer_item_key(finding)
        ]
        if key and isinstance(finding.raw_data, dict)
    }
    for query_name, item_keys in ORACLE_QUERY_ITEM_KEYS.items():
        evidence = evidence_by_query.get(query_name)
        if not isinstance(evidence, dict):
            continue
        for item_key in sorted(item_keys):
            if allowed_item_keys is not None and item_key not in allowed_item_keys:
                continue
            if (query_name, item_key) in covered:
                continue
            category, check_item, finding_text, suggestion = ORACLE_QUERY_SUMMARIES.get(
                item_key,
                ("Oracle Query", query_name, "已执行只读查询", "保留查询证据并结合业务场景复核。"),
            )
            execution_status = result_writer.finding_execution_status(SimpleNamespace(
                metric_name=item_key, metric_value="", check_status=evidence.get("status"), raw_data=evidence,
            ))
            status = "warning" if execution_status != "success" else "normal"
            row_count = evidence.get("row_count")
            message = str(evidence.get("message") or "").strip()
            if execution_status != "success":
                message = message or "查询未成功采集，无法评估此检查项。"
            findings.append(
                Finding(
                    category,
                    check_item,
                    status,
                    item_key,
                    "collection_failed" if execution_status != "success" else str(row_count if row_count is not None else "unknown"),
                    "采集 SQL 成功执行",
                    message or f"{finding_text}，返回行数：{row_count if row_count is not None else '未知'}。",
                    (
                        "检查巡检账号权限、数据库版本和相关组件。"
                        if status == "warning"
                        else suggestion
                    ),
                    {
                        **evidence,
                        "query_name": query_name,
                        "template_item_key": item_key,
                        "template_threshold_eligible": False,
                    },
                )
            )
            covered.add((query_name, item_key))


def _attach_oracle_query_evidence(
    findings: list[Finding],
    metrics: dict,
) -> None:
    evidence_by_query = metrics.get("_query_evidence")
    if not isinstance(evidence_by_query, dict):
        return
    for finding in findings:
        query_name = _oracle_finding_query_name(finding)
        evidence = evidence_by_query.get(query_name)
        if not isinstance(evidence, dict):
            continue
        original = dict(finding.raw_data) if isinstance(finding.raw_data, dict) else {}
        # Analyzer metrics can be derived or error sentinels; they are not cursor rows.
        finding.raw_data = {
            **original,
            **evidence,
            "query_name": query_name,
            "analyzed_metric": original,
        }


def _oracle_finding_query_name(finding: Finding) -> str | None:
    category = str(finding.check_category or "").lower()
    metric = str(finding.metric_name or "").lower()
    if metric == "used_pct":
        if "表空间" in category:
            return "tablespace_usage"
        if "asm" in category:
            return "asm_usage"
        if "fra" in category or "归档" in category:
            return "fra_usage"
    if metric == "temp_used_pct":
        return "temp_usage"
    if metric == "undo_used_pct":
        return "undo_usage"
    if metric == "listener_status":
        return "listener_parameters"
    if metric == "size_gb":
        return "archive_growth"
    if metric == "seconds_in_wait" or "锁等待" in category:
        return "locks"
    if metric == "invalid_count":
        return "invalid_objects"
    if metric == "stale_stats":
        return "stale_statistics"
    if metric == "collector_error":
        if "临时" in category:
            return "temp_usage"
        if "undo" in category:
            return "undo_usage"
        if "监听" in category:
            return "listener_parameters"
        if "表空间" in category:
            return "tablespace_usage"
        if "asm" in category:
            return "asm_usage"
        if "fra" in category or "归档" in category:
            return "fra_usage"
        if "对象" in category:
            return "invalid_objects"
        if "统计" in category:
            return "stale_statistics"
    return None


def _filter_oracle_analyzer_findings(
    findings: list[Finding],
    policy_items: list[dict] | None,
) -> list[Finding]:
    from app.services.inspection.oracle_script_checks import enabled_oracle_query_keys

    allowed = enabled_oracle_query_keys(policy_items)
    if allowed is None:
        return findings

    filtered: list[Finding] = []
    for finding in findings:
        item_key = _oracle_analyzer_item_key(finding)
        runtime_finding = finding.metric_name in {"script_name", "account_capability", "inspection"}
        if not runtime_finding and (not item_key or item_key not in allowed):
            continue
        raw_data = dict(finding.raw_data) if isinstance(finding.raw_data, dict) else {}
        if item_key:
            raw_data["template_item_key"] = item_key
        if runtime_finding:
            raw_data["inspection_runtime"] = True
        finding.raw_data = raw_data
        filtered.append(finding)
    return filtered


def _oracle_analyzer_item_key(finding: Finding) -> str | None:
    category = str(finding.check_category or "").lower()
    metric = str(finding.metric_name or "").lower()
    if metric == "used_pct" and "表空间" in category:
        return "oracle.tablespace.used_pct"
    if metric == "used_pct" and "asm" in category:
        return "oracle.tablespace.used_pct"
    if metric == "used_pct" and "fra" in category:
        return "oracle.fra.used_pct"
    if metric == "temp_used_pct":
        return "oracle.temp.used_pct"
    if metric == "undo_used_pct":
        return "oracle.undo.used_pct"
    if metric == "listener_status":
        return "oracle.listener.status"
    if metric == "size_gb" and "归档" in category:
        return "oracle.archive.generate_mb_per_hour"
    if metric == "seconds_in_wait":
        return "oracle.blocking.seconds"
    if metric == "invalid_count":
        return "oracle.objects.invalid_count"
    if metric == "stale_stats":
        return "oracle.objects.stats_stale"
    if metric == "collector_error":
        if "临时" in category:
            return "oracle.temp.used_pct"
        if "undo" in category:
            return "oracle.undo.used_pct"
        if "监听" in category:
            return "oracle.listener.status"
        if "表空间" in category:
            return "oracle.tablespace.used_pct"
        if "对象" in category:
            return "oracle.objects.invalid_count"
        if "统计" in category:
            return "oracle.objects.stats_stale"
    return None


def _collect_mysql(db: Session, instance: DBInstance, policy=None) -> list[Finding]:
    from app.services.inspection import script_db_checks
    from app.core.database_options import redact_database_error

    policy_items = getattr(policy, "item_config_json", None) if policy is not None else None
    findings = script_db_checks.collect_mysql_script_findings(
        instance,
        policy_items=policy_items,
    )
    selected = script_db_checks._enabled_source_keys(policy_items)
    slow_keys = {"performance.slow_sql", "mysql.slow_queries.per_min",
                 "mysql.script.performance", "mysql.script.long_queries"}
    if selected is not None and not selected.intersection(slow_keys):
        return findings
    sql = MYSQL_QUERIES["slow_sql"]
    try:
        # Script sections expose counters/truncated sessions, not digest snapshots.
        with mysql_connection(instance, read_timeout=10) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
        persistence = _stage_or_save_slow_sql(db, instance, {"slow_sql": rows}, engine="mysql", sql=sql)
    except Exception as exc:
        saved = {"status": "failed", "count": 0, "record_ids": [], "errors": [redact_database_error(exc)]}
        persistence = _slow_sql_finding("mysql", saved, sql=sql)
    findings.append(persistence)
    return findings


def _slow_sql_finding(engine: str, saved: dict, *, sql: str | None = None) -> Finding:
    if engine == "oracle":
        return Finding(
            "SQL 性能", "Oracle Top SQL 持久化", "normal" if saved["status"] == "success" else "warning",
            "oracle.slow_sql.persistence", str(saved["count"]), "selected sources persisted",
            f"已保存 {saved['count']} 条慢 SQL 记录。" if saved["status"] == "success" else
            f"慢 SQL 持久化 {saved['status']}：" + "; ".join(saved["errors"]),
            "按实际记录 ID 打开慢 SQL；部分采集或保存失败不能当作空集成功。",
            {**saved, "execution_status": saved["status"], "read_only": True},
        )
    return Finding(
        "SQL 性能", "MySQL 慢 SQL 持久化", "normal" if saved["status"] == "success" else "warning",
        "mysql.slow_sql.persistence", str(saved["count"]), "selected sources persisted",
        f"已保存 {saved['count']} 条慢 SQL 记录。" if saved["status"] == "success" else
        "慢 SQL 采集或持久化失败：" + "; ".join(saved["errors"]),
        "按实际记录 ID 打开慢 SQL；采集或保存失败不能当作空集成功。",
        {**saved, "execution_status": saved["status"], "read_only": True, "sql": sql,
         "source": "mysql.performance_schema.events_statements_summary_by_digest",
         "statistics_scope": "digest_cumulative", "window_aggregation": False},
    )


def _stage_or_save_slow_sql(db, instance, metrics, *, engine, source_keys=(), sql=None) -> Finding:
    staged = _STAGED_SLOW_SQL.get()
    if staged is None:
        saved = _save_mysql_slow_sql(db, instance, metrics) if engine == "mysql" else _save_oracle_slow_sql(
            db, instance, metrics, source_keys=source_keys,
        )
        return _slow_sql_finding(engine, saved, sql=sql)
    pending = {"status": "pending", "count": 0, "record_ids": [], "errors": [], "persistence_state": "staged"}
    finding = _slow_sql_finding(engine, pending, sql=sql)
    staged.append(StagedSlowSQLWrite(
        engine=engine, instance_id=instance.id, metrics=deepcopy(metrics),
        source_keys=tuple(source_keys), finding=finding, sql=sql,
    ))
    return finding


def materialize_staged_writes(db, findings, *, instance_id=None) -> list[Finding]:
    """Use the existing writers in the caller's fenced result transaction, without commit."""
    staged = getattr(findings, "staged_writes", ())
    if not staged:
        return findings
    published = deepcopy(list(findings))
    copies = {id(original): copied for original, copied in zip(findings, published)}
    for write in staged:
        if instance_id is not None and write.instance_id != instance_id:
            raise ValueError("inspection_staged_target_mismatch")
        target = SimpleNamespace(id=write.instance_id)
        saved = _save_mysql_slow_sql(db, target, write.metrics) if write.engine == "mysql" else _save_oracle_slow_sql(
            db, target, write.metrics, source_keys=write.source_keys,
        )
        persisted = _slow_sql_finding(write.engine, saved, sql=write.sql)
        copied = copies[id(write.finding)]
        copied.check_status = persisted.check_status
        copied.metric_value = persisted.metric_value
        copied.finding = persisted.finding
        copied.raw_data = {**(copied.raw_data or {}), **persisted.raw_data}
        copied.raw_data.pop("persistence_state", None)
    return published


def _policy_revision(policy) -> str:
    payload = {
        "policy_key": getattr(policy, "policy_key", None),
        "name": getattr(policy, "name", None),
        "db_types": getattr(policy, "db_types_json", None) or [],
        "items": getattr(policy, "item_config_json", None) or [],
        "thresholds": getattr(policy, "threshold_config_json", None) or {},
        "actions": getattr(policy, "action_config_json", None) or [],
        "report": getattr(policy, "report_config_json", None) or {},
        "source_script": getattr(policy, "source_script", None),
        "oracle_profile": getattr(policy, "resolved_oracle_profile", None),
        "updated_at": _time_text(getattr(policy, "update_time", None)),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _time_text(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _collect_host(instance: DBInstance, policy=None) -> list[Finding]:
    try:
        if getattr(policy, "resolved_host_platform", None) == "windows" or (getattr(policy, "report_config_json", None) or {}).get("host_platform") == "windows":
            from .windows_host_checks import collect_windows_host_findings

            return collect_windows_host_findings(instance, getattr(policy, "item_config_json", None))
        from app.services import host_overview

        overview = host_overview.get_instance_host_overview(instance)
        policy_items = getattr(policy, "item_config_json", None) if policy is not None else None
        if policy_items is not None:
            checks = host_overview.build_template_checks(overview, policy_items)
            return [
                Finding(
                    check_category=str(item.get("name") or "Host Resources"),
                    check_item=str(item.get("name") or item.get("key") or "主机检查项"),
                    check_status=str(item.get("status") or "info"),
                    metric_name=str(item.get("key") or "host.collector_status"),
                    metric_value=str(
                        item.get("metric_value")
                        if item.get("metric_value") is not None
                        else ""
                    ),
                    threshold_value=str(item.get("threshold_value") or ""),
                    finding=str(item.get("finding") or "主机检查完成"),
                    suggestion=str(item.get("suggestion") or "保留当前主机基线。"),
                    raw_data={
                        "template_item_key": item.get("key"),
                        "template_threshold_eligible": False,
                        "evidence": item.get("evidence"),
                        "read_only": True,
                        "source": "host_overview",
                    },
                )
                for item in checks
            ]
        resources = overview.get("resources") or {}
        warnings = [
            item.get("message")
            for item in (overview.get("risks") or [])
            if item.get("message")
        ]
        if overview.get("error"):
            warnings.append(overview.get("error"))
        status = "warning" if warnings or not overview.get("ssh_enabled") else "ok"
        message = "; ".join(str(item) for item in warnings[:3])
        return [
            Finding(
                check_category="Host Resources",
                check_item="Host resource overview",
                check_status=status,
                metric_name="ssh_enabled",
                metric_value=(
                    "collection_failed"
                    if overview.get("error") or not overview.get("ssh_enabled")
                    else str(bool(overview.get("ssh_enabled")))
                ),
                threshold_value="true",
                finding=message or "Host inspection completed without obvious collection errors.",
                suggestion=(
                    "Configure SSH credentials for complete host resource inspection."
                    if status == "warning"
                    else "Keep regular host resource inspection enabled."
                ),
                raw_data={"overview": overview, "resources": resources},
            )
        ]
    except Exception as exc:
        return [_degraded(instance, f"Host inspection failed: {exc}")]


def _degraded(instance: DBInstance, message: str) -> Finding:
    return Finding(
        check_category="Inspection Runtime",
        check_item="Collector degraded",
        check_status="warning",
        metric_name="collector_status",
        metric_value="degraded",
        threshold_value="ok",
        finding=message,
        suggestion="Check connectivity, driver dependencies, and read-only monitoring privileges.",
        raw_data={
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "read_only": True,
            "error": message,
        },
    )


def _save_mysql_slow_sql(db: Session, instance: DBInstance, metrics: dict) -> dict:
    from app.collectors.oracle.sql_text import validate_stored_sql_text
    from app.core.database_options import redact_database_error

    instance_id = instance.id
    result = {"status": "failed", "count": 0, "record_ids": [], "errors": []}
    try:
        from app.services import system_settings as settings_svc

        crit = settings_svc.get_int(db, "slow_sql_critical_ms", 5000)
        warn = settings_svc.get_int(db, "slow_sql_warning_ms", 1000)
        rows = metrics.get("slow_sql")
        if not isinstance(rows, list) or any(not isinstance(row, dict) or "error" in row for row in rows):
            raise ValueError("MySQL slow SQL query returned missing, invalid or failed rows")
        records = []
        captured_at = datetime.now()
        for row in rows:
            sql_id = row.get("digest")
            if not isinstance(sql_id, str) or not sql_id or len(sql_id) > 100:
                raise ValueError("MySQL slow SQL row has no valid SQL identity")
            avg_ms = _slow_sql_number(_slow_sql_number(row.get("avg_seconds"), "avg_seconds") * 1000, "avg_elapsed_ms")
            total_ms = _slow_sql_number(_slow_sql_number(row.get("total_seconds"), "total_seconds") * 1000, "total_elapsed_ms")
            examined = row.get("sum_rows_examined")
            records.append(
                DBSlowSQL(
                    instance_id=instance_id,
                    db_type="mysql",
                    sql_id=sql_id,
                    schema_name=row.get("schema_name"),
                    sql_text=validate_stored_sql_text(row.get("digest_text")),
                    executions=_slow_sql_number(row.get("count_star"), "executions", integer=True),
                    avg_elapsed_ms=avg_ms,
                    total_elapsed_ms=total_ms,
                    rows_processed=_slow_sql_number(examined, "rows_processed", integer=True) if examined is not None else None,
                    risk_level="critical"
                    if avg_ms > crit
                    else "warning"
                    if avg_ms > warn
                    else "normal",
                    capture_time=captured_at,
                    create_time=captured_at,
                )
            )
        if records:
            if db.get_bind(mapper=DBSlowSQL).dialect.name == "sqlite":
                connection = db.connection(bind_arguments={"mapper": DBSlowSQL})
                # SQLite legacy mode does not BEGIN on SELECT; a root savepoint would commit on release.
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN")
            with db.begin_nested():
                db.add_all(records)
                db.flush()
        result.update(status="success", count=len(records), record_ids=[record.id for record in records])
    except Exception as exc:
        message = redact_database_error(exc)
        result["errors"].append(message)
        logger.warning("MySQL slow SQL persistence failed | instance_id=%s | error=%s", instance_id, message)
    return result


def _save_oracle_slow_sql(
    db: Session, instance: DBInstance, metrics: dict, *,
    source_keys=("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads"),
) -> dict:
    from app.core.database_options import redact_database_error
    from app.collectors.oracle.sql_text import validate_stored_sql_text

    instance_id = instance.id
    result = {"status": "failed", "count": 0, "errors": [], "source_keys": list(source_keys), "record_ids": []}
    rows = {}
    successful_sources = []
    for source in source_keys:
        items = metrics.get(source)
        evidence = (metrics.get("_query_evidence") or {}).get(source) or {}
        if not isinstance(items, list):
            result["errors"].append(f"{source}: collector result missing or invalid")
            continue
        valid_source = evidence.get("status") in (None, "normal", "success", "available")
        if not valid_source:
            result["errors"].append(f"{source}: {redact_database_error(evidence.get('message') or 'collector query failed')}")
        for row in items:
            if not isinstance(row, dict) or row.get("error"):
                message = row.get("error") if isinstance(row, dict) else "invalid row"
                result["errors"].append(f"{source}: {redact_database_error(message)}")
                valid_source = False
                continue
            if not row.get("sql_id") or not str(row.get("sql_text") or "").strip():
                result["errors"].append(f"{source}: SQL identity or text missing")
                valid_source = False
                continue
            key = (row.get("schema_name"), row["sql_id"], row.get("con_id"), row.get("child_number"), row.get("plan_hash"))
            # The three rankings overlap. Keep one observation per child/plan, not a sum.
            rows.setdefault(key, row)
        if valid_source:
            successful_sources.append(source)
    result["successful_sources"] = successful_sources
    try:
        from app.services import system_settings as settings_svc

        records = []
        if rows:
            if db.get_bind(mapper=DBSlowSQL).dialect.name == "sqlite":
                connection = db.connection(bind_arguments={"mapper": DBSlowSQL})
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN")
            with db.begin_nested():
                crit = settings_svc.get_int(db, "slow_sql_critical_ms", 5000)
                warn = settings_svc.get_int(db, "slow_sql_warning_ms", 1000)
                captured_at = datetime.now()
                for row in rows.values():
                    total_ms = _slow_sql_number(
                        _slow_sql_number(row.get("elapsed_seconds"), "elapsed_seconds") * 1000, "total_elapsed_ms",
                    )
                    executions = _slow_sql_number(row.get("executions"), "executions", integer=True)
                    avg_ms = _slow_sql_number(total_ms / executions if executions else 0, "avg_elapsed_ms")
                    counters = {
                        target: _slow_sql_number(row[source], target, integer=True) if row.get(source) is not None else None
                        for target, source in (("logical_reads", "buffer_gets"), ("physical_reads", "disk_reads"),
                                               ("rows_processed", "rows_processed"))
                    }
                    records.append(DBSlowSQL(
                        instance_id=instance_id,
                        db_type="oracle",
                        sql_id=row.get("sql_id"),
                        schema_name=row.get("schema_name"),
                        sql_text=validate_stored_sql_text(row.get("sql_text")),
                        executions=executions,
                        avg_elapsed_ms=avg_ms,
                        total_elapsed_ms=total_ms,
                        **counters,
                        risk_level="critical" if avg_ms > crit else "warning" if avg_ms > warn else "normal",
                        capture_time=captured_at,
                        create_time=captured_at,
                    ))
                db.add_all(records)
                db.flush()
        result.update(
            count=len(records), record_ids=[record.id for record in records],
            status="partial" if result["errors"] and (records or successful_sources) else
                   "failed" if result["errors"] else "success",
        )
    except Exception as exc:
        message = redact_database_error(exc)
        result["errors"].append(message)
        logger.warning("Oracle slow SQL persistence failed | instance_id=%s | error=%s", instance_id, message)
    return result
