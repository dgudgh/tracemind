#!/usr/bin/env python3
"""Crashable fixture Host used by the outer SIGKILL controller.

The controller kills this process, not the Provider. A completely new worker
opens the same SQLite files, performs ownership-checked orphan cleanup, and
recovers only the exact package identity persisted on each task/operation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.engine_host.orchestrator import FixturePackage, FixtureUpdateOrchestrator  # noqa: E402
from app.runtime.engine_host.metrics import redact_sensitive  # noqa: E402
from app.runtime.engine_host.packages import load_package as load_immutable_package  # noqa: E402
from app.runtime.engine_host.store import FixtureStore, Package, StoreError, SyntheticEffectLedger  # noqa: E402
from app.runtime.engine_host.supervisor import FixtureSupervisor  # noqa: E402
from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes  # noqa: E402


KILL_POINTS = (
    "stage_complete", "provider_started_before_handshake", "selftest_before_cas",
    "cas_after_commit", "dual_generation_inflight", "effect_after_commit_before_receipt",
    "drain_midway", "rollback_after_cas",
)


def public_evidence(value):
    """Redact structured values, including JSON serialized inside journal columns."""
    if isinstance(value, dict):
        return redact_sensitive({key: public_evidence(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return [public_evidence(child) for child in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return value
        return json.dumps(public_evidence(decoded), ensure_ascii=False, sort_keys=True)
    return value


def capacity_inputs(args, v1, v2):
    if args.fixture_calibration:
        if args.single_profile or args.single_candidate:
            raise StoreError("capacity_fixture_and_formal_conflict")
        return None, None, FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)
    if not args.single_profile or not args.single_candidate:
        raise StoreError("capacity_formal_evidence_missing")
    profile = json.loads(Path(args.single_profile).read_text())
    candidate = json.loads(Path(args.single_candidate).read_text())
    paths = {
        "rss_bytes": "metrics.resources.rss_bytes.max",
        "process_count": "metrics.resources.process_count.max",
        "fd_count": "metrics.resources.fd_count.max",
        "ipc_connections": "metrics.resources.ipc_connections.max",
        "queue_depth": "metrics.resources.queue_depth.max",
        "inflight": "metrics.resources.inflight.max",
    }
    limits = {}
    for name, path in paths.items():
        value = (profile.get("thresholds", {}).get(path) or {}).get("value")
        if type(value) not in (int, float) or value <= 0:
            raise StoreError("capacity_profile_threshold_missing", path)
        limits[name] = max(1, int(value * 2 + 0.999999))
    budget = FixtureUpdateOrchestrator.build_fixture_budget(
        v1, v2, profile["profile_digest"], candidate["raw_digest"], limits)
    return profile, candidate, budget


def load_package(path: Path) -> FixturePackage:
    try:
        return load_immutable_package(path)
    except (KeyError, ValueError):
        # Backward-compatible reader for the first fixture descriptor shape.
        pass
    value = json.loads(path.read_text())
    key = TrustedEngineKey.create(bytes.fromhex(value["key_raw"]), namespace=value["namespace"],
                                  channel=value["channel"], publisher=value["publisher"],
                                  engine_scopes=tuple(value["engine_scopes"]))
    manifest_sha = "sha256:" + hashlib.sha256(canonical_manifest_bytes(value["manifest"])).hexdigest()
    digest = value["digest"]
    if digest != manifest_sha:
        raise StoreError("package_descriptor_digest_mismatch")
    return FixturePackage(
        value["version"], digest, Path(value["source"]).resolve(), value["manifest"],
        value["signature"], (key,), value.get("payload_digest", ""),
        value.get("runtime_digest", ""), value.get("sdk_version", "engine-sdk/v1"),
        value.get("task_schema", "engine.task/v1"),
        value.get("result_schema", "engine.result/v1"),
        value.get("capability_version", "fixture/v1"),
    )


def _checkpoint(marker: Path, point: str, wanted: str, detail: dict) -> None:
    if point != wanted:
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(public_evidence({"point": point, "host_pid": os.getpid(), **detail}),
                                 sort_keys=True) + "\n")
    # Only SIGKILL from the outer controller releases this point.
    while True:
        time.sleep(60)


def _start(supervisor, package, generation, effect_db):
    kwargs = {"expected_namespace": "tmengine.test", "expected_channel": "test"}
    parameters = inspect.signature(supervisor.start).parameters
    if "generation" in parameters:
        kwargs["generation"] = generation
    if "store_path" in parameters:
        kwargs["store_path"] = effect_db
    return supervisor.start(package.source, package.manifest, package.signature,
                            package.trusted_keys, **kwargs)


def inject(args) -> int:
    v1, v2 = load_package(Path(args.package_v1)), load_package(Path(args.package_v2))
    capacity_profile, capacity_candidate, fixture_budget = capacity_inputs(args, v1, v2)
    store = FixtureStore(Path(args.store))
    orchestrator = FixtureUpdateOrchestrator(store=store, workdir=Path(args.workdir),
                                             effect_store_path=Path(args.effects),
                                             capacity_profile=capacity_profile,
                                             capacity_candidate=capacity_candidate,
                                             fixture_budget=fixture_budget)
    marker = Path(args.marker)
    old = candidate = rollback_host = None
    try:
        if store.active("test.fake") is None:
            store.bootstrap_active(orchestrator._package_record(v1, 1, "active"), generation=1)
        active = store.active("test.fake")
        if active.digest != v1.digest:
            raise StoreError("kill_case_requires_v1")
        old = orchestrator._start(v1, active.generation, label=f"{args.kill_point}-old",
                                  expected_digest=v1.digest)
        identity2 = orchestrator.package_identity(v2)
        operation = orchestrator.updater.prepare(
            engine_id="test.fake", digest=v2.digest, expected_generation=active.generation,
            idempotency_key=f"kill:{args.case_id}:update", version=v2.version,
            payload_digest=identity2["payload_digest"], runtime_digest=identity2["runtime_digest"],
            sdk_version=identity2["sdk_version"], task_schema=identity2["task_schema"],
            result_schema=identity2["result_schema"],
            capability_version=identity2["capability_version"], identity=identity2)
        orchestrator.updater.verified(operation.operation_id, identity2)

        def phase_hook(phase, detail):
            if phase == "staged":
                store.record_staged_artifact(
                    operation_id=operation.operation_id,
                    path=detail["staging_dir"], workdir=detail["workdir"],
                    state="staged", retained_reason="awaiting_provider_launch")
                orchestrator.updater.staged(operation.operation_id, detail)
                _checkpoint(marker, "stage_complete", args.kill_point,
                            {"operation_id": operation.operation_id})
            elif phase == "provider_started":
                launch = store.save_launch(engine_id="test.fake", generation=active.generation + 1,
                                           package_digest=v2.digest, record=detail,
                                           workdir=Path(args.workdir) / "candidate")
                orchestrator.updater.candidate_started(operation.operation_id, launch)
                _checkpoint(marker, "provider_started_before_handshake", args.kill_point,
                            {"operation_id": operation.operation_id,
                             "provider_pid": launch["pid"],
                             "launch_instance_id": launch["launch_instance_id"]})
            elif phase == "candidate_ready":
                row = store.operation(operation.operation_id)
                if row["state"] == "candidate_started":
                    orchestrator.updater.handshake_complete(operation.operation_id, detail)
                    orchestrator.updater.selftest_complete(operation.operation_id,
                                                           {"ok": True, **detail})

        constructor = inspect.signature(FixtureSupervisor).parameters
        kwargs = {"workdir": Path(args.workdir) / "candidate"}
        if "phase_hook" in constructor:
            kwargs["phase_hook"] = phase_hook
        candidate = FixtureSupervisor(**kwargs)
        if "phase_hook" not in constructor:
            # Compatibility while the Supervisor implementation is being landed.
            orchestrator.updater.staged(operation.operation_id, {"source": str(v2.source)})
            _checkpoint(marker, "stage_complete", args.kill_point,
                        {"operation_id": operation.operation_id})
        record = _start(candidate, v2, active.generation + 1, Path(args.effects))
        if "phase_hook" not in constructor:
            launch = store.save_launch(engine_id="test.fake", generation=active.generation + 1,
                                       package_digest=v2.digest, record=record,
                                       workdir=Path(args.workdir) / "candidate")
            orchestrator.updater.candidate_started(operation.operation_id, launch)
            orchestrator.updater.handshake_complete(operation.operation_id,
                                                     orchestrator._call(candidate, "handshake"))
            orchestrator.updater.selftest_complete(operation.operation_id,
                                                   orchestrator._call(candidate, "selftest"))
        _checkpoint(marker, "selftest_before_cas", args.kill_point,
                    {"operation_id": operation.operation_id,
                     "launch_instance_id": record.launch_instance_id})
        admission = orchestrator._capacity_admission(
            v1, v2, orchestrator._runtime_sample({"old": old, "candidate": candidate}))
        orchestrator.updater.candidate_ready(operation.operation_id,
                                             capacity_receipt=admission)
        activated = orchestrator.updater.activate(operation.operation_id)
        _checkpoint(marker, "cas_after_commit", args.kill_point,
                    {"operation_id": operation.operation_id, "generation": activated.generation})

        old_tasks = {}
        for status in ("queued", "running", "retry", "reconcile_required"):
            task_id = f"{args.case_id}-old-{status}"
            store.prepare_task(task_id=task_id, engine_id="test.fake",
                               generation=active.generation, digest=v1.digest,
                               payload={"echo": f"{args.case_id}-{status}"}, status=status,
                               effect_state="unknown" if status == "reconcile_required" else "none",
                               operation_id=f"old-effect-{args.case_id}-{status}")
            old_tasks[status] = task_id
        old_task = old_tasks["running"]
        _checkpoint(marker, "dual_generation_inflight", args.kill_point,
                    {"operation_id": operation.operation_id, "task_ids": old_tasks,
                     "generation": activated.generation})

        effect_task = f"{args.case_id}-effect"
        effect_op = f"effect-{args.case_id}"
        store.prepare_task(task_id=effect_task, engine_id="test.fake",
                           payload={"value": args.case_id}, status="running",
                           effect_state="unknown", operation_id=effect_op)
        task = store.task(effect_task)
        context = {"operation_id": effect_op, "input_digest": task["input_digest"],
                   "generation": task["generation"],
                   "package_digest": task["digest"], "attempt": task["attempt"],
                   "fence_epoch": task["fence"], "capability_id": "test.effect",
                   "scope": json.loads(task["authorization_json"]),
                   "deadline": float(task["deadline"])}
        effect_result = orchestrator._call(candidate, "execute",
                                           {"value": args.case_id}, context=context)
        # Provider commit is durable. Deliberately do not write the Host receipt.
        _checkpoint(marker, "effect_after_commit_before_receipt", args.kill_point,
                    {"operation_id": operation.operation_id, "task_id": effect_task,
                     "effect_operation_id": effect_op})
        store.set_task_result(effect_task, status="succeeded", effect_state="completed",
                              result=effect_result)

        store.set_task_result(old_task, status="succeeded", effect_state="none",
                              result=orchestrator._call(old, "invoke_read", {"echo": args.case_id}))
        for status in ("queued", "retry"):
            orchestrator.execute_task(old, old_tasks[status])
        orchestrator.reconcile_task(old, old_tasks["reconcile_required"])
        for task_id in old_tasks.values():
            if store.task(task_id)["status"] == "succeeded":
                store.release_task_reference(task_id)
        drain = orchestrator._call(old, "drain")
        orchestrator.updater.drain(operation.operation_id, drain)
        store.mark_launch(old.record.launch_instance_id, state="draining")
        _checkpoint(marker, "drain_midway", args.kill_point,
                    {"operation_id": operation.operation_id, "task_id": old_task})
        orchestrator._stop(old); old = None
        orchestrator.updater.retain(operation.operation_id, {"digest": v1.digest})
        orchestrator.updater.complete(operation.operation_id)

        identity1 = orchestrator.package_identity(v1)
        rollback_operation = orchestrator.updater.prepare_rollback(
            engine_id="test.fake", digest=v1.digest,
            expected_generation=activated.generation,
            idempotency_key=f"kill:{args.case_id}:rollback", identity=identity1)
        orchestrator.updater.verified(rollback_operation.operation_id, identity1)

        def rollback_hook(phase, detail):
            row = store.operation(rollback_operation.operation_id)
            if phase == "staged" and row["state"] == "verified":
                store.record_staged_artifact(
                    operation_id=rollback_operation.operation_id,
                    path=detail["staging_dir"], workdir=detail["workdir"],
                    state="staged", retained_reason="awaiting_rollback_provider_launch")
                orchestrator.updater.staged(rollback_operation.operation_id, detail)
            elif phase == "provider_started" and row["state"] == "staged":
                launch = store.save_launch(
                    engine_id="test.fake", generation=activated.generation + 1,
                    package_digest=v1.digest, record=detail,
                    workdir=detail.get("workdir"))
                orchestrator.updater.candidate_started(
                    rollback_operation.operation_id, launch)

        rollback_host = orchestrator._start(
            v1, activated.generation + 1, label=f"{args.kill_point}-rollback",
            expected_digest=v1.digest, phase_hook=rollback_hook)
        rollback_launch = store.launch(rollback_host.record.launch_instance_id)
        if store.operation(rollback_operation.operation_id)["state"] == "staged":
            orchestrator.updater.candidate_started(
                rollback_operation.operation_id, rollback_launch or {})
        rollback_handshake = orchestrator._call(rollback_host, "handshake")
        orchestrator.updater.handshake_complete(
            rollback_operation.operation_id, rollback_handshake)
        rollback_selftest = orchestrator._call(rollback_host, "selftest")
        orchestrator.updater.selftest_complete(
            rollback_operation.operation_id, rollback_selftest)
        rollback_admission = orchestrator._capacity_admission(
            v1, v2, orchestrator._runtime_sample(
                {"old": candidate, "candidate": rollback_host}))
        orchestrator.updater.candidate_ready(
            rollback_operation.operation_id, capacity_receipt=rollback_admission)
        rollback = orchestrator.updater.activate(rollback_operation.operation_id)
        _checkpoint(marker, "rollback_after_cas", args.kill_point,
                    {"operation_id": rollback.operation_id, "generation": rollback.generation})
        raise RuntimeError("kill point was not reached")
    finally:
        # A SIGKILL bypasses this block. It only cleans unexpected normal errors.
        for supervisor in (old, candidate, rollback_host):
            if supervisor is not None:
                try:
                    orchestrator._stop(supervisor)
                except Exception:
                    pass
        store.close()


def recover(args) -> int:
    v1, v2 = load_package(Path(args.package_v1)), load_package(Path(args.package_v2))
    capacity_profile, capacity_candidate, fixture_budget = capacity_inputs(args, v1, v2)
    packages = {v1.digest: v1, v2.digest: v2}
    store = FixtureStore(Path(args.store))
    orchestrator = FixtureUpdateOrchestrator(store=store, workdir=Path(args.workdir),
                                             effect_store_path=Path(args.effects),
                                             capacity_profile=capacity_profile,
                                             capacity_candidate=capacity_candidate,
                                             fixture_budget=fixture_budget)
    try:
        cleanup = orchestrator.recover_owned_launches()
        operations = [dict(row) for row in store.conn.execute(
            "SELECT * FROM updates ORDER BY created_at,operation_id")]
        update = next((row for row in operations if row["kind"] == "update"), None)
        if update is None:
            raise StoreError("recovery_update_missing")
        active = store.active("test.fake")
        # Pre-CAS points keep V1 active. A selftested candidate may safely resume;
        # earlier partial candidates are blocked and do not change routing.
        if update["target_generation"] is None:
            if update["state"] in {"candidate_ready", "selftest_complete"}:
                package = packages.get(update["digest"])
                if package is None:
                    raise StoreError("recovery_package_missing")
                active_package = packages.get(active.digest)
                if active_package is None:
                    raise StoreError("recovery_active_package_missing")
                old = orchestrator._start(
                    active_package, active.generation, label="recover-active",
                    expected_digest=active.digest)
                candidate = orchestrator._start(package, update["expected_generation"] + 1,
                                                label="recover-candidate",
                                                expected_digest=update["digest"])
                try:
                    admission = orchestrator._capacity_admission(
                        active_package, package,
                        orchestrator._runtime_sample({"old": old, "candidate": candidate}))
                    if update["state"] == "selftest_complete":
                        orchestrator.updater.candidate_ready(
                            update["operation_id"], capacity_receipt=admission)
                    orchestrator.updater.activate(update["operation_id"])
                finally:
                    orchestrator._stop(candidate)
                    orchestrator._stop(old)
            else:
                store.transition_update(update["operation_id"], "blocked",
                                        detail={"reason": "incomplete_candidate_recovered"})
                store.mark_unlaunched_staged_artifacts(
                    update["operation_id"], state="retained_audit",
                    retained_reason="pre_pid_stage_verified_no_owned_process")

        # Recover every unfinished task with its own exact persisted digest.
        recovered_tasks = []
        for task in store.tasks(statuses=("queued", "running", "retry", "reconcile_required")):
            package = packages.get(task["digest"])
            if package is None:
                # Deliberately try the caller-supplied version slot so a valid
                # but wrong signed package is reported as an identity mismatch,
                # never silently selected by its self-description.
                package = v1 if task["package_version"] == "1.0.0" else v2
            recovered_tasks.append({"task_id": task["task_id"],
                                    **orchestrator.recover_task(task["task_id"], package)})
        ledger = SyntheticEffectLedger(Path(args.effects))
        try:
            effect_counts = {task["operation_id"]: ledger.count(task["operation_id"])
                             for task in store.tasks() if task["operation_id"] and
                             task["operation_id"].startswith("effect-")}
        finally:
            ledger.close()
        report = {
            "ok": all(not item.get("blocked_reasons") for item in cleanup),
            "case_id": args.case_id, "kill_point": args.kill_point,
            "cleanup": cleanup, "active": store.route("test.fake"),
            "operations": [store.operation(row["operation_id"]) for row in operations],
            "journals": {row["operation_id"]: store.journal(row["operation_id"])
                         for row in operations},
            "tasks": store.tasks(),
            "results": [store.result(task["task_id"]) for task in store.tasks()
                        if store.result(task["task_id"])],
            "recovered_tasks": recovered_tasks, "effect_counts": effect_counts,
            "launch_artifacts": store.launch_artifacts(),
        }
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(public_evidence(report), ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"ok": report["ok"], "case_id": args.case_id,
                          "kill_point": args.kill_point}))
        return 0 if report["ok"] else 2
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--effects", required=True)
    parser.add_argument("--package-v1", required=True)
    parser.add_argument("--package-v2", required=True)
    parser.add_argument("--action", choices=("inject", "recover"), required=True)
    parser.add_argument("--kill-point", choices=KILL_POINTS, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--single-profile")
    parser.add_argument("--single-candidate")
    parser.add_argument("--fixture-calibration", action="store_true")
    args = parser.parse_args(argv)
    return inject(args) if args.action == "inject" else recover(args)


if __name__ == "__main__":
    raise SystemExit(main())
