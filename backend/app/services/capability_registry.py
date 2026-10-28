"""Code-owned capability declarations and fail-closed runtime availability.

The database is a disable/synchronization ledger, never a source of executable handlers.
Reads do not sync the ledger or create plan rows. HTTP/plan integration is a later layer.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import logging
import re

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_effect_receipt import DBCapabilityEffectReceipt
from app.models.db_instance import DBInstance
from app.models.user import User
from app.core.config import settings
from app.services import access_control, operations_capabilities
from app.services.capability_declarations import CAPABILITY_DECLARATIONS
from app.services.capability_effect_contract import (
    COMMON_EFFECT_CAPABILITIES,
    EFFECT_RESULT_SCHEMA,
    EFFECT_RESULT_VERSION,
    digest as effect_digest,
    is_common_effect,
    is_durable_definition,
)
from app.services.capability_handler import (CapabilityContext, HANDLERS, PARAM_SCHEMA_JSON, PARAM_SCHEMAS,
                                             ScopeNotSupported)


logger = logging.getLogger(__name__)
CAPABILITY_REGISTRY = CAPABILITY_DECLARATIONS
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}$")
KINDS = frozenset({"read", "query", "collect", "scan", "refresh", "sync", "bind", "plan", "execute"})
FORBIDDEN_KEYS = frozenset({
    "handler", "module", "path", "function", "handler_ref", "sql", "script", "command",
    "shell", "cmd", "url", "endpoint", "endpoint_template", "__proto__",
})
INSTANCE_FILTERED_HANDLERS = frozenset({"inspection.list_jobs", "operations.capability_matrix", "inspection.run_frozen",
                                       "monitor.capacity_collect", "diagnosis.root_cause", "report.document_generate"})
# Handlers whose reader applies the caller's own asset scope itself, so a scoped caller may run
# them without naming one target. Each entry must pass `allowed_instance_ids` to a service that
# filters by it (dashboard) or return shared metadata only (backup policies); anything that
# aggregates platform-wide stays out of this set and is refused.
SCOPE_FILTERED_HANDLERS = frozenset({"platform.dashboard", "protection.policies", "asset.inventory", "report.catalog",
                                     "monitor.capacity_alerts", "governance.candidates", "monitor.alerts",
                                     "config.governance_summary"})
# Handlers over platform-wide configuration. A restricted caller must not read them at all;
# they are not narrowed by an asset scope and are not offered under a target.
GLOBAL_ONLY_HANDLERS = frozenset({"platform.settings", "integration.notify", "integration.itsm", "rescue.overview",
                                   "platform.users", "protection.platform", "protection.automation",
                                   "platform.ai_config", "platform.identity", "platform.scheduler",
                                   "snapshot.agents", "snapshot.rules", "observability.sources"})
EFFECT_HANDLERS = {
    "inspection.run.execute": "inspection.run_frozen",
    "diagnosis.root_cause.execute": "diagnosis.root_cause",
    # C14: approval-gated effect. Local to the platform database and verified by its own row.
    "monitor.capacity.execute": "monitor.capacity_collect",
    # C14: one metric collection whose evidence is the DBPerfMetric rows it just wrote.
    "performance.baseline.collect": "performance.baseline_collect",
    "performance.capacity.collect": "monitor.capacity_collect",
    "performance.table_profile.collect": "performance.table_profile_collect",
    "performance.index.collect": "performance.index_collect",
    "performance.workload.collect": "performance.workload_collect",
    "performance.sql_profile.collect": "performance.sql_profile_collect",
    "governance.candidate.scan": "governance.candidate_scan",
    "operations.startup.plan": "operations.startup_plan",
    "operations.shutdown.plan": "operations.shutdown_plan",
    "protection.backup.plan": "protection.backup_plan",
    "protection.restore.plan": "protection.restore_plan",
    "monitor.observability.bind": "observability.bind",
    "integration.prometheus.sync": "integration.prometheus_sync",
    "integration.zabbix.sync": "integration.zabbix_sync",
    "monitor.alert.sync": "monitor.alert_import",
    "operations.switchover.plan": "operations.switchover_plan",
    "report.document.generate": "report.document_generate",
}
UNWIRED_ACTIONS = {
    "inspection.report.read": ("inspection.read", "report.read"),
    "operations.termination.execute": ("operations.execute",),
    "protection.backup.execute": ("backup.execute",),
    "protection.restore.execute": ("backup.execute",),
}


class CapabilityError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def definition_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical(entry).encode()).hexdigest()


def binding_actions(entry: dict, *, engine: str | None = None) -> tuple[str, ...]:
    actions = set()
    for binding in entry.get("bindings", ()):
        if engine and binding.get("engine") not in (None, engine):
            continue
        endpoints = binding.get("endpoints") or (binding,)
        for endpoint in endpoints:
            path = endpoint.get("endpoint_template")
            if not path:
                raise CapabilityError("permission_unmapped", entry["capability_id"])
            path = re.sub(r"\{[^{}]+\}", "1", path)
            full_path = path if path.startswith("/api/v1/") else "/api/v1" + path
            action = access_control.action_for_request(endpoint.get("method", "GET"), full_path)
            # This endpoint authenticates the principal itself and exposes no other account.
            if full_path == "/api/v1/auth/current-user" and endpoint.get("method", "GET") == "GET":
                continue
            if action is None:
                raise CapabilityError("permission_unmapped", entry["capability_id"])
            actions.add(action)
    if not actions and entry.get("handler_status") == "declared_not_wired":
        actions.update(UNWIRED_ACTIONS.get(entry["capability_id"], ()))
    return tuple(sorted(actions))


def validate_registry() -> tuple[dict, ...]:
    entries, seen, wired = [], set(), set()
    for source in CAPABILITY_REGISTRY:
        entry = deepcopy(source)
        key = entry.get("capability_id", "")
        if not ID_PATTERN.fullmatch(key) or len(key) > 64 or key in seen:
            raise CapabilityError("invalid_capability_id", str(key))
        seen.add(key)
        if (type(entry.get("version")) is not int or entry["version"] < 1
                or entry.get("kind") not in KINDS
                or entry.get("execution") not in {"server", "client"}):
            raise CapabilityError("invalid_capability_definition", key)
        handler = entry.get("handler")
        if entry.get("execution") == "server":
            if entry.get("handler_status") != "wired" or handler not in HANDLERS or handler not in PARAM_SCHEMAS:
                raise CapabilityError("handler_not_wired", key)
            if (entry["kind"] not in {"read", "query"}
                    and EFFECT_HANDLERS.get(key) != handler):
                raise CapabilityError("server_side_effect_not_approved", key)
            wired.add(handler)
        elif handler is not None:
            raise CapabilityError("client_handler_must_not_be_executable", key)
        entry["required_actions"] = binding_actions(entry)
        # A declaration without a real endpoint is inspectable but never callable.
        if not entry["required_actions"] and entry.get("handler_status") != "declared_not_wired":
            raise CapabilityError("permission_unmapped", key)
        entries.append(entry)
    if wired != set(HANDLERS) or set(HANDLERS) != set(PARAM_SCHEMAS):
        raise CapabilityError("orphan_handler")
    return tuple(entries)


def _entry(capability_id: str) -> dict:
    for entry in validate_registry():
        if entry["capability_id"] == capability_id:
            return entry
    raise CapabilityError("unregistered_capability")


def _derived_category(entry: dict) -> str | None:
    if entry["kind"] != "execute":
        return None
    return operations_capabilities.DERIVED_MATURITY.get(entry.get("catalog_key"))


def _maturity(entry: dict) -> dict:
    category = _derived_category(entry)
    if category:
        return dict(operations_capabilities.protection_maturity(category))
    if entry["capability_id"] == "operations.termination.execute":
        return {engine: "unsupported" for engine in operations_capabilities.ENGINE_ORDER}
    return dict(entry.get("declared_maturity") or {
        engine: "ready" if entry.get("handler_status") == "wired" else "partial"
        for engine in entry.get("engines", ())
    })


def effective_maturity(capability_id: str) -> dict:
    return _maturity(_entry(capability_id))


def _principal(db, actor: dict):
    user_id = actor.get("user_id") if isinstance(actor, dict) else None
    if type(user_id) is not int or user_id <= 0:
        raise CapabilityError("authentication_required")
    user = db.query(User).filter(User.id == user_id).populate_existing().first()
    if (user is None or user.status != "active"
            or (user.locked_until and user.locked_until > datetime.now())):
        raise CapabilityError("actor_inactive")
    grants = {"*"} if user.role == "admin" else access_control.effective_action_keys(db, user.id)[1]
    scope = None if user.role == "admin" else access_control.resolve_scope(db, user.id)
    return user, grants, scope


def _authorized(entry: dict, grants: set, *, engine: str | None = None) -> bool:
    actions = binding_actions(entry, engine=engine)
    return bool(actions) and all(
        access_control.action_matches(grants, action) for action in actions
    )


def _ledger(db) -> dict:
    try:
        return {row.capability_id: row for row in db.query(DBCapabilityDefinition).populate_existing().all()}
    except SQLAlchemyError as exc:
        raise CapabilityError("registry_storage_unavailable") from exc


def _view(entry: dict, row, *, engine: str | None = None) -> dict:
    value = deepcopy(entry)
    reason = None
    if row is None:
        reason = "definition_not_synced"
    elif row.enabled != 1:
        reason = "capability_disabled"
    elif row.definition_version != entry["version"] or row.definition_hash != definition_hash(entry):
        reason = "definition_out_of_sync"
    elif engine and engine not in entry.get("engines", ()):
        reason = "engine_not_supported"
    elif entry.get("release_gate") == "inspection_plan" and not settings.PLAN_INSPECTION_EXECUTION_ENABLED:
        reason = "capability_not_released"
    elif entry.get("handler_status") == "declared_not_wired":
        reason = "handler_not_wired"
    value.update(
        definition_version=entry["version"], definition_hash=definition_hash(entry),
        enabled=bool(row.enabled) if row else False,
        availability="blocked" if reason else "available",
        unavailable_reason=reason,
        plan_executable=not reason and entry["execution"] == "server" and entry.get("handler") in HANDLERS,
        # The typed input contract a manual form renders. Not a permission: every submitted
        # value is still validated by PARAM_SCHEMAS before the handler sees it.
        params_schema=PARAM_SCHEMA_JSON.get(entry.get("handler")),
        effective_maturity=_maturity(entry),
    )
    return value


def _log_conflicts(conflicts: list[dict]) -> None:
    safe = []
    for conflict in conflicts[:20]:
        item = {"conflict_kind": conflict["conflict_kind"]}
        key = conflict["capability_id"]
        item["capability_id"] = key if isinstance(key, str) and len(key) <= 64 and ID_PATTERN.fullmatch(key) else "<invalid>"
        for field, allowed in (
            ("engine", {*operations_capabilities.ENGINE_ORDER, "host"}),
            ("declared", {"ready", "partial", "planned", "blocked", "unsupported"}),
            ("derived", {"ready", "partial", "planned", "blocked", "unsupported"}),
            ("source", {"code", "database"}),
        ):
            if field in conflict:
                value = conflict[field]
                item[field] = value if isinstance(value, str) and value in allowed else "<invalid>"
        safe.append(item)
    logger.warning("capability_registry_conflicts count=%d details=%s", len(conflicts),
                   _canonical(safe), extra={"capability_conflicts": safe})


def detect_conflicts(db) -> list[dict]:
    entries = validate_registry()
    rows = _ledger(db)
    keys = {entry["capability_id"] for entry in entries}
    conflicts = [{"capability_id": key, "conflict_kind": "db_row_without_declaration"}
                 for key in sorted(rows.keys() - keys)]
    for entry in entries:
        row = rows.get(entry["capability_id"])
        if row is None:
            conflicts.append({"capability_id": entry["capability_id"], "conflict_kind": "definition_not_synced"})
            continue
        if row.definition_hash != definition_hash(entry) or row.definition_version != entry["version"]:
            conflicts.append({"capability_id": entry["capability_id"], "conflict_kind": "definition_out_of_sync"})
        if _derived_category(entry):
            derived = _maturity(entry)
            for origin, claimed in (("code", entry.get("declared_maturity")), ("database", row.declared_maturity_json)):
                if claimed is not None and not isinstance(claimed, dict):
                    conflicts.append({"capability_id": entry["capability_id"], "source": origin,
                                      "conflict_kind": "invalid_maturity_claim"})
                    continue
                for engine, maturity in (claimed or {}).items():
                    if derived.get(engine) != maturity:
                        conflicts.append({"capability_id": entry["capability_id"], "engine": engine,
                                          "declared": maturity, "derived": derived.get(engine),
                                          "source": origin, "conflict_kind": "maturity_conflict"})
    if conflicts:
        _log_conflicts(conflicts)
    return conflicts


def effective_capability(db, capability_id: str, *, definition_version: int | None = None,
                         engine: str | None = None) -> dict:
    entry = _entry(capability_id)
    if definition_version is not None and (type(definition_version) is not int or definition_version != entry["version"]):
        raise CapabilityError("stale_definition_version")
    return _view(entry, _ledger(db).get(capability_id), engine=engine)


def list_capabilities(db, *, actor: dict, engine: str | None = None) -> dict:
    _, grants, scope = _principal(db, actor)
    if engine and engine not in {*operations_capabilities.ENGINE_ORDER, "host"}:
        raise CapabilityError("engine_not_supported")
    rows = _ledger(db)
    items = []
    visible_ids = set()
    for entry in validate_registry():
        if engine and engine not in entry.get("engines", ()):
            continue
        authorized_engines = [candidate for candidate in entry.get("engines", ())
                              if (not engine or candidate == engine)
                              and _authorized(entry, grants, engine=candidate)]
        if not authorized_engines:
            continue
        value = _view(entry, rows.get(entry["capability_id"]), engine=engine)
        value["authorized_engines"] = authorized_engines
        value["required_actions_by_engine"] = {
            candidate: binding_actions(entry, engine=candidate) for candidate in authorized_engines
        }
        if engine:
            value["required_actions"] = binding_actions(entry, engine=engine)
        from app.services import engine_execution
        engine_scoped = (engine_execution.bridge() is not None
                         and entry["capability_id"] in engine_execution.READ_OPERATIONS)
        if scope and scope.restricted and entry.get("handler") not in (INSTANCE_FILTERED_HANDLERS | SCOPE_FILTERED_HANDLERS) and not engine_scoped:
            value.update(availability="blocked", unavailable_reason="scope_not_supported", plan_executable=False)
        elif scope and scope.restricted:
            value["requires_instance"] = True
        items.append(value)
        visible_ids.add(entry["capability_id"])
    conflicts = [item for item in detect_conflicts(db) if item["capability_id"] in visible_ids or "*" in grants]
    return {"schema": "capability.registry/v1", "items": items, "total": len(items), "conflicts": conflicts}


def sync_definitions(db, *, actor: dict) -> dict:
    user, grants, scope = _principal(db, actor)
    if not access_control.action_matches(grants, "platform.settings"):
        raise CapabilityError("permission_denied")
    if scope and scope.restricted:
        raise CapabilityError("scope_denied")
    entries = validate_registry()
    for entry in entries:
        if _derived_category(entry) and entry.get("declared_maturity") is not None:
            raise CapabilityError("derived_maturity_is_not_declarable")
    rows, now = _ledger(db), datetime.now(UTC).replace(tzinfo=None)
    for entry in entries:
        row = rows.get(entry["capability_id"])
        if row and entry["version"] < row.definition_version:
            raise CapabilityError("definition_version_regression")
        if (row and entry["version"] == row.definition_version
                and row.definition_hash != definition_hash(entry)):
            raise CapabilityError("definition_version_not_incremented")
    created = updated = 0
    for entry in entries:
        row = rows.get(entry["capability_id"])
        if row is None:
            row = DBCapabilityDefinition(capability_id=entry["capability_id"], enabled=1)
            db.add(row)
            created += 1
        else:
            updated += 1
        row.definition_version = entry["version"]
        row.definition_hash = definition_hash(entry)
        row.domain, row.kind, row.execution = entry["domain"], entry["kind"], entry["execution"]
        row.catalog_key = entry.get("catalog_key")
        row.declared_maturity_json = None if _derived_category(entry) else entry.get("declared_maturity")
        row.synced_at = now
    access_control.append_security_audit(
        db, category="capability", action="sync_definitions", actor=user.username,
        target_type="capability_registry", target_id="code",
        details={"created": created, "updated": updated, "unknown_rows": sorted(rows.keys() - {
            item["capability_id"] for item in entries
        })},
    )
    db.commit()
    return {"created": created, "updated": updated, "conflicts": detect_conflicts(db)}


# Startup has no request context and no user consented to this write, so the audit
# actor names the process itself. It is not a delegated human identity and it grants
# nothing: every later read still resolves its own principal.
STARTUP_SYNC_ACTOR = "system:startup-sync"
STARTUP_SYNC_NOTE = "启动期由进程自身同步代码声明，不代表任何自然人授权"


def _startup_skip_reason(entry: dict, row) -> str | None:
    if _derived_category(entry) and entry.get("declared_maturity") is not None:
        return "derived_maturity_is_not_declarable"
    if row is None:
        return None
    if entry["version"] < row.definition_version:
        return "definition_version_regression"
    if row.definition_version == entry["version"] and row.definition_hash != definition_hash(entry):
        return "definition_version_not_incremented"
    return None


def sync_declarations_at_startup(db) -> dict:
    """Best-effort ledger sync owned by the process, not by a request actor.

    The ledger is optional infrastructure, so nothing here may take the service
    down: a declaration that conflicts with a newer database (a rolled-back
    deployment) is skipped with a structured warning instead of raised. Rows that
    already match their declaration are not rewritten, so a second boot reports
    created=0/updated=0 and leaves ``enabled`` exactly as an operator set it.
    """
    result = {"outcome": "synced", "created": 0, "updated": 0, "unchanged": 0, "skipped": []}
    try:
        entries = validate_registry()
        rows = _ledger(db)
    except CapabilityError as exc:
        result["outcome"] = exc.code
        logger.warning("capability_registry_startup_sync_aborted code=%s", exc.code,
                       extra={"capability_sync": dict(result)})
        return result
    now = datetime.now(UTC).replace(tzinfo=None)
    for entry in entries:
        row = rows.get(entry["capability_id"])
        reason = _startup_skip_reason(entry, row)
        if reason:
            result["skipped"].append({"capability_id": entry["capability_id"], "reason": reason})
            continue
        if (row is not None and row.definition_version == entry["version"]
                and row.definition_hash == definition_hash(entry)):
            result["unchanged"] += 1
            continue
        if row is None:
            row = DBCapabilityDefinition(capability_id=entry["capability_id"], enabled=1)
            db.add(row)
            result["created"] += 1
        else:
            result["updated"] += 1
        row.definition_version = entry["version"]
        row.definition_hash = definition_hash(entry)
        row.domain, row.kind, row.execution = entry["domain"], entry["kind"], entry["execution"]
        row.catalog_key = entry.get("catalog_key")
        row.declared_maturity_json = None if _derived_category(entry) else entry.get("declared_maturity")
        row.synced_at = now
    if result["skipped"]:
        logger.warning("capability_registry_startup_sync_skipped count=%d details=%s",
                       len(result["skipped"]), _canonical(result["skipped"][:20]),
                       extra={"capability_sync": dict(result)})
    try:
        access_control.append_security_audit(
            db, category="capability", action="sync_definitions", actor=STARTUP_SYNC_ACTOR,
            target_type="capability_registry", target_id="code",
            details={**result, "note": STARTUP_SYNC_NOTE,
                     "unknown_rows": sorted(rows.keys() - {item["capability_id"] for item in entries})},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("capability_registry_startup_sync_storage_error error=%s", exc.__class__.__name__,
                       extra={"capability_sync": {**result, "outcome": "registry_storage_unavailable"}})
        return {**result, "outcome": "registry_storage_unavailable"}
    logger.info("capability_registry_startup_sync created=%d updated=%d unchanged=%d skipped=%d",
                result["created"], result["updated"], result["unchanged"], len(result["skipped"]))
    return result


def set_enabled(db, capability_id: str, *, enabled: bool, actor: dict) -> dict:
    user, grants, scope = _principal(db, actor)
    if not access_control.action_matches(grants, "platform.settings"):
        raise CapabilityError("permission_denied")
    if scope and scope.restricted:
        raise CapabilityError("scope_denied")
    _entry(capability_id)
    row = _ledger(db).get(capability_id)
    if row is None:
        raise CapabilityError("definition_not_synced")
    if type(enabled) is not bool:
        raise CapabilityError("invalid_enabled")
    row.enabled = int(enabled)
    access_control.append_security_audit(
        db, category="capability", action="set_enabled", actor=user.username,
        target_type="capability_definition", target_id=capability_id, details={"enabled": enabled},
    )
    db.commit()
    return effective_capability(db, capability_id)


def validate_params(value, *, depth: int = 0) -> None:
    if depth > 8:
        raise CapabilityError("plan_expression_rejected")
    if isinstance(value, dict):
        if len(value) > 100:
            raise CapabilityError("invalid_params")
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in FORBIDDEN_KEYS:
                raise CapabilityError("request_supplied_handler_rejected" if str(key).lower() in {
                    "handler", "module", "path", "function", "handler_ref",
                } else "plan_raw_payload_rejected")
            validate_params(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 200:
            raise CapabilityError("invalid_params")
        for item in value:
            validate_params(item, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 16384:
        raise CapabilityError("invalid_params")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise CapabilityError("invalid_params")
    try:
        _canonical(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapabilityError("invalid_params") from exc


def resolve_handler(db, capability_id: str, definition_version: int, *, actor: dict,
                    instance_id: int | None = None, params: dict | None = None, _allow_effects=False,
                    _allow_engine_bridge=False):
    user, grants, scope = _principal(db, actor)
    view = effective_capability(db, capability_id, definition_version=definition_version)
    if not _authorized(view, grants):
        raise CapabilityError("permission_denied")
    if view["unavailable_reason"]:
        raise CapabilityError(view["unavailable_reason"])
    if view["execution"] != "server" or not view["plan_executable"]:
        raise CapabilityError("handler_not_wired")
    if view["kind"] not in {"read", "query"}:
        if not _allow_effects or EFFECT_HANDLERS.get(capability_id) != view["handler"]:
            raise CapabilityError("explicit_plan_required")
        if user.role != "admin":
            scope = access_control.resolve_scope(db, user.id, minimum_access="operate")
    instance = None
    if instance_id is not None:
        if type(instance_id) is not int or instance_id <= 0:
            raise CapabilityError("invalid_target")
        if scope and scope.restricted and instance_id not in scope.native_instance_ids:
            raise CapabilityError("scope_denied")
        instance = db.query(DBInstance).filter(DBInstance.id == instance_id).populate_existing().first()
        if instance is None or not instance.enabled:
            raise CapabilityError("target_unavailable")
        if instance.db_type not in view.get("engines", ()):
            raise CapabilityError("engine_not_supported")
    if view.get("requires_instance") and instance is None:
        raise CapabilityError("missing_instance")
    if scope and scope.restricted:
        from app.services import engine_execution
        bridge_scope = (_allow_engine_bridge and engine_execution.bridge() is not None
                        and capability_id in engine_execution.READ_OPERATIONS and instance is not None)
        handler_ok = (view["handler"] in INSTANCE_FILTERED_HANDLERS and instance is not None) \
            or view["handler"] in SCOPE_FILTERED_HANDLERS or bridge_scope
        if not handler_ok:
            raise CapabilityError("scope_denied")
    values = {} if params is None else params
    if not isinstance(values, dict):
        raise CapabilityError("invalid_params")
    validate_params(values)
    if len(_canonical(values)) > 16384:
        raise CapabilityError("invalid_params")
    try:
        normalized = PARAM_SCHEMAS[view["handler"]].model_validate(values).model_dump()
    except (ValidationError, ValueError) as exc:
        raise CapabilityError("invalid_params") from exc
    ctx = CapabilityContext(
        db=db, actor_id=user.id, params=normalized, instance=instance,
        allowed_instance_ids=scope.native_instance_ids if scope and scope.restricted else None,
        can_execute_operations=access_control.action_matches(grants, "operations.execute"),
    )
    return HANDLERS[view["handler"]], ctx


def execute_read(db, capability_id: str, definition_version: int, *, actor: dict,
                 instance_id: int | None = None, params: dict | None = None) -> dict:
    try:
        handler, context = resolve_handler(db, capability_id, definition_version, actor=actor,
                                           instance_id=instance_id, params=params)
        try:
            return handler(context)
        except ScopeNotSupported:
            # A reader that aggregates platform-wide is refused, never truncated to a lie.
            raise CapabilityError("scope_not_supported") from None
        except ValueError as exc:
            raise CapabilityError("invalid_params") from exc
        except Exception as exc:
            # A driver/HTTP failure is an unavailable upstream, not an empty result and not a
            # programmer bug: unknown exception types still propagate as themselves.
            origin = type(exc).__module__.split(".", 1)[0]
            if origin in {"pymysql", "psycopg", "psycopg2", "oracledb", "pyodbc", "requests",
                          "urllib3", "httpx", "httpcore", "socket"} or isinstance(exc, TimeoutError):
                raise CapabilityError("upstream_unavailable") from exc
            raise
    except CapabilityError as exc:
        # Security events must survive the caller rolling back a rejected read. Never log params.
        user_id = actor.get("user_id") if isinstance(actor, dict) else None
        event = {
            "actor_user_id": user_id if type(user_id) is int and user_id > 0 else None,
            "capability_id": capability_id if isinstance(capability_id, str)
            and len(capability_id) <= 64 and ID_PATTERN.fullmatch(capability_id) else "<invalid>",
            "definition_version": definition_version if type(definition_version) is int else None,
            "instance_id": instance_id if type(instance_id) is int else None,
            "reason": exc.code,
        }
        logger.warning("capability_read_denied actor_user_id=%s capability_id=%s version=%s instance_id=%s reason=%s",
                       event["actor_user_id"], event["capability_id"], event["definition_version"],
                       event["instance_id"], event["reason"], extra={"security_event": event})
        raise


def execute_effect(db, capability_id: str, definition_version: int, *, actor: dict,
                   instance_id: int | None, params: dict, execution_id: str, execution_guard,
                   target_binding=None, params_binding=None) -> dict:
    """Only the approved durable plan executor supplies identity and publication fencing."""
    if not callable(execution_guard) or EFFECT_HANDLERS.get(capability_id) is None:
        raise CapabilityError("durable_execution_required")
    handler, context = resolve_handler(db, capability_id, definition_version, actor=actor,
                                       instance_id=instance_id, params=params, _allow_effects=True)
    result = handler(replace(context, execution_id=execution_id, execution_guard=execution_guard))
    if not is_common_effect(capability_id, definition_version):
        return result
    if not isinstance(result, dict):
        raise CapabilityError("result_unknown")
    data = deepcopy(result)
    outcome = data.get("outcome")
    if outcome == "succeeded":
        outcome = "success"
        data["outcome"] = outcome
    if outcome not in {"success", "partial", "failed", "unknown"}:
        raise CapabilityError("result_unknown")
    execution_guard()
    envelope = {
        "schema": EFFECT_RESULT_SCHEMA,
        "capability_id": capability_id,
        "definition_version": EFFECT_RESULT_VERSION,
        "instance_id": instance_id,
        "execution_id": execution_id,
        "outcome": outcome,
        "data": data,
    }
    binding = {
        "execution_id": execution_id,
        "capability_id": capability_id,
        "definition_version": definition_version,
        "requested_by": context.actor_id,
        "instance_id": instance_id,
        "target_hash": effect_digest(target_binding),
        "params_hash": effect_digest(params if params_binding is None else params_binding),
        "result_hash": effect_digest(data),
    }
    previous = db.query(DBCapabilityEffectReceipt).filter_by(execution_id=execution_id).first()
    if previous is not None:
        if any(getattr(previous, key) != value for key, value in binding.items()):
            raise CapabilityError("result_unknown")
    else:
        db.add(DBCapabilityEffectReceipt(**binding, created_at=datetime.now(UTC).replace(tzinfo=None)))
        db.flush()
    db.commit()
    return envelope


def durable_effect_supported(capability_id: str, definition_version: int,
                             output_schema: str | None = None) -> bool:
    """Closed admission predicate shared by HTTP delivery and the worker contract."""
    if output_schema is not None:
        return is_durable_definition({
            "capability_id": capability_id,
            "version": definition_version,
            "output_schema": output_schema,
        })
    if is_common_effect(capability_id, definition_version):
        return True
    expected = {
        "diagnosis.root_cause.execute": 1,
        "report.document.generate": 3,
        "inspection.run.execute": 2,
    }
    return expected.get(capability_id) == definition_version
