from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, TypedDict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.automation.models import AutomationPlan, ExecutionRun
from app.cmdb.models import CmdbSource, CmdbSyncJob
from app.identity.models import IdentityProvider, LoginAudit
from app.itsm.models import ItsmConfig, ItsmTicketSyncLog
from app.models.archive_storage import ArchiveStorageConfig, ArchiveTransfer
from app.models.backup_platform import BackupExternalRun, BackupPlatformConfig
from app.models.external_ecosystem import (
    EcosystemIntegrationConfig,
    EcosystemIntegrationEvent,
    EcosystemIntegrationPlan,
)
from app.models.observability import ObservabilityQueryAudit, ObservabilitySource
from app.notify.models import NotifyChannel, NotifyLog


CapabilityStatus = Literal[
    "planned",
    "configured",
    "verified",
    "active",
    "degraded",
    "disabled",
]


class CapabilityDefinition(TypedDict, total=False):
    task_code: str
    task_name: str
    priority: str
    category: str
    domain: str
    menu: str
    route_key: str
    section: str
    status: CapabilityStatus
    targets: list[str]
    goal: str


CAPABILITY_REGISTRY: dict[str, CapabilityDefinition] = {
    "T-051": {
        "task_code": "T-051",
        "task_name": "告警通知通道适配模块",
        "priority": "P0",
        "category": "notification",
        "domain": "notify",
        "menu": "平台管理 / 集成中心",
        "route_key": "integration.notify",
        "status": "active",
        "targets": ["企业微信", "钉钉", "飞书", "SMTP 邮件", "短信网关", "通用 Webhook"],
        "goal": "将告警、巡检、归档和备份异常推送到多通知通道。",
    },
    "T-052": {
        "task_code": "T-052",
        "task_name": "ITSM / 工单系统适配模块",
        "priority": "P0",
        "category": "itsm",
        "domain": "itsm",
        "menu": "平台管理 / 集成中心",
        "route_key": "integration.itsm",
        "status": "active",
        "targets": ["ServiceNow", "Jira", "禅道", "蓝凌 OA", "泛微 OA", "通用 Webhook"],
        "goal": "支持告警、巡检和诊断转工单，并同步处理状态。",
    },
    "T-053": {
        "task_code": "T-053",
        "task_name": "CMDB 资产同步与关系映射模块",
        "priority": "P1",
        "category": "cmdb",
        "domain": "cmdb",
        "menu": "资产管理",
        "route_key": "asset.center",
        "status": "planned",
        "targets": ["CMDB", "主机资产", "应用系统", "工厂 / 产线", "资产关系"],
        "goal": "维护应用、数据库实例、主机和监控对象之间的关系。",
    },
    "T-054": {
        "task_code": "T-054",
        "task_name": "日志平台适配模块",
        "priority": "P1",
        "category": "logs",
        "domain": "observability",
        "menu": "监控告警 / 可观测监控",
        "route_key": "monitor.observability",
        "section": "logAnalysis",
        "status": "planned",
        "targets": ["Elasticsearch", "OpenSearch", "Grafana Loki", "Graylog"],
        "goal": "关联应用、数据库和系统日志，形成故障时间线。",
    },
    "T-055": {
        "task_code": "T-055",
        "task_name": "Grafana Dashboard 模板模块",
        "priority": "P1",
        "category": "grafana",
        "domain": "observability",
        "menu": "监控告警 / 可观测监控",
        "route_key": "monitor.observability",
        "section": "grafana",
        "status": "configured",
        "targets": ["Grafana", "Prometheus"],
        "goal": "生成数据库、归档任务和告警趋势 Dashboard 模板。",
    },
    "T-056": {
        "task_code": "T-056",
        "task_name": "备份平台适配模块",
        "priority": "P2",
        "category": "backup",
        "domain": "backup",
        "menu": "数据保护 / 备份与恢复",
        "route_key": "backup.overview",
        "section": "platform",
        "status": "planned",
        "targets": ["Veeam", "Commvault", "NetBackup", "通用 REST / Webhook"],
        "goal": "同步备份状态、失败告警、容量趋势和恢复抽检记录。",
    },
    "T-057": {
        "task_code": "T-057",
        "task_name": "归档文件对象存储适配模块",
        "priority": "P1",
        "category": "object_storage",
        "domain": "data_governance",
        "menu": "数据库运维 / 数据治理",
        "route_key": "ops.governance",
        "section": "objectStorage",
        "status": "planned",
        "targets": ["MinIO", "S3", "OSS", "COS", "OBS", "本地 NAS"],
        "goal": "支持归档制品上传、校验、生命周期审批和恢复命令生成。",
    },
    "T-058": {
        "task_code": "T-058",
        "task_name": "自动化执行与审批模块",
        "priority": "P2",
        "category": "automation",
        "domain": "automation",
        "menu": "数据保护",
        "route_key": "protection.automation",
        "status": "planned",
        "targets": ["Ansible", "SSH 白名单执行"],
        "goal": "将操作方案提交审批，审批后按安全策略执行并留存输出。",
    },
    "T-059": {
        "task_code": "T-059",
        "task_name": "企业认证与权限适配模块",
        "priority": "P1",
        "category": "identity",
        "domain": "identity",
        "menu": "平台管理 / 系统管理",
        "route_key": "system.identity",
        "status": "planned",
        "targets": ["LDAP", "Active Directory", "OIDC"],
        "goal": "对接企业身份源并将外部用户组映射到平台角色权限。",
    },
    "T-060": {
        "task_code": "T-060",
        "task_name": "APM 链路追踪适配模块",
        "priority": "P2",
        "category": "apm",
        "domain": "observability",
        "menu": "监控告警 / 可观测监控",
        "route_key": "monitor.observability",
        "section": "apmTrace",
        "status": "planned",
        "targets": ["SkyWalking", "OpenTelemetry", "Jaeger", "Zipkin"],
        "goal": "通过 Trace ID 关联接口、SQL 和数据库实例，形成诊断证据。",
    },
}


def get_capability(task_code: str) -> CapabilityDefinition | None:
    return CAPABILITY_REGISTRY.get(task_code)


def _count_by_task(db: Session, model, *, status: str | None = None) -> dict[str, int]:
    query = db.query(model.task_code, func.count(model.id))
    if status is not None:
        query = query.filter(model.status == status)
    return {task_code: int(count) for task_code, count in query.group_by(model.task_code).all()}


def _latest_event_by_task(db: Session, *, status: str) -> dict[str, object]:
    rows = (
        db.query(EcosystemIntegrationEvent.task_code, func.max(EcosystemIntegrationEvent.create_time))
        .filter(EcosystemIntegrationEvent.status == status)
        .group_by(EcosystemIntegrationEvent.task_code)
        .all()
    )
    return dict(rows)


def _count(db: Session, model, *criteria) -> int:
    query = db.query(func.count(model.id))
    if criteria:
        query = query.filter(*criteria)
    return int(query.scalar() or 0)


def _latest(db: Session, model, field, *criteria):
    query = db.query(func.max(field))
    if criteria:
        query = query.filter(*criteria)
    return query.scalar()


def _domain_summary(db: Session, task_code: str) -> dict:
    if task_code == "T-051":
        return {
            "configured_count": _count(db, NotifyChannel),
            "active_count": _count(db, NotifyChannel, NotifyChannel.enabled == 1),
            "domain_event_count": _count(db, NotifyLog),
            "domain_failed_count": _count(db, NotifyLog, NotifyLog.status == "failed"),
            "last_success_time": _latest(db, NotifyLog, NotifyLog.sent_time, NotifyLog.status == "sent"),
            "last_failure_time": _latest(db, NotifyLog, NotifyLog.update_time, NotifyLog.status == "failed"),
        }
    if task_code == "T-052":
        return {
            "configured_count": _count(db, ItsmConfig),
            "active_count": _count(db, ItsmConfig, ItsmConfig.enabled == 1),
            "domain_event_count": _count(db, ItsmTicketSyncLog),
            "domain_failed_count": _count(db, ItsmTicketSyncLog, ItsmTicketSyncLog.sync_status == "failed"),
            "last_success_time": _latest(db, ItsmTicketSyncLog, ItsmTicketSyncLog.sync_time, ItsmTicketSyncLog.sync_status == "success"),
            "last_failure_time": _latest(db, ItsmTicketSyncLog, ItsmTicketSyncLog.sync_time, ItsmTicketSyncLog.sync_status == "failed"),
        }
    if task_code == "T-053":
        return {
            "configured_count": _count(db, CmdbSource),
            "active_count": _count(db, CmdbSource, CmdbSource.enabled == 1),
            "domain_event_count": _count(db, CmdbSyncJob),
            "domain_failed_count": _count(db, CmdbSyncJob, CmdbSyncJob.status == "failed"),
            "last_success_time": _latest(db, CmdbSyncJob, CmdbSyncJob.finished_at, CmdbSyncJob.status == "success"),
            "last_failure_time": _latest(db, CmdbSyncJob, CmdbSyncJob.finished_at, CmdbSyncJob.status == "failed"),
        }
    if task_code in {"T-054", "T-055", "T-060"}:
        source_types = {
            "T-054": {"elasticsearch", "opensearch", "loki", "graylog"},
            "T-055": {"prometheus"},
            "T-060": {"skywalking", "jaeger", "zipkin", "otel", "opentelemetry"},
        }[task_code]
        query_kinds = {
            "T-054": {"log"},
            "T-055": {"grafana_export"},
            "T-060": {"apm_trace", "apm_sql_evidence"},
        }[task_code]
        configured = _count(db, ObservabilitySource, ObservabilitySource.source_type.in_(source_types))
        active = _count(
            db,
            ObservabilitySource,
            ObservabilitySource.source_type.in_(source_types),
            ObservabilitySource.enabled == 1,
        )
        if task_code == "T-055":
            configured = max(configured, 1)  # Built-in versioned dashboard templates are always available.
            active = max(active, 1)
        return {
            "configured_count": configured,
            "active_count": active,
            "domain_event_count": _count(db, ObservabilityQueryAudit, ObservabilityQueryAudit.query_kind.in_(query_kinds)),
            "domain_failed_count": _count(db, ObservabilityQueryAudit, ObservabilityQueryAudit.query_kind.in_(query_kinds), ObservabilityQueryAudit.status == "failed"),
            "last_success_time": _latest(db, ObservabilityQueryAudit, ObservabilityQueryAudit.create_time, ObservabilityQueryAudit.query_kind.in_(query_kinds), ObservabilityQueryAudit.status == "success"),
            "last_failure_time": _latest(db, ObservabilityQueryAudit, ObservabilityQueryAudit.create_time, ObservabilityQueryAudit.query_kind.in_(query_kinds), ObservabilityQueryAudit.status == "failed"),
        }
    if task_code == "T-056":
        return {
            "configured_count": _count(db, BackupPlatformConfig),
            "active_count": _count(db, BackupPlatformConfig, BackupPlatformConfig.enabled == 1),
            "domain_event_count": _count(db, BackupExternalRun),
            "domain_failed_count": _count(db, BackupExternalRun, BackupExternalRun.status == "failed"),
            "last_success_time": _latest(db, BackupExternalRun, BackupExternalRun.finished_at, BackupExternalRun.status == "success"),
            "last_failure_time": _latest(db, BackupExternalRun, BackupExternalRun.finished_at, BackupExternalRun.status == "failed"),
        }
    if task_code == "T-057":
        return {
            "configured_count": _count(db, ArchiveStorageConfig),
            "active_count": _count(db, ArchiveStorageConfig, ArchiveStorageConfig.enabled == 1),
            "domain_event_count": _count(db, ArchiveTransfer),
            "domain_failed_count": _count(db, ArchiveTransfer, ArchiveTransfer.status == "failed"),
            "last_success_time": _latest(db, ArchiveTransfer, ArchiveTransfer.finish_time, ArchiveTransfer.status == "success"),
            "last_failure_time": _latest(db, ArchiveTransfer, ArchiveTransfer.finish_time, ArchiveTransfer.status == "failed"),
        }
    if task_code == "T-058":
        return {
            "configured_count": _count(db, AutomationPlan),
            "active_count": _count(db, AutomationPlan, AutomationPlan.status.in_(["approved", "executed"])),
            "domain_event_count": _count(db, ExecutionRun),
            "domain_failed_count": _count(db, ExecutionRun, ExecutionRun.status == "failed"),
            "last_success_time": _latest(db, ExecutionRun, ExecutionRun.finished_at, ExecutionRun.status == "succeeded"),
            "last_failure_time": _latest(db, ExecutionRun, ExecutionRun.finished_at, ExecutionRun.status == "failed"),
        }
    if task_code == "T-059":
        return {
            "configured_count": _count(db, IdentityProvider),
            "active_count": _count(db, IdentityProvider, IdentityProvider.enabled == 1),
            "domain_event_count": _count(db, LoginAudit),
            "domain_failed_count": _count(db, LoginAudit, LoginAudit.outcome == "failed"),
            "last_success_time": _latest(db, LoginAudit, LoginAudit.create_time, LoginAudit.outcome == "success"),
            "last_failure_time": _latest(db, LoginAudit, LoginAudit.create_time, LoginAudit.outcome == "failed"),
        }
    return {
        "configured_count": 0,
        "active_count": 0,
        "domain_event_count": 0,
        "domain_failed_count": 0,
        "last_success_time": None,
        "last_failure_time": None,
    }


def _time_order_value(value) -> float:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            normalized = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
            return normalized.astimezone(timezone.utc).timestamp()
        except ValueError:
            return float("-inf")
    return float("-inf")


def _newest(*values):
    present = [value for value in values if value is not None]
    return max(present, key=_time_order_value) if present else None


def _health_status(definition: CapabilityDefinition, summary: dict, legacy_config_count: int) -> CapabilityStatus:
    configured_count = int(summary["configured_count"] or 0)
    active_count = int(summary["active_count"] or 0)
    success_time = summary.get("last_success_time")
    failure_time = summary.get("last_failure_time")
    if failure_time and (not success_time or _time_order_value(failure_time) >= _time_order_value(success_time)):
        return "degraded"
    if success_time:
        return "active"
    if configured_count == 0 and legacy_config_count == 0:
        return definition.get("status", "planned")
    if configured_count > 0 and active_count == 0:
        return "disabled"
    return "configured"


def summarize_capabilities(db: Session) -> list[dict]:
    config_counts = _count_by_task(db, EcosystemIntegrationConfig)
    event_counts = _count_by_task(db, EcosystemIntegrationEvent)
    failure_counts = _count_by_task(db, EcosystemIntegrationEvent, status="failed")
    plan_counts = _count_by_task(db, EcosystemIntegrationPlan)
    latest_success = _latest_event_by_task(db, status="success")
    latest_failure = _latest_event_by_task(db, status="failed")

    items = []
    for task_code, definition in CAPABILITY_REGISTRY.items():
        domain = _domain_summary(db, task_code)
        legacy_config_count = config_counts.get(task_code, 0)
        legacy_event_count = event_counts.get(task_code, 0)
        legacy_failure_count = failure_counts.get(task_code, 0)
        success_time = _newest(domain.get("last_success_time"), latest_success.get(task_code))
        failure_time = _newest(domain.get("last_failure_time"), latest_failure.get(task_code))
        dynamic = {**domain, "last_success_time": success_time, "last_failure_time": failure_time}
        health_status = _health_status(definition, dynamic, legacy_config_count)
        items.append({
            **definition,
            "status": health_status,
            "health_status": health_status,
            "configured_count": domain["configured_count"],
            "active_count": domain["active_count"],
            "config_count": legacy_config_count,
            "legacy_config_count": legacy_config_count,
            "event_count": int(domain["domain_event_count"] or 0) + legacy_event_count,
            "domain_event_count": domain["domain_event_count"],
            "failure_count": int(domain["domain_failed_count"] or 0) + legacy_failure_count,
            "domain_failed_count": domain["domain_failed_count"],
            "plan_count": plan_counts.get(task_code, 0),
            "latest_success": success_time,
            "latest_failure": failure_time,
            "last_success_time": success_time,
            "legacy_record_count": legacy_config_count + legacy_event_count + plan_counts.get(task_code, 0),
        })
    return items
