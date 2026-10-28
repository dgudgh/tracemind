"""Standalone fixture Provider server: multi-connection UDS, HMAC transcript, strict JSON.

契约范围（与 backend/tests/test_engine_host_dispatch.py 对齐）：
- `invoke_read`：`echo` / `sleep`（可被协作取消打断）/ `output_bytes` / `spawn_child` / `ignore_cancel`
- `execute`：写 provider_effects 表（幂等键 = operation_id），提交后才 `reply_delay`
- `verify`：按 operation_id + input_digest 找回结果
- `heartbeat`：自述 launch_instance_id / queue / inflight / connections
- `cancel`：任意连接可投递取消标记，运行中的调用在下一个检查点退出

回显约定：请求里的 `context` 与 `optional_fields` 原样回到结果里。宿主负责把 CONTEXT_FIELDS
填全，Provider 只做透传——这样"结果身份"不会由 Provider 猜测。
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
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
LAUNCH_ID = ""
STOP = threading.Event()

# 协作取消：按 operation_id 打标记；运行中的调用在每个检查点轮询。
CANCEL_LOCK = threading.Lock()
CANCELLED: set[str] = set()
# 自述计数：宿主用心跳校验它们是否与实际一致（负值或身份不符即熔断）。
COUNT_LOCK = threading.Lock()
INFLIGHT = 0
CONNECTIONS = 0
CHILDREN: list[subprocess.Popen] = []
CHILD_LOCK = threading.Lock()


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


def is_cancelled(operation_id):
    if not operation_id:
        return False
    with CANCEL_LOCK:
        return operation_id in CANCELLED


def wait_interruptible(seconds, operation_id, *, ignore_cancel=False):
    """可中断等待。`ignore_cancel` 用于验证"不合作的 Provider 会被进程级终止"。"""
    until = time.monotonic() + float(seconds)
    while time.monotonic() < until:
        if not ignore_cancel and is_cancelled(operation_id):
            return True
        time.sleep(0.005)
    return not ignore_cancel and is_cancelled(operation_id)


def spawn_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with CHILD_LOCK:
        CHILDREN.append(child)
    return child.pid


def _effect_write(connection_id, operation_id, input_digest, params, reply_delay, ignore_cancel):
    """提交副作用。返回 (payload 附加项, 是否被取消)。

    关键顺序：**先落库，再延迟回复**。这样"取消时副作用是否已提交"是一个可观测事实，
    而不是靠宿主猜——宿主据此把 effect_state 报成 completed / none。
    """
    if not (EFFECTS_PATH and operation_id):
        if reply_delay:
            wait_interruptible(reply_delay, operation_id, ignore_cancel=ignore_cancel)
        return {}, is_cancelled(operation_id) and not ignore_cancel
    db = sqlite3.connect(EFFECTS_PATH, isolation_level=None, timeout=15)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS provider_effects("
                   "operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,"
                   "value TEXT NOT NULL,result TEXT NOT NULL,committed_at REAL NOT NULL)")
        row = db.execute("SELECT input_digest, result FROM provider_effects WHERE operation_id=?",
                         (operation_id,)).fetchone()
        if row is not None and row[0] != input_digest:
            return {"error": "effect_idempotency_conflict"}, False
        if row is None:
            result_value = {"operation_id": operation_id, "value": params.get("value"),
                            "effect_state": "completed"}
            db.execute("INSERT INTO provider_effects VALUES (?,?,?,?,?)",
                       (operation_id, input_digest, canonical(params.get("value")).decode(),
                        canonical(result_value).decode(), time.time()))
            committed = True
        else:
            result_value = json.loads(row[1])
            committed = True
        # 已提交：即使此刻收到取消请求，副作用也已经发生了。
        delay = float(reply_delay or 0)
        if delay:
            wait_interruptible(delay, operation_id, ignore_cancel=ignore_cancel)
        cancelled = is_cancelled(operation_id) and not ignore_cancel
        return {"value": result_value.get("value"), "committed": committed,
                "effect_state": "completed"}, cancelled
    finally:
        db.close()


def handle(conn):
    global CONNECTIONS
    with COUNT_LOCK:
        CONNECTIONS += 1
    try:
        uid, pid = peer_uid_pid(conn)
        if uid != os.getuid() or (EXPECTED_HOST_PID is not None and pid is not None and pid != EXPECTED_HOST_PID):
            # 有非授权进程连到了 Provider 的 socket：整个 Provider 必须停下来，
            # 而不是"只关掉这一条连接"继续服务——那等于承认端点已被第三方接触过。
            os._exit(2)
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
            optional_fields = request.get("optional_fields") or {}
            return_envelope = request.get("return_envelope") is True
            operation_id = str(context.get("operation_id") or "")
            # verify/prepare 带的是"原操作"的输入摘要，与本次 params 不同：
            # 优先用宿主给的，只有缺席时才按本次参数算。
            input_digest = str(context.get("input_digest")
                               or hashlib.sha256(canonical(params)).hexdigest())
            echo = {"context": context}
            if optional_fields:
                echo["optional_fields"] = optional_fields

            if method == "cancel":
                target = str(params.get("operation_id") or operation_id)
                with CANCEL_LOCK:
                    CANCELLED.add(target)
                payload = {"ok": True, "cancelled": target, "marker": MARKER}
            elif method == "invoke_read":
                if params.get("spawn_child"):
                    spawn_child()
                cancelled = False
                if params.get("sleep"):
                    cancelled = wait_interruptible(float(params["sleep"]), operation_id,
                                                   ignore_cancel=bool(params.get("ignore_cancel")))
                values = {"marker": MARKER, "echo": params.get("echo"),
                          "rows": [{"id": 1}, {"id": 2}] if VERSION == "1.0.1" else [{"id": 1}],
                          "new_optional": "v2-only" if VERSION == "1.0.1" else None}
                if params.get("output_bytes"):
                    values["blob"] = "x" * int(params["output_bytes"])
                if cancelled:
                    payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                               "values": values, "cancelled": True, "effect_state": "none", **echo}
                else:
                    payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                               "values": values, **echo}
            elif method == "handshake":
                payload = {"engine_id": "test.fake", "provider_version": VERSION, "marker": MARKER}
            elif method == "selftest":
                payload = {"ok": True, "marker": MARKER}
            elif method == "heartbeat":
                with COUNT_LOCK:
                    payload = {"ok": True, "marker": MARKER, "queue": 0,
                               "inflight": INFLIGHT, "connections": CONNECTIONS,
                               "target_connections": 0, "launch_instance_id": LAUNCH_ID}
            elif method == "drain":
                payload = {"ok": True, "draining": True, "marker": MARKER}
            elif method == "execute":
                extra, cancelled = _effect_write(
                    conn.fileno(), operation_id, input_digest, params,
                    params.get("reply_delay"), bool(params.get("ignore_cancel")))
                if extra.get("error"):
                    payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                               "error": extra["error"], **echo}
                elif cancelled:
                    payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                               "cancelled": True,
                               "effect_state": extra.get("effect_state", "completed"), **echo}
                else:
                    payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                               "ok": True, "found": True, "value": extra.get("value"),
                               "effect_state": extra.get("effect_state", "completed"),
                               "marker": MARKER, **echo}
            elif method in {"prepare", "verify"}:
                found = False
                stored = None
                if EFFECTS_PATH and operation_id:
                    db = sqlite3.connect(EFFECTS_PATH, isolation_level=None, timeout=15)
                    try:
                        db.execute("CREATE TABLE IF NOT EXISTS provider_effects("
                                   "operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,"
                                   "value TEXT NOT NULL,result TEXT NOT NULL,committed_at REAL NOT NULL)")
                        row = db.execute("SELECT result FROM provider_effects "
                                         "WHERE operation_id=? AND input_digest=?",
                                         (operation_id, input_digest)).fetchone()
                        found = row is not None
                        if found:
                            stored = json.loads(row[0])
                    finally:
                        db.close()
                payload = {"schema": "engine.result/v1", "provider_version": VERSION,
                           "ok": True, "found": found, "result": stored,
                           "effect_state": "prepared" if method == "prepare" else (
                               "completed" if found else "absent"),
                           "marker": MARKER, **echo}
            else:
                payload = {"error": "unknown_method"}
            if method == "invoke_read" and not return_envelope:
                # 非信封模式：调用方习惯直接取 result['echo']；信封模式下不加，
                # 否则 decode_result 会因未知字段拒绝整份结果。
                payload["echo"] = params.get("echo")
            send_frame(conn, payload, provider_key, seq - 1)
    except Exception:
        return
    finally:
        with COUNT_LOCK:
            CONNECTIONS -= 1
        try:
            conn.close()
        except OSError:
            pass


def main():
    global TOKEN, SOCKET, VERSION, MARKER, EXPECTED_HOST_PID, EFFECTS_PATH, LAUNCH_ID
    token_len = struct.unpack("!I", sys.stdin.buffer.read(4))[0]
    if token_len != 32:
        raise SystemExit(2)
    TOKEN = sys.stdin.buffer.read(token_len)
    SOCKET = sys.argv[1]
    VERSION = sys.argv[2] if len(sys.argv) > 2 else "1.0.0"
    MARKER = "v1" if VERSION == "1.0.0" else "v2"
    EXPECTED_HOST_PID = int(sys.argv[3]) if len(sys.argv) > 3 else None
    EFFECTS_PATH = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    LAUNCH_ID = sys.argv[5] if len(sys.argv) > 5 else ""
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
