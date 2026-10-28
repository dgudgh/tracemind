"""Trusted startup bridge from an ordinary plan to an exact signed Provider."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from app.engine_registry import resolve_legacy
from app.models.engine_execution import DBEngineInstanceBinding, DBEnginePlanBinding, DBEngineResultReceipt
from app.runtime.engine_sdk.contracts import TASK_SCHEMA, RESULT_SCHEMA, digest_json
from app.runtime.engine_sdk.wire import canonical, strict_json
from app.runtime.execution_identity import validate_identity
# 状态源统一放在 engine_runtime（OSS 侧协议模块），本模块只做具体实现。
# READ_OPERATIONS 仍从本模块导出，保持既有 import 路径不变。
from app.services.engine_runtime import READ_OPERATIONS as _READ_OPERATIONS
from app.services.engine_runtime import install as _install_runtime
from app.services.engine_runtime import runtime as _runtime

# Source-owned, reviewed capability mapping. Neither a request nor a package may choose it.
READ_OPERATIONS = dict(_READ_OPERATIONS)


class EngineExecutionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def install_bridge(bridge):
    """Only the trusted application startup path may call this (never an HTTP payload)."""
    if bridge is not None and not isinstance(bridge, EngineExecutionBridge):
        raise EngineExecutionError("engine_bridge_invalid")
    _install_runtime(bridge)


def bridge():
    """未安装时为 None —— 调用方的 `bridge() is not None` 守卫因此保持成立。"""
    implementation = _runtime()
    return implementation if isinstance(implementation, EngineExecutionBridge) else None


def wire_engine_business_runtime(app, *, manager, core_release):
    """Install API catalog and plan/worker execution from one trusted startup manager.

    The caller owns the manager lifecycle and must invoke this in each API/Worker
    process after manager recovery, before accepting plan requests or queue work.
    """
    from app.services.engine_catalog_publisher import CatalogPublisher
    if not all(hasattr(manager, name) for name in ("store", "repo", "_hosts", "_retained_hosts")):
        raise EngineExecutionError("engine_manager_invalid")
    active = EngineExecutionBridge(manager=manager, core_release=core_release)
    publisher = CatalogPublisher(manager)
    publisher.refresh()
    app.state.engine_catalog_publisher = publisher
    install_bridge(active)
    return active


def engine_for_instance(db, instance):
    if instance is None:
        raise EngineExecutionError("missing_instance")
    row = db.get(DBEngineInstanceBinding, instance.id)
    if row is not None:
        if row.source_db_type != instance.db_type or row.state != "mapped" or not row.engine_id:
            raise EngineExecutionError("engine_identity_unresolved")
        return row.engine_id
    known = resolve_legacy(instance.db_type)["engine_id"]
    if known is None:
        raise EngineExecutionError("engine_identity_unresolved")
    return known


class EngineExecutionBridge:
    def __init__(self, *, manager, core_release):
        # manager, repository and ProviderSupervisor are supplied by startup, never user data.
        if not isinstance(core_release, str) or not core_release or len(core_release) > 200:
            raise EngineExecutionError("core_release_invalid")
        self.manager = manager
        self.core_release = core_release

    def _host(self, engine_id, digest, generation=None):
        hosts = (*self.manager._hosts.values(), *self.manager._retained_hosts.values())
        return next((h for h in hosts if h.identity and h.identity.get("engine_id") == engine_id
                     and h.identity.get("package_digest") == digest
                     and (generation is None or h.identity.get("generation") == generation)), None)

    def _route(self, engine_id):
        with self.manager.store._connect() as conn:
            row = conn.execute("SELECT generation,digest FROM active WHERE engine_id=?", (engine_id,)).fetchone()
            return dict(row) if row is not None else None

    @contextmanager
    def _package(self, engine_id, digest):
        with self.manager.store._connect() as conn:
            row = conn.execute(
                "SELECT state,revoked,quarantined FROM packages WHERE engine_id=? AND digest=?",
                (engine_id, digest)).fetchone()
        if row is None or row["revoked"] or row["quarantined"]:
            raise EngineExecutionError("provider_version_unavailable")
        try:
            with self.manager.repo.guard_installed(digest) as verified:
                if verified["manifest"]["engine_id"] != engine_id:
                    raise EngineExecutionError("provider_identity_mismatch")
                yield verified
        except EngineExecutionError:
            raise
        except Exception as exc:
            raise EngineExecutionError("provider_version_unavailable") from exc

    def freeze(self, db, instance, capability_id):
        operation = READ_OPERATIONS.get(capability_id)
        if operation is None:
            return None
        engine_id = engine_for_instance(db, instance)
        route = self._route(engine_id)
        if route is None:
            raise EngineExecutionError("provider_not_active")
        digest = route["digest"]
        with self._package(engine_id, digest) as verified:
            with self.manager.store._connect() as conn:
                state = conn.execute("SELECT state FROM packages WHERE engine_id=? AND digest=?",
                                     (engine_id, digest)).fetchone()
            if state is None or state["state"] == "disabled":
                raise EngineExecutionError("provider_disabled")
            manifest = verified["manifest"]
            host = self._host(engine_id, digest, route["generation"])
            if operation not in manifest["capabilities"] or host is None or host.state.value not in {"ready", "active"}:
                raise EngineExecutionError("provider_not_ready")
            identity = dict(validate_identity({
                "core_release": self.core_release, "engine_id": engine_id,
                "package_digest": digest, "runtime_digest": manifest["runtime_digest"],
                "sdk_version": manifest["sdk_version"], "task_schema": manifest["task_schema"],
                "result_schema": manifest["result_schema"],
            }))
            if any(host.identity.get(k) != identity[k] for k in
                   ("engine_id", "package_digest", "runtime_digest", "sdk_version")):
                raise EngineExecutionError("provider_identity_mismatch")
            if host.identity.get("generation") != route["generation"]:
                raise EngineExecutionError("provider_generation_mismatch")
            return {"identity": identity, "provider_version": manifest["version"],
                    "provider_generation": route["generation"], "operation": operation}

    def check(self, frozen):
        identity = dict(validate_identity(frozen["identity"]))
        engine_id, digest = identity["engine_id"], identity["package_digest"]
        with self._package(engine_id, digest) as verified:
            manifest = verified["manifest"]
            if (manifest["version"] != frozen["provider_version"]
                    or frozen["operation"] not in manifest["capabilities"]
                    or any(manifest[k] != identity[k] for k in
                           ("engine_id", "runtime_digest", "sdk_version", "task_schema", "result_schema"))):
                raise EngineExecutionError("provider_identity_mismatch")
            host = self._host(engine_id, digest, frozen["provider_generation"])
            if host is None or host.state.value not in {"ready", "active"}:
                raise EngineExecutionError("provider_version_unavailable")
            if host.identity.get("generation") != frozen["provider_generation"]:
                raise EngineExecutionError("provider_generation_mismatch")
            return host

    def invoke(self, frozen, instance_id, *, timeout):
        host = self.check(frozen)
        # The platform plan's Store artifact reference protects GC; do not hold
        # repository/trust locks through slow network I/O or broker authorization.
        return host.invoke_read(str(instance_id), frozen["operation"], timeout=timeout)

    def retain_plan(self, plan, frozen):
        from app.runtime.engine_host.store import StoreError
        try:
            self.manager.store.prepare_task(
                task_id=plan.plan_id, engine_id=frozen["identity"]["engine_id"],
                generation=frozen["provider_generation"], digest=frozen["identity"]["package_digest"],
                payload={"platform_plan_id": plan.plan_id,
                         "request_hash": plan.capability_snapshot_json["request_hash"]},
                identity={"package_version": frozen["provider_version"],
                          "runtime_digest": frozen["identity"]["runtime_digest"],
                          "sdk_version": frozen["identity"]["sdk_version"],
                          "task_schema": frozen["identity"]["task_schema"],
                          "result_schema": frozen["identity"]["result_schema"]})
        except StoreError as exc:
            raise EngineExecutionError(exc.code) from exc

    def release_completed(self, plan):
        if plan.status != "settled" or plan.outcome != "succeeded":
            return
        # Store metadata only; platform standard snapshot remains the result authority.
        with self.manager.store._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (plan.plan_id,)).fetchone()
            task = dict(row) if row is not None else None
        if task is None or task["digest"] != plan.capability_snapshot_json["execution_identity"]["package_digest"]:
            raise EngineExecutionError("provider_task_reference_missing")
        self.manager.store.set_task_result(plan.plan_id, status="succeeded", effect_state="none",
                                           result={"platform_plan_id": plan.plan_id})
        self.manager.store.release_task_reference(plan.plan_id)


def save_binding(db, plan, frozen):
    from app.services.plan_validation import binding_hash
    db.add(DBEnginePlanBinding(
        plan_id=plan.plan_id, engine_id=frozen["identity"]["engine_id"],
        execution_identity_json=frozen["identity"], provider_version=frozen["provider_version"],
        provider_generation=frozen["provider_generation"],
        operation=frozen["operation"], request_hash=plan.capability_snapshot_json["request_hash"],
        binding_hash=binding_hash(plan.capability_snapshot_json),
        plan_revision=plan.capability_snapshot_json.get("revision", 1), state="ready",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))


def check_binding(db, plan):
    snapshot = plan.capability_snapshot_json
    if "execution_identity" not in snapshot:
        return None
    row = db.get(DBEnginePlanBinding, plan.plan_id)
    from app.services.plan_validation import binding_hash
    if (row is None or row.state != "ready" or row.engine_id != snapshot["execution_identity"]["engine_id"]
            or row.execution_identity_json != snapshot["execution_identity"]
            or row.request_hash != snapshot["request_hash"] or row.binding_hash != binding_hash(snapshot)
            or row.plan_revision != snapshot.get("revision", 1)
            or row.operation != snapshot.get("provider_operation")
            or row.provider_version != snapshot.get("provider_version")
            or row.provider_generation != snapshot.get("provider_generation")):
        raise EngineExecutionError("engine_binding_changed")
    if _bridge is None:
        raise EngineExecutionError("engine_bridge_unavailable")
    _bridge.check({"identity": row.execution_identity_json,
                   "provider_version": row.provider_version,
                   "provider_generation": row.provider_generation, "operation": row.operation})
    return row


def save_result(db, plan, step, values, *, fence_epoch):
    frozen = plan.capability_snapshot_json
    envelope = {"schema": "engine.platform_result/v1", "result_schema": RESULT_SCHEMA,
                "engine_id": frozen["execution_identity"]["engine_id"],
                "execution_identity": frozen["execution_identity"],
                "provider_version": frozen["provider_version"],
                "operation": frozen["provider_operation"], "values": values}
    raw = canonical(envelope)
    if len(raw) > 1048576:
        raise EngineExecutionError("result_budget_exceeded")
    operation_id = f"{plan.plan_id}:{step.seq}:{frozen.get('revision', 1)}"
    existing = db.query(DBEngineResultReceipt).filter_by(plan_id=plan.plan_id, step_seq=step.seq).first()
    digest = digest_json(envelope)
    if existing:
        if existing.result_digest != digest:
            raise EngineExecutionError("result_replay_conflict")
        return envelope
    db.add(DBEngineResultReceipt(plan_id=plan.plan_id, step_seq=step.seq,
                                 operation_id=operation_id, attempt=plan.attempt,
                                 fence_epoch=fence_epoch, result_schema=RESULT_SCHEMA,
                                 result_json=envelope, result_digest=digest,
                                 created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    return envelope


def decode_result(db, plan_id, *, actor):
    """Core-only decoder; no Provider, broker or target database access."""
    from app.services import plan_service as plans
    plan = plans._load(db, plan_id, actor)
    receipts = db.query(DBEngineResultReceipt).filter_by(plan_id=plan_id).order_by(DBEngineResultReceipt.step_seq).all()
    if not receipts:
        return {"schema": "engine.platform_results/v1", "plan_id": plan_id,
                "legacy_result_ref": plan.result_ref_json, "items": []}
    items = []
    for row in receipts:
        raw = canonical(row.result_json)
        if len(raw) > 1048576 or digest_json(row.result_json) != row.result_digest:
            raise EngineExecutionError("result_integrity_invalid")
        value = strict_json(raw, max_bytes=1048576)
        if (set(value) != {"schema", "result_schema", "engine_id", "execution_identity",
                           "provider_version", "operation", "values"}
                or value["schema"] != "engine.platform_result/v1"
                or value["result_schema"] != RESULT_SCHEMA
                or not isinstance(value["values"], dict)):
            raise EngineExecutionError("result_schema_unsupported")
        validate_identity(value["execution_identity"])
        frozen = plan.capability_snapshot_json
        if (value["execution_identity"] != frozen.get("execution_identity")
                or value["engine_id"] != frozen["execution_identity"]["engine_id"]
                or value["provider_version"] != frozen.get("provider_version")
                or value["operation"] != frozen.get("provider_operation")
                or row.operation_id != f"{plan_id}:{row.step_seq}:{frozen.get('revision', 1)}"
                or row.result_schema != RESULT_SCHEMA):
            raise EngineExecutionError("result_identity_mismatch")
        items.append({"step_seq": row.step_seq, "result": value})
    return {"schema": "engine.platform_results/v1", "plan_id": plan_id, "items": items}
