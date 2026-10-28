"""Durable V/V+1 fixture orchestration over the real Supervisor and UDS RPC."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.runtime.engine_host.store import FixtureStore, Package, StoreError
from app.runtime.engine_host.supervisor import FixtureSupervisor
from app.runtime.engine_host.update import FixtureUpdater
from app.runtime.engine_host.metrics import (
    CapacityEvidenceError, assert_raw_counts, base_context, digest_directory,
    digest_file, digest_json, evaluate_report, sample_supervisor,
    validate_frozen_profile,
)
from app.runtime.engine_host.packages import describe_context
from app.runtime.engine_packs.trust import canonical_manifest_bytes


@dataclass(frozen=True)
class FixturePackage:
    """Compatibility package shape; engine_host.packages may return the same fields."""
    version: str
    digest: str
    source: Path
    manifest: dict
    signature: dict
    trusted_keys: tuple
    payload_digest: str = ""
    runtime_digest: str = ""
    sdk_version: str = "engine-sdk/v1"
    task_schema: str = "engine.task/v1"
    result_schema: str = "engine.result/v1"
    capability_version: str = "fixture/v1"


def _value(package, name: str, default=""):
    return getattr(package, name, default)


class FixtureUpdateOrchestrator:
    def __init__(self, *, store: FixtureStore, workdir: Path,
                 effect_store_path: Path | None = None,
                 capacity_profile: dict | Path | None = None,
                 capacity_candidate: dict | Path | None = None,
                 fixture_budget: dict | Path | None = None):
        self.store = store
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.effect_store_path = Path(effect_store_path or self.store.path.with_name("effects.db")).resolve()
        self.capacity_profile = self._load_evidence(capacity_profile)
        self.capacity_candidate = self._load_evidence(capacity_candidate)
        self.fixture_budget = self._load_evidence(fixture_budget)
        self.updater = FixtureUpdater(store)
        self._sequence = 0

    @staticmethod
    def _load_evidence(value: dict | Path | None) -> dict | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return json.loads(json.dumps(value))
        loaded = json.loads(Path(value).read_text())
        if not isinstance(loaded, dict):
            raise StoreError("capacity_evidence_invalid")
        return loaded

    @classmethod
    def build_fixture_budget(cls, v1, v2, single_profile_digest: str = "fixture-harness",
                             single_candidate_raw_digest: str = "fixture-harness",
                             host_limits: dict | None = None) -> dict:
        """Build a digest-bound test budget, never a formal capacity signature."""
        identities = [cls.package_identity(v1), cls.package_identity(v2)]
        context = describe_context()
        value = {
            "schema": "engine.fixture-admission/v1", "fixture_only": True,
            "qa_ops_acceptance": "pending", "single_profile_digest": single_profile_digest,
            "single_candidate_raw_digest": single_candidate_raw_digest,
            "max_generations": 2,
            "package_digests": [item["package_digest"] for item in identities],
            "payload_digests": [item["payload_digest"] for item in identities],
            "runtime_digests": sorted({item["runtime_digest"] for item in identities}),
            "host_context": {key: context[key] for key in
                             ("os", "cpu", "python", "python_abi", "host_digest",
                              "sdk_digest", "launcher_digest")},
            "host_limits": host_limits or {
                "rss_bytes": 1024 * 1024 * 1024, "process_count": 8,
                "fd_count": 1024, "ipc_connections": 8,
                "queue_depth": 512, "inflight": 16},
            "projection_method": ("single_frozen_max_times_exact_generation_count_not_v2_certification"
                                  if single_profile_digest != "fixture-harness"
                                  else "explicit_fixture_harness_limit_not_capacity_certification"),
        }
        value["budget_digest"] = digest_json(value)
        return value

    @staticmethod
    def package_identity(package) -> dict:
        manifest_sha = hashlib.sha256(canonical_manifest_bytes(package.manifest)).hexdigest()
        digest = _value(package, "digest")
        if not digest:
            raise StoreError("package_digest_missing")
        if digest not in {manifest_sha, "sha256:" + manifest_sha}:
            # Package builders may define a larger archive digest. If available,
            # their verify() method is authoritative and must return that digest.
            verify = getattr(package, "verify", None)
            if verify is None:
                raise StoreError("package_manifest_digest_mismatch")
            verified = verify()
            verified_digest = (verified.get("package_digest") if isinstance(verified, dict)
                               else getattr(verified, "package_digest", None))
            if verified_digest != digest:
                raise StoreError("package_manifest_digest_mismatch")
        return {
            "engine_id": package.manifest.get("engine_id"), "package_digest": digest,
            "manifest_sha256": manifest_sha, "version": package.version,
            "payload_digest": _value(package, "payload_digest"),
            "runtime_digest": _value(package, "runtime_digest"),
            "sdk_version": _value(package, "sdk_version") or
                           package.manifest.get("sdk_version", "engine-sdk/v1"),
            "task_schema": _value(package, "task_schema") or
                           package.manifest.get("task_schema", "engine.task/v1"),
            "result_schema": _value(package, "result_schema") or
                             package.manifest.get("result_schema", "engine.result/v1"),
            "capability_version": _value(package, "capability_version") or
                                  package.manifest.get("capability_version", "fixture/v1"),
        }

    def _capacity_admission(self, v1, v2, runtime_sample: dict) -> dict:
        budget = self.fixture_budget
        if not isinstance(budget, dict):
            raise StoreError("capacity_admission_missing")
        unsigned = dict(budget)
        supplied_budget_digest = unsigned.pop("budget_digest", None)
        if supplied_budget_digest != digest_json(unsigned):
            raise StoreError("capacity_budget_digest_mismatch")
        if (budget.get("schema") != "engine.fixture-admission/v1" or
                budget.get("fixture_only") is not True or
                budget.get("qa_ops_acceptance") != "pending"):
            raise StoreError("capacity_budget_scope_invalid")
        identities = [self.package_identity(v1), self.package_identity(v2)]
        if budget.get("package_digests") != [item["package_digest"] for item in identities]:
            raise StoreError("capacity_budget_package_mismatch")
        if budget.get("payload_digests") != [item["payload_digest"] for item in identities]:
            raise StoreError("capacity_budget_payload_mismatch")
        if budget.get("runtime_digests") != sorted({item["runtime_digest"] for item in identities}):
            raise StoreError("capacity_budget_runtime_mismatch")
        if budget.get("max_generations") != 2:
            raise StoreError("capacity_budget_generation_limit")
        expected_projection = ("single_frozen_max_times_exact_generation_count_not_v2_certification"
                               if budget.get("single_profile_digest") != "fixture-harness"
                               else "explicit_fixture_harness_limit_not_capacity_certification")
        if budget.get("projection_method") != expected_projection:
            raise StoreError("capacity_budget_projection_invalid")
        context = describe_context()
        actual_host_context = {key: context[key] for key in
                               ("os", "cpu", "python", "python_abi", "host_digest",
                                "sdk_digest", "launcher_digest")}
        if budget.get("host_context") != actual_host_context:
            raise StoreError("capacity_budget_context_mismatch")
        limits = budget.get("host_limits")
        if (not isinstance(limits, dict) or
                any(type(limits.get(key)) is not int or limits[key] <= 0 for key in
                    ("rss_bytes", "process_count", "fd_count", "ipc_connections",
                     "queue_depth", "inflight"))):
            raise StoreError("capacity_budget_limits_invalid")
        if set(runtime_sample) != {"old", "candidate"}:
            raise StoreError("capacity_runtime_sample_missing")
        measured = {"rss_bytes": 0, "process_count": 0, "fd_count": 0,
                    "ipc_connections": 0, "queue_depth": 0, "inflight": 0}
        for sample in runtime_sample.values():
            tree = sample.get("process_tree") if isinstance(sample, dict) else None
            fields = {
                "rss_bytes": tree.get("rss_bytes") if isinstance(tree, dict) else None,
                "process_count": tree.get("process_count") if isinstance(tree, dict) else None,
                "fd_count": tree.get("fd_count") if isinstance(tree, dict) else None,
                "ipc_connections": sample.get("ipc_connections") if isinstance(sample, dict) else None,
                "queue_depth": sample.get("queue_depth") if isinstance(sample, dict) else None,
                "inflight": sample.get("inflight") if isinstance(sample, dict) else None,
            }
            for name, metric_value in fields.items():
                value = metric_value.get("value") if isinstance(metric_value, dict) else None
                if type(value) not in (int, float):
                    raise StoreError("capacity_runtime_metric_missing", name)
                measured[name] += value
        for name, observed in measured.items():
            if observed > limits[name]:
                raise StoreError("capacity_runtime_budget_exceeded", name)

        profile, candidate = self.capacity_profile, self.capacity_candidate
        if (profile is None) != (candidate is None):
            raise StoreError("capacity_evidence_incomplete")
        if profile is None:
            # Only the isolated harness may use this explicit projected budget.
            if (budget.get("single_profile_digest") != "fixture-harness" or
                    budget.get("single_candidate_raw_digest") != "fixture-harness"):
                raise StoreError("capacity_formal_evidence_missing")
            return {"fixture_only": True, "admitted": True,
                    "gate": "explicit_fixture_harness_budget",
                    "budget_digest": supplied_budget_digest,
                    "measured": measured}

        try:
            frozen = validate_frozen_profile(profile)
            if frozen["kind"] != "single":
                raise StoreError("capacity_profile_kind_invalid")
            if (candidate.get("schema") != "engine.capacity-report/v2" or
                    candidate.get("kind") != "single" or candidate.get("phase") != "formal" or
                    candidate.get("fixture_only") is not True or
                    candidate.get("qa_ops_acceptance") != "pending" or
                    candidate.get("status") != "candidate" or candidate.get("ok") is not True or
                    candidate.get("failures")):
                raise StoreError("capacity_candidate_invalid")
            if candidate.get("profile_digest") != frozen["profile_digest"]:
                raise StoreError("capacity_candidate_profile_mismatch")
            for key in ("context", "packages", "load"):
                if candidate.get(key) != frozen.get(key):
                    raise StoreError(f"capacity_candidate_{key}_mismatch")
            if candidate.get("raw_digest") != digest_json(candidate.get("raw", {})):
                raise StoreError("capacity_candidate_raw_digest_mismatch")
            assert_raw_counts(candidate.get("raw", {}), frozen)
            if evaluate_report(candidate, frozen):
                raise StoreError("capacity_candidate_budget_failed")
        except CapacityEvidenceError as exc:
            raise StoreError(exc.code, exc.detail) from exc
        if budget.get("single_profile_digest") != frozen["profile_digest"]:
            raise StoreError("capacity_budget_profile_mismatch")
        if budget.get("single_candidate_raw_digest") != candidate["raw_digest"]:
            raise StoreError("capacity_budget_candidate_mismatch")
        if frozen["context"].get("machine") != base_context():
            raise StoreError("capacity_profile_machine_mismatch")
        if frozen["context"].get("engine_runtime") != describe_context():
            raise StoreError("capacity_profile_runtime_mismatch")

        expected_package = frozen["packages"][0]
        verified = v1.verify() if callable(getattr(v1, "verify", None)) else {}
        exact = {
            "version": v1.version,
            "source_digest": digest_directory(v1.source),
            "package_digest": identities[0]["package_digest"],
            "manifest_sha256": verified.get("manifest_sha256") if isinstance(verified, dict)
                               else identities[0]["manifest_sha256"],
            "payload_digest": identities[0]["payload_digest"],
            "runtime_digest": identities[0]["runtime_digest"],
            "artifact_path": expected_package.get("artifact_path"),
            "artifact_sha256": expected_package.get("artifact_sha256"),
        }
        artifact = Path(str(exact["artifact_path"])).resolve()
        if not artifact.is_file() or digest_file(artifact) != exact["artifact_sha256"]:
            raise StoreError("capacity_artifact_identity_mismatch")
        if expected_package != exact:
            raise StoreError("capacity_profile_package_mismatch")
        return {"fixture_only": True, "admitted": True,
                "gate": "formal_single_profile_and_candidate_plus_fixture_projection",
                "budget_digest": supplied_budget_digest,
                "formal_profile_digest": frozen["profile_digest"],
                "measured": measured}

    def _package_record(self, package, generation: int, state: str = "candidate") -> Package:
        identity = self.package_identity(package)
        return Package(identity["engine_id"], identity["package_digest"], identity["version"],
                       generation, state, identity["payload_digest"], identity["runtime_digest"],
                       identity["sdk_version"], identity["task_schema"], identity["result_schema"],
                       identity["capability_version"])

    def _next_workdir(self, generation: int, package, label: str) -> Path:
        self._sequence += 1
        return self.workdir / f"g{generation}-{package.version}-{label}-{self._sequence:04d}"

    def _start(self, package, generation: int, *, label: str = "launch",
               expected_digest: str | None = None,
               phase_hook: Callable[[str, dict], None] | None = None) -> FixtureSupervisor:
        identity = self.package_identity(package)
        if expected_digest is not None and identity["package_digest"] != expected_digest:
            raise StoreError("recovery_package_identity_mismatch")
        persisted = self.store.package(identity["engine_id"], identity["package_digest"])
        if persisted is None:
            raise StoreError("package_not_registered")
        if persisted.version != identity["version"]:
            raise StoreError("persisted_package_identity_mismatch")
        launch_workdir = self._next_workdir(generation, package, label)
        constructor_kwargs = {"workdir": launch_workdir}
        if "phase_hook" in inspect.signature(FixtureSupervisor).parameters:
            constructor_kwargs["phase_hook"] = phase_hook
        supervisor = FixtureSupervisor(**constructor_kwargs)
        kwargs = {"expected_namespace": "tmengine.test", "expected_channel": "test"}
        parameters = inspect.signature(supervisor.start).parameters
        if "generation" in parameters:
            kwargs["generation"] = generation
        if "store_path" in parameters:
            kwargs["store_path"] = self.effect_store_path
        record = supervisor.start(package.source, package.manifest, package.signature,
                                  package.trusted_keys, **kwargs)
        saved = self.store.save_launch(engine_id=identity["engine_id"], generation=generation,
                                       package_digest=identity["package_digest"], record=record,
                                       workdir=launch_workdir)
        # Persisted launch identity must agree with the package selected before RPC.
        for key in ("package_digest", "runtime_digest", "sdk_version"):
            actual = saved.get(key) or identity.get(key) or ""
            expected = identity.get(key) or actual
            if actual and expected and actual != expected:
                supervisor.stop()
                self.store.mark_launch(saved["launch_instance_id"], state="blocked",
                                       recovery_state="identity_mismatch")
                raise StoreError("launch_package_identity_mismatch", key)
        return supervisor

    def _persist_launch_members(self, supervisor: FixtureSupervisor) -> None:
        record = getattr(supervisor, "record", None)
        launch_id = getattr(record, "launch_instance_id", None)
        members = getattr(record, "members", None)
        if launch_id and members is not None and self.store.launch(launch_id) is not None:
            self.store.update_launch_members(launch_id, members)

    def _call(self, supervisor: FixtureSupervisor, method: str, params: dict | None = None,
              *, context: dict | None = None, timeout: float = 60) -> dict:
        call_params = inspect.signature(supervisor.call).parameters
        kwargs = {"timeout": timeout}
        if "context" in call_params:
            kwargs["context"] = context
        try:
            return supervisor.call(method, params or {}, **kwargs)
        finally:
            self._persist_launch_members(supervisor)

    def _stop(self, supervisor: FixtureSupervisor, *, state: str = "stopped") -> None:
        launch_id = supervisor.record.launch_instance_id if supervisor.record else None
        supervisor.stop()
        if launch_id and self.store.launch(launch_id):
            self.store.mark_launch(launch_id, state=state, recovery_state="cleaned")
            self.store.mark_staged_artifact(
                launch_instance_id=launch_id, state="cleaned",
                retained_reason="supervisor_stop_removed_runtime_snapshot")

    @staticmethod
    def _observe(observer: Callable[[dict], None] | None, event: dict) -> None:
        if observer is not None:
            observer({"monotonic_ns": time.monotonic_ns(),
                      "utc": datetime.now(timezone.utc).isoformat(), **event})

    def _runtime_sample(self, supervisors: dict[str, FixtureSupervisor]) -> dict:
        samples = {label: sample_supervisor(supervisor)
                   for label, supervisor in supervisors.items()}
        for supervisor in supervisors.values():
            self._persist_launch_members(supervisor)
        return samples

    def execute_task(self, supervisor: FixtureSupervisor, task_id: str,
                     *, method: str = "invoke_read") -> dict:
        task = self.store.task(task_id)
        if task is None:
            raise StoreError("task_not_found")
        record = supervisor.record
        if record is None:
            raise StoreError("supervisor_not_started")
        record_digest = getattr(record, "package_digest", "")
        if record_digest and record_digest != task["digest"]:
            raise StoreError("task_provider_identity_mismatch")
        params = json.loads(task["input_json"])
        context = self._task_context(task, method)
        self.store.set_task_status(task_id, status="running")
        try:
            result = self._call(supervisor, method, params, context=context)
        except Exception as exc:
            reported_effect = str(getattr(exc, "effect_state", "unknown") or "unknown")
            if method in {"execute", "prepare"}:
                # A write RPC whose response was lost is never replayed here.
                # completed still needs verify so the missing Host receipt can
                # be reconstructed from the Provider effect ledger.
                status = "reconcile_required"
                effect_state = reported_effect if reported_effect in {"completed", "unknown"} else "unknown"
            else:
                status = "retry"
                effect_state = "none"
            self.store.record_task_failure(
                task_id, status=status, effect_state=effect_state,
                error_code=str(getattr(exc, "code", type(exc).__name__)),
                category=str(getattr(exc, "category", "runtime")),
                retryable=bool(getattr(exc, "retryable", method == "invoke_read")),
            )
            raise
        values = result.get("values", result) if isinstance(result, dict) else {}
        effect_state = str(values.get("effect_state", "none"))
        self.store.set_task_result(task_id, status="succeeded", effect_state=effect_state,
                                   result=values)
        return result

    def reconcile_task(self, supervisor: FixtureSupervisor, task_id: str) -> dict:
        task = self.store.task(task_id)
        if task is None:
            raise StoreError("task_not_found")
        context = self._task_context(task, "verify")
        verify_params = json.loads(task["input_json"])
        try:
            result = self._call(supervisor, "verify", verify_params, context=context)
        except Exception as exc:
            self.store.record_task_failure(
                task_id, status="reconcile_required",
                effect_state=str(task["effect_state"] or "unknown"),
                error_code=str(getattr(exc, "code", type(exc).__name__)),
                category=str(getattr(exc, "category", "runtime")), retryable=False)
            raise
        values = result.get("values", result) if isinstance(result, dict) else {}
        found = values.get("found") is True or values.get("effect_state") in {"completed", "synthetic"}
        if not found and task.get("operation_id"):
            from app.runtime.engine_host.store import SyntheticEffectLedger
            ledger = SyntheticEffectLedger(self.effect_store_path)
            try:
                if ledger.verify(task["operation_id"], json.loads(task["input_json"])).get("verified") is True:
                    found = True
            finally:
                ledger.close()
        if found:
            durable_result = values.get("result") if isinstance(values.get("result"), dict) else result
            self.store.set_task_result(task_id, status="succeeded", effect_state="completed",
                                       result=durable_result, reconcile=True)
        else:
            self.store.set_task_status(task_id, status="reconcile_required", effect_state="unknown")
        return result

    def recover_owned_launches(self) -> list[dict]:
        """Clean only identity-matched persisted ownership before relaunch."""
        try:
            from app.runtime.engine_host.ownership import cleanup_owned
        except ImportError:
            return [{"launch_instance_id": row["launch_instance_id"], "identity_matched": False,
                     "blocked_reasons": ["ownership_api_unavailable"]}
                    for row in self.store.launches(states=("running", "draining"))]
        reports = []
        for row in self.store.launches(states=("running", "draining")):
            record = {
                "pid": int(row["pid"]), "start_time": float(row["start_time"]),
                "pgid": int(row["pgid"]), "uid": int(row["uid"]),
                "owner_token": row["owner_token"],
                "launch_instance_id": row["launch_instance_id"],
                "socket_path": row["socket_path"], "ipc_root": row["ipc_root"],
                "workdir": row["workdir"], "package_digest": row["package_digest"],
                "members": json.loads(row.get("members_json") or "[]"),
            }
            report = cleanup_owned(record, terminate=True, timeout=2.0)
            resources_clean = (report.get("identity_matched") is True and
                               not report.get("processes_remaining") and
                               not report.get("sockets_remaining") and
                               not report.get("blocked_reasons"))
            snapshot_clean, snapshot_reason, snapshot_path = (
                self._remove_owned_runtime_snapshot(row) if resources_clean else
                (False, "owned_resource_cleanup_incomplete", row["staging_dir"]))
            if snapshot_clean:
                report.setdefault("removed", []).append(snapshot_path)
            else:
                report.setdefault("blocked_reasons", []).append(snapshot_reason)
            reports.append({"launch_instance_id": row["launch_instance_id"], **report})
            if resources_clean and snapshot_clean:
                self.store.mark_launch(row["launch_instance_id"], state="recovered",
                                       recovery_state="cleaned")
                self.store.mark_staged_artifact(
                    launch_instance_id=row["launch_instance_id"], state="cleaned",
                    retained_reason="crash_recovery_identity_matched_snapshot_removed")
            else:
                self.store.mark_launch(row["launch_instance_id"], state="blocked",
                                       recovery_state="identity_unverified")
                self.store.mark_staged_artifact(
                    launch_instance_id=row["launch_instance_id"], state="retained_audit",
                    retained_reason=snapshot_reason)
        return reports

    def _remove_owned_runtime_snapshot(self, launch: dict) -> tuple[bool, str, str]:
        """Remove one exact owned runtime snapshot, never an inferred path."""
        workdir = Path(str(launch.get("workdir", "")))
        staging = Path(str(launch.get("staging_dir", "")))
        path_text = str(staging)
        try:
            if (not workdir.is_absolute() or workdir.is_symlink() or not workdir.is_dir() or
                    workdir.stat().st_uid != os.getuid()):
                raise StoreError("runtime_snapshot_workdir_untrusted")
            marker_path = workdir / "OWNERSHIP"
            if marker_path.is_symlink() or not marker_path.is_file():
                raise StoreError("runtime_snapshot_owner_marker_missing")
            marker = json.loads(marker_path.read_text())
            if (marker.get("schema") != "tmengine.owner/v1" or
                    marker.get("token") != launch.get("owner_token") or
                    marker.get("uid") != int(launch.get("uid")) or
                    marker.get("host_pid") != int(launch.get("host_pid"))):
                raise StoreError("runtime_snapshot_owner_mismatch")
            if not staging.is_absolute() or staging.is_symlink():
                raise StoreError("runtime_snapshot_path_untrusted")
            runtime_root = workdir / "runtime"
            expected_name = str(launch.get("package_digest", "")).removeprefix("sha256:")
            if (runtime_root.is_symlink() or staging.parent != runtime_root or
                    len(expected_name) != 64 or
                    any(character not in "0123456789abcdef" for character in expected_name) or
                    staging.name != expected_name):
                raise StoreError("runtime_snapshot_identity_mismatch")
            artifacts = [item for item in self.store.launch_artifacts()
                         if item.get("launch_instance_id") == launch.get("launch_instance_id")]
            if len(artifacts) != 1 or Path(artifacts[0]["path"]) != staging:
                raise StoreError("runtime_snapshot_artifact_mismatch")
            if staging.exists():
                entries = tuple(staging.rglob("*"))
                if (not staging.is_dir() or staging.stat().st_uid != int(launch["uid"]) or
                        any(item.is_symlink() or item.lstat().st_uid != int(launch["uid"])
                            for item in entries)):
                    raise StoreError("runtime_snapshot_contents_untrusted")
                # Runtime snapshots are intentionally read-only while active.
                # Ownership is fully established above, so only these exact
                # directories are made traversable for their final removal.
                for directory in (staging, *(item for item in entries if item.is_dir())):
                    os.chmod(directory, 0o700)
                shutil.rmtree(staging)
            if staging.exists():
                raise StoreError("runtime_snapshot_remove_failed")
            return True, "crash_recovery_identity_matched_snapshot_removed", path_text
        except (StoreError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return False, getattr(exc, "code", type(exc).__name__), path_text

    @staticmethod
    def _task_context(task: dict, method: str) -> dict:
        return {
            "operation_id": task["operation_id"] or task["task_id"],
            "input_digest": task["input_digest"], "generation": task["generation"],
            "package_digest": task["digest"], "attempt": task["attempt"],
            "fence_epoch": task["fence"],
            "capability_id": "test.effect" if method in {"prepare", "execute", "verify"}
                             else "test.read",
            "scope": json.loads(task["authorization_json"]),
            "deadline": float(task["deadline"]),
        }

    def recover_task(self, task_id: str, package) -> dict:
        task = self.store.task(task_id)
        if task is None:
            raise StoreError("task_not_found")
        identity = self.package_identity(package)
        if identity["package_digest"] != task["digest"]:
            raise StoreError("recovery_package_identity_mismatch")
        supervisor = self._start(package, task["generation"], label=f"recover-{task_id}",
                                 expected_digest=task["digest"])
        try:
            if task["status"] == "reconcile_required" or task["effect_state"] == "unknown":
                result = self.reconcile_task(supervisor, task_id)
            elif task["status"] in {"queued", "running", "retry"}:
                result = self.execute_task(supervisor, task_id)
            else:
                result = self.store.read_result(task_id)
            return {"task": self.store.task(task_id), "result": result}
        finally:
            self._stop(supervisor)

    def _run_cycle(self, v1, v2, *, cycle: int, observer, events: list[dict]) -> dict:
        current = self.store.active("test.fake")
        if current is None:
            current = self.store.bootstrap_active(self._package_record(v1, 1, "active"), generation=1)
        if current.digest != v1.digest:
            raise StoreError("cycle_requires_v1_active")
        generation = current.generation
        old = self._start(v1, generation, label=f"c{cycle}-old", expected_digest=current.digest)
        candidate = rollback_host = None
        cycle_report = {"cycle": cycle, "start_generation": generation,
                        "old_digest": v1.digest, "new_digest": v2.digest,
                        "tasks": {}, "operations": {}}
        try:
            statuses = ("queued", "running", "retry", "reconcile_required")
            for status in statuses:
                task_id = f"c{cycle}-old-{status}-{uuid.uuid4().hex[:8]}"
                effect = "unknown" if status == "reconcile_required" else "none"
                payload = ({"value": f"reconcile-{cycle}"} if status == "reconcile_required"
                           else {"echo": f"{status}-{cycle}",
                                 **({"sleep": 0.5} if status == "running" else {})})
                self.store.prepare_task(task_id=task_id, engine_id="test.fake",
                                        payload=payload, status=status, effect_state=effect,
                                        operation_id=f"effect-{task_id}")
                cycle_report["tasks"][status] = task_id
            # This Future is genuinely executing in V1 when the routing CAS occurs.
            running = cycle_report["tasks"]["running"]
            running_task = self.store.task(running)
            running_future = old.submit(
                "invoke_read", json.loads(running_task["input_json"]), timeout=5,
                context=self._task_context(running_task, "invoke_read"))
            inflight_deadline = time.monotonic() + 1
            observed_inflight = 0
            while time.monotonic() < inflight_deadline:
                observed_inflight = int(old.heartbeat().get("inflight", 0))
                if observed_inflight > 0:
                    break
                if running_future.done():
                    raise StoreError("old_running_completed_before_cas")
                time.sleep(0.005)
            if observed_inflight <= 0:
                raise StoreError("old_running_not_observed_inflight")
            cycle_report["running_observed_inflight"] = observed_inflight

            # Commit a real V1 synthetic effect but intentionally omit its Host
            # receipt; post-CAS recovery must use verify with the same operation.
            reconcile_id = cycle_report["tasks"]["reconcile_required"]
            reconcile_task = self.store.task(reconcile_id)
            self._call(old, "execute", json.loads(reconcile_task["input_json"]),
                       context=self._task_context(reconcile_task, "execute"))

            identity2 = self.package_identity(v2)
            operation = self.updater.prepare(
                engine_id="test.fake", digest=v2.digest, expected_generation=generation,
                idempotency_key=f"cycle:{cycle}:update:{generation}", version=v2.version,
                generation=generation + 1, payload_digest=identity2["payload_digest"],
                runtime_digest=identity2["runtime_digest"], sdk_version=identity2["sdk_version"],
                task_schema=identity2["task_schema"], result_schema=identity2["result_schema"],
                capability_version=identity2["capability_version"], identity=identity2)
            cycle_report["operations"]["update"] = operation.operation_id
            self.updater.verified(operation.operation_id, identity2)
            def candidate_hook(phase: str, detail: dict) -> None:
                row = self.store.operation(operation.operation_id)
                if phase == "staged" and row["state"] == "verified":
                    self.store.record_staged_artifact(
                        operation_id=operation.operation_id,
                        path=detail["staging_dir"], workdir=detail["workdir"],
                        state="staged", retained_reason="awaiting_provider_launch")
                    self.updater.staged(operation.operation_id, detail)
                elif phase == "provider_started" and row["state"] == "staged":
                    launch = self.store.save_launch(
                        engine_id="test.fake", generation=generation + 1,
                        package_digest=v2.digest, record=detail,
                        workdir=detail.get("workdir"))
                    self.updater.candidate_started(operation.operation_id, launch)

            # The phase hook makes the staged/started journal reflect completed
            # Supervisor work instead of pre-writing optimistic states.
            if "phase_hook" not in inspect.signature(FixtureSupervisor).parameters:
                raise StoreError("supervisor_phase_hook_required")
            candidate = self._start(v2, generation + 1, label=f"c{cycle}-candidate",
                                    expected_digest=v2.digest, phase_hook=candidate_hook)
            launch = self.store.launch(candidate.record.launch_instance_id)
            if self.store.operation(operation.operation_id)["state"] == "staged":
                self.updater.candidate_started(operation.operation_id, launch or {})
            handshake = self._call(candidate, "handshake")
            self.updater.handshake_complete(operation.operation_id, handshake)
            selftest = self._call(candidate, "selftest")
            self.updater.selftest_complete(operation.operation_id, selftest)
            both = self._runtime_sample({"old": old, "candidate": candidate})
            admission = self._capacity_admission(v1, v2, both)
            cycle_report["capacity_admission"] = admission
            self.updater.candidate_ready(operation.operation_id,
                                         capacity_receipt=admission)
            if running_future.done():
                raise StoreError("old_running_completed_before_cas")
            self._observe(observer, {"phase": "dual_ready", "cycle": cycle,
                                     "generation": generation, "runtime": both,
                                     "capacity_admission": admission})
            active = self.updater.activate(operation.operation_id)
            cycle_report["activated_generation"] = active.generation
            events.append({"step": "activate", "cycle": cycle, "generation": active.generation,
                           "digest": active.digest, "operation_id": operation.operation_id})

            new_task = f"c{cycle}-new-{uuid.uuid4().hex[:8]}"
            self.store.prepare_task(task_id=new_task, engine_id="test.fake",
                                    payload={"echo": f"new-{cycle}"})
            new_started = time.monotonic_ns()
            new_result = self.execute_task(candidate, new_task)
            self.store.release_task_reference(new_task)
            new_duration_ms = (time.monotonic_ns() - new_started) / 1_000_000
            cycle_report["tasks"]["new"] = new_task
            cycle_report["new_invoke_duration_ms"] = new_duration_ms
            self._observe(observer, {"phase": "new_task_complete", "cycle": cycle,
                                     "generation": active.generation,
                                     "invoke_duration_ms": new_duration_ms,
                                     "runtime": self._runtime_sample({"old": old,
                                                                      "candidate": candidate})})
            assert self.store.task(new_task)["digest"] == v2.digest

            effect_task = f"c{cycle}-effect-{uuid.uuid4().hex[:8]}"
            effect_operation = f"cycle-effect-{cycle}-{uuid.uuid4().hex}"
            self.store.prepare_task(task_id=effect_task, engine_id="test.fake",
                                    payload={"value": f"cycle-{cycle}"},
                                    operation_id=effect_operation)
            effect_result = self.execute_task(candidate, effect_task, method="execute")
            self.store.release_task_reference(effect_task)
            cycle_report["tasks"]["effect"] = effect_task

            # Old work remains on the precise old process/digest after cutover.
            try:
                running_envelope = running_future.result(timeout=3)
                running_result = running_envelope["values"]
            except Exception as exc:
                self.store.record_task_failure(
                    running, status="retry", effect_state="none",
                    error_code=str(getattr(exc, "code", type(exc).__name__)),
                    category=str(getattr(exc, "category", "runtime")), retryable=True)
                raise
            self.store.set_task_result(running, status="succeeded", effect_state="none",
                                       result=running_result)
            for status in ("queued", "retry"):
                self.execute_task(old, cycle_report["tasks"][status])
            # The old effect is verified, never executed a second time.
            reconcile_receipt = self.reconcile_task(
                old, cycle_report["tasks"]["reconcile_required"])
            if not (reconcile_receipt.get("found") is True and
                    reconcile_receipt.get("effect_state") == "completed"):
                raise StoreError("old_reconcile_verify_missing")
            for status in statuses:
                task = self.store.task(cycle_report["tasks"][status])
                if task["digest"] != v1.digest or task["generation"] != generation:
                    raise StoreError("old_task_rebound")
                if task["status"] == "succeeded":
                    self.store.release_task_reference(task["task_id"])
            remaining_refs = self.store.generation_references(
                "test.fake", generation, v1.digest)
            old_heartbeat = old.heartbeat()
            if remaining_refs:
                raise StoreError("drain_blocked_references")
            if int(old_heartbeat.get("inflight", -1)) != 0:
                raise StoreError("drain_blocked_inflight")
            cycle_report["old_pre_drain"] = {
                "references": 0, "inflight": 0,
                "reconcile_effect_state": "completed"}
            drain_started = time.monotonic_ns()
            drain_receipt = self._call(old, "drain")
            drain_duration_ms = (time.monotonic_ns() - drain_started) / 1_000_000
            cycle_report["old_drain_duration_ms"] = drain_duration_ms
            self.updater.drain(operation.operation_id, drain_receipt)
            self.store.mark_launch(old.record.launch_instance_id, state="draining")
            self._observe(observer, {"phase": "old_drained", "cycle": cycle,
                                     "generation": generation,
                                     "drain_duration_ms": drain_duration_ms,
                                     "runtime": self._runtime_sample({"old": old,
                                                                      "candidate": candidate})})
            self._stop(old)
            self.updater.retain(operation.operation_id,
                                {"digest": v1.digest, "references": len(self.store.references("test.fake", v1.digest))})
            self.updater.complete(operation.operation_id)

            # Rollback is a full candidate transaction too: exact package,
            # process start, handshake, selftest and capacity admission all
            # complete before its fenced routing CAS.
            identity1 = self.package_identity(v1)
            rollback_operation = self.updater.prepare_rollback(
                engine_id="test.fake", digest=v1.digest,
                expected_generation=active.generation,
                idempotency_key=f"cycle:{cycle}:rollback:{active.generation}",
                identity=identity1)
            cycle_report["operations"]["rollback"] = rollback_operation.operation_id
            self.updater.verified(rollback_operation.operation_id, identity1)

            def rollback_hook(phase: str, detail: dict) -> None:
                row = self.store.operation(rollback_operation.operation_id)
                if phase == "staged" and row["state"] == "verified":
                    self.store.record_staged_artifact(
                        operation_id=rollback_operation.operation_id,
                        path=detail["staging_dir"], workdir=detail["workdir"],
                        state="staged", retained_reason="awaiting_rollback_provider_launch")
                    self.updater.staged(rollback_operation.operation_id, detail)
                elif phase == "provider_started" and row["state"] == "staged":
                    launch = self.store.save_launch(
                        engine_id="test.fake", generation=active.generation + 1,
                        package_digest=v1.digest, record=detail,
                        workdir=detail.get("workdir"))
                    self.updater.candidate_started(rollback_operation.operation_id, launch)

            rollback_host = self._start(
                v1, active.generation + 1, label=f"c{cycle}-rollback",
                expected_digest=v1.digest, phase_hook=rollback_hook)
            rollback_launch = self.store.launch(rollback_host.record.launch_instance_id)
            if self.store.operation(rollback_operation.operation_id)["state"] == "staged":
                self.updater.candidate_started(
                    rollback_operation.operation_id, rollback_launch or {})
            rb_handshake = self._call(rollback_host, "handshake")
            if rb_handshake.get("provider_version") not in (None, v1.version):
                raise StoreError("rollback_handshake_identity_mismatch")
            self.updater.handshake_complete(rollback_operation.operation_id, rb_handshake)
            rb_selftest = self._call(rollback_host, "selftest")
            self.updater.selftest_complete(rollback_operation.operation_id, rb_selftest)
            rollback_sample = self._runtime_sample(
                {"old": candidate, "candidate": rollback_host})
            rollback_admission = self._capacity_admission(v1, v2, rollback_sample)
            self.updater.candidate_ready(
                rollback_operation.operation_id, capacity_receipt=rollback_admission)
            rollback = self.updater.activate(rollback_operation.operation_id)
            cycle_report["rollback_capacity_admission"] = rollback_admission
            cycle_report["rollback_generation"] = rollback.generation
            rollback_task = f"c{cycle}-rollback-{uuid.uuid4().hex[:8]}"
            self.store.prepare_task(task_id=rollback_task, engine_id="test.fake",
                                    payload={"echo": f"rollback-{cycle}"})
            rollback_result = self.execute_task(rollback_host, rollback_task)
            self.store.release_task_reference(rollback_task)
            cycle_report["tasks"]["rollback"] = rollback_task
            events.append({"step": "rollback", "cycle": cycle,
                           "generation": rollback.generation, "digest": rollback.digest,
                           "operation_id": rollback.operation_id})
            self._observe(observer, {"phase": "rollback_complete", "cycle": cycle,
                                     "generation": rollback.generation,
                                     "runtime": self._runtime_sample({"candidate": candidate,
                                                                      "rollback": rollback_host})})
            cycle_report["results"] = {"new": new_result, "effect": effect_result,
                                       "rollback": rollback_result}
            return cycle_report
        finally:
            for supervisor in (old, candidate, rollback_host):
                if supervisor is not None:
                    try:
                        self._stop(supervisor)
                    except Exception:
                        pass

    def run(self, v1, v2, *, cycles: int = 1,
            observer: Callable[[dict], None] | None = None) -> dict:
        if cycles <= 0:
            raise ValueError("cycles must be positive")
        identity1, identity2 = self.package_identity(v1), self.package_identity(v2)
        if identity1["package_digest"] == identity2["package_digest"]:
            raise StoreError("versions_not_distinct")
        if identity1["engine_id"] != "test.fake" or identity2["engine_id"] != "test.fake":
            raise StoreError("fixture_engine_mismatch")
        if self.store.active("test.fake") is None:
            self.store.bootstrap_active(self._package_record(v1, 1, "active"), generation=1)
        cycle_reports, events = [], []
        for cycle in range(1, cycles + 1):
            cycle_reports.append(self._run_cycle(v1, v2, cycle=cycle,
                                                 observer=observer, events=events))
        active = self.store.active("test.fake")
        return {
            "events": events, "cycles": cycle_reports,
            "active": asdict(active) if active else None,
            "tasks": self.store.tasks(engine_id="test.fake"),
            "launches": self.store.launches(),
            "results": [self.store.result(task["task_id"]) for task in self.store.tasks(engine_id="test.fake")
                        if self.store.result(task["task_id"]) is not None],
        }
