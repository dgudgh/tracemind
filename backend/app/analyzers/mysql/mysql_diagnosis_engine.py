import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.inspection_diagnosis import (
    DBDiagnosisFinding,
    DBInspectionModuleResult,
    DBInspectionTask,
)
from app.services.mysql_backup import generate_backup_plan
from app.services.mysql_basic import get_basic_info
from app.services.mysql_binlog import get_binlog_overview
from app.services.mysql_innodb import get_innodb_status
from app.services.mysql_lock import get_lock_transaction_center
from app.services.mysql_ops_extra import get_error_log_info, get_security_summary
from app.services.mysql_replication import get_replication_status
from app.services.mysql_slow_sql import get_slow_sql_center
from app.services.mysql_table_index import get_table_index_center
from app.services.mysql_table_statistics import get_table_statistics_center

logger = logging.getLogger("app.analyzers.mysql.mysql_diagnosis_engine")


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    name: str
    collector: Callable[[DBInstance, dict[str, Any]], dict[str, Any]]


SEVERITY_WEIGHT = {"critical": 25, "high": 18, "warning": 10, "info": 4, "normal": 0}
SEVERITY_RISK_SCORE = {"critical": 95, "high": 82, "warning": 68, "info": 45, "normal": 0}
MODULE_MANIFEST_SCHEMA = "mysql.diagnosis_module_manifest/v1"


def _valid_module_key(key):
    return isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", key) is not None


def _manifest_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _freeze_module_specs(instance_id, task_id):
    specs = list(_module_specs())
    keys = []
    for spec in specs:
        if (not isinstance(spec, ModuleSpec) or not _valid_module_key(spec.key) or spec.key in keys
                or not isinstance(spec.name, str) or not spec.name.strip() or len(spec.name) > 100
                or not callable(spec.collector)):
            # Never filter/deduplicate a broken registry into apparently complete coverage.
            raise ValueError("mysql_diagnosis_invalid_module_specs")
        keys.append(spec.key)
    body = {"schema": MODULE_MANIFEST_SCHEMA, "task_id": task_id, "instance_id": instance_id, "module_keys": keys}
    return specs, {**body, "digest": _manifest_digest(body)}


def _json_safe(value):
    """递归把 Decimal/日期/字节等转成可 JSON 序列化的类型，供 JSON 列存储。"""
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return [_json_safe(v) for v in value]
    return value


def run_mysql_diagnosis(db: Session, instance: DBInstance) -> dict[str, Any]:
    task_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    started_at = datetime.now()
    specs, manifest = _freeze_module_specs(instance.id, task_id)
    context: dict[str, Any] = {}

    module_payloads: list[dict[str, Any]] = []
    finding_payloads: list[dict[str, Any]] = []

    for spec in specs:
        module_data = _safe_collect(spec, instance, context)
        module_result, findings = _analyze_module(spec, instance, task_id, module_data)
        module_result["evidence"]["collection"]["manifest"] = {**manifest, "module_keys": list(manifest["module_keys"])}
        module_payloads.append(module_result)
        finding_payloads.extend(findings)

    overall = _build_overall(instance, task_id, started_at, module_payloads, finding_payloads)
    task = DBInspectionTask(**overall["task"])
    modules = [DBInspectionModuleResult(**_json_safe(item)) for item in module_payloads]
    findings = [DBDiagnosisFinding(**_json_safe(item)) for item in finding_payloads]

    db.add(task)
    db.add_all(modules)
    db.add_all(findings)
    db.commit()
    db.refresh(task)
    for item in modules:
        db.refresh(item)
    for item in findings:
        db.refresh(item)

    return _diagnosis_view(instance, task, modules, _query_findings(db, instance.id, task_id))


def get_latest_mysql_diagnosis(db: Session, instance_id: int) -> dict[str, Any] | None:
    task = (
        db.query(DBInspectionTask)
        .filter(DBInspectionTask.instance_id == instance_id, DBInspectionTask.db_type == "mysql")
        .order_by(DBInspectionTask.id.desc())
        .first()
    )
    if not task:
        return None
    return get_mysql_diagnosis(db, instance_id, task.task_id)


def get_mysql_diagnosis(db: Session, instance_id: int, task_id: str) -> dict[str, Any] | None:
    task = (
        db.query(DBInspectionTask)
        .filter(DBInspectionTask.instance_id == instance_id, DBInspectionTask.task_id == task_id, DBInspectionTask.db_type == "mysql")
        .first()
    )
    if not task:
        return None
    modules = _query_modules(db, instance_id, task_id)
    findings = _query_findings(db, instance_id, task_id)
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    return _diagnosis_view(instance, task, modules, findings)


def get_mysql_diagnosis_modules(db: Session, instance_id: int, task_id: str) -> list[dict[str, Any]]:
    return [_module_to_dict(item) for item in _query_modules(db, instance_id, task_id)]


def get_mysql_diagnosis_findings(db: Session, instance_id: int, task_id: str) -> list[dict[str, Any]]:
    return [_finding_to_dict(item) for item in _query_findings(db, instance_id, task_id)]


def build_ai_analysis_payload(db: Session, instance_id: int, task_id: str) -> dict[str, Any] | None:
    diagnosis = get_mysql_diagnosis(db, instance_id, task_id)
    if not diagnosis:
        return None
    report = diagnosis["report"]
    return {
        "status": "plan_only",
        "message": "已生成 MySQL 巡检 AI 分析输入。未配置大模型时返回规则诊断摘要。",
        "ai_input": {
            "inspection_result": diagnosis["task"],
            "module_scores": diagnosis["modules"],
            "findings": diagnosis["findings"],
            "required_output": [
                "diagnosis_summary",
                "root_cause_ranking",
                "evidence",
                "business_impact",
                "priority",
                "operation_steps",
                "rollback_plan",
                "ticket_recommendation",
            ],
        },
        "analysis": {
            "summary": report.get("overall_conclusion"),
            "root_cause_ranking": report.get("root_cause_analysis"),
            "business_impact": report.get("business_impact"),
            "priority": report.get("recommended_priorities"),
            "ticket_recommendation": bool(diagnosis["findings"]),
        },
    }


def build_ticket_plan_payload(db: Session, instance_id: int, task_id: str) -> dict[str, Any] | None:
    diagnosis = get_mysql_diagnosis(db, instance_id, task_id)
    if not diagnosis:
        return None
    top_findings = diagnosis["findings"][:5]
    return {
        "status": "plan_only",
        "message": "已生成工单创建方案，不直接调用外部系统。",
        "ticket_plan": {
            "title": f"MySQL 巡检诊断风险处置 - task {task_id}",
            "priority": diagnosis["task"].get("risk_level"),
            "summary": diagnosis["task"].get("summary"),
            "items": [
                {
                    "check_item": item.get("check_item"),
                    "severity": item.get("severity"),
                    "finding": item.get("finding"),
                    "suggestions": item.get("suggestions"),
                    "operation_plan": item.get("operation_plan"),
                }
                for item in top_findings
            ],
        },
    }


def _module_specs() -> list[ModuleSpec]:
    return [
        ModuleSpec("basic_profile", "实例基础画像", _collect_basic),
        ModuleSpec("availability_connection", "可用性与连接", _collect_basic),
        ModuleSpec("replication_ha", "复制与高可用", _collect_replication),
        ModuleSpec("binlog_recovery", "Binlog / GTID / 恢复能力", _collect_binlog),
        ModuleSpec("innodb_health", "InnoDB 健康", _collect_innodb),
        ModuleSpec("locks_transactions", "锁与事务", _collect_locks),
        ModuleSpec("slow_sql_top", "慢 SQL Top", _collect_slow_sql),
        ModuleSpec("table_index", "表与索引", _collect_table_index),
        ModuleSpec("table_statistics", "表统计信息", _collect_table_statistics),
        ModuleSpec("security_account", "安全与账号", _collect_security),
        ModuleSpec("logs_backup", "日志与备份", _collect_logs_backup),
        ModuleSpec("host_context", "主机上下文", _collect_host_context),
    ]


def _collect_basic(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    if "basic_info" not in context:
        context["basic_info"] = get_basic_info(instance)
    return context["basic_info"]


def _collect_replication(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_replication_status(instance)


def _collect_binlog(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_binlog_overview(instance)


def _collect_innodb(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_innodb_status(instance)


def _collect_locks(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_lock_transaction_center(instance)


def _collect_slow_sql(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_slow_sql_center(instance, 200, "24h")


def _collect_table_index(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_table_index_center(instance)


def _collect_table_statistics(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_table_statistics_center(instance, 500)


def _collect_security(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return get_security_summary(instance)


def _collect_logs_backup(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "error_log": get_error_log_info(instance),
        "backup_plan_hint": "此功能暂不支持。",
    }


def _collect_host_context(instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    warnings = []
    if not getattr(instance, "ssh_host", None):
        warnings.append("未配置 SSH，跳过主机 CPU、内存、磁盘、I/O、日志文件系统采集。")
    return {
        "summary": {"risk_level": "info" if warnings else "normal"},
        "warnings": warnings,
        "raw": {
            "ssh_configured": bool(getattr(instance, "ssh_host", None)),
            "zabbix_prometheus_context": "主机资源联动需要配置 SSH/Zabbix/Prometheus 后启用。",
        },
    }


def _safe_collect(spec: ModuleSpec, instance: DBInstance, context: dict[str, Any]) -> dict[str, Any]:
    try:
        data = spec.collector(instance, context)
        if isinstance(data, dict):
            return data
        return {"raw": data}
    except Exception as exc:
        logger.exception("mysql diagnosis module failed | module=%s | instance=%s", spec.key, instance.id)
        return {"error": str(exc), "warnings": [f"{spec.name} 采集失败：{exc}"]}


def _analyze_module(spec: ModuleSpec, instance: DBInstance, task_id: str, data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    collection = _collection_truth(spec.key, data)
    risks = _extract_risks(data)
    if data.get("error"):
        risks.insert(0, {"level": "warning", "item": "模块采集失败", "message": str(data["error"]), "suggestion": "检查账号权限、版本兼容性和网络连通性后重试。"})

    score = max(0, 100 - sum(SEVERITY_WEIGHT.get(_normalize_level(item.get("level") or item.get("severity") or item.get("risk_level")), 10) for item in risks))
    risk_level = _score_to_level(score)
    if any(_normalize_level(item.get("level") or item.get("severity") or item.get("risk_level")) == "critical" for item in risks):
        risk_level = "critical"

    usable = collection["execution_status"] in {"success", "partial"}
    collection["observed_score"] = score if usable else None
    collection["observed_risk_level"] = risk_level if usable else "unknown"
    complete = collection["execution_status"] == "success"
    summary = _module_summary(spec, data, risks) if complete else _incomplete_summary(spec.name, collection)
    module = {
        "task_id": task_id,
        "instance_id": instance.id,
        "module_key": spec.key,
        "module_name": spec.name,
        "score": score if complete else None,
        "risk_level": risk_level if complete else _incomplete_risk(collection, risks),
        "summary": summary,
        "evidence": {**_module_evidence(data), "collection": collection},
        "top_risks": risks[:5],
        "suggestions": _module_suggestions(risks),
        "raw_data": _compact_raw(data),
    }
    findings = [_risk_to_finding(task_id, instance, spec, risk, index) for index, risk in enumerate(risks, start=1)]
    if not complete:
        for finding in findings:
            finding["raw_data"] = {**finding["raw_data"], "execution_status": collection["execution_status"],
                                   "data_quality": "partial" if collection["observation_count"] else "missing"}
    return module, findings


# Only collector observation paths count. Summaries, warnings, counts derived from empty
# lists, action flags, backup scripts and configured host addresses are not measurements.
OBSERVATION_PATHS = {
    "basic_profile": ("raw.variables", "raw.status"),
    "availability_connection": ("raw.variables", "raw.status"),
    "replication_ha": ("variables", "master", "replicas"),
    "binlog_recovery": ("variables", "files", "cache_status"),
    "innodb_health": ("raw", "metrics", "variables"),
    "locks_transactions": ("raw.processlist", "raw.innodb_trx", "raw.innodb_status", "sections.row_lock_stats", "sections.table_lock_stats"),
    "slow_sql_top": ("digest_details", "current_running", "config.variables"),
    "table_index": ("tables", "indexes"),
    "table_statistics": ("sections.table_stats", "sections.cardinality", "sections.histograms", "sections.partition_stats", "sections.config.variables"),
    "security_account": ("users",),
    "logs_backup": ("error_log.variables",),
    "host_context": (),
}
COLLECTION_SCHEMA = "mysql.diagnosis_collection/v1"


def _observation_count(value):
    if isinstance(value, dict):
        return sum(_observation_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_observation_count(item) for item in value)
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "-", "unknown", "n/a"}):
        return 0
    if isinstance(value, (int, float, Decimal)) and not math.isfinite(float(value)):
        return 0
    return int(isinstance(value, (str, int, float, Decimal, bool, date, datetime)))


def _collection_truth(module_key, data):
    sources, count = [], 0
    for path in OBSERVATION_PATHS.get(module_key, ()):
        value = data
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        observed = _observation_count(value)
        if observed:
            sources.append(path)
            count += observed
    status = data.get("execution_status") or data.get("status")
    status = status if isinstance(status, str) else None
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    quality = data.get("data_quality") if isinstance(data.get("data_quality"), str) else None
    level = summary.get("risk_level") if isinstance(summary.get("risk_level"), str) else None
    unconfirmed = quality in {"missing", "unknown", "not_collected"} or level in {"unknown", "unavailable", "missing"}
    nested_error = isinstance(data.get("error_log"), dict) and data["error_log"].get("error")
    failed = bool(data.get("error") or nested_error or status in {"failed", "error", "degraded"})
    incomplete = failed or bool(data.get("warnings")) or quality == "partial" or status in {"partial", "unknown", "not_collected", "skipped", "plan_only"}
    # These two collectors describe configuration/plans, not host/log/backup execution.
    incomplete = incomplete or module_key in {"host_context", "logs_backup"}
    state = ("partial" if incomplete else "success") if count else "failed" if failed else "unknown"
    if unconfirmed or status in {"unknown", "not_collected", "skipped", "plan_only"}:
        state = "unknown"
    basis = {"success": "complete_evidence", "partial": "partial_evidence", "failed": "collection_failed", "unknown": "no_evidence"}[state]
    return {"schema": COLLECTION_SCHEMA, "execution_status": state, "score_basis": basis,
            "observation_count": count, "observed_sources": sources}


def _incomplete_summary(name, collection):
    state = {"failed": "采集失败", "partial": "仅有部分采集证据", "unknown": "没有可确认的采集证据"}[collection["execution_status"]]
    return f"{name}{state}，未生成完整健康评分，不能判断未采集范围是否健康。"


def _incomplete_risk(collection, risks):
    if collection["execution_status"] == "partial" and collection["observation_count"]:
        levels = {_normalize_level(item.get("level") or item.get("severity") or item.get("risk_level")) for item in risks if isinstance(item, dict)}
        if "critical" in levels:
            return "critical"
        if levels & {"high", "warning"}:
            return "warning"
    return "unknown"


def _valid_score(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 100


def _extract_risks(data: dict[str, Any]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for key in ("risk_items", "risks", "recommendations", "warnings"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    risks.append(item)
                elif item:
                    risks.append({"level": "info" if key == "warnings" else "warning", "item": key, "message": str(item), "suggestion": "结合模块明细确认后处理。"})

    summary = data.get("summary")
    if isinstance(summary, dict):
        level = _normalize_level(summary.get("risk_level") or summary.get("riskLevel"))
        if level not in ("normal", ""):
            message = summary.get("health_summary_text") or summary.get("main_risk_text") or summary.get("summary") or f"模块状态为 {level}"
            risks.append({"level": level, "item": "模块摘要", "message": message, "suggestion": "查看该模块证据链并按优先级处理。"})
    return _dedupe_risks(risks)


def _dedupe_risks(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for risk in risks:
        key = (str(risk.get("item") or risk.get("title") or ""), str(risk.get("message") or risk.get("finding") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(risk)
    return sorted(result, key=lambda item: SEVERITY_RISK_SCORE.get(_normalize_level(item.get("level") or item.get("severity") or item.get("risk_level")), 0), reverse=True)


def _risk_to_finding(task_id: str, instance: DBInstance, spec: ModuleSpec, risk: dict[str, Any], index: int) -> dict[str, Any]:
    severity = _normalize_level(risk.get("level") or risk.get("severity") or risk.get("risk_level"))
    message = risk.get("message") or risk.get("finding") or risk.get("title") or risk.get("item") or "发现风险"
    suggestion = risk.get("suggestion") or risk.get("advice") or risk.get("recommendation") or "结合证据链确认影响范围后处理。"
    evidence = risk.get("evidence") or risk.get("raw") or {}
    related_objects = _guess_related_objects(risk)
    related_sql = _guess_related_sql(risk)
    return {
        "task_id": task_id,
        "instance_id": instance.id,
        "db_type": "mysql",
        "module_key": spec.key,
        "check_item": str(risk.get("item") or risk.get("title") or f"{spec.name} 风险 {index}")[:200],
        "severity": severity,
        "risk_score": SEVERITY_RISK_SCORE.get(severity, 45),
        "finding": str(message),
        "evidence": evidence if isinstance(evidence, (dict, list)) else [str(evidence)],
        "root_causes": _root_causes(spec.key, risk),
        "business_impact": _business_impact(spec.key, severity),
        "suggestions": [str(suggestion)],
        "operation_plan": _operation_plan(spec.key, risk),
        "rollback_plan": _rollback_plan(spec.key),
        "related_objects": related_objects,
        "related_sql": related_sql,
        "raw_data": risk,
    }


def _normalize_level(value: Any) -> str:
    text = str(value or "").lower()
    if text in ("critical", "fatal", "error", "danger"):
        return "critical"
    if text in ("high", "major"):
        return "high"
    if text in ("warning", "warn"):
        return "warning"
    if text in ("info", "notice", "hint"):
        return "info"
    return "normal"


def _score_to_level(score: int) -> str:
    if score < 60:
        return "critical"
    if score < 75:
        return "warning"
    if score < 90:
        return "info"
    return "normal"


def _module_summary(spec: ModuleSpec, data: dict[str, Any], risks: list[dict[str, Any]]) -> str:
    summary = data.get("summary")
    if isinstance(summary, dict):
        for key in ("health_summary_text", "summary", "main_risk_text", "message"):
            if summary.get(key):
                return str(summary[key])
    if risks:
        return "；".join(str(item.get("message") or item.get("finding") or item.get("title") or item.get("item")) for item in risks[:3])
    return f"{spec.name} 未发现明显风险。"


def _module_evidence(data: dict[str, Any]) -> dict[str, Any]:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    return {
        "summary": summary,
        "counts": {
            key: len(value)
            for key, value in sections.items()
            if isinstance(value, list)
        },
        "warnings": data.get("warnings", []),
        "error": data.get("error") or (data["error_log"].get("error") if isinstance(data.get("error_log"), dict) else None),
    }


def _module_suggestions(risks: list[dict[str, Any]]) -> list[str]:
    suggestions = []
    for item in risks[:5]:
        suggestion = item.get("suggestion") or item.get("advice") or item.get("recommendation")
        if suggestion:
            suggestions.append(str(suggestion))
    return suggestions


def _compact_raw(data: dict[str, Any]) -> dict[str, Any]:
    raw = {}
    for key in ("summary", "warnings", "error", "risk_items", "risks", "recommendations", "sections"):
        if key in data:
            raw[key] = data[key]
    return raw or {"raw_available": True}


def _root_causes(module_key: str, risk: dict[str, Any]) -> list[str]:
    mapping = {
        "locks_transactions": ["可能存在长事务、热点行更新、缺失索引或业务事务未及时提交。"],
        "slow_sql_top": ["可能存在扫描行数过大、JOIN 条件缺索引、临时表、filesort 或执行次数过高。"],
        "table_index": ["可能存在无主键表、重复/冗余索引、碎片表或对象治理长期缺失。"],
        "table_statistics": ["可能存在统计信息陈旧、基数失真或优化器估算偏差。"],
        "innodb_health": ["可能存在 Buffer Pool 压力、Undo/Purge 堆积、Redo 等待或 I/O 压力。"],
        "binlog_recovery": ["可能存在 Binlog/GTID 配置不完整、保留策略不足或清理边界不明确。"],
        "replication_ha": ["可能存在复制线程异常、延迟、Relay Log 堆积或只读角色配置风险。"],
        "security_account": ["可能存在远程 root、匿名用户、弱认证插件或权限收敛不足。"],
    }
    return mapping.get(module_key, ["需要结合模块证据、实例角色和业务调用链进一步确认根因。"])


def _business_impact(module_key: str, severity: str) -> list[str]:
    base = {
        "locks_transactions": "可能阻塞 MES 过站、报工、库存扣减等更新操作，并导致请求超时。",
        "slow_sql_top": "可能拖慢核心交易响应，放大连接池占用和锁等待风险。",
        "table_index": "可能影响复制、归档、清理、行定位和批量维护效率。",
        "table_statistics": "可能误导优化器执行计划，导致原本稳定 SQL 突然变慢。",
        "innodb_health": "可能引发 I/O 放大、Undo 堆积、事务提交变慢或实例抖动。",
        "binlog_recovery": "可能影响复制、审计、误操作恢复和故障切换能力。",
        "replication_ha": "可能影响主从一致性、读写切换和灾备恢复窗口。",
        "security_account": "可能扩大越权访问、误操作和审计不可追溯风险。",
    }.get(module_key, "可能影响数据库稳定性、可维护性或故障处置效率。")
    if severity in ("critical", "high"):
        return [base, "建议纳入优先处置清单并评估维护窗口。"]
    return [base]


def _operation_plan(module_key: str, risk: dict[str, Any]) -> dict[str, Any]:
    check_item = str(risk.get("item") or risk.get("title") or module_key)
    plan_sql = []
    if module_key == "locks_transactions":
        plan_sql = ["-- plan_only: 根据阻塞链确认 thread_id 后，再生成 KILL QUERY / KILL SESSION 方案。"]
    elif module_key == "binlog_recovery":
        plan_sql = ["-- plan_only: 清理前确认从库位点和备份，再生成 PURGE BINARY LOGS 方案。"]
    elif module_key == "table_statistics":
        plan_sql = ["-- plan_only: 低峰期确认对象后生成 ANALYZE TABLE 方案。"]
    elif module_key == "table_index":
        plan_sql = ["-- plan_only: 确认执行计划和写入成本后生成 ADD/DROP INDEX 或 OPTIMIZE TABLE 方案。"]
    return {
        "status": "plan_only",
        "steps": [
            f"确认风险项：{check_item}",
            "核对证据链、影响对象、业务窗口和回滚条件。",
            "在维护窗口中执行经审批的脚本；本接口只生成方案，不直接执行。",
        ],
        "sql": plan_sql,
        "rollback_plan": _rollback_plan(module_key),
    }


def _rollback_plan(module_key: str) -> list[str]:
    return [
        "执行前记录当前参数、对象 DDL、Binlog 位点和关键指标快照。",
        "变更后持续观察连接、锁等待、慢 SQL、复制延迟和错误日志。",
        "如指标恶化，按审批脚本回退配置或对象变更；KILL 类操作无法回滚，需提前确认业务可重试。",
    ]


def _guess_related_objects(risk: dict[str, Any]) -> list[str]:
    objects = []
    for key in ("schema_table", "table", "object", "object_name", "table_name"):
        value = risk.get(key)
        if value:
            objects.append(str(value))
    evidence = risk.get("evidence")
    if isinstance(evidence, dict):
        schema = evidence.get("table_schema") or evidence.get("schema_name") or evidence.get("schema")
        table = evidence.get("table_name") or evidence.get("object_name") or evidence.get("table")
        if schema and table:
            objects.append(f"{schema}.{table}")
    return list(dict.fromkeys(objects))


def _guess_related_sql(risk: dict[str, Any]) -> list[str]:
    values = []
    for key in ("sql", "sql_text", "digest_text", "trx_query", "query"):
        value = risk.get(key)
        if value:
            values.append(str(value))
    evidence = risk.get("evidence")
    if isinstance(evidence, dict):
        for key in ("sql", "sql_text", "digest_text", "trx_query", "query"):
            if evidence.get(key):
                values.append(str(evidence[key]))
    return list(dict.fromkeys(values))


def _build_overall(instance: DBInstance, task_id: str, started_at: datetime, modules: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    finished_at = datetime.now()
    truth = _overall_truth(modules, task_id, instance.id)
    overall_score = _mean_module_score(modules) if truth["task_status"] == "success" else None
    critical_count = len([item for item in findings if item.get("severity") == "critical"])
    warning_count = len([item for item in findings if item.get("severity") in ("high", "warning")])
    info_count = len([item for item in findings if item.get("severity") == "info"])
    risk_level = _score_to_level(overall_score) if overall_score is not None else "unknown"
    if critical_count:
        risk_level = "critical"
    elif warning_count and risk_level == "normal":
        risk_level = "info"
    if overall_score is None:
        # Collection failures can have warning findings, but are not a health verdict.
        risk_level = _known_module_risk(modules)
    else:
        risk_level = _strongest_risk(risk_level, _known_module_risk(modules))
    summary = _overall_summary(overall_score, risk_level, findings) if overall_score is not None else _overall_incomplete_summary(truth)
    return {
        "task": {
            "task_id": task_id,
            "instance_id": instance.id,
            "db_type": "mysql",
            "overall_score": overall_score,
            "risk_level": risk_level,
            "summary": summary,
            "critical_count": critical_count,
            "warning_count": warning_count,
            "info_count": info_count,
            "started_at": started_at,
            "finished_at": finished_at,
        }
    }


def _overall_summary(score: int, risk_level: str, findings: list[dict[str, Any]]) -> str:
    if not findings:
        return f"当前 MySQL 实例整体评分 {score}，未发现明显巡检风险。"
    top = "；".join(item.get("finding", "") for item in findings[:4] if item.get("finding"))
    return f"当前 MySQL 实例整体评分 {score}，风险等级 {risk_level}。主要风险集中在：{top}。"


def _manifest_coverage(modules, task_id, instance_id):
    expected = None
    for item in modules:
        manifest = (item.get("evidence") or {}).get("collection", {}).get("manifest")
        if manifest is None:
            return {"coverage_basis": "manifest_missing", "expected_module_count": None}
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema", "task_id", "instance_id", "module_keys", "digest"}
                or manifest["schema"] != MODULE_MANIFEST_SCHEMA
                or manifest["task_id"] != task_id
                or type(manifest["instance_id"]) is not int or manifest["instance_id"] != instance_id
                or not isinstance(manifest["module_keys"], list) or not manifest["module_keys"]
                or not all(_valid_module_key(key) for key in manifest["module_keys"])
                or len(set(manifest["module_keys"])) != len(manifest["module_keys"])
                or not isinstance(manifest["digest"], str)
                or manifest["digest"] != _manifest_digest({key: value for key, value in manifest.items() if key != "digest"})):
            return {"coverage_basis": "manifest_invalid", "expected_module_count": None}
        if expected is not None and manifest != expected:
            return {"coverage_basis": "manifest_conflict", "expected_module_count": None}
        expected = manifest
    if expected is None:
        return {"coverage_basis": "manifest_missing", "expected_module_count": None}
    keys = [item.get("module_key") for item in modules]
    complete = all(_valid_module_key(key) for key in keys) and len(set(keys)) == len(keys) and keys == expected["module_keys"]
    return {"coverage_basis": "complete" if complete else "module_keys_mismatch",
            "expected_module_count": len(expected["module_keys"])}


def _mean_module_score(modules):
    return round(sum(item["score"] for item in modules) / len(modules))


def _strongest_risk(*levels):
    return max(levels, key=lambda level: SEVERITY_RISK_SCORE.get(level, -1), default="unknown")


def _known_module_risk(modules):
    # Normal measurements cannot establish health of uncollected/missing modules.
    return _strongest_risk(*(item.get("risk_level") for item in modules if item.get("risk_level") in {"critical", "warning", "info"}))


def _overall_truth(modules, task_id, instance_id):
    coverage = _manifest_coverage(modules, task_id, instance_id)
    states = []
    for item in modules:
        collection = (item.get("evidence") or {}).get("collection", {})
        state = collection.get("execution_status", "unknown")
        if state == "success" and not _valid_score(item.get("score")):
            state = "unknown"
        states.append(state)
    covered = coverage["coverage_basis"] == "complete"
    complete = covered and bool(states) and all(state == "success" for state in states)
    state = "success" if complete else "partial" if any(state in {"success", "partial"} for state in states) else "failed" if covered and states and all(state == "failed" for state in states) else "unknown"
    return {"task_status": state, "score_basis": {"success": "complete_evidence", "partial": "partial_evidence", "failed": "collection_failed", "unknown": "no_evidence"}[state],
            "module_count": len(states), "complete_module_count": states.count("success"), **coverage}


def _overall_incomplete_summary(truth):
    state = {"failed": "采集失败", "partial": "部分采集完成", "unknown": "采集证据未确认"}[truth["task_status"]]
    coverage = f"预期模块 {truth['expected_module_count']}" if truth["expected_module_count"] is not None else "预期覆盖未确认"
    return f"MySQL 巡检{state}（完整模块 {truth['complete_module_count']}，{coverage}），未生成整体健康评分；不能据此判断数据库健康。"


def _query_modules(db: Session, instance_id: int, task_id: str):
    return (
        db.query(DBInspectionModuleResult)
        .filter(DBInspectionModuleResult.instance_id == instance_id, DBInspectionModuleResult.task_id == task_id)
        .order_by(DBInspectionModuleResult.id.asc())
        .all()
    )


def _query_findings(db: Session, instance_id: int, task_id: str):
    return (
        db.query(DBDiagnosisFinding)
        .filter(DBDiagnosisFinding.instance_id == instance_id, DBDiagnosisFinding.task_id == task_id)
        .order_by(DBDiagnosisFinding.risk_score.desc(), DBDiagnosisFinding.id.asc())
        .all()
    )


def _task_to_dict(task: DBInspectionTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "instance_id": task.instance_id,
        "db_type": task.db_type,
        "overall_score": task.overall_score,
        "risk_level": task.risk_level,
        "summary": task.summary,
        "critical_count": task.critical_count,
        "warning_count": task.warning_count,
        "info_count": task.info_count,
        "started_at": _dt(task.started_at),
        "finished_at": _dt(task.finished_at),
        "create_time": _dt(task.create_time),
    }


def _module_to_dict(item: DBInspectionModuleResult) -> dict[str, Any]:
    result = {
        "module_key": item.module_key,
        "module_name": item.module_name,
        "score": item.score,
        "risk_level": item.risk_level,
        "summary": item.summary,
        "evidence": item.evidence,
        "top_risks": item.top_risks,
        "suggestions": item.suggestions,
        "raw_data": item.raw_data,
    }
    evidence = dict(item.evidence) if isinstance(item.evidence, dict) else {}
    collection = evidence.get("collection")
    if not (isinstance(collection, dict) and collection.get("schema") == COLLECTION_SCHEMA
            and isinstance(collection.get("execution_status"), str) and isinstance(collection.get("score_basis"), str)
            and collection.get("execution_status") in {"success", "partial", "failed", "unknown"}
            and collection.get("score_basis") in {"complete_evidence", "partial_evidence", "collection_failed", "no_evidence"}
            and type(collection.get("observation_count")) is int and collection["observation_count"] >= 0
            and isinstance(collection.get("observed_sources"), list)
            and (collection["execution_status"] not in {"success", "partial"}
                 or (collection["observation_count"] > 0 and collection["observed_sources"]))):
        # Old summary/score columns are not proof. Infer only from retained source data,
        # never modify the customer's historical row to repair its presentation.
        raw = item.raw_data if isinstance(item.raw_data, dict) else {}
        retained = {**evidence, **raw, "error": evidence.get("error") or raw.get("error"),
                    "warnings": [*(evidence.get("warnings") or []), *(raw.get("warnings") or [])]}
        collection = _collection_truth(item.module_key, retained)
        collection["historical_projection"] = True
        collection["observed_score"] = item.score if collection["execution_status"] in {"success", "partial"} and _valid_score(item.score) else None
    collection = dict(collection)
    if collection["execution_status"] == "success" and not _valid_score(item.score):
        collection.update(execution_status="unknown", score_basis="invalid_score")
    if collection["execution_status"] == "success" and item.risk_level not in {"normal", "info", "warning", "critical"}:
        collection.update(execution_status="unknown", score_basis="unverified_risk")
    evidence["collection"] = collection
    complete = collection["execution_status"] == "success"
    risks = item.top_risks if isinstance(item.top_risks, list) else []
    result.update(evidence=evidence, execution_status=collection["execution_status"],
                  score_basis=collection["score_basis"],
                  data_quality="good" if complete else "partial" if collection["observation_count"] else "missing",
                  evidence_score=collection.get("observed_score") if collection["execution_status"] in {"success", "partial"} and _valid_score(collection.get("observed_score")) else None,
                  score=item.score if complete else None,
                  risk_level=item.risk_level if complete else _incomplete_risk(collection, risks),
                  summary=item.summary if complete else _incomplete_summary(item.module_name, collection))
    return result


def _diagnosis_view(instance, task, modules, findings):
    projected = project_mysql_diagnosis_evidence(task, modules)
    module_dicts = projected["modules"]
    finding_dicts = [_finding_to_dict(item) for item in findings]
    task_dict = projected["task"]
    return {"task": task_dict, "modules": module_dicts, "findings": finding_dicts,
            "report": _report_dict(instance, task_dict, module_dicts, finding_dicts)}


def _project_task(task, modules):
    result = _task_to_dict(task)
    truth = _overall_truth(modules, task.task_id, task.instance_id)
    score_consistent = truth["task_status"] == "success" and _valid_score(task.overall_score) and task.overall_score == _mean_module_score(modules)
    complete = (truth["task_status"] == "success" and _valid_score(task.overall_score)
                and score_consistent and task.risk_level in {"normal", "info", "warning", "critical"})
    if truth["task_status"] == "success" and not complete:
        basis = "invalid_score" if not _valid_score(task.overall_score) else "inconsistent_score" if not score_consistent else "unverified_risk"
        truth.update(task_status="unknown", score_basis=basis)
    known_risk = _strongest_risk(_known_module_risk(modules), task.risk_level if task.risk_level in {"critical", "warning", "info"} else "unknown")
    result.update(truth, execution_status=truth["task_status"],
                  data_quality="good" if complete else "partial" if truth["task_status"] == "partial" else "missing")
    if not complete:
        result.update(overall_score=None,
                      risk_level=known_risk,
                      summary=_overall_incomplete_summary(truth))
    else:
        risk = _strongest_risk(task.risk_level, _score_to_level(task.overall_score), known_risk)
        result["risk_level"] = risk
        if risk != task.risk_level:
            result["summary"] = f"当前 MySQL 实例整体评分 {task.overall_score}，风险等级 {risk}；保留模块已确认风险。"
    return result


def project_mysql_diagnosis_task(db: Session, task: DBInspectionTask) -> dict[str, Any]:
    """Read-only task DTO for direct-ORM consumers; MySQL evidence only, no writes."""
    return project_mysql_diagnosis_evidence(task, _query_modules(db, task.instance_id, task.task_id))["task"]


def project_mysql_diagnosis_evidence(task: DBInspectionTask, modules: list[DBInspectionModuleResult]) -> dict[str, Any]:
    """Pure DTO projection over fully loaded MySQL task/module rows; no queries or writes.

    Returns {task, modules}. Batch readers group by (instance_id, task_id) before calling;
    the single-task DB helper and ordinary diagnosis reads share this same entry point.
    """
    if task.db_type != "mysql" or any(row.instance_id != task.instance_id or row.task_id != task.task_id for row in modules):
        raise ValueError("mysql_diagnosis_projection_scope_mismatch")
    views = [_module_to_dict(row) for row in modules]
    return {"task": _project_task(task, views), "modules": views}


def _finding_to_dict(item: DBDiagnosisFinding) -> dict[str, Any]:
    return {
        "id": item.id,
        "module_key": item.module_key,
        "check_item": item.check_item,
        "severity": item.severity,
        "risk_score": item.risk_score,
        "finding": item.finding,
        "evidence": item.evidence,
        "root_causes": item.root_causes,
        "business_impact": item.business_impact,
        "suggestions": item.suggestions,
        "operation_plan": item.operation_plan,
        "rollback_plan": item.rollback_plan,
        "related_objects": item.related_objects,
        "related_sql": item.related_sql,
        "raw_data": item.raw_data,
        "create_time": _dt(item.create_time),
    }


def _report_dict(instance: DBInstance | None, task: dict[str, Any], modules: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "instance": {
            "id": getattr(instance, "id", task.get("instance_id")),
            "name": getattr(instance, "instance_name", ""),
            "host": getattr(instance, "host", ""),
            "port": getattr(instance, "port", ""),
            "db_type": "mysql",
            "env_type": getattr(instance, "env_type", ""),
            "account_type": getattr(instance, "account_type", ""),
        },
        "inspection_time": task.get("finished_at"),
        "overall_conclusion": task.get("summary"),
        "risk_score": task.get("overall_score"),
        "risk_level": task.get("risk_level"),
        "task_status": task.get("task_status", "unknown"),
        "score_basis": task.get("score_basis", "no_evidence"),
        "counts": {
            "critical": task.get("critical_count", 0),
            "warning": task.get("warning_count", 0),
            "info": task.get("info_count", 0),
        },
        "module_scores": modules,
        "key_risks": findings[:5],
        "root_cause_analysis": _merge_list(findings, "root_causes"),
        "business_impact": _merge_list(findings, "business_impact"),
        "recommended_priorities": _priorities(findings),
        "operation_suggestions": [item.get("operation_plan") for item in findings[:10]],
        "raw_appendix": {"task": task, "modules": modules, "findings": findings},
    }


def _merge_list(findings: list[dict[str, Any]], key: str) -> list[str]:
    values = []
    for item in findings:
        current = item.get(key) or []
        if isinstance(current, list):
            values.extend(str(value) for value in current if value)
    return list(dict.fromkeys(values))[:10]


def _priorities(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "priority": index,
            "severity": item.get("severity"),
            "check_item": item.get("check_item"),
            "finding": item.get("finding"),
            "status": "plan_only",
        }
        for index, item in enumerate(findings[:5], start=1)
    ]


def _dt(value: Any) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None
