"""Bounded adapters for existing read models; never interpret handler names as code."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime, timezone
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.system_setting import DBSystemSetting
from app.models.user import User
from app.models.backup_platform import BackupExternalRun, BackupPlatformConfig, RestoreDrill
from app.models.inspection_policy import DBInspectionPolicy
from app.cmdb import normalized_service
from app.services import (
    archive_fra, backup_center, backup_policies, basic_info, capacity_monitor, generic_db_ops, mysql_basic,
    mysql_binlog, mysql_capacity, mysql_lock, mysql_replication, mysql_slow_sql,
    object_storage, operations_capabilities, operations_dashboard, oracle_dataguard, perf_baseline,
    report_center, run_status, slow_sql_analysis, suggestion_aggregator, tablespace_enhanced,
    zabbix_service,
)
from app.services.observability import alert_ingest_service, source_service
from app.services.inspection import jobs, policy_service, result_writer, run_readback
from app.services.inspection.plan_adapter import InspectionPlanParams, execute as execute_inspection_plan


class ReadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobListParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    status: str | None = Field(default=None, max_length=300)
    outcome: str | None = Field(default=None, max_length=300)
    trigger_type: str | None = Field(default=None, max_length=20)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        _csv(value, jobs.ALL_STATUSES)
        return value

    @field_validator("outcome")
    @classmethod
    def valid_outcome(cls, value):
        _csv(value, run_status.OUTCOMES)
        return value


class PolicyListParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    db_type: str | None = Field(default=None, pattern="^(oracle|mysql|postgresql|sqlserver|host)$")
    enabled: StrictBool | None = None


class MatrixParams(ReadParams):
    engine: str | None = Field(default=None, pattern="^(oracle|mysql|postgresql|sqlserver)$")


class RuntimeOverviewParams(ReadParams):
    """No caller knobs: the instance engine selects the one overview section."""


class SpaceParams(ReadParams):
    """No caller knobs: the instance engine selects the one space section."""


class LogParams(ReadParams):
    """No caller knobs: the instance engine selects the one log section."""


class SessionParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)


class LockParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)


class SlowSqlParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    # Closed sets only: no free-text filter may reach either engine's SQL builder.
    time_range: str | None = Field(default=None, pattern="^(1h|3h|12h|24h|3d|7d)$")
    category: str | None = Field(
        default=None, pattern="^(active|top_elapsed|top_avg|top_buffer|top_disk|waiting)$")
    hours: StrictInt | None = Field(default=None, ge=1, le=168)


def _jsonable(value):
    """Existing readers return datetimes/Decimals; a persisted plan result must be JSON.

    The HTTP path already serialises through FastAPI; the plan path stores the same payload,
    so normalise once here instead of teaching every reader a second output shape.
    """
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class ScopeNotSupported(ValueError):
    """The upstream reader aggregates platform-wide and cannot filter by asset scope.

    Registered as a refusal (``scope_not_supported``), never as an empty success: a scoped
    caller gets "this read is not available to you", not a partial overview that silently
    counts foreign instances.
    """


class EffectParams(ReadParams):
    """Effects take no free-form knobs: the plan's frozen target is the whole input."""


class RootCauseDiagnosisParams(ReadParams):
    """Customer-observed context for one bounded, current-snapshot diagnosis run."""

    symptom: str = Field(min_length=3, max_length=2000)
    time_window: str = Field(min_length=1, max_length=200)
    impact_scope: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("symptom", "time_window", "impact_scope", "title")
    @classmethod
    def non_blank(cls, value):
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("blank_diagnosis_context")
        return stripped


class CatalogParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    report_type: str | None = Field(default=None, pattern="^[a-z][a-z0-9_.-]{0,39}$")


class AggregateParams(ReadParams):
    """No caller knobs: the reader aggregates platform-wide counters."""


def _window(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("invalid_time_window") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class LogQueryParams(ReadParams):
    """Explicit external log window; no implicit "last hour" is invented for the caller."""
    source_id: StrictInt = Field(ge=1)
    start_time: str = Field(min_length=10, max_length=40)
    end_time: str = Field(min_length=10, max_length=40)
    keyword: str | None = Field(default=None, max_length=500)
    limit: StrictInt = Field(default=200, ge=1, le=2000)


class StartupParams(ReadParams):
    mode: str = Field(default="open", pattern="^(nomount|mount|open)$")
    pfile_path: str | None = Field(default=None, max_length=500)


class ShutdownParams(ReadParams):
    mode: str = Field(default="immediate", pattern="^(normal|transactional|immediate|abort)$")


class BackupPlanParams(ReadParams):
    database: str | None = Field(default=None, max_length=120)
    schema_name: str | None = Field(default=None, max_length=120)
    table_name: str | None = Field(default=None, max_length=200)
    storage_type: str = Field(default="local", pattern="^(local|object_storage)$")


class SourceSyncParams(ReadParams):
    source_id: StrictInt = Field(ge=1)


class ZabbixSyncParams(ReadParams):
    config_id: StrictInt = Field(ge=1)


class ReportDocumentParams(ReadParams):
    report_type: Literal["unified", "after_sales"] = "unified"
    window_hours: StrictInt = Field(default=168, ge=1, le=720)
    format: Literal["markdown", "html"] = "markdown"
    include_ai: StrictBool = True


class CandidateScanParams(ReadParams):
    min_rows: StrictInt = Field(default=1000000, ge=1, le=1000000000)
    schema_name: str | None = Field(default=None, max_length=120)
    table_name: str | None = Field(default=None, max_length=200)


class TraceQueryParams(ReadParams):
    source_id: StrictInt = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=256)


class TrendParams(ReadParams):
    """Problem-trend window; the service caps it at one year."""
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    days: StrictInt = Field(default=30, ge=1, le=365)


class AdminListParams(ReadParams):
    """Bounded list for platform/integration configuration readers."""
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    status: str | None = Field(default=None, max_length=40)


class PolicyPageParams(ReadParams):
    page: StrictInt = Field(default=1, ge=1, le=1000000)
    page_size: StrictInt = Field(default=50, ge=1, le=200)
    include_retired: StrictBool = False


@dataclass(frozen=True)
class CapabilityContext:
    db: Session
    actor_id: int
    params: dict[str, Any]
    instance: DBInstance | None = None
    allowed_instance_ids: frozenset[int] | None = None
    can_execute_operations: bool = False
    execution_id: str | None = None
    execution_guard: Callable | None = None


def _csv(value: str | None, allowed: set | frozenset) -> list[str]:
    values = [part.strip() for part in (value or "").split(",") if part.strip()]
    if set(values) - set(allowed):
        raise ValueError("invalid_filter")
    return values


def inspection_jobs(ctx: CapabilityContext) -> dict:
    values = JobListParams.model_validate(ctx.params)
    rows, total = jobs.list_jobs(
        ctx.db, instance_ids=ctx.allowed_instance_ids,
        instance_id=ctx.instance.id if ctx.instance else None,
        statuses=_csv(values.status, jobs.ALL_STATUSES),
        outcomes=_csv(values.outcome, run_status.OUTCOMES),
        trigger_type=values.trigger_type, page=values.page, page_size=values.page_size,
    )
    ids = [row.id for row in rows]
    counts = result_writer.count_by_task_ids(ctx.db, ids)
    severities = result_writer.severity_counts_by_task_ids(ctx.db, ids)
    return {
        "items": [run_readback.run_summary(ctx.db, row, result_counts=counts,
                                           severity_counts=severities) for row in rows],
        "total": total, "page": values.page, "page_size": values.page_size,
        "schema": "inspection.run_list/v1",
    }


def inspection_policies(ctx: CapabilityContext) -> dict:
    values = PolicyListParams.model_validate(ctx.params)
    query = ctx.db.query(DBInspectionPolicy)
    if values.enabled is not None:
        query = query.filter(DBInspectionPolicy.enabled == int(values.enabled))
    # Match the existing engine-membership semantics, but never seed defaults on a read.
    if values.db_type:
        items = policy_service.list_policies(
            ctx.db, db_type=values.db_type, enabled=values.enabled, initialize_defaults=False,
        )
        total = len(items)
        start = (values.page - 1) * values.page_size
        items = items[start:start + values.page_size]
    else:
        total = query.count()
        rows = query.order_by(DBInspectionPolicy.id).offset(
            (values.page - 1) * values.page_size,
        ).limit(values.page_size).all()
        items = [policy_service._row(row) for row in rows]
    return {"items": items, "total": total, "page": values.page,
            "page_size": values.page_size, "schema": "inspection.policy_list/v1"}


def capability_matrix(ctx: CapabilityContext) -> dict:
    values = MatrixParams.model_validate(ctx.params)
    query = ctx.db.query(DBInstance).order_by(DBInstance.id)
    if ctx.allowed_instance_ids is not None:
        query = query.filter(DBInstance.id.in_(ctx.allowed_instance_ids))
    if ctx.instance is not None:
        query = query.filter(DBInstance.id == ctx.instance.id)
    return operations_capabilities.build_capability_matrix(
        ctx.db, instances=query.all(), engine=values.engine,
        selected_instance_id=ctx.instance.id if ctx.instance else None,
        can_execute=ctx.can_execute_operations,
    )


def _target(ctx: CapabilityContext) -> DBInstance:
    if ctx.instance is None:
        raise ValueError("missing_instance")
    return ctx.instance


def _page(payload: dict, page: int, page_size: int, *, key: str = "items") -> dict:
    """Window the list the upstream service returned; never widen or rewrite its rows."""
    rows = payload.get(key)
    if not isinstance(rows, list):
        rows = []
    total = len(rows)
    start = (page - 1) * page_size
    return {**payload, key: rows[start:start + page_size], "total": total,
            "page": page, "page_size": page_size}


def runtime_overview(ctx: CapabilityContext) -> dict:
    RuntimeOverviewParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "oracle":
        return basic_info.get_basic_info(instance)
    if instance.db_type == "mysql":
        return mysql_basic.get_basic_info(instance)
    if instance.db_type in {"postgresql", "sqlserver"}:
        return generic_db_ops.collect_section(instance, "overview")
    raise ValueError("engine_not_supported")


def operations_space(ctx: CapabilityContext) -> dict:
    SpaceParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "oracle":
        return tablespace_enhanced.get_tablespace_overview(instance)
    if instance.db_type == "mysql":
        # Same response shape as GET /mysql-ops/{instance_id}/capacity/schemas.
        return {"items": mysql_capacity.get_schema_sizes(instance)}
    if instance.db_type in {"postgresql", "sqlserver"}:
        return generic_db_ops.collect_section(instance, "space")
    raise ValueError("engine_not_supported")


def operations_log(ctx: CapabilityContext) -> dict:
    LogParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "oracle":
        return archive_fra.get_archive_config(instance)
    if instance.db_type == "mysql":
        return mysql_binlog.get_binlog_overview(instance)
    if instance.db_type == "postgresql":
        return generic_db_ops.collect_section(instance, "wal")
    if instance.db_type == "sqlserver":
        return generic_db_ops.collect_section(instance, "logs")
    raise ValueError("engine_not_supported")


def operations_session(ctx: CapabilityContext) -> dict:
    values = SessionParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "mysql":
        payload = mysql_lock.get_lock_transaction_center(instance)
    elif instance.db_type in {"postgresql", "sqlserver"}:
        payload = generic_db_ops.collect_section(instance, "sessionTransaction")
    else:
        raise ValueError("engine_not_supported")
    return _page(payload, values.page, values.page_size)


def performance_lock(ctx: CapabilityContext) -> dict:
    values = LockParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "oracle":
        # Reuse the exact live reader behind the Oracle lock-analysis page. Importing
        # here avoids an API/registry import cycle while keeping one SQL implementation.
        from app.api.v1.oracle import _get_blocking_tree

        blocking = _get_blocking_tree(instance, strict=True)
        payload = {
            "items": blocking.get("tree", []),
            "lock_pairs": blocking.get("lock_pairs", []),
            "summary": blocking.get("summary", {}),
        }
    elif instance.db_type == "mysql":
        center = mysql_lock.get_lock_transaction_center(instance, strict_lock_waits=True)
        if center.get("status") != "ok":
            raise RuntimeError("mysql_lock_query_failed")
        payload = {
            "items": center.get("sections", {}).get("lock_waits", []),
            "summary": center.get("summary", {}),
            "warnings": center.get("warnings", []),
        }
    elif instance.db_type in {"postgresql", "sqlserver"}:
        payload = generic_db_ops.collect_section(instance, "sessionTransaction")
    else:
        raise ValueError("engine_not_supported")
    return _page(payload, values.page, values.page_size)


def performance_slow_sql(ctx: CapabilityContext) -> dict:
    values = SlowSqlParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "mysql":
        payload = mysql_slow_sql.get_slow_sql_center(
            instance, min(values.page * values.page_size, 200), values.time_range)
        return _page(payload, values.page, values.page_size)
    if instance.db_type == "oracle":
        payload = slow_sql_analysis.get_slow_sql_list(
            instance, values.category or "top_avg", hours=values.hours or 1)
        return _page(payload, values.page, values.page_size, key="rows")
    raise ValueError("engine_not_supported")


def platform_dashboard(ctx: CapabilityContext) -> dict:
    """The same dashboard the web entry builds, with the caller's own asset scope applied.

    A named target narrows the aggregation instead of being silently ignored; it can never
    widen it, because the intersection never adds an id.
    """
    AggregateParams.model_validate(ctx.params)
    allowed = ctx.allowed_instance_ids
    if ctx.instance is not None:
        allowed = frozenset({ctx.instance.id}) if allowed is None else (allowed & {ctx.instance.id})
    return _jsonable(operations_dashboard.build_operations_dashboard(ctx.db, allowed_instance_ids=allowed))






def performance_overview(ctx: CapabilityContext) -> dict:
    AggregateParams.model_validate(ctx.params)
    if ctx.allowed_instance_ids is not None:
        raise ScopeNotSupported("performance.overview")
    return _jsonable(suggestion_aggregator.compute_overview(ctx.db))


def operations_ha(ctx: CapabilityContext) -> dict:
    AggregateParams.model_validate(ctx.params)
    instance = _target(ctx)
    if instance.db_type == "oracle":
        return _jsonable(oracle_dataguard.get_dataguard_overview(instance, db=ctx.db))
    if instance.db_type == "mysql":
        return _jsonable(mysql_replication.get_replication_status(instance))
    if instance.db_type in {"postgresql", "sqlserver"}:
        return generic_db_ops.collect_section(instance, "ha")
    raise ValueError("engine_not_supported")


def asset_inventory(ctx: CapabilityContext) -> dict:
    """The normalized asset overview the tree page uses, filtered by the caller's scope."""
    CatalogParams.model_validate(ctx.params)
    return _jsonable(normalized_service.overview(ctx.db, allowed_instance_ids=ctx.allowed_instance_ids))


def report_catalog(ctx: CapabilityContext) -> dict:
    values = CatalogParams.model_validate(ctx.params)
    rows = report_center.list_artifacts(
        ctx.db, report_type=values.report_type, allowed_instance_ids=ctx.allowed_instance_ids,
        limit=values.page * values.page_size, metadata_only=True)
    items = [report_center.serialize_artifact(row) for row in rows]
    return _jsonable(_page({"items": items}, values.page, values.page_size))


def integration_zabbix(ctx: CapabilityContext) -> dict:
    """Configuration, host bindings and cached problems from the existing Zabbix service."""
    CatalogParams.model_validate(ctx.params)
    return _jsonable({"configs": zabbix_service.list_configs(ctx.db),
                      "hosts": zabbix_service.list_hosts(ctx.db),
                      "problems": zabbix_service.list_problems(ctx.db, status="all", limit=100)})


def integration_prometheus(ctx: CapabilityContext) -> dict:
    CatalogParams.model_validate(ctx.params)
    return _jsonable({"sources": source_service.list_sources(ctx.db),
                      "alerts": alert_ingest_service.list_alerts(ctx.db, limit=100)})


def rescue_cases(ctx: CapabilityContext) -> dict:
    values = CatalogParams.model_validate(ctx.params)
    payload = recovery_governance.list_cases(ctx.db, limit=200)
    return _jsonable(_page(payload, values.page, values.page_size))


def rescue_tools(ctx: CapabilityContext) -> dict:
    CatalogParams.model_validate(ctx.params)
    return _jsonable({"tools": recovery_governance.list_agent_tools(ctx.db),
                      "authorizations": recovery_governance.list_tool_authorizations(ctx.db)})




def _effect_guard(ctx: CapabilityContext):
    """An effect needs a durable execution identity and a live fence before it writes."""
    if not ctx.execution_id or not callable(ctx.execution_guard):
        raise ValueError("durable_execution_required")
    ctx.execution_guard()


def collect_perf_baseline(ctx: CapabilityContext) -> dict:
    """One real metric collection. The new DBPerfMetric rows are the evidence, not this dict."""
    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    value = perf_baseline.collect_perf_metrics(ctx.db, instance)
    _effect_guard(ctx)
    if not isinstance(value, dict):
        return {"outcome": "unknown", "instance_id": instance.id, "error_code": "collector_not_confirmed"}
    errors = [str(item)[:200] for item in (value.get("errors") or [])][:20]
    warnings = [str(item)[:200] for item in (value.get("warnings") or [])][:20]
    count = int(value.get("count") or 0)
    names = sorted(value.get("metrics") or {})
    if errors and count == 0:
        return {"outcome": "failed", "instance_id": instance.id, "error_code": "collector_error",
                "count": 0, "errors": errors}
    outcome = "partial" if errors or warnings else "success"
    return {"outcome": outcome, "instance_id": instance.id, "count": count,
            "metric_names": names, "errors": errors, "warnings": warnings}


def execute_root_cause_diagnosis(ctx: CapabilityContext) -> dict:
    """Run the existing diagnosis orchestrator and return a compact durable reference."""
    from app.models.diagnosis_center import DiagIncident
    from app.services.diagnosis import orchestrator
    from app.services.diagnosis.capability_effect import (
        COLLECTION_MODE,
        ROOT_CAUSE_RESULT_SCHEMA,
        customer_context_signals,
        customer_context_summary,
        trigger_source_for_execution,
    )

    values = RootCauseDiagnosisParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    trigger_source = trigger_source_for_execution(ctx.execution_id)
    intent_summary = customer_context_summary(values.time_window, values.impact_scope)
    intent_signals = customer_context_signals()
    detail = orchestrator.run_diagnosis(
        ctx.db,
        instance.id,
        title=values.title or f"{instance.instance_name} 根因分析：{values.symptom[:120]}",
        symptom=values.symptom,
        intent_summary=intent_summary,
        intent_signals=intent_signals,
        recommended_cards=["诊断摘要", "证据链", "根因候选", "故障时间线"],
        trigger_source=trigger_source,
    )
    _effect_guard(ctx)

    incident_id = detail.get("id") if isinstance(detail, dict) else None
    incident = (
        ctx.db.query(DiagIncident).filter(DiagIncident.id == incident_id).populate_existing().first()
        if type(incident_id) is int else None
    )
    if incident is None or incident.instance_id != instance.id or incident.trigger_source != trigger_source:
        return {
            "schema": ROOT_CAUSE_RESULT_SCHEMA,
            "outcome": "unknown",
            "instance_id": instance.id,
            "error_code": "diagnosis_incident_not_confirmed",
            "sampling": {
                "requested_time_window": values.time_window,
                "impact_scope": values.impact_scope,
                "collection_mode": COLLECTION_MODE,
                "historical_replay_performed": False,
            },
        }

    from app.models.diagnosis_center import DiagEvidence
    manual = ctx.db.query(DiagEvidence.id).filter_by(
        incident_id=incident.id, source="manual_symptom").first()
    if manual is None:
        # Early architecture/collection failures can occur before the orchestrator writes
        # customer context. Preserve that context on the durable failed incident as well.
        orchestrator._add_manual_symptom_evidence(
            ctx.db,
            incident.id,
            values.symptom,
            intent_summary,
            intent_signals,
            ["诊断摘要", "证据链", "根因候选", "故障时间线"],
        )
        incident.evidence_count = ctx.db.query(DiagEvidence.id).filter_by(incident_id=incident.id).count()
        _effect_guard(ctx)
        ctx.db.commit()
        ctx.db.refresh(incident)

    ai_result = incident.ai_result if isinstance(incident.ai_result, dict) else {}
    execution_status = str(ai_result.get("execution_status") or "unknown")
    if incident.status == "failed" or execution_status == "failed":
        outcome, code_field, code = "failed", "error_code", "diagnosis_failed"
    elif (incident.status == "root_caused" and int(incident.root_cause_count or 0) > 0
          and execution_status == "success"):
        outcome, code_field, code = "success", None, None
    elif int(incident.root_cause_count or 0) > 0:
        outcome, code_field, code = "partial", "reason_code", "collection_incomplete"
    else:
        outcome, code_field, code = "partial", "reason_code", "no_supported_root_cause"

    result = {
        "schema": ROOT_CAUSE_RESULT_SCHEMA,
        "outcome": outcome,
        "instance_id": incident.instance_id,
        "incident_id": incident.id,
        "incident_no": incident.incident_no,
        "status": incident.status,
        "severity": incident.severity,
        "root_cause_summary": incident.root_cause_summary,
        "primary_layer": incident.primary_layer,
        "evidence_count": int(incident.evidence_count or 0),
        "root_cause_count": int(incident.root_cause_count or 0),
        "execution_status": execution_status,
        "data_quality": str(ai_result.get("data_quality") or "unknown"),
        "conclusion_status": str(ai_result.get("conclusion_status") or "unknown"),
        "sampling": {
            "requested_time_window": values.time_window,
            "impact_scope": values.impact_scope,
            "collection_mode": COLLECTION_MODE,
            "historical_replay_performed": False,
        },
    }
    if code_field:
        result[code_field] = code
    return _jsonable(result)


def collect_capacity_snapshot(ctx: CapabilityContext) -> dict:
    """Persist one capacity snapshot through the existing collector; its policy row is evidence."""
    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    value = capacity_monitor.collect_snapshot(ctx.db, instance)
    _effect_guard(ctx)
    if not isinstance(value, dict):
        return {"outcome": "unknown", "instance_id": instance.id, "execution_id": ctx.execution_id,
                "reason_code": "snapshot_not_confirmed"}
    status = value.get("status")
    if status in {"skipped", "no_data"}:
        # Nothing was stored: report the collector's own verdict, never a successful snapshot.
        return {"outcome": "partial", "instance_id": instance.id, "execution_id": ctx.execution_id,
                "reason_code": f"capacity_{status}", "snapshot": _jsonable(value.get("snapshot") or {})}
    if not isinstance(value.get("snapshot"), dict):
        return {"outcome": "unknown", "instance_id": instance.id, "execution_id": ctx.execution_id,
                "reason_code": "snapshot_not_confirmed"}
    return {"outcome": "succeeded", "instance_id": instance.id, "execution_id": ctx.execution_id,
            "sample_count": int((value.get("policy") or {}).get("last_sample_count") or 0),
            "snapshot": _jsonable(value["snapshot"])}






def grafana_templates(ctx: CapabilityContext) -> dict:
    """Static Grafana template catalogue; export/build stays a separate action."""
    from app.services.observability import grafana

    values = AdminListParams.model_validate(ctx.params)
    items = grafana.list_templates()
    return _jsonable({"items": items, "total": len(items), "page": values.page,
                      "page_size": values.page_size})


def collect_table_profile(ctx: CapabilityContext) -> dict:
    """Full table snapshot; a complete empty result is partial because no row proves the run."""
    from app.services import perf_profile_collector

    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    value = perf_profile_collector.collect_table_profile(ctx.db, instance)
    _effect_guard(ctx)
    return _snapshot_outcome(value, instance.id)


def collect_index_findings(ctx: CapabilityContext) -> dict:
    from app.services import perf_profile_collector

    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    value = perf_profile_collector.collect_index_findings(ctx.db, instance)
    _effect_guard(ctx)
    return _snapshot_outcome(value, instance.id)


def _snapshot_outcome(value, instance_id):
    if not isinstance(value, dict):
        return {"outcome": "unknown", "instance_id": instance_id, "error_code": "collector_not_confirmed"}
    status = str(value.get("status") or "unknown")
    count = int(value.get("count") or 0)
    published = bool(value.get("published"))
    errors = [str(item)[:200] for item in (value.get("errors") or [])][:20]
    if status == "success" and published and count > 0:
        return {"outcome": "success", "instance_id": instance_id, "count": count, "errors": errors}
    if status in {"success", "partial"} and (published or count == 0):
        return {"outcome": "partial", "instance_id": instance_id, "count": count, "errors": errors,
                "reason_code": "empty_or_incomplete_snapshot" if count == 0 else "collector_partial"}
    return {"outcome": "failed", "instance_id": instance_id, "count": count, "errors": errors,
            "error_code": "collector_error"}


def report_document_generate(ctx: CapabilityContext) -> dict:
    from app.services import report_document

    values = ReportDocumentParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    value = report_document.generate(
        ctx.db, instance, report_type=values.report_type, window_hours=values.window_hours,
        format=values.format, include_ai=values.include_ai, execution_id=ctx.execution_id,
        actor=f"user:{ctx.actor_id}",
    )
    _effect_guard(ctx)
    return value


def oracle_switchover_plan(ctx: CapabilityContext) -> dict:
    """Generate the Data Guard pre-check plan only; never execute a switchover."""
    from app.services import oracle_dataguard

    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    if str(instance.db_type).lower() != "oracle":
        raise ValueError("engine_not_supported")
    _effect_guard(ctx)
    value = oracle_dataguard.build_switchover_check_plan(instance)
    _effect_guard(ctx)
    ok = (isinstance(value, dict) and value.get("status") == "plan_only"
          and bool(str(value.get("sql") or "").strip()))
    return {"outcome": "success" if ok else "failed", "plan_only": True,
            "execution_blocked": True, "status": (value or {}).get("status"),
            "sql": (value or {}).get("sql") or "",
            "script": (value or {}).get("sql") or "",
            "warnings": (value or {}).get("warnings") or [],
            "current": (value or {}).get("current") or {},
            "destinations": (value or {}).get("destinations") or [],
            "error_code": None if ok else "plan_generation_failed",
            "reason_code": None}


def import_external_alerts(ctx: CapabilityContext) -> dict:
    from app.services import alert_import_service

    EffectParams.model_validate(ctx.params)
    _effect_guard(ctx)
    value = alert_import_service.import_external_alerts(
        ctx.db, allowed_instance_ids=ctx.allowed_instance_ids)
    _effect_guard(ctx)
    ids = sorted(item for item in (value.get("event_ids") or []) if type(item) is int)
    outcome = "success" if ids else "partial"
    return {"outcome": outcome, "count": len(ids), "event_ids": ids,
            "imported": int(value.get("imported") or 0),
            "prometheus": int(value.get("prometheus") or 0),
            "zabbix": int(value.get("zabbix") or 0),
            "observability": int(value.get("observability") or 0),
            "error_code": None, "reason_code": None if ids else "empty_alert_cache"}


def operations_objects(ctx: CapabilityContext) -> dict:
    """Stored table profiles for the target; a read never triggers a new collection."""
    from app.models.table_profile import DBTableProfile

    values = AdminListParams.model_validate(ctx.params)
    instance = _target(ctx)
    query = ctx.db.query(DBTableProfile).filter(DBTableProfile.instance_id == instance.id)
    rows = query.order_by(DBTableProfile.capture_time.desc(), DBTableProfile.id.desc()).limit(
        values.page_size).all()
    return _jsonable({"items": [{"id": row.id, "schema_name": row.schema_name, "table_name": row.table_name,
                                 "size_mb": row.size_mb, "row_count": row.row_count,
                                 "issues": row.issues, "capture_time": row.capture_time} for row in rows],
                      "total": query.count(), "page": values.page, "page_size": values.page_size})


def observability_sources(ctx: CapabilityContext) -> dict:
    """Stored source/binding/policy registers; no live probe and no seeding on this path."""
    from app.models.observability import (ObservabilityAlertEvent, ObservabilityCollectTask,
                                          ObservabilityCurrentMetric, ObservabilityCollectPolicy)
    from app.services.observability import binding_service, source_service

    values = AdminListParams.model_validate(ctx.params)
    sources = source_service.list_sources(ctx.db)
    bindings = binding_service.list_bindings(ctx.db)
    policies = ctx.db.query(ObservabilityCollectPolicy).order_by(ObservabilityCollectPolicy.id).limit(values.page_size).all()
    return _jsonable({
        "items": sources[:values.page_size], "total": len(sources),
        "page": values.page, "page_size": values.page_size,
        "bindings": bindings[:values.page_size],
        "policies": [{"id": row.id, "enabled": row.enabled} for row in policies],
        "alert_count": ctx.db.query(ObservabilityAlertEvent).count(),
        "metric_count": ctx.db.query(ObservabilityCurrentMetric).count(),
        "task_count": ctx.db.query(ObservabilityCollectTask).count(),
    })


def observability_linkage(ctx: CapabilityContext) -> dict:
    """Stored linkage health for one instance; probing stays an explicit separate request."""
    from app.services.observability import linkage_health_service

    values = AdminListParams.model_validate(ctx.params)
    instance = _target(ctx)
    value = linkage_health_service.get_linkage_health(
        ctx.db, instance_id=instance.id, probe_sources=False, probe_timeout_seconds=0.5)
    return _jsonable({"items": [value], "total": 1, "page": values.page,
                      "page_size": values.page_size, "linkage": value})


def sync_zabbix_hosts_problems(ctx: CapabilityContext) -> dict:
    from datetime import datetime as _dt
    from app.models.external_monitoring import ZabbixConfig, ZabbixHostMapping, ZabbixProblemCache
    from app.services import zabbix_service

    values = ZabbixSyncParams.model_validate(ctx.params)
    config = ctx.db.get(ZabbixConfig, values.config_id)
    if config is None or config.enabled != 1:
        raise ValueError("zabbix_config_unavailable")
    _effect_guard(ctx)
    started = _dt.utcnow().replace(microsecond=0)
    hosts = zabbix_service.sync_hosts(ctx.db, config)
    problems = zabbix_service.sync_problems(ctx.db, config)
    _effect_guard(ctx)
    host_ids = sorted((hosts or {}).get("host_ids") or [])
    problem_ids = sorted((problems or {}).get("problem_ids") or [])
    host_rows = ctx.db.query(ZabbixHostMapping.id).filter(
        ZabbixHostMapping.zabbix_config_id == config.id, ZabbixHostMapping.id.in_(host_ids or [-1]),
    ).all()
    problem_rows = ctx.db.query(ZabbixProblemCache.id).filter(
        ZabbixProblemCache.zabbix_config_id == config.id, ZabbixProblemCache.id.in_(problem_ids or [-1]),
    ).all()
    host_count = int((hosts or {}).get("synced_count") or 0)
    problem_count = int((problems or {}).get("synced_count") or 0)
    total = host_count + problem_count
    matched = len(host_rows) == host_count and len(problem_rows) == problem_count
    if total > 0 and matched:
        outcome = "success"
    elif matched:
        outcome = "partial"
    else:
        outcome = "failed"
    return {"outcome": outcome, "config_id": config.id, "host_count": host_count,
            "problem_count": problem_count, "host_rows": len(host_rows),
            "problem_rows": len(problem_rows), "host_ids": host_ids, "problem_ids": problem_ids,
            "error_code": None if outcome != "failed" else "sync_evidence_mismatch",
            "reason_code": None if outcome != "partial" else "empty_zabbix_sync"}


def sync_prometheus_metrics(ctx: CapabilityContext) -> dict:
    from datetime import datetime as _dt
    from app.models.observability import ObservabilityCurrentMetric
    from app.services.observability import prometheus_standard_service, source_service

    values = SourceSyncParams.model_validate(ctx.params)
    source = source_service.get_source(ctx.db, values.source_id)
    if source is None or source.enabled != 1 or source.source_type != "prometheus":
        raise ValueError("prometheus_source_unavailable")
    _effect_guard(ctx)
    started = _dt.utcnow()
    value = prometheus_standard_service.sync_standard_metrics(ctx.db, source)
    _effect_guard(ctx)
    rows = ctx.db.query(ObservabilityCurrentMetric.id).filter(
        ObservabilityCurrentMetric.source_id == source.id,
        ObservabilityCurrentMetric.collect_time >= started.replace(microsecond=0),
    ).all()
    stored = int((value or {}).get("stored_count") or 0)
    status = str((value or {}).get("status") or "failed")
    if status == "success" and len(rows) == stored and stored > 0:
        outcome = "success"
    elif status in {"success", "partial"} and len(rows) == stored:
        outcome = "partial"
    else:
        outcome = "failed"
    return {"outcome": outcome, "source_id": source.id, "stored_count": stored,
            "row_count": len(rows), "status": status,
            "failed": (value or {}).get("failed") or [],
            "error_code": None if outcome != "failed" else "sync_failed",
            "reason_code": None if outcome != "partial" else "no_metric_rows"}


def bind_observability_sources(ctx: CapabilityContext) -> dict:
    """Auto-bind sources to instances; the persisted binding rows are the evidence."""
    from app.services.observability import binding_service

    EffectParams.model_validate(ctx.params)
    _effect_guard(ctx)
    value = binding_service.auto_bind(ctx.db)
    _effect_guard(ctx)
    ids = value.get("binding_ids") if isinstance(value, dict) else None
    ids = sorted(item for item in (ids or []) if type(item) is int)
    outcome = "success" if ids else "partial"
    return {"outcome": outcome, "count": len(ids), "binding_ids": ids,
            "matched_count": int((value or {}).get("matched_count") or 0),
            "error_code": None, "reason_code": None if ids else "no_matching_binding"}


def plan_startup(ctx: CapabilityContext) -> dict:
    from app.services import db_control

    values = StartupParams.model_validate(ctx.params)
    instance = _target(ctx)
    value = db_control.generate_startup_script(instance, values.mode, values.pfile_path)
    return _jsonable({**value, "outcome": "success", "execution_blocked": True, "plan_only": True})


def plan_shutdown(ctx: CapabilityContext) -> dict:
    from app.services import db_control

    values = ShutdownParams.model_validate(ctx.params)
    instance = _target(ctx)
    value = db_control.generate_shutdown_script(instance, values.mode)
    return _jsonable({**value, "outcome": "success", "execution_blocked": True, "plan_only": True})






def collect_sql_profile(ctx: CapabilityContext) -> dict:
    """Profile collection; only rows stamped by this run count as live evidence."""
    from datetime import datetime as _dt
    from app.models.sql_profile import DBSqlProfile
    from app.services import perf_profile_collector

    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    started = _dt.utcnow()
    value = perf_profile_collector.collect_sql_profile(ctx.db, instance)
    _effect_guard(ctx)
    rows = ctx.db.query(DBSqlProfile.id).filter(
        DBSqlProfile.instance_id == instance.id, DBSqlProfile.capture_time >= started).all()
    live_ids = sorted(row.id for row in rows)
    status = str(value.get("status") or "unknown") if isinstance(value, dict) else "unknown"
    errors = [str(item)[:200] for item in ((value or {}).get("errors") or [])][:20]
    count = int((value or {}).get("count") or 0)
    if status == "success" and live_ids:
        outcome = "success"
    elif status in {"success", "partial", "empty"}:
        outcome = "partial"
    else:
        outcome = "failed"
    return {"outcome": outcome, "instance_id": instance.id, "count": count,
            "live_count": len(live_ids), "live_ids": live_ids,
            "sources": (value or {}).get("sources") or [], "errors": errors,
            "error_code": None if outcome != "failed" else "collector_error",
            "reason_code": None if outcome != "partial" else "no_live_profile_rows"}




def collect_workload_report(ctx: CapabilityContext) -> dict:
    from app.services import workload_report

    EffectParams.model_validate(ctx.params)
    instance = _target(ctx)
    _effect_guard(ctx)
    report = workload_report.collect_report(ctx.db, instance, {"source": "auto"}, actor=str(ctx.actor_id))
    _effect_guard(ctx)
    status = getattr(report, "status", "unknown")
    outcome = "success" if status == "success" else "partial" if status == "partial" else "failed"
    return {"outcome": outcome, "instance_id": instance.id, "report_id": report.id,
            "report_no": report.report_no, "status": status,
            "error_code": None if outcome != "failed" else "collector_error"}


def performance_awr_history(ctx: CapabilityContext) -> dict:
    """Stored Oracle AWR report history; reading a report file/detail is a separate action."""
    from app.services import oracle_awr

    instance = _target(ctx)
    values = AdminListParams.model_validate(ctx.params)
    items = oracle_awr.list_awr_reports(ctx.db, instance.id)
    start = (values.page - 1) * values.page_size
    window = items[start:start + values.page_size]
    return _jsonable({"items": window, "total": len(items), "page": values.page,
                      "page_size": values.page_size})


def operations_security(ctx: CapabilityContext) -> dict:
    """Database accounts/roles from the target engine; a failed connection is an error, not []."""
    instance = _target(ctx)
    values = AdminListParams.model_validate(ctx.params)
    engine = str(instance.db_type or "").lower()
    if engine == "oracle":
        from app.services import user_mgmt
        users = user_mgmt.list_users(instance).get("users", [])
        roles = user_mgmt.list_roles(instance).get("roles", [])
    elif engine in {"mysql", "mariadb"}:
        from app.collectors.mysql.mysql_connection import mysql_connection
        with mysql_connection(instance) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user, host, plugin, account_locked, password_expired "
                               "FROM mysql.user ORDER BY user, host")
                users = list(cursor.fetchall())
        roles = []
    else:
        raise ValueError("security_read_unsupported")
    return _jsonable({"items": users[:values.page_size], "total": len(users), "page": values.page,
                      "page_size": values.page_size, "roles": roles[:values.page_size]})


def monitor_logs(ctx: CapabilityContext) -> dict:
    """Query the configured log source and record the same query audit the page records."""
    from app.services.observability import log_query, source_service

    values = LogQueryParams.model_validate(ctx.params)
    source = source_service.get_source(ctx.db, values.source_id)
    if source is None or source.enabled != 1:
        raise ValueError("log_source_unavailable")
    if source.source_type not in {"elasticsearch", "opensearch", "loki", "graylog"}:
        raise ValueError("log_source_unsupported")
    start, end = _window(values.start_time, "start_time"), _window(values.end_time, "end_time")
    events = log_query.query_logs(source, {
        "start_time": start, "end_time": end,
        "keyword": values.keyword, "limit": values.limit,
    })
    log_query.record_query_audit(
        ctx.db, query_kind="log", source_id=source.id, instance_id=ctx.instance.id if ctx.instance else None,
        start_time=start, end_time=end, query_summary=values.keyword,
        result_count=len(events), metadata={"source_type": source.source_type, "limit": values.limit},
    )
    return _jsonable({"items": events, "total": len(events), "count": len(events),
                      "page": 1, "page_size": values.limit})


def monitor_apm_trace(ctx: CapabilityContext) -> dict:
    """Query one trace from the configured APM source; unsupported adapters fail explicitly."""
    from app.services.observability import apm, log_query, source_service

    values = TraceQueryParams.model_validate(ctx.params)
    source = source_service.get_source(ctx.db, values.source_id)
    if source is None or source.enabled != 1:
        raise ValueError("apm_source_unavailable")
    trace = apm.query_trace(source, values.trace_id)
    spans = trace.get("spans") if isinstance(trace, dict) else []
    log_query.record_query_audit(
        ctx.db, query_kind="apm_trace", source_id=source.id, instance_id=None,
        start_time=None, end_time=None, query_summary=values.trace_id,
        result_count=len(spans or []), metadata={"source_type": source.source_type},
    )
    return _jsonable({"items": spans or [], "total": len(spans or []), "page": 1, "page_size": len(spans or []),
                      "trace_id": trace.get("trace_id") if isinstance(trace, dict) else None,
                      "trace": trace})




def config_governance_summary(ctx: CapabilityContext) -> dict:
    """Baseline/drift summary through the shared service, with the caller's own scope."""
    from app.models.config_governance import DBConfigBaseline, DBConfigDrift
    from app.services import config_governance

    values = AdminListParams.model_validate(ctx.params)
    allowed = ctx.allowed_instance_ids
    if ctx.instance is not None:
        allowed = frozenset({ctx.instance.id}) if allowed is None else (allowed & {ctx.instance.id})
    value = config_governance.summary(ctx.db, allowed_instance_ids=allowed)
    baselines_query = ctx.db.query(DBConfigBaseline).filter(DBConfigBaseline.status != "archived")
    baseline_rows = baselines_query.order_by(DBConfigBaseline.id.desc()).limit(values.page_size).all()
    drift_query = ctx.db.query(DBConfigDrift)
    if allowed is not None:
        drift_query = drift_query.filter(DBConfigDrift.instance_id.in_(allowed or {-1}))
    drift_rows = drift_query.order_by(DBConfigDrift.id.desc()).limit(values.page_size).all()
    return _jsonable({"items": baseline_rows and [{"id": row.id, "name": row.name, "db_type": row.db_type,
                                                    "status": row.status} for row in baseline_rows] or [],
                      "total": len(baseline_rows), "page": values.page, "page_size": values.page_size,
                      "summary": value,
                      "drifts": [{"id": row.id, "instance_id": row.instance_id, "severity": row.severity,
                                  "status": row.status} for row in drift_rows]})


def platform_identity(ctx: CapabilityContext) -> dict:
    """Identity providers and login audit through the existing handlers (secrets redacted)."""
    from app.identity import api as identity_api

    values = AdminListParams.model_validate(ctx.params)
    providers = identity_api.list_providers(ctx.db)
    audit = identity_api.list_login_audit(limit=values.page_size, db=ctx.db)
    items = [row.model_dump() if hasattr(row, "model_dump") else row for row in providers]
    return _jsonable({"items": items, "total": len(items), "page": values.page,
                      "page_size": values.page_size, "login_audit": audit})


def platform_scheduler(ctx: CapabilityContext) -> dict:
    """Scheduler lease and recent dispatches; never claims a role from a local variable."""
    from datetime import datetime, timezone
    from app.models.report_center import SchedulerDispatchRun, SchedulerLease

    values = AdminListParams.model_validate(ctx.params)
    lease = ctx.db.query(SchedulerLease).filter_by(lease_key="master_scheduler").first()
    dispatches = ctx.db.query(SchedulerDispatchRun).order_by(
        SchedulerDispatchRun.started_at.desc()).limit(values.page_size).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lease_until = getattr(lease, "lease_until", None)
    return _jsonable({
        "items": [{"id": row.id, "status": row.status, "owner_id": row.owner_id,
                   "started_at": row.started_at, "finished_at": row.finished_at,
                   "fencing_epoch": row.fencing_epoch} for row in dispatches],
        "total": len(dispatches), "page": values.page, "page_size": values.page_size,
        "lease": {"owner_id": lease.owner_id, "lease_until": lease_until,
                  "heartbeat_at": lease.heartbeat_at, "epoch": lease.epoch,
                  "active": bool(lease_until and lease_until > now)} if lease else None,
    })


def monitor_alerts(ctx: CapabilityContext) -> dict:
    """Unified alert events. A read must not advance SLA state; the scheduled consumer does that."""
    from app.api.v1.alert_events import _event_to_dict
    from app.models.alert_event import DBAlertEvent
    from app.services import alert_lifecycle

    values = AdminListParams.model_validate(ctx.params)
    query = ctx.db.query(DBAlertEvent)
    if ctx.allowed_instance_ids is not None:
        query = query.filter(DBAlertEvent.instance_id.in_(ctx.allowed_instance_ids))
    if ctx.instance is not None:
        query = query.filter(DBAlertEvent.instance_id == ctx.instance.id)
    if values.status == "active":
        query = query.filter(DBAlertEvent.status.in_(alert_lifecycle.ACTIVE_STATUSES))
    elif values.status and values.status != "all":
        query = query.filter(DBAlertEvent.status == values.status)
    rows = query.order_by(DBAlertEvent.last_seen.desc(), DBAlertEvent.id.desc()).limit(values.page_size).all()
    return _jsonable({"items": [_event_to_dict(row) for row in rows], "total": len(rows),
                      "page": values.page, "page_size": values.page_size})


def performance_trend(ctx: CapabilityContext) -> dict:
    """The existing problem-trend aggregate; the caller's target only narrows the count set."""
    from app.api.v1.performance import api_problem_trend

    values = TrendParams.model_validate(ctx.params)
    instance_id = ctx.instance.id if ctx.instance is not None else None
    value = api_problem_trend(instance_id=instance_id, days=values.days, db=ctx.db)
    return _jsonable({"items": value.get("items", value if isinstance(value, list) else []),
                      "total": len(value.get("items", [])) if isinstance(value, dict) else 0,
                      "page": values.page, "page_size": values.page_size,
                      "days": values.days, "summary": value if isinstance(value, dict) else None})


def platform_ai_config(ctx: CapabilityContext) -> dict:
    """Provider list for operators; endpoints and credential material are not selected."""
    from app.models.ai_provider_config import AIProviderConfig

    values = AdminListParams.model_validate(ctx.params)
    rows = ctx.db.query(AIProviderConfig).order_by(AIProviderConfig.id).offset(
        (values.page - 1) * values.page_size).limit(values.page_size).all()
    return _jsonable({"items": [{"id": row.id, "provider_name": row.provider_name,
                                 "provider_type": row.provider_type, "model_name": row.model_name,
                                 "enabled": row.enabled, "is_default": row.is_default,
                                 "timeout_seconds": row.timeout_seconds} for row in rows],
                      "total": ctx.db.query(AIProviderConfig).count(),
                      "page": values.page, "page_size": values.page_size})








def protection_automation(ctx: CapabilityContext) -> dict:
    """Automation plans, approval requests and runs, without executing or approving anything."""
    from app.automation.service import AutomationService

    values = AdminListParams.model_validate(ctx.params)
    service = AutomationService(ctx.db)
    plans = service.list_plans()[:values.page_size]
    requests = service.list_requests()[:values.page_size]
    runs = service.list_runs()[:values.page_size]
    return _jsonable({
        "items": [{"id": row.id, "name": getattr(row, "name", None),
                   "status": getattr(row, "status", None)} for row in plans],
        "total": len(plans), "page": values.page, "page_size": values.page_size,
        "approvals": [{"id": row.id, "status": getattr(row, "status", None),
                       "plan_id": getattr(row, "plan_id", None)} for row in requests],
        "runs": [{"id": row.id, "status": getattr(row, "status", None)} for row in runs],
    })


def ai_evaluation_summary(ctx: CapabilityContext) -> dict:
    """Stored evaluation aggregates only; no model call and no re-scoring."""
    from app.services import diagnosis_evaluation

    values = AdminListParams.model_validate(ctx.params)
    value = diagnosis_evaluation.summary(ctx.db)
    return _jsonable({"items": value if isinstance(value, list) else [value], "total": 1,
                      "page": values.page, "page_size": values.page_size, "summary": value})


def platform_users(ctx: CapabilityContext) -> dict:
    """User directory without password hashes, MFA secrets or session material."""
    values = AdminListParams.model_validate(ctx.params)
    query = ctx.db.query(User)
    if values.status:
        query = query.filter(User.status == values.status)
    rows = query.order_by(User.id).offset((values.page - 1) * values.page_size).limit(values.page_size).all()
    return _jsonable({"items": [{"id": row.id, "username": row.username, "display_name": row.display_name,
                                 "role": row.role, "status": row.status} for row in rows],
                      "total": query.count(), "page": values.page, "page_size": values.page_size})


def monitor_capacity_alerts(ctx: CapabilityContext) -> dict:
    """Capacity alerts through the existing scoped reader; a read must not release isolations."""
    from app.models.capacity_policy import DBCapacityAlert

    values = AdminListParams.model_validate(ctx.params)
    allowed = ctx.allowed_instance_ids
    instance_id = ctx.instance.id if ctx.instance is not None else None
    if instance_id is not None:
        allowed = frozenset({instance_id}) if allowed is None else (allowed & {instance_id})
    query = ctx.db.query(DBCapacityAlert)
    if allowed is not None:
        query = query.filter(DBCapacityAlert.instance_id.in_(allowed))
    if instance_id is not None:
        query = query.filter(DBCapacityAlert.instance_id == instance_id)
    query = query.filter(DBCapacityAlert.status == (values.status or "open"))
    rows = query.order_by(DBCapacityAlert.create_time.desc()).limit(values.page_size).all()
    return _jsonable({"items": [capacity_monitor._alert_to_dict(row) for row in rows],
                      "total": len(rows), "page": values.page, "page_size": values.page_size})


def protection_platform(ctx: CapabilityContext) -> dict:
    """External backup platform configuration and recent run/drill registry (secrets omitted)."""
    values = AdminListParams.model_validate(ctx.params)
    from app.services import backup_platform

    configs = [backup_platform.config_to_dict(row) for row in ctx.db.query(BackupPlatformConfig)
               .order_by(BackupPlatformConfig.id.desc()).limit(values.page_size).all()]
    runs = ctx.db.query(BackupExternalRun).order_by(
        BackupExternalRun.finished_at.desc(), BackupExternalRun.id.desc()).limit(values.page_size).all()
    drills = ctx.db.query(RestoreDrill).order_by(RestoreDrill.id.desc()).limit(values.page_size).all()
    return _jsonable({"items": configs, "total": len(configs), "page": values.page,
                      "page_size": values.page_size,
                      "runs": [{"id": row.id, "config_id": row.config_id, "status": row.status,
                                "finished_at": row.finished_at} for row in runs],
                      "restore_drills": [{"id": row.id, "instance_id": row.instance_id,
                                          "status": row.status, "create_time": row.create_time} for row in drills]})


def rescue_agent(ctx: CapabilityContext) -> dict:
    """Agent tool inventory and current authorizations, from the recovery governance service."""
    values = AdminListParams.model_validate(ctx.params)
    tools = recovery_governance.list_agent_tools(ctx.db)
    authorizations = recovery_governance.list_tool_authorizations(ctx.db)
    return _jsonable({"items": tools.get("items", []), "total": tools.get("total", 0),
                      "page": values.page, "page_size": values.page_size,
                      "authorizations": authorizations.get("items", [])})


def platform_settings(ctx: CapabilityContext) -> dict:
    """Stored settings only. A read never seeds defaults or rewrites a missing key."""
    values = AdminListParams.model_validate(ctx.params)
    query = ctx.db.query(DBSystemSetting).filter(~DBSystemSetting.key.like("internal.%"))
    rows = query.order_by(DBSystemSetting.key).offset(
        (values.page - 1) * values.page_size).limit(values.page_size).all()
    return _jsonable({"items": [{"key": row.key, "value": row.value, "value_type": row.value_type,
                                 "category": row.category} for row in rows],
                      "total": query.count(),
                      "page": values.page, "page_size": values.page_size})


def integration_notify(ctx: CapabilityContext) -> dict:
    """Notification configuration as the existing serializers expose it (secrets redacted)."""
    values = AdminListParams.model_validate(ctx.params)
    from app.notify import service as notify_service

    channels = [notify_service.serialize_channel(row) for row in notify_service.list_channels(ctx.db)]
    policies = notify_service.list_policies(ctx.db)
    templates = [{"id": row.id, "template_name": row.template_name, "channel_type": row.channel_type}
                 for row in notify_service.list_templates(ctx.db)]
    recipient_total = len(notify_service.list_recipients(ctx.db))
    return _jsonable({"items": channels, "total": len(channels), "page": values.page,
                      "page_size": values.page_size, "policies": policies,
                      "templates": templates, "recipient_total": recipient_total})


def integration_itsm(ctx: CapabilityContext) -> dict:
    """ITSM configuration and recent tickets; field mappings are intentionally not expanded."""
    values = AdminListParams.model_validate(ctx.params)
    from app.itsm import service as itsm_service

    configs = [itsm_service.serialize_config(row) for row in itsm_service.list_configs(ctx.db)]
    tickets = [itsm_service.serialize_ticket(row) for row in itsm_service.list_tickets(
        ctx.db, status=values.status, limit=values.page_size)]
    return _jsonable({"items": configs, "total": len(configs), "page": values.page,
                      "page_size": values.page_size, "tickets": tickets})




HANDLERS: dict[str, Callable[[CapabilityContext], dict]] = {    "inspection.list_jobs": inspection_jobs,
    "inspection.list_policies": inspection_policies,
    "operations.capability_matrix": capability_matrix,
    "inspection.run_frozen": execute_inspection_plan,
    "operations.runtime_overview": runtime_overview,
    "operations.space": operations_space,
    "operations.log": operations_log,
    "operations.session": operations_session,
    "performance.lock": performance_lock,
    "performance.slow_sql": performance_slow_sql,
    "platform.dashboard": platform_dashboard,
    "performance.overview": performance_overview,
    "operations.ha": operations_ha,
    "asset.inventory": asset_inventory,
    "report.catalog": report_catalog,
    "integration.zabbix": integration_zabbix,
    "integration.prometheus": integration_prometheus,
    "rescue.cases": rescue_cases,
    "rescue.tools": rescue_tools,
    "monitor.capacity_collect": collect_capacity_snapshot,
    "performance.baseline_collect": collect_perf_baseline,
    "platform.settings": platform_settings,
    "integration.notify": integration_notify,
    "integration.itsm": integration_itsm,
    "platform.users": platform_users,
    "monitor.capacity_alerts": monitor_capacity_alerts,
    "protection.platform": protection_platform,
    "rescue.agents": rescue_agent,
    "protection.automation": protection_automation,
    "ai.evaluation_summary": ai_evaluation_summary,
    "platform.ai_config": platform_ai_config,
    "platform.identity": platform_identity,
    "platform.scheduler": platform_scheduler,
    "monitor.alerts": monitor_alerts,
    "performance.trend": performance_trend,
    "grafana.templates": grafana_templates,
    "config.governance_summary": config_governance_summary,
    "diagnosis.root_cause": execute_root_cause_diagnosis,
    "monitor.logs": monitor_logs,
    "operations.security": operations_security,
    "performance.awr_history": performance_awr_history,
    "performance.table_profile_collect": collect_table_profile,
    "performance.index_collect": collect_index_findings,
    "performance.workload_collect": collect_workload_report,
    "performance.sql_profile_collect": collect_sql_profile,
    "observability.bind": bind_observability_sources,
    "integration.prometheus_sync": sync_prometheus_metrics,
    "integration.zabbix_sync": sync_zabbix_hosts_problems,
    "monitor.alert_import": import_external_alerts,
    "observability.sources": observability_sources,
    "operations.objects": operations_objects,
    "operations.switchover_plan": oracle_switchover_plan,
    "report.document_generate": report_document_generate,
    "observability.linkage": observability_linkage,
    "operations.startup_plan": plan_startup,
    "operations.shutdown_plan": plan_shutdown,
    "monitor.apm": monitor_apm_trace,
}
PARAM_SCHEMAS: dict[str, type[ReadParams]] = {
    "inspection.list_jobs": JobListParams,
    "inspection.list_policies": PolicyListParams,
    "operations.capability_matrix": MatrixParams,
    "inspection.run_frozen": InspectionPlanParams,
    "operations.runtime_overview": RuntimeOverviewParams,
    "operations.space": SpaceParams,
    "operations.log": LogParams,
    "operations.session": SessionParams,
    "performance.lock": LockParams,
    "performance.slow_sql": SlowSqlParams,
    "platform.dashboard": AggregateParams,
    "performance.overview": AggregateParams,
    "operations.ha": AggregateParams,
    "asset.inventory": CatalogParams,
    "report.catalog": CatalogParams,
    "integration.zabbix": CatalogParams,
    "integration.prometheus": CatalogParams,
    "rescue.cases": CatalogParams,
    "rescue.tools": CatalogParams,
    "monitor.capacity_collect": EffectParams,
    "performance.baseline_collect": EffectParams,
    "platform.settings": AdminListParams,
    "integration.notify": AdminListParams,
    "integration.itsm": AdminListParams,
    "platform.users": AdminListParams,
    "monitor.capacity_alerts": AdminListParams,
    "protection.platform": AdminListParams,
    "rescue.agents": AdminListParams,
    "protection.automation": AdminListParams,
    "ai.evaluation_summary": AdminListParams,
    "platform.ai_config": AdminListParams,
    "platform.identity": AdminListParams,
    "platform.scheduler": AdminListParams,
    "monitor.alerts": AdminListParams,
    "performance.trend": TrendParams,
    "grafana.templates": AdminListParams,
    "config.governance_summary": AdminListParams,
    "diagnosis.root_cause": RootCauseDiagnosisParams,
    "monitor.logs": LogQueryParams,
    "operations.security": AdminListParams,
    "performance.awr_history": AdminListParams,
    "performance.table_profile_collect": EffectParams,
    "performance.index_collect": EffectParams,
    "performance.workload_collect": EffectParams,
    "performance.sql_profile_collect": EffectParams,
    "observability.bind": EffectParams,
    "integration.prometheus_sync": SourceSyncParams,
    "integration.zabbix_sync": ZabbixSyncParams,
    "monitor.alert_import": EffectParams,
    "observability.sources": AdminListParams,
    "operations.objects": AdminListParams,
    "operations.switchover_plan": EffectParams,
    "report.document_generate": ReportDocumentParams,
    "observability.linkage": AdminListParams,
    "operations.startup_plan": StartupParams,
    "operations.shutdown_plan": ShutdownParams,
    "monitor.apm": TraceQueryParams,
}


#: JSON schema of the accepted parameters, so a manual form can be generated from the
#: same model the handler validates with. This is descriptive; the server still validates.
PARAM_SCHEMA_JSON: dict[str, dict] = {
    capability_id: model.model_json_schema(mode="validation")
    for capability_id, model in PARAM_SCHEMAS.items()
}
