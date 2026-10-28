"""Local administrative daemon for signed engine packages.

The socket is separate from platform login. Production construction requires
trusted repository, supervisor, capacity, OS policy and handoff injections.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import struct
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import psutil

from app.runtime.engine_packs.archive import ArchiveError, _no_link_path, _regular_open, open_verified_archive
from app.runtime.engine_packs.repository import EngineRepository, LabRepository
from app.runtime.engine_packs.trust import TrustError, parse_manifest_json
from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator, FixturePackage
from app.runtime.engine_host.store import FixtureStore, Package
from app.runtime.engine_host.supervisor import FixtureSupervisor
from app.runtime.engine_host.update import FixtureUpdater
from app.runtime.engine_sdk.wire import canonical, peer_credentials, strict_json

SCHEMA = "tmengine.management/v1"
METHODS = frozenset({"list", "inspect", "verify", "install", "initial-activate", "status", "recover", "initial-rollback",
                     "upgrade", "rollback", "disable", "uninstall", "gc"})
MAX_FRAME = 65536
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REQUEST = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def management_socket_path(repository_root):
    root = Path(repository_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ManagementError("management_repository_root_invalid")
    name = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    directory = Path("/tmp").resolve() / ("tmengine-management-" + name)
    return directory / "management.sock"


def _endpoint(manager):
    directory = manager.socket_path.parent
    expected_mode = 0o710 if manager.mode == "production" else 0o700
    created = not directory.exists() and not directory.is_symlink()
    if created:
        directory.mkdir(mode=expected_mode)
    else:
        existing = directory.lstat()
        if (not stat.S_ISDIR(existing.st_mode) or existing.st_uid != manager.service_uid
                or existing.st_mode & 0o777 != expected_mode
                or (manager.mode == "production" and existing.st_gid != manager.admin_gid)):
            raise ManagementError("management_socket_root_unsafe")
    if manager.mode == "production" and created:
        try:
            os.chown(directory, -1, manager.admin_gid)
        except OSError as exc:
            raise ManagementError("management_admin_group_unavailable") from exc
    if created:
        os.chmod(directory, expected_mode)
    st = directory.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != manager.service_uid
            or st.st_mode & 0o777 != expected_mode
            or (manager.mode == "production" and st.st_gid != manager.admin_gid)):
        raise ManagementError("management_socket_root_unsafe")
    descriptor = directory / "ENDPOINT.json"
    if descriptor.exists() or descriptor.is_symlink():
        dst = descriptor.lstat()
        if (dst.st_uid != manager.service_uid or dst.st_mode & 0o777 !=
                (0o640 if manager.mode == "production" else 0o600)
                or (manager.mode == "production" and dst.st_gid != manager.admin_gid)):
            raise ManagementError("management_endpoint_descriptor_unsafe")
        with _regular_open(descriptor) as source:
            prior = strict_json(source.read(MAX_FRAME + 1), max_bytes=MAX_FRAME)
        if prior.get("repository") != str(manager.root) or prior.get("server_uid") != manager.service_uid:
            raise ManagementError("management_socket_root_mismatch")
    value = {"schema": "tmengine.management-endpoint/v1", "repository": str(manager.root),
             "server_uid": manager.service_uid, "admin_gid": manager.admin_gid,
             "server_pid": os.getpid(), "server_start_time": psutil.Process().create_time(),
             "socket_mode": 0o660 if manager.mode == "production" else 0o600,
             "mode": manager.mode}
    temp = directory / (".ENDPOINT-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        if manager.mode == "production":
            try: os.chown(temp, -1, manager.admin_gid)
            except OSError as exc: raise ManagementError("management_admin_group_unavailable") from exc
            os.chmod(temp, 0o640)
        os.replace(temp, descriptor)
    finally:
        temp.unlink(missing_ok=True)
    return value


class ManagementError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _recv(conn):
    header = _exact(conn, 4)
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= MAX_FRAME:
        raise ManagementError("management_frame_limit")
    return strict_json(_exact(conn, size), max_bytes=MAX_FRAME)


def _exact(conn, count):
    out = bytearray()
    while len(out) < count:
        chunk = conn.recv(count - len(out))
        if not chunk:
            raise ManagementError("management_frame_truncated")
        out.extend(chunk)
    return bytes(out)


def _send(conn, value):
    data = canonical(value)
    if len(data) > MAX_FRAME:
        raise ManagementError("management_frame_limit")
    conn.sendall(struct.pack("!I", len(data)) + data)


def _private_file(path):
    path = _no_link_path(Path(path))
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ManagementError("management_policy_unsafe")
    return path


def load_policy(path, *, mode="lab"):
    with _regular_open(_private_file(path)) as source:
        raw = source.read(MAX_FRAME + 1)
    value = strict_json(raw, max_bytes=MAX_FRAME)
    expected = "tmengine.lab-management-policy/v1" if mode == "lab" else "tmengine.management-policy/v1"
    fields = {"schema", "allowed_uids", "allowed_methods"} if mode == "lab" else {
        "schema", "role", "allowed_uids", "allowed_methods"}
    if (set(value) != fields or value["schema"] != expected
            or (mode != "lab" and value.get("role") != "local-engine-admin")
            or not isinstance(value["allowed_uids"], list)
            or not isinstance(value["allowed_methods"], list)
            or not value["allowed_uids"] or not value["allowed_methods"]
            or any(type(x) is not int or x < 0 for x in value["allowed_uids"])
            or len(set(value["allowed_uids"])) != len(value["allowed_uids"])
            or any(type(x) is not str or x not in METHODS for x in value["allowed_methods"])
            or len(set(value["allowed_methods"])) != len(value["allowed_methods"])):
        raise ManagementError("management_policy_invalid")
    return value


class LabManagement:
    def __init__(self, repository_root, *, trusted_keys=(), policy_path, mode="lab", checkpoint=None,
                 capacity_budget=None, capacity_profile=None, capacity_candidate=None,
                 repository=None, host_factory=None, production_admission=None,
                 capacity_admitter=None, service_uid=None, admin_gid=None):
        if mode not in {"lab", "production"}:
            raise ManagementError("management_mode_invalid")
        self.mode = mode
        self._production_admission_cb = production_admission
        self.service_uid = os.getuid() if service_uid is None else service_uid
        self.admin_gid = admin_gid
        if type(self.service_uid) is not int or self.service_uid != os.getuid():
            raise ManagementError("management_service_uid_mismatch")
        if mode == "production":
            if (not isinstance(repository, EngineRepository) or repository.mode != "managed"
                    or repository.namespace != "tmengine.production" or repository.trust_guard is None
                    or not callable(host_factory) or not callable(production_admission)
                    or not callable(capacity_admitter) or type(admin_gid) is not int or admin_gid < 0):
                raise ManagementError("production_management_admission_missing")
            admission = production_admission(action="startup", manifest=None, package_digest=None)
            if (type(admission) is not dict or admission.get("accepted") is not True
                    or admission.get("namespace") != "tmengine.production"
                    or admission.get("fixture_only") is not False):
                raise ManagementError("production_management_admission_rejected")
            self.production_admission = admission
        else:
            if (repository is not None or host_factory is not None or production_admission is not None
                    or admin_gid is not None):
                raise ManagementError("management_lab_injection_forbidden")
            repository = LabRepository(repository_root, trusted_keys=trusted_keys)
            host_factory = FixtureSupervisor
            self.production_admission = None
        self.host_factory = host_factory
        self.capacity_admitter = capacity_admitter
        self.policy_path = Path(policy_path)
        self.policy = load_policy(self.policy_path, mode=mode)
        self.repo = repository
        if self.repo.root != _no_link_path(Path(repository_root)):
            raise ManagementError("management_repository_root_mismatch")
        if mode == "production":
            self._validate_production_admission()
        self.root = self.repo.root
        self.socket_path = management_socket_path(self.root)
        self.store = FixtureStore(self.root / "management-host.sqlite")
        self.db_path = self.root / "management.sqlite"
        if not self.db_path.exists():
            os.close(os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600))
        _private_file(self.db_path)
        self._lock = threading.RLock()
        self._hosts = {}
        self._candidate_hosts = {}
        self._retained_hosts = {}
        self.checkpoint = checkpoint or (lambda _point, _row: None)
        for evidence in (capacity_budget, capacity_profile, capacity_candidate):
            if evidence is not None:
                _private_file(evidence)
        self.orchestrator = FixtureUpdateOrchestrator(
            store=self.store, workdir=self.root / "management-orchestrator",
            fixture_budget=capacity_budget, capacity_profile=capacity_profile,
            capacity_candidate=capacity_candidate)
        self.updater = FixtureUpdater(self.store)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activations (
                  request_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                  engine_id TEXT NOT NULL, digest TEXT NOT NULL, generation INTEGER NOT NULL,
                  state TEXT NOT NULL, launch_instance_id TEXT, error_code TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL,
                  previous_digest TEXT NOT NULL, record_digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lifecycle (
                  request_id TEXT PRIMARY KEY, method TEXT NOT NULL, digest TEXT NOT NULL,
                  state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
        _private_file(self.db_path)
        self.audit()

    def _validate_production_admission(self):
        receipt = self.production_admission
        required = {"schema", "accepted", "namespace", "fixture_only", "handoff_digest",
                    "e07_status", "package_digest", "production_release_ready",
                    "admin_policy_digest", "trust_root_version", "service_uid", "admin_gid"}
        if (type(receipt) is not dict or set(receipt) != required
                or receipt["schema"] != "tmengine.production-management-admission/v1"
                or receipt["accepted"] is not True or receipt["namespace"] != "tmengine.production"
                or receipt["fixture_only"] is not False
                or receipt["e07_status"] != "production-trust-accepted"
                or (receipt["package_digest"] is not None and
                    (type(receipt["package_digest"]) is not str or DIGEST.fullmatch(receipt["package_digest"]) is None))
                or type(receipt["production_release_ready"]) is not bool
                or type(receipt["handoff_digest"]) is not str or DIGEST.fullmatch(receipt["handoff_digest"]) is None
                or type(receipt["admin_policy_digest"]) is not str
                or receipt["admin_policy_digest"] != "sha256:" + hashlib.sha256(canonical(self.policy)).hexdigest()
                or receipt["service_uid"] != self.service_uid or receipt["admin_gid"] != self.admin_gid):
            raise ManagementError("production_management_admission_rejected")
        trust = self.repo.trust_guard.status()
        if (trust.get("namespace") != "tmengine.production" or trust.get("bootstrapped") is not True
                or trust.get("fixture_only") is not False or trust.get("timestamp_version") is None
                or type(receipt["trust_root_version"]) is not int
                or receipt["trust_root_version"] != trust.get("root_version")):
            raise ManagementError("production_management_trust_not_ready")

    def _require_production_package(self, action, manifest, digest, payload_digest):
        if self.mode != "production":
            return
        self.production_admission = self._production_admission_cb(
            action=action, manifest=manifest, package_digest=digest,
            payload_digest=payload_digest)
        self._validate_production_admission()
        if (self.production_admission["package_digest"] != digest
                or self.production_admission["production_release_ready"] is not True):
            raise ManagementError("production_package_release_not_ready")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            if db.in_transaction: db.commit()
        except BaseException:
            if db.in_transaction: db.rollback()
            raise
        finally:
            db.close()

    def _audit(self, *, peer, method, operation_id, digest, generation, result, code=None):
        method = method if type(method) is str and method in METHODS else "invalid"
        operation_id = operation_id if type(operation_id) is str and REQUEST.fullmatch(operation_id) else None
        digest = digest if type(digest) is str and DIGEST.fullmatch(digest) else None
        generation = generation if type(generation) is int and 0 < generation <= 2**31 - 1 else None
        record = {"at": _now(), "uid": peer[0], "pid": peer[1],
                  "gid": peer[2] if len(peer) > 2 else None, "method": method,
                  "operation_id": operation_id, "digest": digest, "generation": generation,
                  "result": result, "code": code}
        data = canonical(record).decode()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prev = db.execute("SELECT record_digest FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
            previous = prev[0] if prev else "0" * 64
            digest_value = hashlib.sha256((previous + "\n" + data).encode()).hexdigest()
            db.execute("INSERT INTO audit(record,previous_digest,record_digest) VALUES(?,?,?)",
                       (data, previous, digest_value))
            db.execute("COMMIT")

    def audit(self):
        with self._db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY sequence")]
        previous = "0" * 64
        for number, row in enumerate(rows, 1):
            actual = hashlib.sha256((previous + "\n" + row["record"]).encode()).hexdigest()
            if row["sequence"] != number or row["previous_digest"] != previous or row["record_digest"] != actual:
                raise ManagementError("management_audit_broken")
            previous = actual
        return rows

    @staticmethod
    def _args(method, args):
        fields = {"list": set(), "inspect": {"digest"}, "verify": {"import_name", "archive_sha256"},
                  "install": {"import_name", "archive_sha256", "request_id"},
                  "initial-activate": {"digest", "request_id"}, "status": set(),
                  "recover": set(), "initial-rollback": {"request_id"},
                  "upgrade": {"digest", "expected_generation", "request_id"},
                  "rollback": {"digest", "expected_generation", "request_id"},
                  "disable": {"digest", "request_id"},
                  "uninstall": {"digest", "request_id"}, "gc": set()}
        if method not in fields or type(args) is not dict or set(args) != fields[method]:
            raise ManagementError("management_request_invalid")
        for key, value in args.items():
            if key == "expected_generation":
                if type(value) is not int or not 1 <= value <= 2**31 - 1:
                    raise ManagementError("management_generation_invalid")
                continue
            if type(value) is not str:
                raise ManagementError("management_request_invalid")
            if key in {"digest", "archive_sha256"} and DIGEST.fullmatch(value) is None:
                raise ManagementError("management_digest_invalid")
            if key == "request_id" and REQUEST.fullmatch(value) is None:
                raise ManagementError("management_request_invalid")
            if key == "import_name":
                LabRepository._import_name(value)
        return args

    def _installed(self, digest):
        item = self.repo.inspect(digest)
        if self.mode == "lab" and item["engine_id"] != "test.fake":
            raise ManagementError("lab_fixture_only")
        source = self.root / "packages" / digest[7:]
        with _regular_open(source / "ENGINE-MANIFEST.sig") as stream:
            signature = parse_manifest_json(stream.read(4 * 1024 * 1024 + 1))
        return item, source, signature

    def _start(self, digest, generation, *, publish=True, verified=None):
        if verified is None:
            item, source, signature = self._installed(digest)
            trusted_keys = self.repo.keys
        else:
            item = {"engine_id": verified["manifest"]["engine_id"],
                    "version": verified["manifest"]["version"],
                    "payload_digest": verified["payload_digest"],
                    "manifest": verified["manifest"]}
            source, signature, trusted_keys = (verified["source"], verified["signature"],
                                               verified["trusted_keys"])
        workdir = self.root / "management-hosts" / uuid.uuid4().hex
        workdir.parent.mkdir(mode=0o700, exist_ok=True)
        host = self.host_factory(workdir=workdir, restart=False)
        saved_launch = [None]
        def hook(phase, detail):
            if phase == "provider_started":
                self.store.save_launch(engine_id=item["engine_id"], generation=generation,
                                       package_digest=digest, record=detail, workdir=workdir)
                saved_launch[0] = detail["launch_instance_id"]
        host.phase_hook = hook
        try:
            record = host.start(source, item["manifest"], signature, trusted_keys,
                                expected_namespace=self.repo.namespace, expected_channel=self.repo.channel,
                                generation=generation)
            handshake = host.call("handshake", timeout=5)
            selftest = host.call("selftest", timeout=5)
            if host.state.value != "ready" or selftest.get("ok") is not True:
                raise ManagementError("management_candidate_not_ready")
            if publish:
                self._hosts[item["engine_id"]] = host
            else:
                self._candidate_hosts[record.launch_instance_id] = host
            return item, record, handshake, selftest
        except BaseException:
            host.stop()
            if saved_launch[0]:
                self.store.mark_launch(saved_launch[0], state="stopped", recovery_state="normal_stop")
            raise

    def _stop_host(self, engine_id, host):
        launch_id = host.record.launch_instance_id if host.record else None
        host.stop()
        if launch_id:
            self.store.mark_launch(launch_id, state="stopped", recovery_state="normal_stop")
        if self._hosts.get(engine_id) is host:
            self._hosts.pop(engine_id, None)
        for key, value in list(self._retained_hosts.items()):
            if value is host: self._retained_hosts.pop(key, None)
        for key, value in list(self._candidate_hosts.items()):
            if value is host: self._candidate_hosts.pop(key, None)

    def _fixture_package(self, digest):
        item, source, signature = self._installed(digest)
        manifest = item["manifest"]
        return FixturePackage(version=item["version"], digest=digest, source=source,
            manifest=manifest, signature=signature, trusted_keys=self.repo.keys,
            payload_digest=item["payload_digest"], runtime_digest=manifest["runtime_digest"],
            sdk_version=manifest["sdk_version"], task_schema=manifest["task_schema"],
            result_schema=manifest["result_schema"],
            capability_version="fixture/v1" if self.mode == "lab" else "provider/v1")

    def _finalize_retained(self):
        completed = []
        for key, host in list(self._retained_hosts.items()):
            engine_id, generation, digest, operation_id = key
            if self.store.generation_references(engine_id, generation, digest):
                continue
            heartbeat = host.heartbeat()
            if int(heartbeat.get("inflight", -1)) != 0:
                continue
            self.updater.drain(operation_id, {"inflight": 0, "references": 0})
            self._stop_host(engine_id, host)
            self.updater.retain(operation_id, {"digest": digest, "references": 0})
            self.updater.complete(operation_id)
            completed.append(operation_id)
        return completed

    def switch(self, method, digest, expected_generation, request_id):
        if self.mode == "lab" and self.orchestrator.fixture_budget is None:
            raise ManagementError("management_capacity_evidence_missing")
        target = self._fixture_package(digest)
        self._require_production_package(method, target.manifest, digest, target.payload_digest)
        engine_id = target.manifest["engine_id"]
        prior = self.store.conn.execute(
            "SELECT * FROM updates WHERE engine_id=? AND idempotency_key=?",
            (engine_id, request_id)).fetchone()
        if prior is not None:
            if prior["digest"] != digest or prior["kind"] != ("rollback" if method == "rollback" else "update") or prior["expected_generation"] != expected_generation:
                raise ManagementError("management_idempotency_conflict")
            if prior["target_generation"] is not None and prior["result_json"]:
                return {"operation_id": prior["operation_id"], "state": prior["state"],
                        "route": json.loads(prior["result_json"]), "replay": True, "lab_only": self.mode == "lab"}
            raise ManagementError("management_recover_required")
        old_route = self.store.route(engine_id)
        if old_route is None or old_route["generation"] != expected_generation:
            raise ManagementError("management_stale_generation")
        old = self._hosts.get(engine_id)
        if old is None or old.state.value != "ready" or old.identity.get("package_digest") != old_route["digest"]:
            raise ManagementError("management_active_host_unavailable")
        current = self._fixture_package(old_route["digest"])
        stored = self.store.package(engine_id, digest)
        if (stored and stored.state == "disabled") or self._disabled_digest(digest):
            raise ManagementError("management_package_disabled")
        if digest == old_route["digest"]:
            raise ManagementError("management_already_active")
        identity = self.orchestrator.package_identity(target)
        if method == "rollback":
            operation = self.updater.prepare_rollback(engine_id=engine_id, digest=digest,
                expected_generation=expected_generation, idempotency_key=request_id,
                expected_fence=old_route["fence"], identity=identity)
        else:
            operation = self.updater.prepare(engine_id=engine_id, digest=digest,
                expected_generation=expected_generation, idempotency_key=request_id,
                expected_fence=old_route["fence"], version=target.version,
                payload_digest=target.payload_digest, runtime_digest=target.runtime_digest,
                sdk_version=target.sdk_version, task_schema=target.task_schema,
                result_schema=target.result_schema, capability_version=target.capability_version,
                identity=identity)
        if operation.state in {"active", "draining", "retained", "complete"}:
            route = self.store.route(engine_id)
            if route and route["digest"] == digest and route["generation"] == operation.generation:
                return {"operation_id": operation.operation_id, "state": operation.state,
                        "route": route, "lab_only": self.mode == "lab"}
            raise ManagementError("management_recover_required")
        if operation.state != "prepared":
            raise ManagementError("management_recover_required")
        candidate = None
        stack = ExitStack()
        try:
            guarded = stack.enter_context(self.repo.guard_installed(digest, rollback=method == "rollback"))
            self.updater.verified(operation.operation_id, identity)
            item, record, handshake, selftest = self._start(digest, expected_generation + 1,
                                                            publish=False, verified=guarded)
            candidate = self.store.launch(record.launch_instance_id)
            self.updater.staged(operation.operation_id, {"staging_dir": record.staging_dir,
                "workdir": record.workdir, "digest": digest})
            self.updater.candidate_started(operation.operation_id, self.store.launch(record.launch_instance_id))
            self.updater.handshake_complete(operation.operation_id, handshake)
            self.updater.selftest_complete(operation.operation_id, selftest)
            # This field is set only after every readiness phase, including
            # measured dual process budget admission, has succeeded.
            sample = self.orchestrator._runtime_sample({"old": old, "candidate": self._find_host(record)})
            admission = (self.orchestrator._capacity_admission(current, target, sample)
                         if self.mode == "lab" else self.capacity_admitter(current, target, sample))
            if (type(admission) is not dict or admission.get("admitted") is not True
                    or (self.mode == "production" and admission.get("fixture_only") is not False)):
                raise ManagementError("management_capacity_admission_rejected")
            self.updater.candidate_ready(operation.operation_id, capacity_receipt=admission)
            self.checkpoint("switch_ready", {"operation_id": operation.operation_id, "digest": digest})
            self.updater.activate(operation.operation_id)
            self.checkpoint("switch_committed", {"operation_id": operation.operation_id, "digest": digest})
            old_key = (engine_id, expected_generation, old_route["digest"], operation.operation_id)
            self._retained_hosts[old_key] = old
            new_host = self._candidate_hosts.pop(record.launch_instance_id)
            self._hosts[engine_id] = new_host
            self._finalize_retained()
            return {"operation_id": operation.operation_id, "state": self.store.operation(operation.operation_id)["state"],
                    "route": self.store.route(engine_id), "capacity_admission": admission, "lab_only": self.mode == "lab"}
        except Exception as exc:
            route = self.store.route(engine_id)
            if not route or route["digest"] != digest or route["generation"] != expected_generation + 1:
                if candidate:
                    host = self._candidate_hosts.pop(candidate["launch_instance_id"], None)
                    if host: self._stop_host(engine_id, host)
                self.updater.phase(operation.operation_id, "blocked", detail={"code": getattr(exc, "code", type(exc).__name__)})
                def release(db):
                    db.execute("DELETE FROM leases WHERE engine_id=? AND operation_id=?",
                               (engine_id, operation.operation_id))
                self.store._write(release)
            raise
        finally:
            stack.close()

    def _find_host(self, record):
        return self._candidate_hosts[record.launch_instance_id]

    def _lifecycle(self, request_id, method, digest):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM lifecycle WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["method"] != method or row["digest"] != digest:
                    raise ManagementError("management_idempotency_conflict")
                return dict(row)
            now = _now()
            db.execute("INSERT INTO lifecycle VALUES(?,?,?,?,?,?)",
                       (request_id, method, digest, "prepared", now, now))
            db.execute("COMMIT")
            return {"request_id": request_id, "method": method, "digest": digest, "state": "prepared"}

    def _lifecycle_done(self, request_id):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE lifecycle SET state='complete',updated_at=? WHERE request_id=?", (_now(), request_id))
            db.execute("COMMIT")

    def _disabled_digest(self, digest):
        with self._db() as db:
            return db.execute("SELECT 1 FROM lifecycle WHERE method='disable' AND digest=? "
                              "AND state='complete' LIMIT 1", (digest,)).fetchone() is not None

    def _repository_row(self, digest):
        return next((row for row in self.repo.list() if row["package_digest"] == digest), None)

    def _assert_no_update_lease(self, engine_id):
        lease = self.store.conn.execute("SELECT expires_at FROM leases WHERE engine_id=?", (engine_id,)).fetchone()
        if lease and float(lease["expires_at"]) > time.time():
            raise ManagementError("management_lease_busy")

    def disable(self, digest, request_id):
        row = self._repository_row(digest)
        if row is None:
            raise ManagementError("management_package_missing")
        engine_id = row["engine_id"]
        self._assert_no_update_lease(engine_id)
        row = self._lifecycle(request_id, "disable", digest)
        if row["state"] == "complete":
            return {"state": "disabled", "digest": digest, "lab_only": self.mode == "lab"}
        if self.store.package(engine_id, digest) is None and self.mode == "lab":
            item, _, _ = self._installed(digest)
            manifest = item["manifest"]
            self.store.add_package(Package(engine_id, digest, item["version"],
                payload_digest=item["payload_digest"], runtime_digest=manifest["runtime_digest"],
                sdk_version=manifest["sdk_version"], task_schema=manifest["task_schema"],
                result_schema=manifest["result_schema"], capability_version="fixture/v1"))
        if self.store.package(engine_id, digest) is not None:
            self.store.set_package_state(engine_id, digest, "disabled")
        self._lifecycle_done(request_id)
        return {"state": "disabled", "digest": digest, "active_route": self.store.route(engine_id),
                "effect": "new_task_admission_denied_existing_bindings_preserved", "lab_only": self.mode == "lab"}

    def _can_uninstall(self, item):
        engine_id, digest = item["engine_id"], item["package_digest"]
        if not self.store.can_collect_package(engine_id, digest):
            raise ManagementError("management_package_referenced")
        pending_update = self.store.conn.execute(
            "SELECT 1 FROM updates WHERE engine_id=? AND digest=? AND state IN "
            "('prepared','verified','staged','candidate_started','handshake_complete',"
            "'selftest_complete','candidate_ready','active','draining') LIMIT 1",
            (engine_id, digest)).fetchone()
        if pending_update:
            raise ManagementError("management_update_referenced")
        if any(host.identity.get("package_digest") == digest for host in
               (*self._hosts.values(), *self._retained_hosts.values(), *self._candidate_hosts.values())):
            raise ManagementError("management_package_running")
        pending = self.store.conn.execute(
            "SELECT task_id,status FROM tasks WHERE engine_id=? AND digest=?", (engine_id, digest)).fetchall()
        for task in pending:
            if task["status"] in {"queued", "running", "retry", "reconcile_required"}:
                raise ManagementError("management_task_pending")
            if self.store.result(task["task_id"]) is not None:
                self.store.read_result(task["task_id"])

    def _remove_installed(self, item, request_id):
        digest = item["package_digest"]
        target = self.repo._target(digest)
        row = self._lifecycle(request_id, "uninstall", digest)
        if row["state"] == "complete":
            return {"state": "uninstalled", "digest": digest, "lab_only": self.mode == "lab"}
        with self.repo._locked() as db:
            installed = db.execute("SELECT 1 FROM packages WHERE package_digest=?", (digest,)).fetchone()
            if target.exists():
                if row["state"] != "prepared" or installed is None:
                    raise ManagementError("management_uninstall_journal_invalid")
                # Validate precise owner-controlled target before removing any
                # read-only installed bytes. A crash may leave a partial tree;
                # the durable prepared row authorizes only this exact digest path.
                _no_link_path(target)
                for base, dirs, files in os.walk(target, followlinks=False):
                    for name in dirs + files:
                        path = Path(base) / name
                        st = path.lstat()
                        if st.st_uid != os.getuid() or stat.S_ISLNK(st.st_mode) or (not stat.S_ISDIR(st.st_mode) and
                                (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1)):
                            raise ManagementError("management_uninstall_tree_unsafe")
                        if stat.S_ISDIR(st.st_mode): path.chmod(0o700)
                target.chmod(0o700)
                shutil.rmtree(target)
                self.checkpoint("uninstall_removed", {"operation_id": request_id, "digest": digest})
            if installed:
                with db:
                    db.execute("DELETE FROM packages WHERE package_digest=?", (digest,))
                    self.repo._audit(db, "uninstall", "removed", operation_id=request_id)
        self._lifecycle_done(request_id)
        return {"state": "uninstalled", "digest": digest, "lab_only": self.mode == "lab"}

    def uninstall(self, digest, request_id):
        with self._db() as db:
            prior = db.execute("SELECT * FROM lifecycle WHERE request_id=?", (request_id,)).fetchone()
        if prior and prior["state"] == "complete" and prior["method"] == "uninstall" and prior["digest"] == digest:
            return {"state": "uninstalled", "digest": digest, "lab_only": self.mode == "lab"}
        try:
            item = self.repo.inspect(digest)
        except (ArchiveError, TrustError, FileNotFoundError):
            if prior and prior["state"] == "prepared" and prior["method"] == "uninstall" and prior["digest"] == digest:
                with self.repo._locked() as db:
                    row = db.execute("SELECT * FROM packages WHERE package_digest=?", (digest,)).fetchone()
                    if row is None:
                        self._lifecycle_done(request_id)
                        return {"state": "uninstalled", "digest": digest, "lab_only": self.mode == "lab"}
                    item = dict(row)
            else:
                item = self._repository_row(digest)
                if item is None:
                    raise
        self._can_uninstall(item)
        self._assert_no_update_lease(item["engine_id"])
        # Quarantine blocks a new task binding before the second reference
        # check and physical removal. Existing frozen tasks/results remain.
        if self.store.package(item["engine_id"], digest) is not None:
            self.store.quarantine_package(item["engine_id"], digest)
            self._can_uninstall(item)
        return self._remove_installed(item, request_id)

    def gc(self):
        self._finalize_retained()
        removed, retained = [], []
        for item in self.repo.list():
            digest = item["package_digest"]
            package = self.store.package(item["engine_id"], digest)
            if (package is None or package.state not in {"disabled", "retired", "quarantined"}) and not self._disabled_digest(digest):
                retained.append({"digest": digest, "code": "management_not_gc_candidate"})
                continue
            try:
                removed.append(self.uninstall(digest, "gc:" + digest))
            except ManagementError as exc:
                if exc.code in {"management_package_referenced", "management_package_running", "management_task_pending",
                                "management_update_referenced"}:
                    retained.append({"digest": digest, "code": exc.code})
                else: raise
        return {"removed": removed, "retained": retained, "lab_only": self.mode == "lab"}

    def _activation(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM activations WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def _set_state(self, request_id, state, *, launch_id=None, code=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE activations SET state=?,launch_instance_id=COALESCE(?,launch_instance_id),"
                       "error_code=?,updated_at=? WHERE request_id=?",
                       (state, launch_id, code, _now(), request_id))
            db.execute("COMMIT")

    def initial_activate(self, digest, request_id):
        existing = self._activation(request_id)
        if existing:
            if existing["digest"] != digest:
                raise ManagementError("management_idempotency_conflict")
            if existing["state"] == "superseded":
                return {"operation_id": existing["operation_id"], "state": "superseded",
                        "package_digest": digest, "lab_only": self.mode == "lab"}
            if existing["state"] == "active":
                route = self.store.route(existing["engine_id"])
                if route and route["digest"] == digest and existing["engine_id"] in self._hosts:
                    return {"operation_id": existing["operation_id"], "state": "active", "route": route,
                            "lab_only": self.mode == "lab"}
                raise ManagementError("management_recover_required")
            raise ManagementError("management_recover_required")
        item, _, _ = self._installed(digest)
        self._require_production_package("initial-activate", item["manifest"], digest, item["payload_digest"])
        if self.store.route(item["engine_id"]) is not None:
            raise ManagementError("management_initial_route_exists")
        operation_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO activations VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (request_id, operation_id, item["engine_id"], digest, 1, "prepared", None, None, _now(), _now()))
            db.execute("COMMIT")
        self.checkpoint("activation_prepared", {"operation_id": operation_id, "digest": digest})
        stack = ExitStack()
        try:
            guarded = stack.enter_context(self.repo.guard_installed(digest))
            item, record, handshake, selftest = self._start(digest, 1, verified=guarded)
            self._set_state(request_id, "ready", launch_id=record.launch_instance_id)
            self.checkpoint("activation_ready", {"operation_id": operation_id, "digest": digest})
            package = Package(item["engine_id"], digest, item["version"], state="active",
                              payload_digest=item["payload_digest"],
                              runtime_digest=item["manifest"]["runtime_digest"],
                              sdk_version=item["manifest"]["sdk_version"],
                              task_schema=item["manifest"]["task_schema"],
                              result_schema=item["manifest"]["result_schema"],
                              capability_version="fixture/v1" if self.mode == "lab" else "provider/v1")
            active = self.store.bootstrap_active(package, generation=1)
            self._set_state(request_id, "active")
            self.checkpoint("activation_committed", {"operation_id": operation_id, "digest": digest})
            return {"operation_id": operation_id, "state": "active", "route": self.store.route(item["engine_id"]),
                    "launch_instance_id": record.launch_instance_id, "provider_pid": record.pid,
                    "handshake": handshake, "selftest": selftest, "lab_only": self.mode == "lab"}
        except Exception as exc:
            if self.store.route(item["engine_id"]) is None:
                host = self._hosts.pop(item["engine_id"], None)
                if host:
                    self._stop_host(item["engine_id"], host)
                self._set_state(request_id, "failed", code=getattr(exc, "code", type(exc).__name__))
            raise
        finally:
            stack.close()

    def recover(self):
        # Every persisted launch is first checked by owner token, pid start time,
        # group, digest and snapshot identity. A mismatch blocks relaunch.
        for engine_id, host in list(self._hosts.items()):
            self._stop_host(engine_id, host)
        for key, host in list(self._retained_hosts.items()):
            self._stop_host(key[0], host)
        for host in list(self._candidate_hosts.values()):
            self._stop_host(host.identity.get("engine_id", ""), host)
        orchestrator = FixtureUpdateOrchestrator(store=self.store, workdir=self.root / "management-recovery")
        launches = orchestrator.recover_owned_launches()
        if any(x.get("blocked_reasons") for x in launches):
            raise ManagementError("management_owner_recovery_blocked")
        installed = self.repo.recover()
        if any(x.get("state") == "blocked" for x in installed):
            raise ManagementError("management_install_recovery_blocked")
        removed = []
        with self._db() as db:
            rows = [dict(x) for x in db.execute("SELECT * FROM activations ORDER BY created_at")]
            pending_removals = [dict(x) for x in db.execute(
                "SELECT * FROM lifecycle WHERE method='uninstall' AND state='prepared'")]
        for row in pending_removals:
            removed.append(self.uninstall(row["digest"], row["request_id"]))
        for row in self.store.conn.execute("SELECT * FROM updates WHERE state IN "
                "('prepared','verified','staged','candidate_started','handshake_complete',"
                "'selftest_complete','candidate_ready')").fetchall():
            route = self.store.route(row["engine_id"])
            if route is None or route["generation"] == row["expected_generation"]:
                self.updater.phase(row["operation_id"], "blocked",
                                   detail={"code": "management_crash_before_cas"})
                self.store._write(lambda db: db.execute(
                    "DELETE FROM leases WHERE engine_id=? AND operation_id=?",
                    (row["engine_id"], row["operation_id"])))
        routes = [self.store.route(x["engine_id"]) for x in self.store.conn.execute("SELECT engine_id FROM active")]
        for row in rows:
            if self.store.route(row["engine_id"]) is None and row["state"] in {"prepared", "ready"}:
                self._set_state(row["request_id"], "failed", code="management_crash_before_initial_cas")
        for route in routes:
            if route is None: continue
            current_item, _, _ = self._installed(route["digest"])
            self._require_production_package("recover", current_item["manifest"], route["digest"],
                                             current_item["payload_digest"])
            with self.repo.guard_installed(route["digest"]) as guarded:
                _, record, _, _ = self._start(route["digest"], route["generation"], verified=guarded)
            for row in rows:
                if row["engine_id"] == route["engine_id"] and row["digest"] == route["digest"]:
                    self._set_state(row["request_id"], "active", launch_id=record.launch_instance_id)
                elif row["engine_id"] == route["engine_id"] and row["state"] == "active":
                    self._set_state(row["request_id"], "superseded")
        for row in self.store.conn.execute("SELECT * FROM updates WHERE state='active'").fetchall():
            prior = self.store.conn.execute(
                "SELECT digest FROM updates WHERE engine_id=? AND target_generation=? "
                "AND state IN ('active','draining','retained','complete') ORDER BY created_at DESC LIMIT 1",
                (row["engine_id"], row["expected_generation"])).fetchone()
            old_digest = prior["digest"] if prior else next(
                (x["digest"] for x in rows if x["engine_id"] == row["engine_id"] and
                 x["generation"] == row["expected_generation"]), None)
            if old_digest is None:
                raise ManagementError("management_prior_route_unresolved")
            if old_digest and self.store.generation_references(row["engine_id"], row["expected_generation"], old_digest):
                old_item, _, _ = self._installed(old_digest)
                self._require_production_package("recover-old", old_item["manifest"], old_digest,
                                                 old_item["payload_digest"])
                with self.repo.guard_installed(old_digest, rollback=True) as guarded:
                    _, record, _, _ = self._start(old_digest, row["expected_generation"],
                                                  publish=False, verified=guarded)
                host = self._candidate_hosts.pop(record.launch_instance_id)
                self._retained_hosts[(row["engine_id"], row["expected_generation"], old_digest, row["operation_id"])] = host
            elif old_digest:
                self.updater.drain(row["operation_id"], {"inflight": 0, "references": 0})
                self.updater.retain(row["operation_id"], {"digest": old_digest, "references": 0})
                self.updater.complete(row["operation_id"])
        return {"install": installed, "uninstall": removed, "launch_cleanup": launches,
                "routes": routes,
                "lab_only": self.mode == "lab"}

    def status(self):
        with self._db() as db:
            activations = [dict(x) for x in db.execute("SELECT * FROM activations ORDER BY created_at")]
        routes = [self.store.route(x["engine_id"]) for x in self.store.conn.execute("SELECT engine_id FROM active")]
        return {"activations": activations, "routes": routes,
                "live_engines": sorted(k for k, host in self._hosts.items() if host.state.value == "ready"),
                "updates": [dict(x) for x in self.store.conn.execute("SELECT * FROM updates ORDER BY created_at")],
                "retained_launches": [host.record.launch_instance_id for host in self._retained_hosts.values()],
                "lab_only": self.mode == "lab"}

    def dispatch(self, method, args, *, peer):
        operation_id = args.get("request_id") if type(args) is dict else None
        digest = args.get("digest") if type(args) is dict else None
        generation = (1 if method == "initial-activate" else
                      args.get("expected_generation") + 1 if type(args) is dict and
                      method in {"upgrade", "rollback"} and type(args.get("expected_generation")) is int else None)
        with self._lock:
            try:
                self.policy = load_policy(self.policy_path, mode=self.mode)
                if self.mode == "production" and method not in {"status", "disable", "uninstall", "gc"}:
                    self.production_admission = self._production_admission_cb(
                        action="command", manifest=None, package_digest=None)
                    self._validate_production_admission()
                if peer[0] not in self.policy["allowed_uids"] or method not in self.policy["allowed_methods"]:
                    raise ManagementError("management_unauthorized")
                args = self._args(method, args)
                if method == "list": result = self.repo.list()
                elif method == "inspect": result = self.repo.inspect(args["digest"])
                elif method == "verify":
                    with open_verified_archive(self.root / "incoming" / args["import_name"], trusted_keys=self.repo.keys,
                            expected_namespace=self.repo.namespace, expected_channel=self.repo.channel,
                            expected_archive_sha256=args["archive_sha256"],
                            trust_guard=self.repo.trust_guard,
                            entrypoint_ids=getattr(self.repo, "entrypoint_ids", ("python.sdk.fixture.v1",))) as package:
                        manifest, package_digest, payload_digest = (package.manifest,
                            package.package_digest, package.payload_digest)
                        result = {"package_digest": package.package_digest, "archive_sha256": package.archive_sha256,
                                  "payload_digest": package.payload_digest, "engine_id": package.manifest["engine_id"]}
                    self._require_production_package("verify", manifest, package_digest, payload_digest)
                elif method == "install":
                    # The archive API pins and snapshots one FD. The caller supplies
                    # the expected archive digest; only an import leaf is accepted.
                    with open_verified_archive(self.root / "incoming" / args["import_name"], trusted_keys=self.repo.keys,
                            expected_namespace=self.repo.namespace, expected_channel=self.repo.channel,
                            expected_archive_sha256=args["archive_sha256"],
                            trust_guard=self.repo.trust_guard,
                            entrypoint_ids=getattr(self.repo, "entrypoint_ids", ("python.sdk.fixture.v1",))) as package:
                        manifest, digest, payload_digest = (package.manifest,
                            package.package_digest, package.payload_digest)
                    self._require_production_package("install", manifest, digest, payload_digest)
                    result = self.repo.install(args["import_name"], args["request_id"],
                                               expected_archive_sha256=args["archive_sha256"])
                    if result["archive_sha256"] != args["archive_sha256"] or result["package_digest"] != digest:
                        raise ManagementError("management_import_changed")
                    operation_id = result["operation_id"]
                elif method == "initial-activate":
                    result = self.initial_activate(args["digest"], args["request_id"])
                    operation_id, generation = result["operation_id"], 1
                elif method in {"upgrade", "rollback"}:
                    result = self.switch(method, args["digest"], args["expected_generation"], args["request_id"])
                    operation_id, generation = result["operation_id"], result["route"]["generation"]
                elif method == "disable":
                    result = self.disable(args["digest"], args["request_id"])
                    operation_id = args["request_id"]
                elif method == "uninstall":
                    result = self.uninstall(args["digest"], args["request_id"])
                    operation_id = args["request_id"]
                elif method == "gc": result = self.gc()
                elif method == "status": result = self.status()
                elif method == "recover": result = self.recover()
                elif method == "initial-rollback":
                    row = self._activation(args["request_id"])
                    if row is None or row["state"] not in {"prepared", "ready", "failed", "rolled_back"} or self.store.route(row["engine_id"]):
                        raise ManagementError("management_rollback_not_unrouted")
                    self.recover()
                    self._set_state(args["request_id"], "rolled_back")
                    result = {"operation_id": row["operation_id"], "state": "rolled_back", "lab_only": self.mode == "lab"}
                    operation_id, digest, generation = row["operation_id"], row["digest"], row["generation"]
                else: raise ManagementError("management_method_unknown")
                if len(canonical({"ok": True, "result": result})) > MAX_FRAME:
                    raise ManagementError("management_response_limit")
                self._audit(peer=peer, method=method, operation_id=operation_id, digest=digest,
                            generation=generation, result="ok")
                return result
            except Exception as exc:
                self._audit(peer=peer, method=method, operation_id=operation_id, digest=digest,
                            generation=generation, result="rejected", code=getattr(exc, "code", type(exc).__name__))
                raise

    def close(self):
        for engine_id, host in list(self._hosts.items()):
            self._stop_host(engine_id, host)
        for key, host in list(self._retained_hosts.items()):
            self._stop_host(key[0], host)
        for host in list(self._candidate_hosts.values()):
            self._stop_host(host.identity.get("engine_id", ""), host)
        self.store.close()


def serve(manager, *, ready=None):
    path = manager.socket_path
    _endpoint(manager)
    if path.exists() or path.is_symlink():
        raise ManagementError("management_socket_exists")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        if manager.mode == "production":
            try: os.chown(path, -1, manager.admin_gid)
            except OSError as exc: raise ManagementError("management_admin_group_unavailable") from exc
            os.chmod(path, 0o660)
        else:
            os.chmod(path, 0o600)
        server.listen(16)
        if ready: ready()
        while True:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(5)
                peer_info = peer_credentials(conn)
                peer = (peer_info.uid, peer_info.pid, peer_info.gid)
                method = "invalid"
                try:
                    nonce = uuid.uuid4().hex
                    _send(conn, {"schema": SCHEMA, "server_uid": os.getuid(), "server_pid": os.getpid(), "nonce": nonce})
                    request = _recv(conn)
                    method = request.get("method", "invalid")
                    if (set(request) != {"schema", "nonce", "method", "args"} or request["schema"] != SCHEMA
                            or request["nonce"] != nonce or type(method) is not str):
                        raise ManagementError("management_session_mismatch")
                    conn.settimeout(300)
                    result = manager.dispatch(method, request["args"], peer=peer)
                    _send(conn, {"ok": True, "result": result})
                except Exception as exc:
                    code = getattr(exc, "code", type(exc).__name__)
                    if method == "invalid" or code in {"management_session_mismatch", "management_frame_limit", "management_frame_truncated", "frame_json_invalid"}:
                        manager._audit(peer=peer, method="invalid", operation_id=None, digest=None,
                                       generation=None, result="rejected", code=code)
                    try: _send(conn, {"ok": False, "code": code})
                    except OSError: pass
    finally:
        server.close()
        path.unlink(missing_ok=True)


def request(socket_path, method, args, *, expected_server_uid, expected_repository_root=None):
    path = _no_link_path(Path(socket_path))
    descriptor = path.parent / "ENDPOINT.json"
    with _regular_open(descriptor) as source:
        info = strict_json(source.read(MAX_FRAME + 1), max_bytes=MAX_FRAME)
    if (set(info) != {"schema", "repository", "server_uid", "admin_gid", "server_pid",
                      "server_start_time", "socket_mode", "mode"}
            or info["schema"] != "tmengine.management-endpoint/v1"
            or info["server_uid"] != expected_server_uid
            or info["mode"] not in {"lab", "production"}
            or management_socket_path(info["repository"]) != path
            or (expected_repository_root is not None and info["repository"] != str(Path(expected_repository_root)))
            or type(info["server_pid"]) is not int or info["server_pid"] <= 0
            or type(info["server_start_time"]) not in (int, float)
            or type(info["admin_gid"]) not in (int, type(None))
            or info["socket_mode"] != (0o660 if info["mode"] == "production" else 0o600)):
        raise ManagementError("management_endpoint_descriptor_invalid")
    if info["mode"] == "production" and expected_repository_root is None:
        raise ManagementError("management_repository_root_required")
    descriptor_st = descriptor.lstat()
    socket_dir_st = path.parent.lstat()
    if (descriptor_st.st_uid != expected_server_uid
            or descriptor_st.st_mode & 0o777 != (0o640 if info["mode"] == "production" else 0o600)
            or socket_dir_st.st_uid != expected_server_uid
            or socket_dir_st.st_mode & 0o777 != (0o710 if info["mode"] == "production" else 0o700)
            or (info["mode"] == "production" and
                (descriptor_st.st_gid != info["admin_gid"] or socket_dir_st.st_gid != info["admin_gid"]))):
        raise ManagementError("management_endpoint_descriptor_unsafe")
    st = path.lstat()
    if (not stat.S_ISSOCK(st.st_mode) or st.st_uid != expected_server_uid
            or st.st_mode & 0o777 != info["socket_mode"]
            or (info["mode"] == "production" and st.st_gid != info["admin_gid"])):
        raise ManagementError("management_socket_unsafe")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(300); conn.connect(str(path))
        peer = peer_credentials(conn)
        hello = _recv(conn)
        try:
            process_start = psutil.Process(peer.pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise ManagementError("management_server_identity_mismatch") from exc
        if (peer.uid != expected_server_uid or peer.pid != info["server_pid"]
                or abs(process_start - info["server_start_time"]) > .001
                or hello.get("schema") != SCHEMA
                or hello.get("server_uid") != peer.uid or hello.get("server_pid") != peer.pid
                or type(hello.get("nonce")) is not str or len(hello["nonce"]) != 32):
            raise ManagementError("management_server_identity_mismatch")
        _send(conn, {"schema": SCHEMA, "nonce": hello["nonce"], "method": method, "args": args})
        return _recv(conn)
