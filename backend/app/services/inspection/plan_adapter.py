"""Plan-only bridge to the existing inspection queue, collector and read model."""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import CodeType, SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import event

from app.collectors.oracle import oracle_inspection_profile
from app.core.database_options import redact_database_error
from app.models.db_instance import DBInstance
from app.models.inspection_policy import DBInspectionPolicy
from app.models.report_center import ReportArtifact
from app.models.user import User
from app.services.inspection import custom_metric_service, jobs, result_writer, run_readback, runner, template_profiles, template_runtime

SCHEMA = "inspection.plan_result/v1"
TRIGGER = "capability_plan"
BINDING_STAGE = "plan_bound"
ENGINES = frozenset({"oracle", "mysql", "postgresql", "sqlserver"})
_HASH = r"^[0-9a-f]{64}$"


class InspectionPlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    policy_id: int = Field(gt=0)
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    policy_revision: str | None = Field(default=None, pattern=_HASH)
    runtime_digest: str | None = Field(default=None, pattern=_HASH)
    target_fingerprint: str | None = Field(default=None, pattern=_HASH)


class InspectionPlanError(jobs.JobError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code, code=code)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _params(params) -> InspectionPlanParams:
    if isinstance(params, InspectionPlanParams):
        params = params.model_dump()
    try:
        return InspectionPlanParams.model_validate(params)
    except ValidationError as exc:
        raise InspectionPlanError("inspection_params_invalid") from exc


def _constant(value):
    """Canonicalize SQL maps/sets without serializing module runtime objects."""
    if value is None or type(value) in (str, bool, int, float):
        return value
    if type(value) in (list, tuple):
        return [_constant(item) for item in value]
    if type(value) in (set, frozenset):
        return sorted((_constant(item) for item in value), key=repr)
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _constant(item) for key, item in value.items()}
    raise TypeError("not a SQL constant")


def _module_binding(name: str) -> dict:
    module = importlib.import_module(name)
    code, constants = {}, {}
    for key, value in vars(module).items():
        if inspect.isfunction(value) and value.__module__ == name:
            code[key] = _code_digest(value.__code__)
        elif inspect.isclass(value) and value.__module__ == name:
            for member, method in vars(value).items():
                if isinstance(method, (staticmethod, classmethod)):
                    method = method.__func__
                if inspect.isfunction(method):
                    code[f"{key}.{member}"] = _code_digest(method.__code__)
        elif key.isupper():
            try:
                constants[key] = _constant(value)
            except TypeError:
                pass
    return {
        "source_sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
        "loaded_code": code, "loaded_constants": constants,
    }


def _code_digest(code: CodeType) -> str:
    # marshal's string-interning flags can change on first execution, without a code edit.
    def constant(value):
        if isinstance(value, CodeType):
            return {"code": _code_digest(value)}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if value is Ellipsis:
            return {"ellipsis": True}
        if isinstance(value, complex):
            return {"complex": [value.real, value.imag]}
        if isinstance(value, (tuple, frozenset)):
            values = [constant(item) for item in value]
            return sorted(values, key=repr) if isinstance(value, frozenset) else values
        return _constant(value)

    fields = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_flags",
              "co_names", "co_varnames", "co_freevars", "co_cellvars")
    return _digest({
        **{key: getattr(code, key) for key in fields},
        "bytecode": code.co_code.hex(), "constants": [constant(value) for value in code.co_consts],
        "exceptions": getattr(code, "co_exceptiontable", b"").hex(),
    })


def _runtime_digest(engine: str, policy) -> str:
    common = (
        "app.services.inspection.runner", "app.services.inspection.execution_policy",
        "app.services.inspection.template_runtime", "app.services.inspection.template_profiles",
        "app.services.inspection.custom_metric_service",
    )
    modules = {
        "oracle": (
            "app.collectors.oracle.oracle_inspection_profile",
            "app.collectors.oracle.oracle_health_collector",
            "app.services.inspection.oracle_script_checks", "app.analyzers.oracle_rule_engine",
        ),
        "mysql": (
            "app.services.inspection.script_db_checks",
            "app.collectors.mysql.mysql_health_collector",
        ),
        "postgresql": (
            "app.services.inspection.script_db_checks",
            "app.services.inspection.collectors.postgresql_inspection_collector",
        ),
        "sqlserver": (
            "app.services.generic_db_ops",
            "app.services.inspection.collectors.sqlserver_inspection_collector",
        ),
    }[engine]
    config = template_profiles.normalize_profile_config(policy.db_types_json, policy.report_config_json)
    if engine == "oracle":
        profile = config.get("oracle_profile", "auto")
        candidates = oracle_inspection_profile.SCRIPT_CATALOG if profile == "auto" else {
            profile: oracle_inspection_profile.SCRIPT_CATALOG[profile],
        }
        scripts = {key: item["script_name"] for key, item in candidates.items()}
        dispatch = {"mode": "version_cdb_dispatch", "expected_profile": profile}
    else:
        script = {"mysql": "mysql_check.sh", "postgresql": "pg_check.sh", "sqlserver": "mssql_check.sh"}[engine]
        scripts, dispatch = {engine: script}, {"mode": "fixed_collector"}
    try:
        script_bindings = {}
        for key, script in scripts.items():
            content = (oracle_inspection_profile.SCRIPT_DIR / script).read_bytes()
            if not content:
                raise OSError("empty script")
            script_bindings[key] = {"name": script, "sha256": hashlib.sha256(content).hexdigest()}
        binding = {name: _module_binding(name) for name in (*common, *modules)}
    except OSError as exc:
        raise InspectionPlanError("inspection_runtime_unavailable") from exc
    return _digest({
        "engine": engine, "dispatch": dispatch, "scripts": script_bindings, "modules": binding,
    })


def _current(db, instance, values):
    instance_id = getattr(instance, "id", None)
    with db.no_autoflush:
        target = db.query(DBInstance).filter_by(id=instance_id).populate_existing().first()
        policy = db.query(DBInspectionPolicy).filter_by(id=values.policy_id).populate_existing().first()
        if target is None or not target.enabled:
            raise InspectionPlanError("instance_disabled")
        if target.db_type not in ENGINES:
            raise InspectionPlanError("inspection_engine_unsupported")
        if policy is None or not policy.enabled:
            raise InspectionPlanError("template_missing")
        if target.db_type not in (policy.db_types_json or []):
            raise InspectionPlanError("inspection_policy_not_applicable")
        snapshot = jobs.freeze_policy(policy)
        try:
            custom_metric_service.normalize_item_configs(policy.item_config_json)
            custom_metric_service.validate_metric_db_types(policy.item_config_json, policy.db_types_json)
            runtime = _runtime_digest(target.db_type, policy)
            template_runtime.require_template_runtime(
                policy.db_types_json, policy.item_config_json, policy.report_config_json,
            )
        except (template_runtime.TemplateRuntimeError, custom_metric_service.CustomMetricError) as exc:
            raise InspectionPlanError("inspection_template_not_executable") from exc
        return target, snapshot, {
            "policy_revision": jobs._digest(snapshot),
            "runtime_digest": runtime, "target_fingerprint": jobs.target_fingerprint(target),
        }


def prepare_params(db, instance, params) -> dict:
    """Freeze server-controlled hashes; no target I/O, implicit policy or seeding."""
    values = _params(params)
    if any(getattr(values, key) is not None for key in (
        "policy_revision", "runtime_digest", "target_fingerprint",
    )):
        raise InspectionPlanError("inspection_frozen_fields_forbidden")
    _target, _snapshot, binding = _current(db, instance, values)
    return {**values.model_dump(), **binding}


def _validated(db, instance, params):
    values = _params(params)
    keys = ("policy_revision", "runtime_digest", "target_fingerprint")
    if any(getattr(values, key) is None for key in keys):
        raise InspectionPlanError("inspection_params_not_frozen")
    target, snapshot, binding = _current(db, instance, values)
    for key, code in (
        ("target_fingerprint", "target_changed"),
        ("policy_revision", "inspection_policy_changed"),
        ("runtime_digest", "inspection_runtime_changed"),
    ):
        if getattr(values, key) != binding[key]:
            raise InspectionPlanError(code)
    return values, target, snapshot


def validate_params(db, instance, params) -> dict:
    """Recheck without replacing approved hashes or writing platform records."""
    values, _target, _snapshot = _validated(db, instance, params)
    return values.model_dump()


def _guard(ctx):
    callback = getattr(ctx, "execution_guard", None)
    if callback is not None:
        try:
            signature = inspect.signature(callback)
            try:
                signature.bind(ctx.db)
            except TypeError:
                signature.bind()
                accepted = callback()
            else:
                accepted = callback(ctx.db)
            if accepted is False:
                raise InspectionPlanError("inspection_execution_guard_rejected")
        except Exception as exc:
            raise InspectionPlanError("inspection_execution_guard_rejected") from exc


def _authorize(db, row):
    # Refresh the principal as well as policy/target: db.get alone can return a cached grant.
    with db.no_autoflush:
        db.query(User).filter_by(id=row.requested_by).populate_existing().first()
        jobs.validate_job_target(db, row)


def _binding_matches(row, binding):
    events = [event for event in (row.progress_json or []) if event.get("stage") == BINDING_STAGE]
    return bool(events) and events[-1].get("detail") == binding


@contextmanager
def _collection_writes(db):
    state = {"pending": False}

    def flushed(session, context):
        state["pending"] = state["pending"] or bool(session.new or session.dirty or session.deleted)

    def committed(session):
        if not session.in_nested_transaction():
            state["pending"] = False

    event.listen(db, "after_flush", flushed)
    event.listen(db, "after_commit", committed)
    try:
        yield state
    finally:
        event.remove(db, "after_flush", flushed)
        event.remove(db, "after_commit", committed)


def _result(db, row) -> dict:
    db.refresh(row)
    # Use the shared list/read model, never materialize findings or Markdown into a plan.
    summary = run_readback.run_summary(
        db, row, result_counts=result_writer.count_by_task_ids(db, [row.id]),
    )
    fields = (
        "job_id", "instance_id", "db_type", "status", "stage", "attempt", "trigger_type",
        "policy_revision", "timeout_seconds", "created_at", "started_at", "finished_at",
        "terminal", "outcome", "outcome_kind", "requires_attention", "results_published",
        "cancel_requested", "can_retry", "result_count",
    )
    run = {key: summary[key][:128] if isinstance(summary[key], str) else summary[key] for key in fields}
    artifact = db.query(ReportArtifact.id, ReportArtifact.status, ReportArtifact.archive_state).filter(
        ReportArtifact.source_key == f"{run_readback.REPORT_SOURCE_PREFIX}{row.id}",
    ).first()
    artifacts = [] if artifact is None else [{
        "id": artifact.id, "status": str(artifact.status)[:30],
        "archive_state": str(artifact.archive_state)[:30],
    }]
    return {
        "schema": SCHEMA, "job_id": row.id, "instance_id": row.instance_id,
        "status": run["status"], "outcome": run["outcome"],
        "error_code": (jobs.failure_code(row) or "")[:128] or None, "run": run,
        "report": {"available": bool(artifacts), "state": "synced" if artifacts else "not_synced",
                   "artifacts": artifacts},
    }


def execute(ctx) -> dict:
    """Only the plan runner may supply execution identity and its ownership guard."""
    execution_id = getattr(ctx, "execution_id", None)
    if not isinstance(execution_id, str) or not re.fullmatch(r"[^:\s]{1,128}:[1-9][0-9]*:[1-9][0-9]*", execution_id):
        raise InspectionPlanError("inspection_execution_id_required")
    if type(ctx.actor_id) is not int or ctx.actor_id <= 0:
        raise InspectionPlanError("actor_inactive")
    values, target, snapshot = _validated(ctx.db, ctx.instance, ctx.params)
    _guard(ctx)
    binding = _digest(values.model_dump())
    approved = values.model_dump()
    try:
        row = jobs.enqueue(
            ctx.db, target, target.db_type, SimpleNamespace(**snapshot),
            requested_by=ctx.actor_id, trigger_type=TRIGGER, idempotency_key=execution_id,
            timeout_seconds=values.timeout_seconds,
        )
    except jobs.JobError as exc:
        raise InspectionPlanError(exc.code or "inspection_enqueue_refused") from exc
    _authorize(ctx.db, row)
    if row.status != "queued":
        if not _binding_matches(row, binding):
            raise InspectionPlanError("inspection_idempotency_conflict")
        return _result(ctx.db, row)
    _guard(ctx)
    token = uuid.uuid4().hex
    if jobs.claim(ctx.db, row.id, token) is None:
        return _result(ctx.db, row)
    with jobs.lease_guard(row.id, token):
        try:
            if not jobs.heartbeat(ctx.db, row.id, token, stage=BINDING_STAGE, detail=binding):
                raise InspectionPlanError("inspection_job_ownership_lost")
            _values, target, _snapshot = _validated(ctx.db, ctx.instance, approved)
            _authorize(ctx.db, row)
            _guard(ctx)
            ctx.db.commit()  # Release the pre-I/O ownership lock; publication gets a fresh fence.
            with _collection_writes(ctx.db) as writes:
                _target, findings = runner.prepare_findings(
                    ctx.db, target, policy=jobs.frozen_policy(row), frozen=True,
                )
            pending = writes["pending"] or bool(ctx.db.new or ctx.db.dirty or ctx.db.deleted)
            ctx.db.rollback()
            ctx.db.refresh(row)
            if pending:
                raise InspectionPlanError("inspection_collector_pending_writes")
            _validated(ctx.db, ctx.instance, approved)
            _authorize(ctx.db, row)
            jobs.commit_results(ctx.db, row.id, token, findings, execution_guard=lambda db: _guard(ctx),
                                sync_report_artifact=True)
        except Exception as exc:
            ctx.db.rollback()
            if isinstance(exc, InspectionPlanError) and exc.code == "inspection_execution_guard_rejected":
                raise
            jobs.finish_error(
                ctx.db, row.id, token, "failed", redact_database_error(str(exc)),
                code=getattr(exc, "code", None) or "inspection_execution_failed",
                execution_guard=lambda db: _guard(ctx),
            )
    return _result(ctx.db, row)
