"""Separate ordinary-business Host UDS, shared by API and pinned Worker processes.

The Host owns the signed Provider and broker. This socket has no package-management
methods and accepts only source-owned operation names. It authenticates local
service processes with peer credentials, owner start time, a fresh challenge and
HMAC. Business authorization is then reloaded from the platform database.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import stat
import struct
import threading
from contextlib import closing
from pathlib import Path

import psutil

from app.runtime.engine_sdk import wire
from app.services.engine_execution import EngineExecutionBridge, EngineExecutionError, READ_OPERATIONS

SCHEMA = "engine.business_host/v1"
MAX_FRAME = 1024 * 1024
METHODS = {"freeze", "check", "invoke", "retain", "release"}


def _frame(sock, value):
    raw = wire.canonical(value)
    if len(raw) > MAX_FRAME:
        raise EngineExecutionError("business_frame_too_large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _read(sock):
    size = struct.unpack("!I", wire.recv_exact(sock, 4))[0]
    if size > MAX_FRAME:
        raise EngineExecutionError("business_frame_too_large")
    return wire.strict_json(wire.recv_exact(sock, size), max_bytes=MAX_FRAME)


def _mac(key, label, value):
    return hmac.new(key, label + wire.canonical(value), hashlib.sha256).hexdigest()


def _safe_socket_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(os.fsencode(path)) >= 100:
        raise EngineExecutionError("business_socket_path_invalid")
    root = path.parent
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EngineExecutionError("business_socket_root_unsafe")
    return path


class BusinessHost:
    def __init__(self, *, manager, session_factory, socket_path, client_key):
        if not isinstance(client_key, bytes) or len(client_key) != 32:
            raise EngineExecutionError("business_client_key_invalid")
        self.bridge = EngineExecutionBridge(manager=manager, core_release="host-only")
        self.session_factory = session_factory
        self.path = _safe_socket_path(socket_path)
        self.key = client_key
        self.uid = os.getuid()
        self.pid = os.getpid()
        self.start_time = psutil.Process(self.pid).create_time()
        self.stop_event = threading.Event()
        self.listener = None
        self.thread = None

    def start(self):
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise EngineExecutionError("business_socket_unsafe")
            # A live previous owner is never displaced.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.settimeout(.2)
                    probe.connect(str(self.path))
                except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                    self.path.unlink()
                else:
                    raise EngineExecutionError("business_host_already_running")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        listener.listen(32)
        listener.settimeout(.2)
        self.listener = listener
        self.thread = threading.Thread(target=self._serve, daemon=True, name="engine-business-host")
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(timeout=1)
        if self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
            self.path.unlink()

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True,
                             name="engine-business-request").start()

    def _handle(self, conn):
        with conn:
            try:
                conn.settimeout(65)
                peer = wire.peer_credentials(conn)
                if peer.uid != self.uid:
                    raise EngineExecutionError("business_peer_denied")
                hello = _read(conn)
                if set(hello) != {"schema", "client_nonce"} or hello["schema"] != SCHEMA or not isinstance(hello["client_nonce"], str) or len(hello["client_nonce"]) != 64:
                    raise EngineExecutionError("business_handshake_invalid")
                server_nonce = secrets.token_hex(32)
                transcript = {"schema": SCHEMA, "client_nonce": hello["client_nonce"],
                              "server_nonce": server_nonce, "host_pid": self.pid,
                              "host_start": self.start_time, "client_pid": peer.pid, "client_uid": peer.uid}
                _frame(conn, {"transcript": transcript,
                              "proof": _mac(self.key, b"business-host-challenge\0", transcript)})
                request = _read(conn)
                if set(request) != {"transcript", "method", "args", "proof"} or request["transcript"] != transcript or request["method"] not in METHODS or not isinstance(request["args"], dict):
                    raise EngineExecutionError("business_request_invalid")
                signed = {"transcript": transcript, "method": request["method"], "args": request["args"]}
                if not hmac.compare_digest(request["proof"], _mac(self.key, b"business-client-request\0", signed)):
                    raise EngineExecutionError("business_request_auth_invalid")
                result = self._dispatch(request["method"], request["args"])
                _frame(conn, {"ok": True, "result": result})
            except Exception as exc:
                code = getattr(exc, "code", None)
                try:
                    _frame(conn, {"ok": False, "code": code if isinstance(code, str) else "business_host_unavailable"})
                except Exception:
                    pass

    def _authorized_plan(self, db, plan_id, actor, *, claim=None):
        from app.services import capability_registry as registry, plan_service as plans
        from app.models.capability_execution import DBCapabilityExecution
        from app.runtime.execution_identity import worker_matches
        plan = plans._load(db, plan_id, actor, owner_only=True)
        snapshot = plan.capability_snapshot_json
        if "execution_identity" not in snapshot or len(snapshot["steps"]) != 1:
            raise EngineExecutionError("engine_binding_missing")
        entry = snapshot["definitions"][0]
        registry.resolve_handler(db, entry["capability_id"], entry["version"], actor=actor,
                                 instance_id=plan.instance_id, params={}, _allow_engine_bridge=True)
        if entry["capability_id"] not in READ_OPERATIONS:
            raise EngineExecutionError("provider_operation_unregistered")
        if claim is not None:
            row = db.get(DBCapabilityExecution, claim.get("id"))
            if (row is None or row.plan_id != plan_id or row.state != "running"
                    or row.owner_token != claim.get("owner_token")
                    or row.worker_id != claim.get("worker_id")
                    or row.binding_hash != plans._hash(snapshot)
                    or not worker_matches(snapshot, claim.get("worker_identity"))
                    or row.lease_expires_at is None or row.lease_expires_at <= plans._now()):
                raise EngineExecutionError("execution_lease_lost")
        return plan

    def _dispatch(self, method, args):
        from app.services import capability_registry as registry, plan_service as plans
        from app.models.db_instance import DBInstance
        from app.services.engine_execution import engine_for_instance
        if method == "freeze":
            if set(args) != {"actor", "instance_id", "capability_id", "core_release"} or args["capability_id"] not in READ_OPERATIONS:
                raise EngineExecutionError("business_request_invalid")
            with self.session_factory() as db:
                entry = registry._entry(args["capability_id"])
                registry.resolve_handler(db, entry["capability_id"], entry["version"], actor=args["actor"],
                                         instance_id=args["instance_id"], params={}, _allow_engine_bridge=True)
                instance = db.get(DBInstance, args["instance_id"])
                if not instance or not instance.enabled:
                    raise EngineExecutionError("target_unavailable")
                frozen = self.bridge.freeze(db, instance, args["capability_id"])
                frozen["identity"]["core_release"] = args["core_release"]
                return frozen
        if method == "retain":
            if set(args) != {"plan_id", "actor", "instance_id", "capability_id", "request_hash", "frozen"}:
                raise EngineExecutionError("business_request_invalid")
            with self.session_factory() as db:
                entry = registry._entry(args["capability_id"])
                registry.resolve_handler(db, entry["capability_id"], entry["version"], actor=args["actor"],
                                         instance_id=args["instance_id"], params={}, _allow_engine_bridge=True)
                self.bridge.check(args["frozen"])
            pseudo = type("Plan", (), {"plan_id": args["plan_id"],
                     "capability_snapshot_json": {"request_hash": args["request_hash"]}})()
            self.bridge.retain_plan(pseudo, args["frozen"])
            return {"retained": True}
        if method in {"check", "invoke", "release"}:
            expected = {"plan_id", "actor", "frozen"} if method == "check" else (
                {"plan_id", "actor", "frozen", "claim", "timeout"} if method == "invoke" else {"plan_id", "actor"})
            if set(args) != expected:
                raise EngineExecutionError("business_request_invalid")
            with self.session_factory() as db:
                plan = self._authorized_plan(db, args["plan_id"], args["actor"],
                                             claim=args.get("claim"))
                if method == "release":
                    if plan.status != "settled" or plan.outcome != "succeeded":
                        raise EngineExecutionError("plan_not_settled")
                    self.bridge.release_completed(plan)
                    return {"released": True}
                frozen = {"identity": plan.capability_snapshot_json["execution_identity"],
                          "provider_version": plan.capability_snapshot_json["provider_version"],
                          "provider_generation": plan.capability_snapshot_json["provider_generation"],
                          "operation": plan.capability_snapshot_json["provider_operation"]}
                if frozen != args["frozen"]:
                    raise EngineExecutionError("engine_binding_changed")
                self.bridge.check(frozen)
                if method == "check":
                    return {"checked": True}
                if plan.status != "running" or type(args["timeout"]) is not int or not 0 < args["timeout"] <= 300:
                    raise EngineExecutionError("invalid_plan_state")
                values = self.bridge.invoke(frozen, plan.instance_id, timeout=args["timeout"])
                # A long Provider call must never publish after revocation/fence loss.
                db.rollback()
                plan = self._authorized_plan(db, args["plan_id"], args["actor"], claim=args["claim"])
                plans._check_plan(db, plan, args["actor"])
                return values
        raise EngineExecutionError("business_method_unavailable")


class RemoteEngineExecutionBridge:
    def __init__(self, *, socket_path, client_key, core_release):
        self.path = _safe_socket_path(socket_path)
        if not isinstance(client_key, bytes) or len(client_key) != 32:
            raise EngineExecutionError("business_client_key_invalid")
        self.key, self.core_release = client_key, core_release

    def _call(self, method, args):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(65)
            sock.connect(str(self.path))
            peer = wire.peer_credentials(sock)
            if peer.uid != os.getuid():
                raise EngineExecutionError("business_host_identity_mismatch")
            nonce = secrets.token_hex(32)
            _frame(sock, {"schema": SCHEMA, "client_nonce": nonce})
            challenge = _read(sock)
            if set(challenge) != {"transcript", "proof"}:
                raise EngineExecutionError("business_handshake_invalid")
            transcript = challenge["transcript"]
            if (transcript.get("client_nonce") != nonce or transcript.get("host_pid") != peer.pid
                    or transcript.get("client_pid") != os.getpid() or transcript.get("client_uid") != os.getuid()
                    or transcript.get("schema") != SCHEMA):
                raise EngineExecutionError("business_host_identity_mismatch")
            try:
                if abs(psutil.Process(peer.pid).create_time() - transcript["host_start"]) > .001:
                    raise EngineExecutionError("business_host_identity_mismatch")
            except (psutil.Error, TypeError, ValueError) as exc:
                raise EngineExecutionError("business_host_identity_mismatch") from exc
            if not hmac.compare_digest(challenge["proof"], _mac(self.key, b"business-host-challenge\0", transcript)):
                raise EngineExecutionError("business_host_identity_mismatch")
            signed = {"transcript": transcript, "method": method, "args": args}
            _frame(sock, {**signed, "proof": _mac(self.key, b"business-client-request\0", signed)})
            reply = _read(sock)
            if reply.get("ok") is not True:
                raise EngineExecutionError(reply.get("code", "business_host_unavailable"))
            return reply["result"]

    def freeze(self, db, instance, capability_id, *, actor):
        return self._call("freeze", {"actor": actor, "instance_id": instance.id,
                                     "capability_id": capability_id, "core_release": self.core_release})

    def check(self, frozen, *, plan_id, actor):
        return self._call("check", {"plan_id": plan_id, "actor": actor, "frozen": frozen})

    def retain_plan(self, plan, frozen):
        return self._call("retain", {"plan_id": plan.plan_id, "actor": {"user_id": plan.requested_by},
                                     "instance_id": plan.instance_id, "capability_id": plan.capability_id,
                                     "request_hash": plan.capability_snapshot_json["request_hash"],
                                     "frozen": frozen})

    def invoke(self, frozen, instance_id, *, timeout, plan_id, actor, claim):
        return self._call("invoke", {"plan_id": plan_id, "actor": actor, "frozen": frozen,
                                     "claim": claim, "timeout": timeout})

    def release_completed(self, plan):
        return self._call("release", {"plan_id": plan.plan_id,
                                      "actor": {"user_id": plan.requested_by}})
