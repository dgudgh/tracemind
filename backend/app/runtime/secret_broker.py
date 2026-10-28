"""Host-owned, authenticated short secret leases for a signed Provider process.

No secret value, secret reference, principal, channel key or request context is persisted.
The Host owns all callbacks; Provider RPC accepts opaque lease IDs only.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import socket
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.runtime.engine_sdk import wire


class BrokerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_BINDING = ("engine_id", "package_digest", "runtime_digest", "launch_instance_id",
            "operation_id", "target_digest", "purpose", "fence_epoch", "deadline", "scope")
_IDENTITY = ("engine_id", "package_digest", "runtime_digest", "launch_instance_id")


def _record(record):
    return dict(record) if isinstance(record, dict) else vars(record).copy()


def _digest(value):
    return hashlib.sha256(wire.canonical(value)).hexdigest()


def _subset(requested, allowed):
    if isinstance(requested, dict) and isinstance(allowed, dict):
        return bool(requested) and all(k in allowed and _subset(v, allowed[k]) for k, v in requested.items())
    if isinstance(requested, list) and isinstance(allowed, list):
        return all(item in allowed for item in requested)
    return type(requested) is type(allowed) and requested == allowed


def _context(context):
    if not isinstance(context, dict):
        raise BrokerError("broker_context_invalid")
    if "purpose" not in context and "capability_id" in context:
        context = {**context, "purpose": context["capability_id"]}
    if ("capability_id" in context and context["purpose"] != context["capability_id"]
            or any(k not in context for k in _BINDING)):
        raise BrokerError("broker_context_invalid")
    for key in _BINDING:
        if key not in {"fence_epoch", "deadline", "scope"} and (type(context[key]) is not str or not context[key]):
            raise BrokerError("broker_context_invalid")
    if type(context["fence_epoch"]) is not int or context["fence_epoch"] < 1:
        raise BrokerError("broker_context_invalid")
    if type(context["deadline"]) not in (int, float) or not 0 < context["deadline"] < 1e12:
        raise BrokerError("broker_context_invalid")
    if not isinstance(context["scope"], dict) or not context["scope"]:
        raise BrokerError("broker_context_invalid")
    wire.canonical(context)
    return {key: context[key] for key in _BINDING}


@dataclass
class _Launch:
    record: dict
    key: bytes


@dataclass
class _Lease:
    lease_id: str
    secret_ref: str | None
    principal: dict
    context: dict
    issued: float
    expires: float
    state: str = "issued"
    grace_until: float | None = None
    close_sent: bool = False
    terminated: bool = False


class SecretBroker:
    """One Host process owns this broker. Callbacks run outside the broker lock.

    authorize(principal, context, phase) must return {"scope": allowed_scope}.
    resolve_secret(secret_ref, principal, context) must return a JSON object.
    on_close_request(launch_id, reason) and on_terminate(launch_id, reason)
    are Host lifecycle hooks; on_terminate must verify the LaunchRecord again.
    """

    def __init__(self, workdir, *, authorize: Callable, resolve_secret: Callable,
                 on_terminate: Callable, on_close_request: Callable | None = None,
                 clock: Callable = time.time, grace_seconds: float = 0.5,
                 scan_interval: float = 0.05):
        root = Path(workdir)
        if not root.is_absolute() or root.is_symlink():
            raise BrokerError("broker_root_unsafe")
        if root.exists():
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BrokerError("broker_root_unsafe")
        else:
            root.mkdir(mode=0o700)
        self.root = root.resolve()
        self.path = self.root / "b.sock"
        if len(os.fsencode(self.path)) >= 100:
            raise BrokerError("broker_path_too_long")
        self.db_path = self.root / "leases.sqlite3"
        if self.db_path.exists() or self.db_path.is_symlink():
            info = self.db_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise BrokerError("broker_metadata_unsafe")
        else:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.authorize = authorize
        self.resolve_secret = resolve_secret
        self.on_close_request = on_close_request
        self.on_terminate = on_terminate
        self.clock = clock
        self.grace_seconds = grace_seconds
        self.scan_interval = scan_interval
        if not 0 <= grace_seconds <= 60 or not 0.01 <= scan_interval <= 5:
            raise BrokerError("broker_limits_invalid")
        self._host_pid = os.getpid()
        self._host_uid = os.getuid()
        self._lock = threading.RLock()
        self._launches: dict[str, _Launch] = {}
        self._leases: dict[str, _Lease] = {}
        self._closed = threading.Event()
        self._listener = None
        self._accept_thread = None
        self._monitor_thread = None
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        os.chmod(self.db_path, 0o600)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS leases (lease_hash TEXT PRIMARY KEY, launch_hash TEXT NOT NULL, state TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL, changed REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, lease_hash TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY, code TEXT NOT NULL, peer_pid INTEGER NOT NULL, at REAL NOT NULL)")
            abandoned = [row[0] for row in self._db.execute("SELECT lease_hash FROM leases WHERE state IN ('issued','active')")]
            self._db.execute("UPDATE leases SET state='revoked', changed=? WHERE state IN ('issued','active')", (self.clock(),))
            self._db.executemany("INSERT INTO events(lease_hash,event,at) VALUES (?,'host_restart_revoked',?)",
                                 [(lease_hash, self.clock()) for lease_hash in abandoned])

    def _ensure_host(self):
        if os.getpid() != self._host_pid:
            raise BrokerError("broker_wrong_host_process")

    def _event(self, lease: _Lease, state: str, event: str):
        now = self.clock()
        hashed = hashlib.sha256(lease.lease_id.encode()).hexdigest()
        with self._db:
            self._db.execute("UPDATE leases SET state=?,changed=? WHERE lease_hash=?", (state, now, hashed))
            self._db.execute("INSERT INTO events(lease_hash,event,at) VALUES (?,?,?)", (hashed, event, now))
        lease.state = state

    def _audit_denial(self, code, peer_pid=0):
        # Codes are fixed protocol identifiers. Never persist exception detail or input.
        allowed = {"broker_handshake_invalid", "broker_peer_invalid", "broker_nonce_invalid",
                   "broker_method_invalid", "broker_ack_invalid", "broker_lease_unknown",
                   "broker_scope_mismatch", "broker_lease_not_active", "broker_lease_unavailable",
                   "broker_unauthorized", "broker_secret_unavailable", "broker_context_invalid"}
        safe = code if code in allowed else "broker_protocol_rejected"
        with self._lock, self._db:
            self._db.execute("INSERT INTO security_events(code,peer_pid,at) VALUES (?,?,?)",
                             (safe, int(peer_pid), self.clock()))

    def start(self):
        self._ensure_host()
        if self._listener:
            return self
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self._host_uid:
                raise BrokerError("broker_socket_unsafe")
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(32)
            listener.settimeout(self.scan_interval)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True, name="engine-secret-broker-accept")
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="engine-secret-broker-monitor")
        self._accept_thread.start()
        self._monitor_thread.start()
        return self

    def close(self):
        self._ensure_host()
        self._closed.set()
        if self._listener:
            self._listener.close()
            self._listener = None
        for thread in (self._accept_thread, self._monitor_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
        with self._lock:
            for lease in self._leases.values():
                if lease.state in ("issued", "active"):
                    self._event(lease, "revoked", "broker_closed")
                lease.secret_ref = None
            self._launches.clear()
            self._db.close()
        if self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
            self.path.unlink()

    def register_launch(self, record, *, channel_key: bytes):
        self._ensure_host()
        r = _record(record)
        if not isinstance(channel_key, bytes) or len(channel_key) != 32:
            raise BrokerError("broker_channel_key_invalid")
        try:
            process = psutil.Process(r["pid"])
            valid = (r["host_pid"] == self._host_pid and r["uid"] == self._host_uid
                     and process.ppid() == self._host_pid
                     and process.uids().real == r["uid"]
                     and abs(process.create_time() - r["start_time"]) <= .001
                     and os.getpgid(process.pid) == r["pgid"] == r["pid"]
                     and type(r["launch_instance_id"]) is str and bool(r["launch_instance_id"])
                     and all(type(r[k]) is str and bool(r[k]) for k in ("engine_id", "package_digest", "runtime_digest")))
        except (KeyError, TypeError, psutil.Error, OSError):
            valid = False
        if not valid:
            raise BrokerError("broker_launch_identity_invalid")
        with self._lock:
            if r["launch_instance_id"] in self._launches:
                raise BrokerError("broker_launch_duplicate")
            self._launches[r["launch_instance_id"]] = _Launch(r, channel_key)

    def _live_launch(self, launch: _Launch):
        try:
            r = launch.record
            p = psutil.Process(r["pid"])
            return (p.ppid() == self._host_pid and p.uids().real == r["uid"]
                    and abs(p.create_time() - r["start_time"]) <= .001
                    and os.getpgid(p.pid) == r["pgid"])
        except (psutil.Error, OSError):
            return False

    def _authorized(self, principal, context, phase):
        try:
            policy = self.authorize(principal, dict(context), phase)
            return isinstance(policy, dict) and _subset(context["scope"], policy.get("scope"))
        except Exception:
            return False

    def grant(self, secret_ref, principal, request_context, ttl):
        self._ensure_host()
        context = _context(request_context)
        now = self.clock()
        if (type(secret_ref) is not str or not secret_ref or not isinstance(principal, dict)
                or type(principal.get("id")) is not str or not principal["id"]
                or type(ttl) not in (int, float) or not 0 < ttl <= 60
                or now >= context["deadline"] or ttl > context["deadline"] - now):
            raise BrokerError("broker_grant_invalid")
        launch_id = context["launch_instance_id"]
        with self._lock:
            launch = self._launches.get(launch_id)
            if launch is None or not self._live_launch(launch):
                raise BrokerError("broker_launch_invalid")
            if any(context[k] != launch.record[k] for k in _IDENTITY):
                raise BrokerError("broker_launch_mismatch")
        if not self._authorized(principal, context, "grant"):
            raise BrokerError("broker_unauthorized")
        lease_id = secrets.token_urlsafe(32)
        lease = _Lease(lease_id, secret_ref, dict(principal), context, now, now + ttl)
        with self._lock:
            if not self._live_launch(launch) or now >= context["deadline"]:
                raise BrokerError("broker_launch_invalid")
            self._leases[lease_id] = lease
            with self._db:
                self._db.execute("INSERT INTO leases VALUES (?,?,?,?,?,?)",
                                 (hashlib.sha256(lease_id.encode()).hexdigest(),
                                  _digest({"launch": launch_id}), "issued", now, now + ttl, now))
                self._db.execute("INSERT INTO events(lease_hash,event,at) VALUES (?,?,?)",
                                 (hashlib.sha256(lease_id.encode()).hexdigest(), "granted", now))
        return lease_id

    def _dispatch_callback(self, callback, launch_id, reason):
        if callback:
            def run():
                try:
                    callback(launch_id, reason)
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True, name="engine-secret-broker-hook").start()

    def _expire(self, lease, reason):
        if lease.state not in ("issued", "active"):
            return
        active = lease.state == "active"
        self._event(lease, "revoked", reason)
        lease.secret_ref = None
        if active:
            lease.grace_until = self.clock() + self.grace_seconds
            if not lease.close_sent:
                lease.close_sent = True
                self._dispatch_callback(self.on_close_request, lease.context["launch_instance_id"], reason)

    def revoke(self, lease_id=None, *, launch_instance_id=None, reason="explicit_revocation"):
        self._ensure_host()
        if lease_id is None and launch_instance_id is None:
            raise BrokerError("broker_revoke_selector_required")
        if reason not in {"explicit_revocation", "credential_revoked", "authorization_revoked",
                          "fence_invalid", "quarantine", "launch_exited"}:
            raise BrokerError("broker_revocation_reason_invalid")
        with self._lock:
            for lease in self._leases.values():
                if (lease_id is None or lease.lease_id == lease_id) and (launch_instance_id is None or lease.context["launch_instance_id"] == launch_instance_id):
                    self._expire(lease, reason)

    def poll(self):
        self._ensure_host()
        now = self.clock()
        with self._lock:
            candidates = [lease for lease in self._leases.values() if lease.state in ("issued", "active")]
        for lease in candidates:
            authorized = self._authorized(lease.principal, lease.context, "monitor")
            with self._lock:
                if lease.state in ("issued", "active"):
                    if now >= lease.expires or now >= lease.context["deadline"]:
                        self._expire(lease, "lease_expired")
                    elif not authorized:
                        self._expire(lease, "authorization_revoked")
                    elif not (launch := self._launches.get(lease.context["launch_instance_id"])) or not self._live_launch(launch):
                        self._expire(lease, "launch_exited")
        with self._lock:
            for lease in self._leases.values():
                if lease.grace_until is not None and now >= lease.grace_until and not lease.terminated:
                    lease.terminated = True
                    launch_id = lease.context["launch_instance_id"]
                    launch = self._launches.get(launch_id)
                    if launch and self._live_launch(launch):
                        self._dispatch_callback(self.on_terminate, launch_id, "lease_revocation_unacknowledged")

    def _monitor_loop(self):
        while not self._closed.wait(self.scan_interval):
            try:
                self.poll()
            except Exception:
                pass

    def _accept_loop(self):
        while not self._closed.is_set():
            try:
                conn, _ = self._listener.accept()
            except (socket.timeout, OSError, AttributeError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True,
                             name="engine-secret-broker-client").start()

    def _serve(self, conn):
        peer_pid = 0
        try:
            conn.settimeout(5)
            peer = wire.peer_credentials(conn)
            peer_pid = peer.pid
            hello = wire.recv_plain(conn)
            if set(hello) != {"type", "launch_instance_id", "nonce"} or hello["type"] != "hello":
                raise BrokerError("broker_handshake_invalid")
            launch_id = hello["launch_instance_id"]
            with self._lock:
                launch = self._launches.get(launch_id)
                if (launch is None or peer.uid != launch.record["uid"] or peer.pid != launch.record["pid"]
                        or not self._live_launch(launch)):
                    raise BrokerError("broker_peer_invalid")
                key = launch.key
                identity = {k: launch.record[k] for k in _IDENTITY}
            client_nonce = bytes.fromhex(hello["nonce"])
            if len(client_nonce) != 32:
                raise BrokerError("broker_nonce_invalid")
            server_nonce = os.urandom(32)
            transcript = {"protocol": "tmengine.secret-broker/v1", "connection_id": secrets.token_hex(16),
                          "client_nonce": client_nonce.hex(), "server_nonce": server_nonce.hex(),
                          "host_pid": self._host_pid, "host_uid": self._host_uid,
                          "provider_pid": peer.pid, "provider_uid": peer.uid,
                          "identity": identity, "deadline": self.clock() + 5}
            wire.send_plain(conn, {"transcript": transcript,
                                   "proof": wire.mac(key, b"broker-host-to-provider\0" + wire.canonical(transcript)).hex()})
            response = wire.recv_plain(conn)
            if set(response) != {"proof"} or self.clock() >= transcript["deadline"]:
                raise BrokerError("broker_handshake_invalid")
            wire.verify_mac(key, b"broker-provider-to-host\0" + wire.canonical(transcript), bytes.fromhex(response["proof"]))
            host_key, provider_key = tuple(HKDF(algorithm=hashes.SHA256(), length=32,
                salt=server_nonce + client_nonce, info=b"tmengine.secret-broker/v1\0" + direction + b"\0" + wire.canonical(transcript)).derive(key)
                for direction in (b"host-to-provider", b"provider-to-host"))
            request = wire.recv_frame(conn, provider_key, 0, connection_id=transcript["connection_id"],
                                      direction="broker-provider-to-host", max_bytes=65536)
            if set(request) != {"method", "lease_id", "context"} or request["method"] not in ("redeem", "release"):
                raise BrokerError("broker_method_invalid")
            try:
                result = self._handle_request(request, peer.pid)
                reply = {"ok": True, "result": result}
            except BrokerError as exc:
                self._audit_denial(exc.code, peer_pid)
                reply = {"ok": False, "error": exc.code}
            wire.send_frame(conn, reply, host_key, 0, connection_id=transcript["connection_id"],
                            direction="broker-host-to-provider", max_bytes=65536)
            # Acknowledgement proves complete delivery. It never releases an active lease.
            ack = wire.recv_frame(conn, provider_key, 1, connection_id=transcript["connection_id"],
                                  direction="broker-provider-to-host", max_bytes=65536)
            if ack != {"ack": True}:
                raise BrokerError("broker_ack_invalid")
        except (BrokerError, wire.IpcError, ValueError, KeyError, TypeError, OSError) as exc:
            try:
                self._audit_denial(getattr(exc, "code", "broker_protocol_rejected"), peer_pid)
            except Exception:
                pass
        finally:
            conn.close()

    def _handle_request(self, request, peer_pid):
        context = _context(request["context"])
        lease_id = request["lease_id"]
        if type(lease_id) is not str or not lease_id:
            raise BrokerError("broker_lease_unknown")
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise BrokerError("broker_lease_unknown")
            launch = self._launches.get(lease.context["launch_instance_id"])
            if launch is None or launch.record["pid"] != peer_pid or not self._live_launch(launch):
                raise BrokerError("broker_peer_invalid")
            if context != lease.context:
                raise BrokerError("broker_scope_mismatch")
            if request["method"] == "release":
                if lease.state not in ("active", "revoked"):
                    raise BrokerError("broker_lease_not_active")
                self._event(lease, "released", "released")
                lease.grace_until = None
                lease.secret_ref = None
                return {"released": True}
            if lease.state != "issued" or self.clock() >= lease.expires or self.clock() >= context["deadline"]:
                raise BrokerError("broker_lease_unavailable")
            principal, secret_ref = dict(lease.principal), lease.secret_ref
        if not self._authorized(principal, context, "redeem"):
            with self._lock:
                self._expire(lease, "authorization_revoked")
            raise BrokerError("broker_unauthorized")
        try:
            secret = self.resolve_secret(secret_ref, principal, dict(context))
            if not isinstance(secret, dict) or not secret:
                raise ValueError("empty secret")
            if len(wire.canonical(secret)) > 60000:
                raise ValueError("secret too large")
        except Exception as exc:
            raise BrokerError("broker_secret_unavailable") from exc
        with self._lock:
            if lease.state != "issued" or self.clock() >= lease.expires or not self._live_launch(launch):
                raise BrokerError("broker_lease_unavailable")
            self._event(lease, "active", "redeemed")
            lease.secret_ref = None
        return secret
