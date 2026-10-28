from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cmdb.models import (
    AssetCluster,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetSite,
    AssetZone,
)
from app.models.access_control import (
    AccessRole,
    RoleActionPermission,
    SecurityAuditEvent,
    UserAccessRole,
    UserAssetScope,
)


ACTION_CATALOG = [
    ("asset.read", "查看资产与拓扑", "资产管理"),
    ("asset.manage", "维护资产与发现结果", "资产管理"),
    ("instance.read", "查看数据库连接", "实例接入"),
    ("instance.manage", "新增、修改和删除连接", "实例接入"),
    ("instance.credential", "轮换凭据和管理证书", "实例接入"),
    ("inspection.read", "查看巡检任务与报告", "巡检"),
    ("inspection.execute", "执行巡检", "巡检"),
    ("inspection.template.manage", "维护巡检模板与策略", "巡检"),
    ("monitoring.read", "查看监控、日志和告警", "监控告警"),
    ("monitoring.manage", "维护采集、告警和集成配置", "监控告警"),
    ("operations.read", "查看数据库运行信息", "数据库运维"),
    ("operations.execute", "执行数据库运维动作", "数据库运维"),
    ("backup.read", "查看备份与恢复", "数据保护"),
    ("backup.manage", "维护备份与恢复策略", "数据保护"),
    ("backup.execute", "执行备份、恢复与演练", "数据保护"),
    ("recovery.agent", "恢复代理注册、领取与回报任务", "数据保护"),
    ("data_governance.read", "查看归档与数据治理", "数据治理"),
    ("data_governance.manage", "维护归档策略和计划", "数据治理"),
    ("data_governance.execute", "执行归档与恢复验证", "数据治理"),
    ("data_governance.approve", "审批归档计划", "数据治理"),
    ("data_governance.legal_hold", "设置和解除归档保留", "数据治理"),
    ("diagnosis.read", "查看诊断与事故证据", "智能诊断"),
    ("diagnosis.execute", "发起诊断与现场采集", "智能诊断"),
    ("diagnosis.export", "下载事故原始证据", "智能诊断"),
    ("scene.capture.approve", "审批高风险现场采集", "智能诊断"),
    ("report.read", "查看报告", "报告中心"),
    ("report.export", "下载和导出报告", "报告中心"),
    ("report.manage", "维护报告归档、调度和订阅", "报告中心"),
    ("automation.approve", "审批自动化计划", "自动化"),
    ("automation.read", "查看自动化计划与审批队列", "自动化"),
    ("automation.execute", "执行和回滚自动化计划", "自动化"),
    ("control.probe", "查看任务控制状态", "任务控制"),
    ("control.pause", "请求任务安全点暂停", "任务控制"),
    ("control.resume", "从已验证检查点恢复任务", "任务控制"),
    ("control.stop", "请求任务安全点停止", "任务控制"),
    ("platform.settings", "维护平台参数", "平台管理"),
    ("platform.security", "维护用户、角色和安全策略", "平台管理"),
    ("audit.read", "查看安全与操作审计", "平台管理"),
]

BUILTIN_ROLES: dict[str, tuple[str, list[str]]] = {
    "platform_admin": ("平台管理员", ["*"]),
    "dba_operator": (
        "数据库运维人员",
        [
            "asset.read", "instance.read", "inspection.*", "monitoring.read",
            "operations.*", "backup.*", "diagnosis.*", "report.*",
            "data_governance.read", "data_governance.manage", "data_governance.execute",
            "automation.read", "automation.execute",
        ],
    ),
    "approver": (
        "变更审批人员",
        ["asset.read", "instance.read", "inspection.read", "monitoring.read", "report.read", "backup.read", "automation.read", "automation.approve", "data_governance.read", "data_governance.approve"],
    ),
    "auditor": (
        "审计人员",
        ["asset.read", "instance.read", "inspection.read", "monitoring.read", "operations.read", "backup.read", "diagnosis.read", "report.read", "audit.read", "data_governance.read", "automation.read"],
    ),
    "viewer": (
        "只读用户",
        ["asset.read", "instance.read", "inspection.read", "monitoring.read", "operations.read", "backup.read", "diagnosis.read", "report.read", "data_governance.read"],
    ),
}

SCOPE_TYPES = {"all", "tenant", "organization", "region", "site", "zone", "environment", "host", "cluster", "instance"}
ACCESS_LEVELS = {"read", "operate", "admin"}
ACCESS_LEVEL_RANK = {"read": 1, "operate": 2, "admin": 3}


@dataclass(frozen=True)
class ScopeSet:
    restricted: bool
    normalized_instance_ids: frozenset[int]
    native_instance_ids: frozenset[int]
    host_ids: frozenset[int]
    cluster_ids: frozenset[int]


def ensure_builtin_roles(db: Session) -> list[AccessRole]:
    roles: list[AccessRole] = []
    for role_key, (name, action_keys) in BUILTIN_ROLES.items():
        row = db.query(AccessRole).filter(AccessRole.role_key == role_key).first()
        if row is None:
            row = AccessRole(role_key=role_key, name=name, builtin=1, status="active")
            db.add(row)
            db.flush()
            for action_key in action_keys:
                db.add(RoleActionPermission(role_id=row.id, action_key=action_key))
        roles.append(row)
    db.commit()
    return roles


def action_catalog() -> list[dict]:
    return [
        {"action_key": key, "name": name, "category": category}
        for key, name, category in ACTION_CATALOG
    ]


POLICY_PREFIX = "/api/v1"


def prefix_supports_action_policy(prefix: str) -> bool:
    """Does the action policy table below cover this router prefix?

    The table is written against the literal ``/api/v1`` while the router mounts at
    ``settings.API_V1_PREFIX``. A non-default prefix therefore routes normally but matches
    no branch: ``action_for_request`` returns ``None`` and ``is_action_allowed`` treats
    ``None`` as *denied*, so every non-administrator is refused while administrators keep
    bypassing policy. That is a **closed** failure, not an open one. (An earlier report of
    this round called it fail-open; it was wrong, and
    ``test_the_action_policy_table_is_written_against_the_default_api_prefix`` now pins the
    real behaviour.)

    ``main.py`` logs an error at startup when this returns False. Making arbitrary prefixes
    genuinely work means rewriting the table against a parameter -- a wider change than a
    defect fix, so it is left for a product decision.
    """
    return (prefix or "").rstrip("/") == POLICY_PREFIX


def api_prefix_mismatch_message(prefix: str) -> str | None:
    """Startup diagnostic for a router prefix the action table cannot serve.

    ``None`` when the prefix is fine. Kept as a pure function so the wording and the
    trigger condition are unit-testable without booting the application -- the
    ``lifespan`` hook only logs whatever this returns. (The message itself is the
    contract: an operator who changes the prefix has to be told the consequence in
    the same breath, otherwise "all non-admin requests 403" reads like a bug.)
    """
    if prefix_supports_action_policy(prefix):
        return None
    return (
        "API_V1_PREFIX=%r 与动作权限表不一致（表按 %r 编写）：路由仍然可访问，"
        "但每个非管理员请求都会因匹配不到动作而返回 403。这是配置错误而非权限缺陷，"
        "请改回 %r，或先扩展权限表再改前缀。"
    ) % (prefix, POLICY_PREFIX, POLICY_PREFIX)


def action_for_request(method: str, path: str) -> str | None:
    method = method.upper()
    path = path.rstrip("/") or "/"
    mutating = method in {"POST", "PUT", "PATCH", "DELETE"}

    def under(*prefixes: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)

    if path in {"/metrics", "/docs", "/openapi.json", "/redoc"}:
        return "platform.settings"
    if method == "GET" and re.fullmatch(r"/api/v1/assistant/executions/[^/]+/control", path):
        return "control.probe"
    if method == "POST" and (match := re.fullmatch(r"/api/v1/assistant/executions/[^/]+/(pause|resume|stop|probe)", path)):
        return "control." + match.group(1)
    if ((method == "GET" and (path in {"/api/v1/assistant/workflows", "/api/v1/assistant/workflows/preparation",
                                       "/api/v1/assistant/workflows/proposals", "/api/v1/assistant/workflows/providers"}
                              or re.fullmatch(r"/api/v1/assistant/workflows/[^/]+/versions/[^/]+", path)))
            or (method == "POST" and (path in {"/api/v1/assistant/workflows/match", "/api/v1/assistant/workflows/proposals"}
                                     or re.fullmatch(r"/api/v1/assistant/workflows/[^/]+/versions/[^/]+/prepare", path)
                                     or re.fullmatch(r"/api/v1/assistant/workflows/plans/[^/]+/revisions", path)))):
        return "asset.read"  # Each recipe still checks every underlying business action and target.
    # Route floor only. Assistant handlers must still check every capability's real actions.
    if ((method == "GET" and (path in {"/api/v1/assistant/capabilities", "/api/v1/assistant/plans", "/api/v1/assistant/batches"}
                             or re.fullmatch(r"/api/v1/assistant/batches/[^/]+", path)
                             or re.fullmatch(r"/api/v1/assistant/(?:capabilities|plans)/[^/]+", path)
                             or re.fullmatch(r"/api/v1/assistant/dispatch-intent/[^/]+", path)
                             or path == "/api/v1/assistant/dispatch-metrics"
                             or re.fullmatch(r"/api/v1/assistant/plans/[^/]+/(?:validation|approval)", path)))
            or (method == "POST" and (path in {"/api/v1/assistant/plans", "/api/v1/assistant/batches", "/api/v1/assistant/batches/prepare", "/api/v1/assistant/dispatch-intent"}
                                     or re.fullmatch(r"/api/v1/assistant/batches/[^/]+/cancel", path)
                                     or re.fullmatch(r"/api/v1/assistant/plans/[^/]+/(?:execute|revisions|approval/(?:request|revoke))", path)))):
        return "asset.read"
    if method == "POST" and re.fullmatch(r"/api/v1/assistant/plans/[^/]+/approval/approve", path):
        return "automation.approve"
    if not mutating and under("/api/v1/security/audit", "/api/v1/incident-audit"):
        return "audit.read"
    if under("/api/v1/users", "/api/v1/identity", "/api/v1/security"):
        return "platform.security"
    if under("/api/v1/settings", "/api/v1/modules", "/api/v1/setup", "/api/v1/system/init", "/api/v1/ai-config", "/api/v1/ecosystem", "/api/v1/ssh"):
        return "platform.settings"
    if under("/api/v1/release-center"):
        # Read-only surface: the upgrade transaction stays on the operator CLI.
        return "platform.settings"
    if under("/api/v1/system"):
        return "platform.settings" if mutating else "asset.read"
    if under("/api/v1/connection-security"):
        return "instance.credential" if mutating else "instance.read"
    if method == "GET" and under("/api/v1/engine-catalog"):
        # 只读元数据：目录清单与各引擎驱动可用性，都属"看有哪些引擎"。
        return "asset.read"
    if under("/api/v1/instances"):
        if "/credentials/" in path:
            return "instance.credential"
        return "instance.manage" if mutating else "instance.read"
    if under("/api/v1/assets", "/api/v1/cmdb"):
        return "asset.manage" if mutating else "asset.read"
    if under("/api/v1/inspection", "/api/v1/scheduler"):
        if "/templates" in path or "/policies" in path or "/custom-metrics" in path:
            return "inspection.template.manage" if mutating else "inspection.read"
        return "inspection.execute" if mutating else "inspection.read"
    if under(
        "/api/v1/observability",
        "/api/v1/zabbix",
        "/api/v1/zabbix-push",
        "/api/v1/prometheus",
        "/api/v1/alert-events",
        "/api/v1/capacity",
        "/api/v1/notify",
        "/api/v1/itsm",
    ):
        return "monitoring.manage" if mutating else "monitoring.read"
    if under("/api/v1/data-governance"):
        if not mutating:
            return "data_governance.read"
        if re.fullmatch(r"/api/v1/data-governance/jobs/[^/]+/status", path):
            return "data_governance.approve"
        if "/legal-holds" in path:
            return "data_governance.legal_hold"
        if any(part in path for part in ("/execute", "/precheck", "/recovery-test", "/restore", "/upload", "/lifecycle")):
            return "data_governance.execute"
        return "data_governance.manage"
    if under("/api/v1/backup", "/api/v1/backup-center", "/api/v1/recovery"):
        if under("/api/v1/recovery/agent"):
            return "recovery.agent"
        if method == "POST" and path == "/api/v1/recovery/tasks":
            return "backup.execute"
        if mutating and path.endswith(("/approve", "/reject")):
            return "automation.approve"
        if mutating and path == "/api/v1/backup-center/plan":
            return "backup.execute"
        if any(part in path for part in ("/execute", "/run", "/restore", "/drill")) and mutating:
            return "backup.execute"
        return "backup.manage" if mutating else "backup.read"
    if under("/api/v1/automation", "/api/v1/approvals"):
        if not mutating:
            return "automation.read"
        return "automation.approve" if any(part in path for part in ("approve", "reject")) else "automation.execute"
    # Existing operators have diagnosis.*; approval must not inherit that grant.
    if method == "POST" and re.fullmatch(r"/api/v1/scene-captures/[^/]+/approval", path):
        return "scene.capture.approve"
    if under(
        "/api/v1/ai", "/api/v1/diagnosis", "/api/v1/diagnosis-center", "/api/v1/diagnosis-evaluation",
        "/api/v1/incidents", "/api/v1/incident-snapshots", "/api/v1/incident-agents", "/api/v1/incident-trigger-rules",
        "/api/v1/incident-snapshot-health", "/api/v1/incident-audit",
        "/api/v1/states", "/api/v1/fault-patterns", "/api/v1/scene-captures", "/api/v1/probe-templates",
        "/api/v1/probe-leases", "/api/v1/kernel-observability",
    ):
        if path.endswith("/download"):
            return "diagnosis.export"
        return "diagnosis.execute" if mutating else "diagnosis.read"
    if under("/api/v1/reports"):
        # Generating a derived report is a read-side artifact, not target-DB mutation.
        if path.rstrip("/") == "/api/v1/reports/document/generate":
            return "report.read"
        if any(part in path for part in ("/export", "/download")):
            return "report.export"
        if path.startswith(("/api/v1/reports/inspect", "/api/v1/reports/dbcheck")) and mutating:
            return "inspection.execute"
        return "report.manage" if mutating else "report.read"
    if under("/api/v1/oracle", "/api/v1/mysql", "/api/v1/mysql-ops", "/api/v1/postgresql", "/api/v1/sqlserver", "/api/v1/operations", "/api/v1/slow-sql", "/api/v1/performance", "/api/v1/sql-profiles"):
        if path.endswith(("/download", "/export", "/export-word", "/export-html", "/export-markdown")):
            return "report.export"
        return "operations.execute" if mutating else "operations.read"
    return None


def additional_actions_for_request(method: str, path: str, payload) -> set[str]:
    """Connection edits can change secrets or send saved credentials to a new host."""
    if method.upper() not in {"POST", "PUT", "PATCH"} or not isinstance(payload, dict):
        return set()
    if not (path.rstrip("/") == "/api/v1/instances" or path.startswith("/api/v1/instances/")):
        return set()
    connection_fields = {
        "host", "port", "username", "password", "password_secret_ref", "db_type", "account_type",
        "service_name", "sid", "oracle_connect_type", "database_name", "schema_name",
        "ssh_host", "ssh_port", "ssh_username", "ssh_password", "ssh_password_secret_ref",
        "certificate_profile_id", "tls_server_name", "tls_verify", "sslmode", "encrypt",
        "trust_server_certificate", "driver_name",
    }
    return {"instance.credential"} if connection_fields.intersection(payload) else set()


def effective_action_keys(db: Session, user_id: int) -> tuple[bool, set[str]]:
    role_ids = [row.role_id for row in db.query(UserAccessRole).filter(UserAccessRole.user_id == user_id).all()]
    if not role_ids:
        return False, set()
    actions = {
        row.action_key
        for row in db.query(RoleActionPermission).join(
            AccessRole, AccessRole.id == RoleActionPermission.role_id,
        ).filter(RoleActionPermission.role_id.in_(role_ids), AccessRole.status == "active").all()
    }
    return True, actions


def action_matches(grants: Iterable[str], required: str) -> bool:
    for grant in grants:
        if grant == "*" or grant == required:
            return True
        if grant.endswith(".*") and required.startswith(grant[:-1]):
            return True
    return False


def is_action_allowed(db: Session, user_id: int, required: str | None) -> tuple[bool, bool]:
    """Only an explicitly assigned active role can authorize a known action."""
    if not required:
        return False, False
    explicit, grants = effective_action_keys(db, user_id)
    return action_matches(grants, required), explicit


def is_superuser_identity(auth_info: dict | None) -> bool:
    """Mirror the middleware bypass in app.main: only a local role=admin skips policy.

    ``mes`` identities are always role=user, and non-local identities are rejected
    before reaching any business endpoint, so ``role == "admin"`` is the exact
    superuser marker the request pipeline honours.
    """
    return str((auth_info or {}).get("role") or "") == "admin"


def role_action_keys(db: Session, role_ids: Iterable[int]) -> list[str]:
    """Action keys granted by the given active roles."""
    ids = sorted({int(item) for item in role_ids})
    if not ids:
        return []
    return sorted({
        row.action_key
        for row in db.query(RoleActionPermission).join(
            AccessRole, AccessRole.id == RoleActionPermission.role_id,
        ).filter(RoleActionPermission.role_id.in_(ids), AccessRole.status == "active").all()
    })


def actions_beyond_actor(
    db: Session,
    actor_user_id: int,
    action_keys: Iterable[str],
    *,
    actor_is_superuser: bool,
) -> list[str]:
    """Actions a pending grant would add that the actor does not itself hold.

    Enforces a privilege ceiling on role assignment so that a delegated
    administrator cannot hand out (or self-assign) permissions above their own.
    """
    if actor_is_superuser:
        return []
    _, grants = effective_action_keys(db, actor_user_id)
    return sorted({key for key in action_keys if not action_matches(grants, key)})


def resolve_scope(db: Session, user_id: int, minimum_access: str = "read") -> ScopeSet:
    rows = db.query(UserAssetScope).filter(UserAssetScope.user_id == user_id).all()
    if not rows:
        return ScopeSet(False, frozenset(), frozenset(), frozenset(), frozenset())
    minimum_rank = ACCESS_LEVEL_RANK.get(minimum_access, ACCESS_LEVEL_RANK["read"])
    rows = [row for row in rows if ACCESS_LEVEL_RANK.get(row.access_level, 0) >= minimum_rank]
    if any(row.scope_type == "all" for row in rows):
        return ScopeSet(False, frozenset(), frozenset(), frozenset(), frozenset())

    predicates = []
    for row in rows:
        scope_id = row.scope_id
        if scope_id is None:
            continue
        field = {
            "tenant": AssetInstance.tenant_id,
            "organization": AssetInstance.organization_id,
            "region": AssetInstance.region_id,
            "site": AssetInstance.site_id,
            "zone": AssetInstance.zone_id,
            "environment": AssetInstance.env_id,
            "host": AssetInstance.host_id,
            "cluster": AssetInstance.cluster_id,
            "instance": AssetInstance.id,
        }.get(row.scope_type)
        if field is not None:
            predicates.append(field == scope_id)
    if not predicates:
        return ScopeSet(True, frozenset(), frozenset(), frozenset(), frozenset())
    instances = db.query(AssetInstance).filter(
        or_(*predicates), AssetInstance.status.not_in(["inactive", "merged", "deleted"]),
    ).all()
    instance_ids = {row.id for row in instances}
    native_ids = {row.native_instance_id for row in instances if row.native_instance_id is not None}
    host_ids = {row.host_id for row in instances}
    cluster_ids = {row.cluster_id for row in instances if row.cluster_id is not None}
    return ScopeSet(True, frozenset(instance_ids), frozenset(native_ids), frozenset(host_ids), frozenset(cluster_ids))


def scope_allows_entity(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
    minimum_access: str = "read",
) -> bool:
    scope = resolve_scope(db, user_id, minimum_access=minimum_access)
    if not scope.restricted:
        return True
    allowed = {
        "native_instance": scope.native_instance_ids,
        "instance": scope.normalized_instance_ids,
        "host": scope.host_ids,
        "cluster": scope.cluster_ids,
    }.get(entity_type)
    return entity_id in allowed if allowed is not None else False


def scope_allows_path(db: Session, user_id: int, path: str, method: str = "GET") -> bool:
    patterns = (
        (r"^/api/v1/instances/(\d+)(?:/|$)", "native_instance"),
        (r"^/api/v1/(?:oracle|mysql|mysql-ops|postgresql|sqlserver)/(\d+)(?:/|$)", "native_instance"),
        (r"^/api/v1/operations/(?:oracle|mysql|postgresql|sqlserver)/(\d+)(?:/|$)", "native_instance"),
        (r"^/api/v1/assets/instances/(\d+)(?:/|$)", "instance"),
        (r"^/api/v1/assets/hosts/(\d+)(?:/|$)", "host"),
        (r"^/api/v1/assets/clusters/(\d+)(?:/|$)", "cluster"),
    )
    minimum_access = "operate" if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} else "read"
    for pattern, entity_type in patterns:
        match = re.match(pattern, path)
        if match:
            return scope_allows_entity(
                db,
                user_id,
                entity_type,
                int(match.group(1)),
                minimum_access=minimum_access,
            )
    return True


def scope_options(db: Session) -> dict:
    return {
        "regions": _named_options(db.query(AssetRegion).filter(AssetRegion.status == "active").order_by(AssetRegion.name).all()),
        "sites": _named_options(db.query(AssetSite).filter(AssetSite.status == "active").order_by(AssetSite.name).all()),
        "zones": _named_options(db.query(AssetZone).filter(AssetZone.status == "active").order_by(AssetZone.name).all()),
        "environments": _named_options(db.query(AssetEnvironment).filter(AssetEnvironment.status == "active").order_by(AssetEnvironment.name).all()),
        "hosts": [{"id": row.id, "name": row.hostname or row.name, "detail": row.ip} for row in db.query(AssetHost).order_by(AssetHost.name).all()],
        "clusters": [{"id": row.id, "name": row.name, "detail": row.db_type} for row in db.query(AssetCluster).order_by(AssetCluster.name).all()],
        "instances": [{"id": row.id, "name": row.name, "detail": f"{row.ip}:{row.port}"} for row in db.query(AssetInstance).order_by(AssetInstance.name).all()],
    }


def _named_options(rows) -> list[dict]:
    return [{"id": row.id, "name": row.name, "detail": getattr(row, "code", None)} for row in rows]


_SENSITIVE_KEY = re.compile(r"password|secret|token|authorization|private.?key|cookie", re.I)


def sanitize_audit_details(value):
    if isinstance(value, dict):
        return {
            str(key): "***" if _SENSITIVE_KEY.search(str(key)) else sanitize_audit_details(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_audit_details(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", value)
        text = re.sub(r"(?i)(password|secret|token)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
        return text[:2000]
    return value


def append_security_audit(
    db: Session,
    *,
    category: str,
    action: str,
    actor: str,
    actor_user_id: int | None = None,
    outcome: str = "success",
    target_type: str | None = None,
    target_id: str | int | None = None,
    details=None,
    retention_days: int = 365,
) -> SecurityAuditEvent | None:
    from app.core.transaction_lock import lock_transaction

    # The lock lives until the caller commits/rolls back its business transaction.
    lock_transaction(db, "security_audit")
    try:
        previous = db.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id.desc()).with_for_update().first()
        previous_hash = previous.event_hash if previous else ""
        created_at = datetime.now().replace(microsecond=0)
        clean_details = sanitize_audit_details(details)
        canonical = json.dumps(
            {
                "previous_hash": previous_hash,
                "category": category,
                "action": action,
                "outcome": outcome,
                "actor_user_id": actor_user_id,
                "actor": actor,
                "target_type": target_type,
                "target_id": str(target_id) if target_id is not None else None,
                "details": clean_details,
                "created_at": created_at.isoformat(timespec="microseconds"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        row = SecurityAuditEvent(
            category=category,
            action=action,
            outcome=outcome,
            actor_user_id=actor_user_id,
            actor=actor,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=clean_details,
            previous_hash=previous_hash,
            event_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            retention_until=created_at + timedelta(days=max(1, retention_days)),
            create_time=created_at,
        )
        db.add(row)
        db.flush()
        return row
    except SQLAlchemyError:
        # A sensitive operation must not commit without its requested audit record.
        raise


def verify_audit_chain(rows: Iterable[SecurityAuditEvent]) -> dict:
    previous_hash = ""
    checked = 0
    for row in rows:
        checked += 1
        if row.previous_hash != previous_hash:
            return {"valid": False, "checked": checked, "broken_event_id": row.id}
        canonical = json.dumps(
            {
                "previous_hash": row.previous_hash,
                "category": row.category,
                "action": row.action,
                "outcome": row.outcome,
                "actor_user_id": row.actor_user_id,
                "actor": row.actor,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "details": row.details,
                "created_at": row.create_time.isoformat(timespec="microseconds"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if expected != row.event_hash:
            return {"valid": False, "checked": checked, "broken_event_id": row.id}
        previous_hash = row.event_hash
    return {"valid": True, "checked": checked, "broken_event_id": None, "head_hash": previous_hash}
