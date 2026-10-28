"""Fail-closed, pre-routing asset authorization for AuthMiddleware.

Call ``scope_allows_request_sync(db, user_id, request, body)`` in the Session's
owning worker after asynchronously caching request.body(). This supplements action RBAC; it
does not authenticate, commit, execute business handlers, or rewrite parameters.
Request.body() is cached for Starlette BaseHTTPMiddleware's downstream replay.

Coverage is intentionally bounded: instance-addressed engine operations, native
instance maintenance, normalized asset details, backup operations/policies, and
governance policies/jobs/executions/artifacts/holds. Unknown routes, unfiltered
collections, multipart bodies, generic CMDB IDs/writes, connection retargeting,
unreviewed engine report/task IDs, and global storage/configuration management require
unrestricted scope. In particular,
an extra allowed instance_id never turns an unknown/global route into a grant.
New indirect resource families need an explicit ownership resolver below.
Assistant coverage is limited to safe catalogs and explicit read workflows;
stored/global plan ownership is never inferred from caller-supplied hints.
Raw artifact registration also stays denied: its caller-selected source_path
has no ownership link to the supplied archive_job_id. Use scoped job export.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect, Request
from starlette.routing import Match, Mount

from app.cmdb.models import AssetInstance
from app.models.ai_provider_config import AIProviderConfig
from app.models.archive_storage import ArchiveArtifact, ArchiveStorageConfig
from app.models.backup_operation import BackupOperation
from app.models.backup_policy import DBBackupPolicy
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.inspection_policy import DBInspectionPolicy
from app.models.check_result import DBCheckResult
from app.models.inspection_job import DBInspectionJob
from app.models.data_governance import (
    DBArchiveExecution,
    DBArchiveJob,
    DBArchiveLegalHold,
    DBArchivePolicy,
)
from app.models.diagnosis_center import DiagIncident
from app.models.restore_drill_policy import DBRestoreDrillPolicy
from app.models.report_center import ReportArtifact, ReportSchedule
from app.services import access_control
from app.services.workflow_library import WORKFLOW_ID_PATTERN


_PREFIX = "/api/v1"
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_SOURCE_REPORT_ROUTES = {
    "/reports/{task_id}/detail", "/reports/{task_id}/export",
    "/reports/inspections/{instance_id}/{task_id}",
    "/reports/inspections/{instance_id}/{task_id}/export-word",
}
_MAX_BODY_BYTES = 1_048_576
_MAX_DEPTH = 32
_MAX_VALUES = 4096

# These existing handlers explicitly pass request.state.auth's resolved scope
# into their queries/services (including assistant.py's plan list). Do not
# add other collections just because they accept an optional instance_id.
_FILTERED_COLLECTIONS = {
    "/instances",
    "/cmdb/assets", "/cmdb/relations", "/cmdb/bindings", "/cmdb/topology",
    "/assets/overview", "/assets/inventory", "/assets/topology", "/assets/tree",
    "/assets/instances", "/assets/hosts", "/assets/clusters",
    "/operations/capability-matrix",
    "/system/dashboard-stats",
    "/inspection/tasks", "/inspection/results", "/inspection/jobs",
    "/reports/catalog", "/reports/catalog/stats", "/reports/schedules",
    "/reports/subscriptions",
    "/assistant/plans",
    "/assistant/batches",
    "/assistant/batches/preparation",
    "/assistant/batches/admission",
    "/assistant/workflows/preparation",
}

# Harmless metadata only: system/config exposes branding/version, not credentials.
_CATALOGS = {
    "/backup-center/catalog", "/data-governance/execution/capabilities",
    "/mysql-ops/modules", "/operations/oracle/modules",
    "/operations/postgresql/modules", "/operations/sqlserver/modules",
    "/operations/{engine}/modules",
    "/system/config", "/system/api-modules",
    "/engine-catalog", "/engine-catalog/drivers",
    "/assistant/capabilities", "/assistant/capabilities/{capability}",
    "/assistant/workflows", "/assistant/workflows/{workflow_id}/versions/{workflow_version}",
    "/assistant/workflows/proposals", "/assistant/workflows/providers",
}

# Only the reviewed method/template pairs below belong to this family. Catalogs
# expose safe metadata with a business gate; plan lists must filter server-side.
_ASSISTANT_METHODS = {
    "/assistant/capabilities": {"GET"},
    "/assistant/capabilities/{capability}": {"GET"},
    "/assistant/dispatch-intent": {"POST"},
    "/assistant/dispatch-intent/{request_id}": {"GET"},
    "/assistant/plans": {"GET", "POST"},
    "/assistant/plans/{plan_id}": {"GET"},
    "/assistant/plans/{plan_id}/execute": {"POST"},
    "/assistant/plans/{plan_id}/validation": {"GET"},
    "/assistant/plans/{plan_id}/revisions": {"POST"},
    "/assistant/plans/{plan_id}/approval": {"GET"},
    "/assistant/plans/{plan_id}/approval/request": {"POST"},
    "/assistant/plans/{plan_id}/approval/approve": {"POST"},
    "/assistant/plans/{plan_id}/approval/revoke": {"POST"},
    "/assistant/batches": {"GET", "POST"},
    "/assistant/batches/prepare": {"POST"},
    "/assistant/batches/preparation": {"GET"},
    "/assistant/batches/admission": {"GET"},
    "/assistant/batches/{batch_id}": {"GET"},
    "/assistant/batches/{batch_id}/cancel": {"POST"},
    "/assistant/executions/{capability_execution_id}/control": {"GET"},
    "/assistant/executions/{capability_execution_id}/pause": {"POST"},
    "/assistant/executions/{capability_execution_id}/resume": {"POST"},
    "/assistant/executions/{capability_execution_id}/stop": {"POST"},
    "/assistant/executions/{capability_execution_id}/probe": {"POST"},
    "/assistant/workflows": {"GET"},
    "/assistant/workflows/preparation": {"GET"},
    "/assistant/workflows/providers": {"GET"},
    "/assistant/workflows/proposals": {"GET", "POST"},
    "/assistant/workflows/{workflow_id}/versions/{workflow_version}": {"GET"},
    "/assistant/workflows/match": {"POST"},
    "/assistant/workflows/{workflow_id}/versions/{workflow_version}/prepare": {"POST"},
    "/assistant/workflows/plans/{plan_id}/revisions": {"POST"},
}
_ASSISTANT_READ_POSTS = {"/assistant/dispatch-intent", "/assistant/plans", "/assistant/plans/{plan_id}/execute",
                         "/assistant/plans/{plan_id}/revisions", "/assistant/plans/{plan_id}/approval/request",
                         "/assistant/plans/{plan_id}/approval/approve", "/assistant/plans/{plan_id}/approval/revoke",
                         "/assistant/batches", "/assistant/batches/prepare", "/assistant/batches/{batch_id}/cancel",
                         "/assistant/workflows/match", "/assistant/workflows/proposals",
                         "/assistant/workflows/{workflow_id}/versions/{workflow_version}/prepare",
                         "/assistant/workflows/plans/{plan_id}/revisions"}
_WORKFLOW_PREPARE_ROUTES = {
    "/assistant/workflows/{workflow_id}/versions/{workflow_version}/prepare",
    "/assistant/workflows/plans/{plan_id}/revisions",
}
_PROPOSAL_ROUTE = "/assistant/workflows/proposals"

# Storage has no tenant/instance ownership column. Existing configured, enabled
# destinations are shared capabilities for these already-authorized job actions,
# never a grant to list/read credentials or mutate the configuration itself.
_STORAGE_DESTINATION_ROUTES = {
    "/data-governance/jobs/{job_id}/execute",
    "/data-governance/object-storage/jobs/{job_id}/upload-plan",
}

# Each handler actually filters on this exact query parameter. Omission, null,
# empty lists, zero, and body-only copies must not select the global collection.
_QUERY_COLLECTIONS = {
    "/diagnosis-center/incidents": "instance_id",
    "/backup-center/operations": "instance_id",
    "/backup-center/platform/restore-drills": "instance_id",
    "/data-governance/policies": "instance_id",
    "/data-governance/candidates": "instance_id",
    "/data-governance/jobs": "instance_id",
    "/data-governance/executions": "job_id",
    "/data-governance/recovery-tests": "artifact_id",
    "/data-governance/legal-holds": "artifact_id",
}

# The values are alternative top-level body anchors, not any nested ID found in
# the payload. For example, jobs/plan can inherit its instance from its policy.
_BODY_ROUTES = {
    "/assistant/dispatch-intent": ("instance_id",),
    "/assistant/plans": ("instance_id",),
    "/assistant/batches": ("plan_ids",),
    "/assistant/batches/prepare": ("targets",),
    "/assistant/workflows/{workflow_id}/versions/{workflow_version}/prepare": ("scope",),
    "/diagnosis-center/diagnose": ("instance_id",),
    "/backup-center/preflight": ("instance_id",),
    "/backup-center/plan": ("instance_id",),
    "/backup-center/operations": ("instance_id",),
    "/backup-center/policies": ("instance_id",),
    "/backup-center/drill-policies": ("source_backup_policy_id",),
    "/data-governance/policies": ("instance_id",),
    "/data-governance/candidates/scan": ("instance_id",),
    "/data-governance/jobs/plan": ("instance_id", "policy_id"),
    "/data-governance/migration/plan": ("source_instance_id",),
    "/data-governance/export/plan": ("instance_id",),
    "/data-governance/partition/plan": ("instance_id",),
}

# These change what an already-authorized ID points at or its scope membership.
# Even no-op submissions are denied: proving an ownership-preserving write needs
# a dedicated transactional update policy, not a pre-routing ID check.
_REASSIGNMENT_FIELDS = {
    "tenant_id", "organization_id", "region_id", "site_id", "zone_id",
    "env_id", "environment_id", "host", "ip", "hostname", "port", "db_type",
    "ssh_host", "ssh_port", "service_name", "sid", "database_name",
    "oracle_connect_type", "native_instance_id", "host_id", "cluster_id",
}
_INSTANCE_MAINTENANCE = ("/instances", "/assets", "/cmdb")
_ENGINE_PREFIXES = (
    "/oracle/{instance_id}", "/mysql/{instance_id}",
    "/mysql-ops/{instance_id}", "/postgresql/{instance_id}",
    "/sqlserver/{instance_id}", "/operations/oracle/{instance_id}",
    "/operations/mysql/{instance_id}", "/operations/postgresql/{instance_id}",
    "/operations/sqlserver/{instance_id}", "/operations/{engine}/{instance_id}",
)
_ENGINE_PATH_ARGUMENTS = {
    "instance_id", "engine", "section", "plan_type", "owner", "table_name",
    "tablespace_name", "user", "username",
}
# SQL/session identifiers refer to objects inside the authorized database, not
# platform records. Stored report_id/task_id/base_id/target_id are NOT exempt.
_ENGINE_LOCAL_IDS = {"sql_id", "thread_id", "session_id", "trx_id", "server_id"}


class _Denied(ValueError):
    pass


@dataclass(frozen=True)
class _Rule:
    location: str
    anchors: tuple[str, ...] = ()


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _rule(path: str, method: str, params: dict[str, Any]) -> _Rule | None:
    if _within(path, "/assistant") and method not in _ASSISTANT_METHODS.get(path, ()):
        return None
    if path in _ASSISTANT_METHODS and "plan_id" in params:
        return _Rule("path", ("plan_id",))
    if path in _ASSISTANT_METHODS and "capability_execution_id" in params:
        return _Rule("path", ("capability_execution_id",))
    if path in _ASSISTANT_METHODS and "batch_id" in params:
        return _Rule("path", ("batch_id",))
    if path == "/assistant/dispatch-intent/{request_id}" and method == "GET":
        # The receipt service re-validates the persisted plan/batch against current scope.
        return _Rule("catalog")
    if path == "/assistant/workflows/match" and method == "POST":
        return _Rule("catalog")  # Suggests a filtered fixed recipe, never a target grant or execution.
    if path == _PROPOSAL_ROUTE and method == "POST":
        # Consent + one model call: the body walk below still checks every target, template
        # and provider ID, and the result can only be adopted into a fresh prepare.
        return _Rule("catalog")
    if method in _READ_METHODS:
        if path in _FILTERED_COLLECTIONS:
            return _Rule("filtered")
        if path in _CATALOGS:
            return _Rule("catalog")
        if path in _QUERY_COLLECTIONS:
            return _Rule("query", (_QUERY_COLLECTIONS[path],))
        if path == "/assistant/plans/{plan_id}":
            return _Rule("path", ("plan_id",))
        if path == "/inspection/jobs/{job_id}":
            return _Rule("path", ("job_id",))
        if path in {"/inspection/tasks/{task_id}", "/inspection/report/{task_id}"}:
            return _Rule("path", ("task_id",))
        if path == "/diagnosis-center/incidents/{incident_id}":
            return _Rule("path", ("incident_id",))
        if path in _SOURCE_REPORT_ROUTES:
            return _Rule("path", ("task_id",))
        if path in {
            "/reports/catalog/{artifact_id}", "/reports/catalog/{artifact_id}/verify",
            "/reports/catalog/{artifact_id}/download",
        }:
            return _Rule("path", ("artifact_id",))
        if path == "/reports/schedules/{schedule_id}/runs":
            return _Rule("path", ("schedule_id",))
    if method == "POST" and path == "/assistant/plans/{plan_id}/execute":
        return _Rule("path", ("plan_id",))
    if method == "POST" and path == "/inspection/{db_type}/{instance_id}/run":
        return _Rule("path", ("instance_id",))
    if method == "POST" and path in {
        "/inspection/jobs/{job_id}/cancel", "/inspection/jobs/{job_id}/retry",
    }:
        # Cancelling or retrying somebody else's run is control, not read: the anchor is the
        # job's own instance, resolved from the stored row below.
        return _Rule("path", ("job_id",))
    if method == "POST" and path in _BODY_ROUTES:
        return _Rule("body", _BODY_ROUTES[path])
    if any(_within(path, prefix) for prefix in _ENGINE_PREFIXES):
        if set(params) <= _ENGINE_PATH_ARGUMENTS:
            return _Rule("path", ("instance_id",))
        return None
    if _within(path, "/instances/{instance_id}") and set(params) == {"instance_id"}:
        return _Rule("path", ("instance_id",))
    if path == "/cmdb/instance-context/{instance_id}":
        return _Rule("path", ("instance_id",))
    if path == "/assets/instances/{instance_id}" and method in _READ_METHODS:
        return _Rule("path", ("instance_id",))
    if path == "/assets/hosts/{host_id}" and method in _READ_METHODS:
        return _Rule("path", ("host_id",))
    if path in {
        "/assets/clusters/{cluster_id}",
        "/assets/clusters/{cluster_id}/ha-diagnostics",
        "/assets/clusters/{cluster_id}/ha-diagnostics/history",
    } and method in _READ_METHODS:
        return _Rule("path", ("cluster_id",))
    if path == "/assets/clusters/{cluster_id}/ha-diagnostics/refresh" and method == "POST":
        return _Rule("path", ("cluster_id",))
    if path == "/backup-center/platform/health/{instance_id}" and method in _READ_METHODS:
        return _Rule("path", ("instance_id",))
    families = {
        "/backup-center/operations/{operation_id}": {"", "/execute", "/approve", "/cancel"},
        "/backup-center/policies/{policy_id}": {"", "/run-now", "/retention-preview"},
        "/backup-center/drill-policies/{policy_id}": {"", "/run-now"},
        "/data-governance/policies/{policy_id}": {""},
        "/data-governance/jobs/{job_id}": {"/status", "/precheck", "/execute"},
        "/data-governance/object-storage/jobs/{job_id}": {"/upload-plan"},
        "/data-governance/object-storage/artifacts/{artifact_id}": {
            "/recovery-test", "/legal-holds", "/verify", "/lifecycle-delete", "/restore-command",
        },
        "/data-governance/legal-holds/{hold_id}": {"/release"},
    }
    for prefix, suffixes in families.items():
        if _within(path, prefix) and path[len(prefix):] in suffixes:
            return _Rule("path", (prefix.rsplit("{", 1)[1].rstrip("}"),))
    return None


def _matched_route(request: Request) -> tuple[str, dict[str, Any]] | None:
    # Middleware runs before Router installs path_params. Match in router order,
    # not by hand-parsing the URL, and never skip a first FULL match to get a grant.
    scope = {**request.scope, "path_params": {}}
    for route in getattr(request.scope.get("app"), "routes", ()):
        match, child = route.matches(scope)
        if match == Match.FULL:
            if isinstance(route, Mount):
                return None  # Mounted applications need their own reviewed policy.
            path = getattr(route, "path_format", None)
            if not isinstance(path, str) or not path.startswith(_PREFIX + "/"):
                return None
            return path[len(_PREFIX):], child.get("path_params", {})
    return None


def _identifier(value: Any) -> int:
    if isinstance(value, bool):
        raise _Denied("Boolean is not a resource ID")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) and int(value) > 0:
        return int(value)
    raise _Denied("Unresolvable resource ID")


def _capability_identifier(value: Any) -> None:
    if (not isinstance(value, str) or len(value) > 64
            or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}", value)):
        raise _Denied("Invalid capability ID")


def _minimum_access(request: Request) -> str:
    method = request.method.upper()
    if method in _READ_METHODS:
        return "read"
    matched = _matched_route(request) if method == "POST" else None
    if matched is not None and matched[0] in _ASSISTANT_READ_POSTS:
        # Transport edits/approval are not target-side operations. Plan services
        # must enforce operate scope for every side-effectful capability itself.
        return "read"
    return "operate"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Denied("Ambiguous duplicate JSON key")
        result[key] = value
    return result


def _key(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


class _Targets:
    def __init__(self, db: Session, scope: access_control.ScopeSet, path: str, mutating: bool):
        self.db = db
        self.scope = scope
        self.path = path
        self.mutating = mutating
        self.engine = any(_within(path, prefix) for prefix in _ENGINE_PREFIXES)
        self.assistant = path in _ASSISTANT_METHODS
        self.checked: set[tuple[type, int]] = set()
        self.visiting: set[tuple[type, int]] = set()
        self.values = 0
        self.source_instances: set[int] | None = None

    def source_task(self, value: Any) -> None:
        if not isinstance(value, str) or not value or len(value) > 30:
            raise _Denied("Invalid report task ID")
        job = self.db.get(DBInspectionJob, value)
        rows = self.db.query(DBCheckResult.instance_id).filter_by(task_id=value).distinct().limit(_MAX_VALUES + 1).all()
        if len(rows) > _MAX_VALUES:
            raise _Denied("Excessive report ownership set")
        owners = {row[0] for row in rows}
        if job is not None:
            if owners - {job.instance_id}:
                raise _Denied("Report results disagree with persisted job owner")
            owners.add(job.instance_id)
        if not owners:
            raise _Denied("Missing report task")
        for instance_id in owners:
            self.native(instance_id)
        self.source_instances = owners

    def native(self, value: Any) -> None:
        if _identifier(value) not in self.scope.native_instance_ids:
            raise _Denied("Native instance outside scope")

    def group(self, field: str, value: Any) -> None:
        entity_id = _identifier(value)
        allowed = self.scope.host_ids if field == "host_id" else self.scope.cluster_ids
        if entity_id not in allowed:
            raise _Denied("Asset group outside scope")
        # Detail/HA handlers include every member, not just the granted one.
        members = self.db.query(AssetInstance.id).filter(getattr(AssetInstance, field) == entity_id).all()
        if not members or any(row[0] not in self.scope.normalized_instance_ids for row in members):
            raise _Denied("Asset group includes unauthorized instances")

    def stored(self, model: type, value: Any) -> None:
        identity = (model, _identifier(value))
        if identity in self.checked:
            return
        if identity in self.visiting or len(self.visiting) >= _MAX_DEPTH:
            raise _Denied("Cyclic or excessive ownership chain")
        row = self.db.get(*identity)
        if row is None:
            raise _Denied("Missing parent resource")
        self.visiting.add(identity)
        if model is ReportArtifact:
            extra = row.extra_json or {}
            if not isinstance(extra, dict):
                raise _Denied("Invalid report scope")
            instance_ids = extra.get("scope_instance_ids") or []
            if not isinstance(instance_ids, list):
                raise _Denied("Invalid report scope")
            instance_ids = list(instance_ids)
            if row.instance_id is not None:
                instance_ids.append(row.instance_id)
            if not instance_ids:
                raise _Denied("Report has no provable instance scope")
            for instance_id in instance_ids:
                self.native(instance_id)
        elif model is ReportSchedule:
            filters = row.filter_json or {}
            instance_ids = filters.get("instance_ids") if isinstance(filters, dict) else None
            if not isinstance(instance_ids, list) or not instance_ids:
                raise _Denied("Schedule has no provable instance scope")
            for instance_id in instance_ids:
                self.native(instance_id)
        elif model is ArchiveArtifact:
            self.stored(DBArchiveJob, row.archive_job_id)
        elif model is DBArchiveLegalHold:
            self.stored(ArchiveArtifact, row.artifact_id)
        else:
            self.native(row.instance_id)
            if model is BackupOperation:
                for field, parent in (
                    ("source_operation_id", BackupOperation), ("policy_id", DBBackupPolicy),
                    ("drill_policy_id", DBRestoreDrillPolicy),
                ):
                    if getattr(row, field) is not None:
                        self.stored(parent, getattr(row, field))
                if row.parameters_json:
                    # Parameters can contain an explicit verification source.
                    self.walk(row.parameters_json, stored=True)
            elif model is DBRestoreDrillPolicy:
                self.stored(DBBackupPolicy, row.source_backup_policy_id)
            elif model is DBArchiveJob and row.policy_id is not None:
                self.stored(DBArchivePolicy, row.policy_id)
            elif model is DBArchiveExecution:
                self.stored(DBArchiveJob, row.job_id)
                if row.artifact_id is not None:
                    self.stored(ArchiveArtifact, row.artifact_id)
        self.visiting.remove(identity)
        self.checked.add(identity)

    def reference(self, key: str, value: Any) -> None:
        if isinstance(value, list):
            if not value:
                raise _Denied("Empty resource list")
            for item in value:
                self.reference(key, item)
            return
        singular = key[:-1] if key.endswith("_ids") else key
        if singular == "instance_id" and self.path.startswith("/assets/"):
            if _identifier(value) not in self.scope.normalized_instance_ids:
                raise _Denied("Normalized instance outside scope")
        elif singular in {"normalized_instance_id", "asset_instance_id"}:
            if _identifier(value) not in self.scope.normalized_instance_ids:
                raise _Denied("Normalized instance outside scope")
        elif singular == "instance_id" or singular.endswith("_instance_id"):
            self.native(value)
            if self.path in _SOURCE_REPORT_ROUTES and self.source_instances != {_identifier(value)}:
                raise _Denied("Report instance hint disagrees with persisted task")
        elif singular in {"host_id", "cluster_id"}:
            self.group(singular, value)
        elif singular == "storage_id" and self.mutating and self.path in _STORAGE_DESTINATION_ROUTES:
            config = self.db.get(ArchiveStorageConfig, _identifier(value))
            if config is None or config.enabled != 1 or config.status not in {"configured", "active"}:
                raise _Denied("Archive destination is not enabled and configured")
        elif self.assistant:
            if singular == "workflow_id" and self.path in _ASSISTANT_METHODS:
                if not isinstance(value, str) or len(value) > 64 or WORKFLOW_ID_PATTERN.fullmatch(value) is None:
                    raise _Denied("Invalid workflow ID")
                return
            if singular == "capability_execution_id":
                member = self.db.get(DBCapabilityExecution, _identifier(value))
                if member is None:
                    raise _Denied("Missing capability execution")
                self.reference("plan_id", member.plan_id)
                return
            if singular == "batch_id":
                if not isinstance(value, str) or not re.fullmatch(r"CB[0-9a-f]{28}", value):
                    raise _Denied("Invalid capability batch")
                batch = self.db.get(DBCapabilityBatch, value)
                members = self.db.query(DBCapabilityExecution.plan_id).filter_by(batch_id=value).all()
                if batch is None or not members:
                    raise _Denied("Missing capability batch")
                for (plan_id,) in members:
                    self.reference("plan_id", plan_id)
                return
            if singular == "policy_id" and self.path in {
                "/assistant/plans", "/assistant/plans/{plan_id}/revisions", "/assistant/batches/prepare",
                *_WORKFLOW_PREPARE_ROUTES,
            }:
                policy = self.db.get(DBInspectionPolicy, _identifier(value))
                # Templates are shared metadata, not target grants. The typed plan
                # adapter still checks the selected target, engine and execute scope.
                if policy is None or policy.enabled != 1:
                    raise _Denied("Missing inspection policy")
                return
            if self.path == _PROPOSAL_ROUTE:
                # A proposal sends text and receives a structure; it selects a provider and
                # optionally a template, neither of which is an asset grant. Both must still
                # exist and be enabled, so a stale ID cannot be carried into consent.
                if singular == "provider_config_id":
                    config = self.db.get(AIProviderConfig, _identifier(value))
                    if config is None or config.enabled != 1:
                        raise _Denied("Provider is not enabled")
                    return
                if singular in {"policy_id", "confirmed_policy_id"}:
                    policy = self.db.get(DBInspectionPolicy, _identifier(value))
                    if policy is None or policy.enabled != 1:
                        raise _Denied("Missing inspection policy")
                    return
            plan_refs = {"plan_id", "parent_plan_id"}
            if self.path in _WORKFLOW_PREPARE_ROUTES:
                plan_refs.add("source_plan_id")
            if (singular not in plan_refs or not isinstance(value, str)
                    or not re.fullmatch(r"pl[0-9a-f]{26}", value)):
                raise _Denied("Unresolved assistant resource")
            plan = self.db.get(DBCapabilityPlan, value)
            if plan is None:
                raise _Denied("Missing capability plan")
            # NULL/global plans have no provable asset owner. Caller hints cannot
            # replace the persisted plan's native instance, including for parents.
            self.native(plan.instance_id)
        elif self.path.startswith("/inspection/") and singular in {"task_id", "job_id"}:
            if not isinstance(value, str) or not value or len(value) > 30:
                raise _Denied("Invalid inspection run ID")
            if singular == "job_id":
                # A run is owned by its *job row*, not by whatever result rows it happened to
                # produce. Looking this up through DBCheckResult would judge every zero-result
                # run "resource does not exist" - which is the visibility defect itself, moved
                # into the permission layer.
                job = self.db.get(DBInspectionJob, value)
                if job is None:
                    raise _Denied("Missing inspection job")
                self.native(job.instance_id)
            else:
                members = self.db.query(DBCheckResult.instance_id).filter(DBCheckResult.task_id == value).distinct().all()
                if not members:
                    raise _Denied("Missing inspection task")
                for member in members:
                    self.native(member[0])
        elif self.path.startswith("/diagnosis-center/"):
            if singular != "incident_id":
                raise _Denied("Unresolved diagnosis resource")
            self.stored(DiagIncident, value)
        elif self.path.startswith("/reports/"):
            if self.path in _SOURCE_REPORT_ROUTES and singular == "task_id":
                if self.source_instances is None:
                    self.source_task(value)
                return
            model = {"artifact_id": ReportArtifact, "schedule_id": ReportSchedule}.get(singular)
            if model is None:
                raise _Denied("Unresolved report resource")
            self.stored(model, value)
        elif self.path.startswith("/backup-center/"):
            model = {
                "operation_id": BackupOperation, "source_operation_id": BackupOperation,
                "policy_id": DBRestoreDrillPolicy if "/drill-policies" in self.path else DBBackupPolicy,
                "source_backup_policy_id": DBBackupPolicy, "drill_policy_id": DBRestoreDrillPolicy,
            }.get(singular)
            if model is None:
                raise _Denied("Unresolved backup resource")
            self.stored(model, value)
        elif self.path.startswith("/data-governance/"):
            model = {
                "policy_id": DBArchivePolicy, "job_id": DBArchiveJob, "archive_job_id": DBArchiveJob,
                "artifact_id": ArchiveArtifact, "hold_id": DBArchiveLegalHold,
                "execution_id": DBArchiveExecution, "precheck_execution_id": DBArchiveExecution,
            }.get(singular)
            if model is None:
                raise _Denied("Unresolved governance resource")
            self.stored(model, value)
        elif not (self.engine and singular in _ENGINE_LOCAL_IDS):
            raise _Denied("Unresolved business resource")

    def walk(self, value: Any, depth: int = 0, *, stored: bool = False) -> None:
        self.values += 1
        if depth > _MAX_DEPTH or self.values > _MAX_VALUES:
            raise _Denied("Excessive request nesting or targets")
        if isinstance(value, list):
            for item in value:
                self.walk(item, depth + 1, stored=stored)
        elif isinstance(value, dict):
            for raw_key, item in value.items():
                key = _key(raw_key)
                if self.mutating and any(_within(self.path, prefix) for prefix in _INSTANCE_MAINTENANCE):
                    if key in _REASSIGNMENT_FIELDS:
                        raise _Denied("Asset retargeting requires unrestricted scope")
                if key == "idempotency_key" and self.path.startswith("/backup-center/") and item:
                    if not isinstance(item, str):
                        raise _Denied("Invalid idempotency key")
                    if not stored:
                        row = self.db.query(BackupOperation).filter(BackupOperation.idempotency_key == item).first()
                        if row is not None:
                            self.stored(BackupOperation, row.id)
                if self.assistant and key == "capability_id":
                    # Exact semantic field only (also in nested steps), not a
                    # blanket exemption for *_id, capability_ids or arbitrary IDs.
                    _capability_identifier(item)
                elif key in {"id", "ids"} or key.endswith(("_id", "_ids")):
                    if item is not None:
                        self.reference(key, item)
                self.walk(item, depth + 1, stored=stored)


async def scope_allows_request(db: Session, user_id: int, request: Request) -> bool:
    """Compatibility wrapper for callers that already own a synchronous Session.

    Do not offload a caller-owned Session. Production middleware uses the sync
    entry point with a Session created and closed entirely in its worker.
    """
    try:
        if user_id <= 0:
            return False
        scope = access_control.resolve_scope(
            db, user_id, minimum_access=_minimum_access(request),
        )
        if not scope.restricted:
            return True
        if _matched_route(request) is None:
            return False
        body = await request.body()
        return scope_allows_request_sync(db, user_id, request, body, scope=scope)
    except (ValueError, TypeError, RecursionError, SQLAlchemyError, ClientDisconnect):
        return False


def scope_allows_request_sync(
    db: Session, user_id: int, request: Request, body: bytes,
    *, scope: access_control.ScopeSet | None = None,
) -> bool:
    """Return False for a restricted request whose complete scope is unproven.

    No-scope local users retain legacy behavior. The parent owns authentication,
    MES-user policy, admin bypass, action permissions, denial responses and audit.
    Storage errors propagate to the middleware's 503 boundary. The Session is
    used only for local metadata reads, never live DB IO.
    """
    method = request.method.upper()
    try:
        if user_id <= 0:
            return False
        if scope is None:
            scope = access_control.resolve_scope(
                db, user_id, minimum_access=_minimum_access(request),
            )
        if not scope.restricted:
            return True
        matched = _matched_route(request)
        if matched is None:
            return False
        path, params = matched
        rule = _rule(path, method, params)
        if rule is None or params.get("engine", "postgresql") not in {"postgresql", "sqlserver"}:
            return False
        if path == "/assistant/dispatch-intent/{request_id}" and method == "GET":
            # A request id is not an asset id; the receipt service revalidates the
            # persisted plan/batch against the caller's current scope and permission.
            return True
        if rule.location == "filtered":
            auth = getattr(request.state, "auth", {}) or {}
            if (auth.get("source") not in {"db", "identity"} or auth.get("role") == "admin"
                    or str(auth.get("user_id")) != str(user_id)):
                return False  # These handlers otherwise bypass their scope filters.

        targets = _Targets(db, scope, path, method not in _READ_METHODS)
        if path in _SOURCE_REPORT_ROUTES:
            # Resolve the persisted task before evaluating any caller instance hints.
            targets.source_task(params.get("task_id"))
        if path == "/assistant/capabilities/{capability}":
            _capability_identifier(params.get("capability"))
        # Path identities are independent of any conflicting query/body values.
        for key, value in params.items():
            if key.endswith(("_id", "_ids")):
                targets.reference(key, value)
        for key, value in request.query_params.multi_items():
            targets.walk({key: value})

        if len(body) > _MAX_BODY_BYTES:
            return False
        payload: Any = {}
        if body:
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type and media_type != "application/json" and not media_type.endswith("+json"):
                return False
            payload = json.loads(body, object_pairs_hook=_unique_object)
            if not isinstance(payload, (dict, list)):
                return False
            targets.walk(payload)

        if path in _WORKFLOW_PREPARE_ROUTES:
            # A scoped caller cannot turn a target-free "global" recipe into a grant.
            invocation_scope = payload.get("scope") if isinstance(payload, dict) else None
            if (not isinstance(invocation_scope, dict) or invocation_scope.get("mode") != "explicit"
                    or not isinstance(invocation_scope.get("targets"), list) or not invocation_scope["targets"]):
                return False

        if rule.location == "path":
            return all(key in params for key in rule.anchors)
        if rule.location == "query":
            return all(request.query_params.getlist(key) for key in rule.anchors)
        if rule.location == "body":
            return isinstance(payload, dict) and any(payload.get(key) is not None for key in rule.anchors)
        return True
    except (ValueError, TypeError, RecursionError):
        return False
