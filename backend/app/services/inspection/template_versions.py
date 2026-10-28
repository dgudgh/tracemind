from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from functools import wraps

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.inspection_policy import DBInspectionPolicy
from app.models.inspection_template_version import InspectionTemplateVersion


_CONTEXT_KEY = "inspection_template_version_context"
_SECRET_KEY = re.compile(r"password|secret|token|authorization|private.?key|cookie", re.I)
_SCRIPT_KEY = re.compile(r"script|command|executable|shell", re.I)
_RUNTIME_KEYS = {"last_run_at", "last_status", "last_message", "next_run_time"}
_REDACTED = "***"


class TemplateVersionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def atomic_mutation(function):
    """Keep policy and history writes in the service's existing commit boundary."""
    @wraps(function)
    def wrapped(db, *args, **kwargs):
        try:
            return function(db, *args, **kwargs)
        except Exception:
            db.rollback()
            raise
    return wrapped


def snapshot_policy(policy) -> dict:
    """Detached JSON snapshot with the same attributes consumed by the runner."""
    result = {}
    for column in DBInspectionPolicy.__table__.columns:
        value = getattr(policy, column.name, None)
        if hasattr(value, "isoformat"):
            value = value.isoformat(sep=" ", timespec="seconds")
        result[column.name] = copy.deepcopy(value)
    return result


def content_revision(snapshot: dict) -> str:
    """Compatibility fingerprint for runner._policy_revision, not an edit token."""
    payload = {
        "policy_key": snapshot.get("policy_key"),
        "name": snapshot.get("name"),
        "db_types": snapshot.get("db_types_json") or [],
        "items": snapshot.get("item_config_json") or [],
        "thresholds": snapshot.get("threshold_config_json") or {},
        "actions": snapshot.get("action_config_json") or [],
        "report": snapshot.get("report_config_json") or {},
        "updated_at": snapshot.get("update_time"),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _redact(value):
    if isinstance(value, dict):
        return {key: _REDACTED if _SECRET_KEY.search(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(password|secret|token)\s*[:=]\s*[^\s,;]+", r"\1=***", value)
    return value


def policy_content(policy) -> dict:
    result = snapshot_policy(policy)
    result.pop("create_time", None)
    result.pop("update_time", None)
    result["report_config_json"] = {
        key: value for key, value in (result.get("report_config_json") or {}).items()
        if key not in _RUNTIME_KEYS
    }
    return result


def _history_snapshot(policy) -> dict:
    return _redact(policy_content(policy))


def add_policy(db: Session, policy) -> None:
    db.add(policy)
    db.flush()
    # SQLite's legacy policy table can reuse a deleted rowid. Never join a new
    # template to the retained history of a deleted template, even for the same key.
    if current_revision(db, policy.id):
        history_max = db.scalar(select(func.max(InspectionTemplateVersion.policy_id))) or 0
        live_max = db.scalar(select(func.max(DBInspectionPolicy.id))) or 0
        policy.id = max(history_max, live_max) + 1
        db.flush()


@contextmanager
def version_context(db: Session, *, actor=None, operation: str | None = None):
    previous = db.info.get(_CONTEXT_KEY)
    db.info[_CONTEXT_KEY] = {"actor": actor, "operation": operation}
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_CONTEXT_KEY, None)
        else:
            db.info[_CONTEXT_KEY] = previous


def _actor(db, actor):
    actor = actor if actor is not None else (db.info.get(_CONTEXT_KEY) or {}).get("actor")
    if actor is None:
        return None
    # Never retain the caller's entire user/session object (tokens, password hashes).
    keys = ("id", "user_id", "username", "display_name", "request_id")
    return {key: str(value)[:200] for key in keys
            if (value := actor.get(key) if isinstance(actor, dict) else getattr(actor, key, None)) is not None}


def version_history_available(db: Session) -> bool:
    """True when the version table has been migrated in this deployment.

    History is created by an additive migration, so a running installation can be
    one migration behind. Version rows must never be a precondition for editing a
    template: callers use this to degrade instead of breaking the write path.
    """
    try:
        from sqlalchemy import inspect as sa_inspect

        # Must probe on the session's own connection. Going through the engine
        # opens a second connection, and closing it rolls back whatever this
        # transaction had not committed yet - which silently discarded the very
        # write we were about to record (observed as StaleDataError at commit).
        return bool(sa_inspect(db.connection()).has_table(InspectionTemplateVersion.__tablename__))
    except Exception:  # pragma: no cover - unreachable with supported drivers
        return False


def current_revision(db: Session, policy_id: int) -> int:
    with db.no_autoflush:
        return db.scalar(select(func.max(InspectionTemplateVersion.revision)).where(
            InspectionTemplateVersion.policy_id == policy_id
        )) or 0


def lock_policy(db: Session, policy_id: int, *, expected_revision=None, observed=None):
    """A write lock also serializes SQLite, where SELECT FOR UPDATE is ignored."""
    with db.no_autoflush:
        result = db.execute(update(DBInspectionPolicy).where(DBInspectionPolicy.id == policy_id).values(
            id=DBInspectionPolicy.id, update_time=DBInspectionPolicy.update_time
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise TemplateVersionError("policy_not_found", "Inspection policy not found")
        item = db.scalars(select(DBInspectionPolicy).where(DBInspectionPolicy.id == policy_id)
                          .execution_options(populate_existing=True)).one()
        revision = current_revision(db, policy_id)
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision != revision
        ):
            raise TemplateVersionError("revision_conflict", "Inspection template revision has changed")
        if observed is not None and observed != policy_content(item):
            raise TemplateVersionError("revision_conflict", "Inspection template was changed by another writer")
        return item


def capture_version(db: Session, policy, *, operation: str, actor=None,
                    source_policy_id=None, source_revision=None) -> InspectionTemplateVersion:
    db.flush()
    snapshot = _history_snapshot(policy)
    version = InspectionTemplateVersion(
        policy_id=policy.id, policy_key=policy.policy_key,
        revision=current_revision(db, policy.id) + 1,
        content_hash=_hash(snapshot), snapshot_json=snapshot,
        operation=((db.info.get(_CONTEXT_KEY) or {}).get("operation") or operation)
        if operation == "create" else operation,
        actor_json=_actor(db, actor), source_policy_id=source_policy_id, source_revision=source_revision,
    )
    db.add(version)
    db.flush()
    return version


def capture_baseline(db: Session, policy):
    if current_revision(db, policy.id) == 0:
        capture_version(db, policy, operation="baseline")


def _row(version, *, detail=False):
    result = {key: getattr(version, key) for key in (
        "id", "policy_id", "policy_key", "revision", "content_hash", "operation",
        "source_policy_id", "source_revision",
    )}
    result["actor"] = copy.deepcopy(version.actor_json)
    result["created_at"] = version.create_time.isoformat(sep=" ", timespec="seconds")
    if detail:
        result["snapshot"] = copy.deepcopy(version.snapshot_json)
    return result


def list_versions(db: Session, policy_id: int) -> list[dict]:
    return [_row(version) for version in db.scalars(select(InspectionTemplateVersion).where(
        InspectionTemplateVersion.policy_id == policy_id
    ).order_by(InspectionTemplateVersion.revision.desc()))]


def get_version(db: Session, policy_id: int, revision: int) -> dict | None:
    version = db.scalars(select(InspectionTemplateVersion).where(
        InspectionTemplateVersion.policy_id == policy_id,
        InspectionTemplateVersion.revision == revision,
    )).first()
    return _row(version, detail=True) if version else None


def _restore_safe(historical, current):
    if isinstance(historical, dict):
        current = current if isinstance(current, dict) else {}
        result = {}
        for key, value in historical.items():
            if _SECRET_KEY.search(key) or _SCRIPT_KEY.search(key):
                if key in current:
                    result[key] = copy.deepcopy(current[key])
            else:
                result[key] = _restore_safe(value, current.get(key))
        for key, value in current.items():
            if _SECRET_KEY.search(key) or _SCRIPT_KEY.search(key):
                result[key] = copy.deepcopy(value)
        return result
    if isinstance(historical, list):
        current = current if isinstance(current, list) else []
        by_key = {item.get("key"): item for item in current if isinstance(item, dict) and item.get("key")}
        return [_restore_safe(value, by_key.get(value.get("key")) if isinstance(value, dict) else None)
                for value in historical]
    if isinstance(historical, str) and _REDACTED in historical:
        return copy.deepcopy(current)
    return copy.deepcopy(historical)


def restore_version(db: Session, policy_id: int, revision: int, expected_revision: int, *, actor=None) -> dict:
    from . import policy_service

    try:
        if expected_revision is None:
            raise TemplateVersionError("expected_revision_required", "Restore requires expected_revision")
        item = lock_policy(db, policy_id, expected_revision=expected_revision)
        if item.read_only:
            raise TemplateVersionError("builtin_read_only", "Clone a built-in template before restoring it")
        version = get_version(db, policy_id, revision)
        if version is None:
            raise TemplateVersionError("version_not_found", "Inspection template version not found")
        if _hash(version["snapshot"]) != version["content_hash"]:
            raise TemplateVersionError("history_corrupt", "Inspection template content hash mismatch")
        snapshot = _restore_safe(version["snapshot"], snapshot_policy(item))
        payload = {key: snapshot.get(key) for key in ("name", "enabled", "schedule", "remark")}
        for key in ("db_types", "categories", "item_config", "threshold_config", "action_config", "report_config"):
            payload[key] = snapshot.get(f"{key}_json")
        report = payload["report_config"] or {}
        current_report = item.report_config_json or {}
        for key in _RUNTIME_KEYS | {"instance_ids"}:
            report.pop(key, None)
            if key in current_report:
                report[key] = copy.deepcopy(current_report[key])
        payload["report_config"] = report
        return policy_service.update_policy(
            db, item, payload, expected_revision=expected_revision, actor=actor,
            operation="restore", source_policy_id=policy_id, source_revision=revision,
        )
    except Exception:
        db.rollback()
        raise
