"""Single-node platform/Provider update transaction over real process controls.

The release gate accepts only verified ReleaseManager payloads in deployment
mode. Fixture mode is explicit and cannot produce release qualification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.runtime.platform_router import Backend, RouterState, control_request
from app.runtime.release_checkpoint import ReleaseCheckpoint
from app.runtime.release_manager import ReleaseManager, verify_release

RECEIPTS = ("single-provider-capacity-v0", "dual-generation-capacity-v0", "platform-ab-budget-v0")


class UpdateRefused(RuntimeError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Slot:
    generation: str
    release_root: Path
    python: Path
    port: int
    release_id: str
    artifact_digest: str
    receipt_path: Path
    migration: dict


def slots_from_config(config: dict) -> dict[str, Slot]:
    result = {}
    for raw in config["slots"]:
        name = raw["generation"]
        root = Path(raw["release_root"]).expanduser().resolve()
        if name in result or name not in {"A", "B"} or not (root / "scripts/platform_role.py").is_file():
            raise UpdateRefused("invalid_platform_slot")
        if config.get("scope") == "release":
            manager = ReleaseManager(config["install_root"])
            if root != manager.releases_dir / raw["release_id"]:
                raise UpdateRefused("release_slot_not_installed")
            manifest = verify_release(root, trusted_public_keys=config.get("trusted_public_keys", ()),
                                      require_signature=True)
            if manifest["release_id"] != raw["release_id"]:
                raise UpdateRefused("release_manifest_identity_mismatch")
            artifact = digest(manifest)
        elif config.get("scope") == "fixture" and config.get("allow_fixture") is True:
            marker = root / "build-marker"
            if not marker.is_file():
                raise UpdateRefused("fixture_build_marker_missing")
            artifact = "sha256:" + hashlib.sha256(
                (root / "scripts/platform_role.py").read_bytes() + marker.read_bytes()).hexdigest()
        else:
            raise UpdateRefused("unsupported_release_scope")
        receipt_path = Path(raw.get("migration_receipt") or config["migration_receipt"]).expanduser().resolve()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        migration = {key: receipt.get(key) for key in ("revision", "checksum", "source_checksum")}
        if any(not isinstance(value, str) or not value for value in migration.values()):
            raise UpdateRefused("slot_migration_receipt_incomplete")
        result[name] = Slot(name, root, Path(raw["python"]).expanduser().absolute(),
                            int(raw["port"]), raw["release_id"], artifact,
                            receipt_path, migration)
    if set(result) != {"A", "B"} or result["A"].release_root == result["B"].release_root:
        raise UpdateRefused("two_distinct_platform_slots_required")
    return result


def _available_memory_bytes() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except ImportError:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))


def verify_capacity_receipts(config: dict, candidate: Slot, provider_digest: str) -> dict:
    paths = config.get("capacity_receipts") or {}
    if set(paths) != set(RECEIPTS):
        raise UpdateRefused("three_exact_capacity_receipts_required")
    expected = {
        "profile": config["profile"], "platform_artifact_digest": candidate.artifact_digest,
        "provider_package_digest": provider_digest,
        "host_context_digest": config["host_context_digest"],
        "driver_digest": config["driver_digest"], "load_digest": config["load_digest"],
        "os": platform.system().lower(), "cpu": platform.machine().lower(),
    }
    observed = {}
    for name in RECEIPTS:
        value = json.loads(Path(paths[name]).read_text(encoding="utf-8"))
        if value.get("output") != name or value.get("admitted") is not True or value.get("frozen") is not True:
            raise UpdateRefused(f"capacity_receipt_not_admitted:{name}")
        if any(value.get(key) != item for key, item in expected.items()):
            raise UpdateRefused(f"capacity_receipt_profile_mismatch:{name}")
        if not all(isinstance(value.get(key), (int, float)) and value[key] >= 0 for key in
                   ("startup_seconds", "drain_seconds", "rss_peak_bytes", "process_peak",
                    "connection_peak", "fd_peak")):
            raise UpdateRefused(f"capacity_receipt_measurements_missing:{name}")
        thresholds = value.get("thresholds")
        if not isinstance(thresholds, dict) or any(
            not isinstance(thresholds.get(key), (int, float)) or value[key] > thresholds[key]
            for key in ("startup_seconds", "drain_seconds", "rss_peak_bytes",
                        "process_peak", "connection_peak", "fd_peak")
        ):
            raise UpdateRefused(f"capacity_receipt_threshold_exceeded:{name}")
        signature = value.get("signature")
        unsigned = {key: item for key, item in value.items() if key not in {"digest", "signature"}}
        if value.get("digest") != digest(unsigned):
            raise UpdateRefused(f"capacity_receipt_digest_mismatch:{name}")
        if config.get("scope") == "fixture":
            if value.get("scope") != "fixture" or config.get("allow_fixture") is not True:
                raise UpdateRefused("fixture_capacity_scope_invalid")
        else:
            if value.get("scope") != "qualified" or not isinstance(signature, dict):
                raise UpdateRefused("qualified_capacity_signature_required")
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            public = Path(config["capacity_trusted_public_key"]).read_bytes()
            key = serialization.load_pem_public_key(public)
            if not isinstance(key, Ed25519PublicKey):
                raise UpdateRefused("capacity_trust_key_invalid")
            try:
                key.verify(base64.b64decode(signature["ed25519"], validate=True),
                           value["digest"].encode())
            except Exception as exc:
                raise UpdateRefused("capacity_signature_invalid") from exc
            if not value.get("qa_frozen_by") or not value.get("ops_frozen_by"):
                raise UpdateRefused("capacity_qa_ops_freeze_missing")
        observed[name] = value
    budget = observed["platform-ab-budget-v0"]
    needed = budget.get("required_available_memory_bytes")
    if not isinstance(needed, int) or needed <= 0 or _available_memory_bytes() < needed:
        raise UpdateRefused("platform_memory_budget_insufficient")
    needed_disk = budget.get("required_free_disk_bytes")
    if (not isinstance(needed_disk, int) or needed_disk <= 0 or
            shutil.disk_usage(Path(paths["platform-ab-budget-v0"]).parent).free < needed_disk):
        raise UpdateRefused("platform_disk_budget_insufficient")
    fd_limit = budget.get("required_fd_soft_limit")
    if not isinstance(fd_limit, int) or fd_limit <= 0 or resource.getrlimit(resource.RLIMIT_NOFILE)[0] < fd_limit:
        raise UpdateRefused("platform_fd_budget_insufficient")
    if budget.get("components") != ["platform_A", "platform_B", "router", "worker", "provider"]:
        raise UpdateRefused("platform_budget_components_incomplete")
    return {name: value["digest"] for name, value in observed.items()}


def plan_path(graph: dict, start: tuple[str, str], target: tuple[str, str]) -> list[list[str]]:
    raw_nodes = graph.get("nodes", [])
    raw_edges = graph.get("edges", [])
    if any(not isinstance(item, list) or len(item) != 2 or
           any(not isinstance(value, str) or not value for value in item) for item in raw_nodes):
        raise UpdateRefused("invalid_compatibility_node")
    if any(not isinstance(pair, list) or len(pair) != 2 or
           any(not isinstance(item, list) or len(item) != 2 for item in pair) for pair in raw_edges):
        raise UpdateRefused("invalid_directed_compatibility_edge")
    nodes = {tuple(item) for item in raw_nodes}
    edges = [(tuple(pair[0]), tuple(pair[1])) for pair in raw_edges]
    if start not in nodes or target not in nodes:
        raise UpdateRefused("combination_outside_compatibility_graph")
    for source, destination in edges:
        if source not in nodes or destination not in nodes or sum(a != b for a, b in zip(source, destination)) != 1:
            raise UpdateRefused("invalid_directed_compatibility_edge")
    frontier = [[start]]
    visited = {start}
    while frontier:
        path = frontier.pop(0)
        if path[-1] == target:
            return [list(item) for item in path]
        for source, destination in edges:
            if source == path[-1] and destination not in visited:
                visited.add(destination)
                frontier.append([*path, destination])
    raise UpdateRefused("no_compatible_directed_update_path")


class LocalLease:
    """Persistent epoch lease; SQLite serializes takeover after owner expiry."""

    def __init__(self, path: Path, ttl: float = 5):
        self.path, self.ttl = path, ttl
        self.owner = uuid.uuid4().hex
        self.epoch = 0
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, epoch INTEGER NOT NULL, expires REAL NOT NULL)")
        return db

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,epoch,expires FROM lease WHERE id=1").fetchone()
            if row and row[2] > time.time():
                raise UpdateRefused("platform_update_lease_held")
            self.epoch = (row[1] if row else 0) + 1
            db.execute("INSERT INTO lease(id,owner,epoch,expires) VALUES(1,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,epoch=excluded.epoch,expires=excluded.expires",
                       (self.owner, self.epoch, time.time() + self.ttl))
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stop.wait(self.ttl / 3):
            try:
                with self._connect() as db:
                    cursor = db.execute("UPDATE lease SET expires=? WHERE id=1 AND owner=? AND epoch=? AND expires>?",
                                        (time.time() + self.ttl, self.owner, self.epoch, time.time()))
                    if cursor.rowcount != 1:
                        self.lost.set()
                        return
            except sqlite3.Error:
                self.lost.set()
                return

    def assert_current(self):
        if self.lost.is_set():
            raise UpdateRefused("platform_update_lease_lost")
        with self._connect() as db:
            row = db.execute("SELECT owner,epoch,expires FROM lease WHERE id=1").fetchone()
        if row is None or row[0] != self.owner or row[1] != self.epoch or row[2] <= time.time():
            raise UpdateRefused("platform_update_lease_lost")

    def release(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        with self._connect() as db:
            db.execute("UPDATE lease SET expires=0 WHERE id=1 AND owner=? AND epoch=?",
                       (self.owner, self.epoch))

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class PlatformUpdate:
    def __init__(self, config: dict):
        self.config = config
        self.root = Path(config["transaction_root"]).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = slots_from_config(config)
        self.checkpoint = ReleaseCheckpoint(self.root)
        self.lease_path = self.root / "platform-update-lease.sqlite"
        self.router_socket = Path(config["router_socket"])
        self.provider_socket = Path(config["provider_control_socket"])
        self.receipt_path = Path(config["migration_receipt"])
        self._candidate_process = None

    def _router(self, request: dict) -> dict:
        return control_request(self.router_socket, request)

    def _provider(self, request: dict) -> dict:
        return control_request(self.provider_socket, request)

    def actual(self, *, allow_unready_platform: bool = False) -> tuple[str, str]:
        route = self._router({"command": "status"})
        provider = self._provider({"command": "status"})
        if route.get("automatic_failover") is not False:
            raise UpdateRefused("combination_router_must_disable_blind_failover")
        generation = route.get("active")
        package = provider.get("active_digest")
        if generation not in self.slots or not isinstance(package, str) or not provider.get("pid"):
            raise UpdateRefused("runtime_identity_unknown")
        if not allow_unready_platform:
            self._router_probe(generation)
        self._provider_probe(package)
        return generation, package

    def _router_probe(self, generation: str) -> dict:
        slot = self.slots[generation]
        # Router has no probe control action: read the actual candidate HTTP
        # process directly, then compare to the schema receipt from the DB.
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", slot.port, timeout=3)
        try:
            conn.request("GET", "/health/ready/platform")
            response = conn.getresponse()
            body = json.loads(response.read(65536))
        finally:
            conn.close()
        from app.core.database import engine
        from app.core.config import settings
        from app.runtime.process_roles import verify_migration_receipt
        from app.migrations.contract import verify_schema_ready

        contract = verify_schema_ready(engine)
        verify_migration_receipt(self.receipt_path, contract, settings.DATABASE_URL)
        if (response.status != 200 or body.get("role") != "api" or
                body.get("generation") != generation or
                body.get("artifact_digest") != slot.artifact_digest or
                body.get("migration") != slot.migration):
            raise UpdateRefused("platform_runtime_or_migration_mismatch")
        return body

    def _provider_probe(self, package_digest: str) -> dict:
        value = self._provider({"command": "probe", "digest": package_digest})
        if (value.get("digest") != package_digest or value.get("ready") is not True
                or not isinstance(value.get("pid"), int)):
            raise UpdateRefused("provider_runtime_identity_mismatch")
        return value

    def _ensure_candidate(self, generation: str) -> dict:
        slot = self.slots[generation]
        try:
            return self._router_probe(generation)
        except OSError:
            pass
        log = (self.root / f"candidate-{generation}.log").open("ab", buffering=0)
        env = os.environ.copy()
        env["TRACEMIND_PROCESS_ROLE"] = "api"
        self._candidate_process = subprocess.Popen([
            str(slot.python), str(slot.release_root / "scripts/platform_role.py"),
            "api", "--receipt", str(slot.receipt_path),
            "--generation", generation, "--artifact-digest", slot.artifact_digest,
            "--port", str(slot.port),
        ], cwd=slot.release_root, env=env, stdout=log, stderr=subprocess.STDOUT,
           start_new_session=True)
        log.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._candidate_process.poll() is not None:
                raise UpdateRefused("candidate_process_exited")
            try:
                return self._router_probe(generation)
            except (OSError, ValueError):
                time.sleep(0.1)
        raise UpdateRefused("candidate_process_not_ready")

    def _switch_one(self, source: tuple[str, str], target: tuple[str, str], lease: LocalLease) -> None:
        lease.assert_current()
        if sum(a != b for a, b in zip(source, target)) != 1:
            raise UpdateRefused("two_pointers_cannot_switch_together")
        if source[0] != target[0]:
            self._router_probe(target[0])
            self._router({"command": "switch", "target": target[0], "expected": source[0],
                          "lease_owner": lease.owner, "lease_epoch": lease.epoch})
        else:
            self._provider_probe(target[1])
            self._provider({"command": "switch", "target": target[1], "expected": source[1],
                            "lease_owner": lease.owner, "lease_epoch": lease.epoch})
        if self.actual(allow_unready_platform=True) != target:
            raise UpdateRefused("post_switch_runtime_identity_mismatch")
        lease.assert_current()

    def _gate(self, target: tuple[str, str]) -> dict:
        slot = self.slots[target[0]]
        receipts = verify_capacity_receipts(self.config, slot, target[1])
        self._provider_probe(target[1])
        from app.runtime.execution_identity import validate_identity
        import psutil

        expected_identity = dict(validate_identity(self.config["worker_identity"]))
        if (expected_identity["core_release"] != slot.release_id or
                expected_identity["package_digest"] != target[1] or
                expected_identity["engine_id"] != self.config["engine_id"]):
            raise UpdateRefused("worker_identity_profile_mismatch")
        try:
            worker = json.loads(Path(self.config["worker_status_file"]).read_text(encoding="utf-8"))
            process = psutil.Process(worker["pid"])
            if (worker.get("identity") != expected_identity or
                    abs(float(worker["process_created_at"]) - process.create_time()) > 0.01 or
                    time.time() - float(worker["heartbeat_at"]) > 3 or
                    "platform_role.py" not in " ".join(process.cmdline()) or
                    "worker" not in process.cmdline()):
                raise UpdateRefused("worker_runtime_identity_mismatch")
        except (OSError, KeyError, ValueError, psutil.Error) as exc:
            raise UpdateRefused("worker_runtime_unavailable") from exc
        return receipts

    def _crash(self, point: str, selected: str | None):
        if selected == point:
            os._exit(77)

    def start(self, target: tuple[str, str], *, crash_after: str | None = None) -> dict:
        with LocalLease(self.lease_path) as lease:
            if self.checkpoint.current() is not None:
                raise UpdateRefused("unfinished_update_requires_recovery")
            start = self.actual(allow_unready_platform=True)
            path = plan_path(self.config["compatibility_graph"], start, target)
            if len(path) < 2:
                raise UpdateRefused("already_at_target")
            receipts = self._gate(target)
            transaction = self.checkpoint.begin({
                "kind": "platform-provider-update", "scope": self.config["scope"],
                "path": path, "next_step": 0, "capacity_receipts": receipts,
                "source_release_id": self.slots[start[0]].release_id,
                "target_release_id": self.slots[target[0]].release_id,
                "lease_epoch": lease.epoch,
            })
            self.checkpoint.record("prepared", "done")
            try:
                candidate = self._ensure_candidate(target[0])
                self.checkpoint.record("candidate_started", "ready",
                                       facts={"candidate_pid": candidate.get("pid")})
                self._crash("candidate_started", crash_after)
                return self._advance(path, 0, lease, crash_after)
            except Exception:
                self._compensate(path, lease)
                raise

    def _advance(self, path: list[list[str]], next_step: int, lease: LocalLease,
                 crash_after: str | None = None) -> dict:
        for index in range(next_step, len(path) - 1):
            source, target = tuple(path[index]), tuple(path[index + 1])
            current_receipts = self._gate(tuple(path[-1]))
            if current_receipts != self.checkpoint.current()["facts"]["capacity_receipts"]:
                raise UpdateRefused("capacity_receipts_changed_during_update")
            lease.assert_current()
            self.checkpoint.record("step_intent", "pending", facts={"next_step": index})
            self._crash(f"step_{index}_intent", crash_after)
            actual = self.actual(allow_unready_platform=True)
            if actual == source:
                self._switch_one(source, target, lease)
            elif actual != target:
                raise UpdateRefused("runtime_diverged_from_update_path")
            self._crash(f"step_{index}_effect", crash_after)
            self.checkpoint.record("step_applied", "done", facts={"next_step": index + 1})
            self._crash(f"step_{index}_record", crash_after)
        old = path[0][0]
        if old != path[-1][0]:
            drained = self._router({"command": "drain", "generation": old, "timeout": 10})
            if not drained.get("drained"):
                raise UpdateRefused("old_platform_not_drained")
            self.checkpoint.record("old_platform_drain", "done")
        final = self.actual()
        if final != tuple(path[-1]):
            raise UpdateRefused("final_runtime_identity_mismatch")
        self.checkpoint.record("complete", "done", terminal=True)
        return {"status": "complete", "active": list(final), "steps": len(path) - 1}

    def _compensate(self, path: list[list[str]], lease: LocalLease) -> None:
        try:
            current = self.actual(allow_unready_platform=True)
        except Exception:
            self.checkpoint.record("compensation", "blocked", facts={"reason": "runtime_unobservable"})
            return
        tuples = [tuple(item) for item in path]
        if current not in tuples:
            self.checkpoint.record("compensation", "blocked", facts={"reason": "runtime_diverged"})
            return
        index = tuples.index(current)
        try:
            for step in range(index, 0, -1):
                self.checkpoint.record("compensation_intent", "pending", facts={"compensating_step": step})
                self._switch_one(tuples[step], tuples[step - 1], lease)
                self.checkpoint.record("compensation_step", "done", facts={"compensating_step": step - 1})
            self.checkpoint.record("compensated", "done", terminal=True)
        except Exception:
            self.checkpoint.record("compensation", "blocked", facts={"reason": "manual_reconcile_required"})

    def recover(self, *, crash_after: str | None = None) -> dict:
        with LocalLease(self.lease_path) as lease:
            current = self.checkpoint.current()
            if current is None:
                raise UpdateRefused("no_unfinished_update")
            facts = current["facts"]
            if facts.get("kind") != "platform-provider-update":
                raise UpdateRefused("wrong_transaction_kind")
            path = facts["path"]
            if any(str(event.get("phase", "")).startswith("compensation") for event in current["events"]):
                self._compensate(path, lease)
                remaining = self.checkpoint.current()
                return {"status": "compensated" if remaining is None else "manual_reconcile_required",
                        "active": list(self.actual())}
            target = tuple(path[-1])
            if self._gate(target) != facts.get("capacity_receipts"):
                raise UpdateRefused("capacity_receipts_changed_during_recovery")
            self._ensure_candidate(target[0])
            index = int(facts["next_step"])
            actual = self.actual(allow_unready_platform=True)
            if index + 1 < len(path) and actual == tuple(path[index + 1]):
                self.checkpoint.record("reconciled_effect", "done", facts={"next_step": index + 1,
                                                                           "lease_epoch": lease.epoch})
                index += 1
            elif actual != tuple(path[index]):
                raise UpdateRefused("runtime_diverged_from_journal")
            return self._advance(path, index, lease, crash_after)
