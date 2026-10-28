"""Bounded authenticated local transport, shared by Host and signed Providers."""
from __future__ import annotations
import ctypes
import json
import math
import os
import socket
import struct
from dataclasses import dataclass
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_FRAME_BYTES = 1024 * 1024
MAX_WIRE_BYTES = MAX_FRAME_BYTES + 4096
MAX_DEPTH = 32
MAX_NODES = 65536


class IpcError(ValueError):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, detail
        super().__init__(code)


def validate_tree(value, depth=0, budget=None):
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= 1
    if depth > MAX_DEPTH:
        raise IpcError("frame_json_too_deep")
    if budget[0] < 0:
        raise IpcError("frame_json_too_complex")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise IpcError("frame_json_invalid")
            validate_tree(child, depth + 1, budget)
    elif isinstance(value, list):
        for child in value:
            validate_tree(child, depth + 1, budget)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif not isinstance(value, float) or not math.isfinite(value):
        raise IpcError("frame_json_invalid")


def strict_json(raw, *, max_bytes=MAX_FRAME_BYTES):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        raise IpcError("frame_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if not isinstance(key, str) or key in result:
                raise IpcError("frame_json_invalid")
            result[key] = value
        return result
    def constant(_):
        raise IpcError("frame_json_invalid")
    try:
        value = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
        validate_tree(value)
    except IpcError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise IpcError("frame_json_invalid") from exc
    if not isinstance(value, dict):
        raise IpcError("frame_json_invalid")
    return value


def canonical(value, *, validate=True):
    try:
        if validate:
            validate_tree(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except IpcError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IpcError("frame_json_invalid") from exc


def mac(key, data):
    signer = hmac.HMAC(key, hashes.SHA256())
    signer.update(data)
    return signer.finalize()


def verify_mac(key, data, signature):
    verifier = hmac.HMAC(key, hashes.SHA256())
    verifier.update(data)
    try:
        verifier.verify(signature)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise IpcError("frame_mac_invalid") from exc


def session_keys(token, host_nonce, provider_nonce, host_id="", provider_id="", *, transcript=None):
    if not isinstance(token, bytes) or len(token) != 32:
        raise IpcError("launch_secret_invalid")
    if len(host_nonce) != 32 or len(provider_nonce) != 32:
        raise IpcError("nonce_invalid")
    binding = canonical(transcript or {"host_id": host_id, "provider_id": provider_id})
    return tuple(HKDF(algorithm=hashes.SHA256(), length=32, salt=host_nonce + provider_nonce,
                      info=b"tmengine.rpc/v1\0" + label + b"\0" + binding).derive(token)
                 for label in (b"host-to-provider", b"provider-to-host"))


def encode_frame(payload, key, sequence, *, connection_id="", direction="", max_bytes=MAX_FRAME_BYTES):
    if type(sequence) is not int or sequence < 0:
        raise IpcError("frame_sequence_invalid")
    envelope = {"seq": sequence, "payload": payload}
    if connection_id or direction:
        envelope.update(connection_id=connection_id, direction=direction)
    body = canonical(envelope)
    if len(body) > max_bytes:
        raise IpcError("frame_too_large")
    frame = mac(key, body).hex().encode("ascii") + b"." + body
    return struct.pack("!I", len(frame)) + frame


def decode_frame(raw, key, expected_sequence, *, connection_id="", direction="", max_bytes=MAX_FRAME_BYTES):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes + 65:
        raise IpcError("frame_too_large")
    try:
        signature, body = bytes(raw).split(b".", 1)
        if len(signature) != 64:
            raise ValueError("signature length")
        signature = bytes.fromhex(signature.decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise IpcError("frame_malformed") from exc
    verify_mac(key, body, signature)
    value = strict_json(body, max_bytes=max_bytes)
    fields = {"seq", "payload"}
    if connection_id or direction:
        fields |= {"connection_id", "direction"}
        if value.get("connection_id") != connection_id or value.get("direction") != direction:
            raise IpcError("frame_connection_invalid")
    if set(value) != fields:
        raise IpcError("frame_envelope_invalid")
    if type(value["seq"]) is not int or value["seq"] != expected_sequence:
        raise IpcError("frame_sequence_invalid")
    if not isinstance(value["payload"], dict):
        raise IpcError("frame_payload_invalid")
    return value["payload"]


def recv_exact(sock, length):
    if type(length) is not int or length < 0 or length > MAX_WIRE_BYTES:
        raise IpcError("frame_too_large")
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise IpcError("frame_truncated")
        data.extend(chunk)
    return bytes(data)


def recv_frame(sock, key, expected_sequence, **kwargs):
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    if length > kwargs.get("max_bytes", MAX_FRAME_BYTES) + 65:
        raise IpcError("frame_too_large")
    return decode_frame(recv_exact(sock, length), key, expected_sequence, **kwargs)


def send_frame(sock, payload, key, sequence, **kwargs):
    sock.sendall(encode_frame(payload, key, sequence, **kwargs))


def send_plain(sock, payload):
    body = canonical(payload)
    if len(body) > 65536:
        raise IpcError("handshake_too_large")
    sock.sendall(struct.pack("!I", len(body)) + body)


def recv_plain(sock):
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    if length > 65536:
        raise IpcError("handshake_too_large")
    return strict_json(recv_exact(sock, length), max_bytes=65536)


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    gid: int
    pid: int


def peer_credentials(sock):
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    elif os.uname().sysname == "Darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        u, g, p = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_int()
        size = ctypes.c_uint(ctypes.sizeof(p))
        if libc.getpeereid(sock.fileno(), ctypes.byref(u), ctypes.byref(g)) != 0:
            raise IpcError("peer_identity_unsupported")
        if libc.getsockopt(sock.fileno(), 0, 2, ctypes.byref(p), ctypes.byref(size)) != 0:
            raise IpcError("peer_identity_unsupported")
        if size.value != ctypes.sizeof(p):
            raise IpcError("peer_identity_unsupported")
        uid, gid, pid = u.value, g.value, p.value
    else:
        raise IpcError("peer_identity_unsupported")
    if pid <= 0:
        raise IpcError("peer_identity_unsupported")
    return PeerCredentials(int(uid), int(gid), int(pid))


def read_fd_exact(fd, size):
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise IpcError("launch_secret_truncated")
        data.extend(chunk)
    return bytes(data)


def read_launch(fd):
    size = struct.unpack("!I", read_fd_exact(fd, 4))[0]
    if size != 32:
        raise IpcError("launch_secret_invalid")
    token = read_fd_exact(fd, size)
    length = struct.unpack("!I", read_fd_exact(fd, 4))[0]
    if length > 65536:
        raise IpcError("launch_config_invalid")
    config = strict_json(read_fd_exact(fd, length), max_bytes=65536)
    os.close(fd)
    return token, config
