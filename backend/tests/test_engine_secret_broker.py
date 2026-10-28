"""Real UDS subprocess checks. Test credentials are synthetic and never persisted."""
import json
import os
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import psutil
import pytest

from app.runtime.engine_sdk import wire
from app.runtime.secret_broker import BrokerError, SecretBroker


CHILD = r'''
import hashlib, json, os, sys
from app.runtime.engine_sdk import wire
from app.runtime.engine_sdk.broker_client import BrokerClient, BrokerClientError
_, config = wire.read_launch(int(sys.argv[1]))
client = BrokerClient.from_launch_config(config)
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    try:
        if command["method"] == "redeem":
            value = client.redeem(command["lease_id"], command["context"])
            result = {"ok": True, "matched": hashlib.sha256(value["password"].encode()).hexdigest() == "31ed3a4657ae42b82518adeeed200b5b88b800fc903f09c03d97237e1ed9b8ec"}
        elif command["method"] == "release":
            result = {"ok": client.release(command["lease_id"], command["context"])}
        else:
            result = {"error": "test_invalid_command"}
    except BrokerClientError as exc:
        result = {"error": exc.code}
    print(json.dumps(result), flush=True)
'''


def _root():
    return Path(tempfile.mkdtemp(prefix="sb-", dir="/tmp"))


def _child(broker, launch_id, key):
    read_fd, write_fd = os.pipe()
    identity = {"engine_id": "test.fake", "package_digest": "sha256:" + "a" * 64,
                "runtime_digest": "sha256:" + "b" * 64, "launch_instance_id": launch_id}
    config = {"identity": identity, "secret_broker": {"broker_path": str(broker.path),
              "broker_pid": os.getpid(), "broker_key": key.hex()}}
    proc = subprocess.Popen([os.sys.executable, "-u", "-c", CHILD, str(read_fd)],
                            cwd=str(Path(__file__).resolve().parents[1]),
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            pass_fds=(read_fd,), start_new_session=True, text=True, bufsize=1)
    os.close(read_fd)
    encoded = wire.canonical(config)
    with os.fdopen(write_fd, "wb", buffering=0) as stream:
        stream.write(struct.pack("!I", 32) + b"T" * 32 + struct.pack("!I", len(encoded)) + encoded)
    assert json.loads(proc.stdout.readline()) == {"ready": True}
    return proc, identity


def _record(proc, identity):
    p = psutil.Process(proc.pid)
    return {**identity, "pid": proc.pid, "uid": os.getuid(), "host_pid": os.getpid(),
            "start_time": p.create_time(), "pgid": os.getpgid(proc.pid)}


def _context(identity, *, operation="op-1", target="sha256:" + "c" * 64, scope=None):
    return {**identity, "operation_id": operation, "target_digest": target,
            "capability_id": "db.read", "fence_epoch": 1, "deadline": time.time() + 5,
            "scope": scope or {"read": True}}


def _command(proc, method, lease_id, context):
    proc.stdin.write(json.dumps({"method": method, "lease_id": lease_id, "context": context}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.communicate(timeout=2)


def _broker(root, *, authorization=None, on_close=None, on_term=None, grace=.05):
    return SecretBroker(root, authorize=authorization or (lambda p, c, phase: {"scope": {"read": True}}),
                        resolve_secret=lambda ref, p, c: {"password": "TEST_ONLY_SECRET_VALUE"},
                        on_close_request=on_close or (lambda launch, reason: None),
                        on_terminate=on_term or (lambda launch, reason: None),
                        grace_seconds=grace, scan_interval=.02).start()


def test_real_peer_scope_replay_release_and_no_persistence():
    root = _root()
    broker = _broker(root)
    key = os.urandom(32)
    proc, identity = _child(broker, "launch-good", key)
    wrong, _ = _child(broker, "launch-good", key)
    try:
        broker.register_launch(_record(proc, identity), channel_key=key)
        context = _context(identity)
        with pytest.raises(BrokerError, match="broker_unauthorized"):
            broker.grant("vault/test/ref", {"id": "principal-test"},
                         {**context, "scope": {"write": True}}, 3)
        with pytest.raises(BrokerError, match="broker_grant_invalid"):
            broker.grant("vault/test/ref", {"id": "principal-test"}, context, 60)
        lease = broker.grant("vault/test/ref", {"id": "principal-test"}, context, 3)
        assert _command(wrong, "redeem", lease, context)["error"] == "broker_transport_failed"
        assert _command(proc, "redeem", lease, {**context, "operation_id": "other"})["error"] == "broker_scope_mismatch"
        assert _command(proc, "redeem", lease, context) == {"ok": True, "matched": True}
        assert _command(proc, "redeem", lease, context)["error"] == "broker_lease_unavailable"
        assert _command(proc, "release", lease, context) == {"ok": True}
        assert _command(proc, "release", lease, context)["error"] == "broker_lease_not_active"
        lease2 = broker.grant("vault/test/ref", {"id": "principal-test"}, context, 3)
        assert _command(proc, "redeem", lease2, {**context, "target_digest": "sha256:" + "d" * 64})["error"] == "broker_scope_mismatch"
        assert _command(proc, "redeem", lease2, {**context, "scope": {"write": True}})["error"] == "broker_scope_mismatch"
        assert _command(proc, "redeem", lease2, context) == {"ok": True, "matched": True}
        assert _command(proc, "release", lease2, context) == {"ok": True}
        all_bytes = b"".join(p.read_bytes() for p in root.iterdir() if p.is_file())
        assert b"TEST_ONLY_SECRET_VALUE" not in all_bytes
        assert b"vault/test/ref" not in all_bytes
        with sqlite3.connect(broker.db_path) as db:
            codes = {row[0] for row in db.execute("SELECT code FROM security_events")}
            assert {"broker_peer_invalid", "broker_scope_mismatch"} <= codes
        assert b"TEST_ONLY_SECRET_VALUE" not in " ".join(psutil.Process(proc.pid).cmdline()).encode()
        assert b"TEST_ONLY_SECRET_VALUE" not in repr(psutil.Process(proc.pid).environ()).encode()
    finally:
        _stop(wrong); _stop(proc); broker.close()


def test_expired_redeemed_lease_requests_close_then_terminates_noncooperator():
    root = _root()
    called = []
    proc_holder = {}
    def terminate(launch, reason):
        called.append(("term", launch, reason))
        process = proc_holder["proc"]
        if process.poll() is None:
            process.terminate()
    broker = _broker(root, on_close=lambda launch, reason: called.append(("close", launch, reason)),
                     on_term=terminate, grace=.05)
    key = os.urandom(32)
    proc, identity = _child(broker, "launch-expire", key)
    proc_holder["proc"] = proc
    try:
        broker.register_launch(_record(proc, identity), channel_key=key)
        context = _context(identity)
        lease = broker.grant("vault/test/ref", {"id": "principal-test"}, context, .15)
        assert _command(proc, "redeem", lease, context) == {"ok": True, "matched": True}
        proc.wait(timeout=3)
        until = time.time() + 1
        while not any(event[0] == "close" for event in called) and time.time() < until:
            time.sleep(.01)
        assert any(event[:2] == ("close", "launch-expire") for event in called)
        assert any(event[0] == "term" for event in called)
        with sqlite3.connect(broker.db_path) as db:
            assert db.execute("SELECT state FROM leases").fetchone()[0] == "revoked"
    finally:
        _stop(proc); broker.close()


def test_restart_revokes_persisted_unconsumed_metadata_and_authorization_recheck():
    root = _root()
    db_path = root / "leases.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE leases (lease_hash TEXT PRIMARY KEY, launch_hash TEXT NOT NULL, state TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL, changed REAL NOT NULL)")
        db.execute("INSERT INTO leases VALUES (?,?,?,?,?,?)", ("old-hash", "launch-hash", "active", 1, 60, 1))
    os.chmod(db_path, 0o600)
    enabled = {"value": True}
    def auth(principal, context, phase):
        return {"scope": {"read": True}} if enabled["value"] else None
    broker = _broker(root, authorization=auth)
    key = os.urandom(32)
    proc, identity = _child(broker, "launch-recheck", key)
    try:
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT state FROM leases WHERE lease_hash='old-hash'").fetchone()[0] == "revoked"
        broker.register_launch(_record(proc, identity), channel_key=key)
        context = _context(identity)
        lease = broker.grant("vault/test/ref", {"id": "principal-test"}, context, 3)
        enabled["value"] = False
        assert _command(proc, "redeem", lease, context)["error"] in {
            "broker_unauthorized", "broker_lease_unavailable"}
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT state FROM leases WHERE lease_hash=?",
                (__import__('hashlib').sha256(lease.encode()).hexdigest(),)).fetchone()[0] == 'revoked'
        with pytest.raises(BrokerError, match="broker_unauthorized"):
            broker.grant("vault/test/ref", {"id": "principal-test"}, context, 3)
    finally:
        _stop(proc); broker.close()
