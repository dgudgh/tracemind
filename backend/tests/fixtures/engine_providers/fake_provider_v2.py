"""Standalone fixture Provider server: multi-connection UDS, HMAC transcript, strict JSON."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import signal
import socket
import struct
import sys
import threading
import sqlite3
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_FRAME = 1024 * 1024
TOKEN = None
SOCKET = None
VERSION = "1.0.0"
MARKER = "v1"
EXPECTED_HOST_PID = None
EFFECTS_PATH = None
STOP = threading.Event()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def _depth(value, depth=0):
    if depth > 32:
        raise ValueError("json_too_deep")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("json_key_invalid")
            _depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, depth + 1)
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError("json_invalid")


def strict_loads(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if not isinstance(key, str) or key in result:
                raise ValueError("duplicate_or_non_string_json_key")
            result[key] = value
        return result
    def no_constant(_):
        raise ValueError("non_finite_json_number")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=no_constant)
    if not isinstance(value, dict):
        raise ValueError("json_root_invalid")
    _depth(value)
    return value


def session_keys(token, host_nonce, provider_nonce, launch_id):
    binding = canonical({"host_id": "host", "provider_id": "provider", "launch_id": launch_id})
    salt = host_nonce + provider_nonce
    return tuple(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                      info=b"tmengine.rpc/v1\0" + label + b"\0" + binding).derive(token)
                 for label in (b"host-to-provider", b"provider-to-host"))


def recv_raw(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def recv_json(sock):
    length = struct.unpack("!I", recv_raw(sock, 4))[0]
    if length > MAX_FRAME:
        raise ValueError("frame_too_large")
    return strict_loads(recv_raw(sock, length))


def send_json(sock, value):
    body = canonical(value)
    sock.sendall(struct.pack("!I", len(body)) + body)


def recv_frame(sock, key, seq):
    length = struct.unpack("!I", recv_raw(sock, 4))[0]
    if length > MAX_FRAME:
        raise ValueError("frame_too_large")
    raw = recv_raw(sock, length)
    mac, body = raw.split(b".", 1)
    if len(mac) != 64 or not hmac.compare_digest(mac, hmac.new(key, body, hashlib.sha256).hexdigest().encode()):
        raise ValueError("frame_mac_invalid")
    value = strict_loads(body)
    if set(value) != {"seq", "payload"}:
        raise ValueError("frame_envelope_invalid")
    if type(value["seq"]) is not int or value["seq"] != seq:
        raise ValueError("frame_sequence_invalid")
    if not isinstance(value["payload"], dict):
        raise ValueError("frame_payload_invalid")
    return value["payload"]


def send_frame(sock, payload, key, seq):
    body = canonical({"seq": seq, "payload": payload})
    mac = hmac.new(key, body, hashlib.sha256).hexdigest().encode()
    sock.sendall(struct.pack("!I", len(mac) + 1 + len(body)) + mac + b"." + body)


def peer_uid_pid(sock):
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return uid, pid
    libc = ctypes.CDLL(None, use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise ValueError("peer_identity_unsupported")
    value = ctypes.c_int(); size = ctypes.c_uint(ctypes.sizeof(value))
    pid = None
    if libc.getsockopt(sock.fileno(), 0, 0x002, ctypes.byref(value), ctypes.byref(size)) == 0:
        pid = int(value.value)
    return int(uid.value), pid


def handle(conn):
    try:
        uid, pid = peer_uid_pid(conn)
        if uid != os.getuid() or (EXPECTED_HOST_PID is not None and pid is not None and pid != EXPECTED_HOST_PID):
            return
        conn.settimeout(15)
        provider_nonce = os.urandom(32)
        send_json(conn, {"hello": "v1", "nonce": provider_nonce.hex(), "provider_version": VERSION})
        challenge = recv_json(conn)
        host_nonce = bytes.fromhex(challenge["nonce"])
        host_id = str(challenge.get("host_id", ""))
        launch_id = str(challenge.get("launch_id", ""))
        transcript = (b"hello" + provider_nonce + host_nonce + host_id.encode() +
                      launch_id.encode() + VERSION.encode())
        expected = hmac.new(TOKEN, transcript, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(challenge.get("auth"))):
            return
        send_json(conn, {"auth": hmac.new(TOKEN, b"auth" + host_nonce + provider_nonce +
                                          host_id.encode() + launch_id.encode(),
                                          hashlib.sha256).hexdigest()})
        host_key, provider_key = session_keys(TOKEN, host_nonce, provider_nonce, launch_id)
        seq = 0
        while not STOP.is_set():
            request = recv_frame(conn, host_key, seq)
            seq += 1
            method = request.get("method")
            params = request.get("params") or {}
            context = request.get("context") or {}
            operation_id = str(context.get("operation_id") or "")
            input_digest = hashlib.sha256(canonical(params)).hexdigest()
            if method == "invoke_read":
                if params.get("sleep"):
                    import time
                    time.sleep(float(params["sleep"]))
                payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                           "values": {"marker": MARKER, "echo": params.get("echo"),
                                      "rows": [{"id": 1}, {"id": 2}] if VERSION == "1.0.1" else [{"id": 1}],
                                      "new_optional": "v2-only" if VERSION == "1.0.1" else None}}
            elif method == "handshake":
                payload = {"engine_id": "test.fake", "provider_version": VERSION, "marker": MARKER}
            elif method == "selftest":
                payload = {"ok": True, "marker": MARKER}
            elif method == "heartbeat":
                payload = {"ok": True, "marker": MARKER, "queue": 0, "inflight": 0}
            elif method == "drain":
                payload = {"ok": True, "draining": True, "marker": MARKER}
            elif method in {"prepare", "execute", "verify"}:
                found = False
                if EFFECTS_PATH and operation_id:
                    db = sqlite3.connect(EFFECTS_PATH, isolation_level=None, timeout=15)
                    try:
                        db.execute("CREATE TABLE IF NOT EXISTS provider_effects("
                                   "operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,"
                                   "value TEXT NOT NULL,result TEXT NOT NULL,committed_at REAL NOT NULL)")
                        if method == "execute":
                            row = db.execute("SELECT * FROM provider_effects WHERE operation_id=?",
                                             (operation_id,)).fetchone()
                            if row is None:
                                result_value = {"operation_id": operation_id,
                                                "effect_state": "completed", "value": params}
                                db.execute("INSERT INTO provider_effects VALUES (?,?,?,?,?)",
                                           (operation_id, input_digest, canonical(params).decode(),
                                            canonical(result_value).decode(), __import__("time").time()))
                                found = True
                            else:
                                if row[1] != input_digest:
                                    payload = {"error": "effect_idempotency_conflict"}; found = None
                                else:
                                    found = True
                        elif method == "verify":
                            found = db.execute("SELECT 1 FROM provider_effects WHERE operation_id=? AND input_digest=?",
                                               (operation_id, input_digest)).fetchone() is not None
                    finally:
                        db.close()
                payload = {"ok": True, "found": found,
                           "effect_state": "prepared" if method == "prepare" else ("completed" if found else "absent"),
                           "marker": MARKER}
            else:
                payload = {"error": "unknown_method"}
            send_frame(conn, payload, provider_key, seq - 1)
    except Exception:
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    global TOKEN, SOCKET, VERSION, MARKER, EXPECTED_HOST_PID, EFFECTS_PATH
    token_len = struct.unpack("!I", sys.stdin.buffer.read(4))[0]
    if token_len != 32:
        raise SystemExit(2)
    TOKEN = sys.stdin.buffer.read(token_len)
    SOCKET = sys.argv[1]
    VERSION = sys.argv[2] if len(sys.argv) > 2 else "1.0.0"
    MARKER = "v1" if VERSION == "1.0.0" else "v2"
    EXPECTED_HOST_PID = int(sys.argv[3]) if len(sys.argv) > 3 else None
    EFFECTS_PATH = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    server.bind(SOCKET)
    os.chmod(SOCKET, 0o600)
    server.listen(32)
    signal.signal(signal.SIGTERM, lambda *_: (STOP.set(), server.close()))
    while not STOP.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    try:
        server.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()
