"""Stable local HTTP router for two real platform API processes.

The control socket is local and owner-only. A switch is a CAS on the active
generation after a fresh readiness probe against the target process.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_HEADERS = {"connection", "proxy-connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


@dataclass(frozen=True)
class Backend:
    generation: str
    url: str
    revision: str
    checksum: str
    source_checksum: str
    artifact_digest: str = ""


class RouterState:
    def __init__(self, backends: dict[str, Backend], active: str,
                 state_path: Path | None = None, automatic_failover: bool = False):
        if active not in backends or len(backends) != 2:
            raise ValueError("router requires two named backends and an active generation")
        for backend in backends.values():
            endpoint = urlsplit(backend.url)
            if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or not endpoint.port:
                raise ValueError("backend must use an explicit loopback HTTP port")
        self.backends = backends
        self.automatic_failover = automatic_failover
        self.state_path = state_path
        saved = None
        if state_path is not None and state_path.exists():
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            signatures = {key: vars(value) for key, value in backends.items()}
            if (saved.get("format") != 1 or saved.get("active") not in backends
                    or saved.get("previous") not in {*backends, None}
                    or saved.get("backends") != signatures):
                raise ValueError("router_state_invalid")
            active = saved["active"]
        self.active = active
        self.previous: str | None = saved.get("previous") if saved is not None else None
        self.inflight = {key: 0 for key in backends}
        self.lock = threading.Lock()

    def _persist(self, active: str, previous: str | None) -> None:
        if self.state_path is None:
            return
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"format": 1, "active": active, "previous": previous,
                           "backends": {key: vars(value) for key, value in self.backends.items()}}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            from app.runtime.release_checkpoint import sync_directory
            sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self) -> dict:
        with self.lock:
            return {"active": self.active, "previous": self.previous,
                    "automatic_failover": self.automatic_failover,
                    "inflight": dict(self.inflight)}

    def claim(self) -> str:
        with self.lock:
            generation = self.active
            self.inflight[generation] += 1
            return generation

    def release(self, generation: str) -> None:
        with self.lock:
            self.inflight[generation] -= 1

    def probe(self, generation: str) -> dict:
        backend = self.backends[generation]
        endpoint = urlsplit(backend.url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            connection.request("GET", "/health/ready/platform")
            response = connection.getresponse()
            payload = json.loads(response.read(65536))
        finally:
            connection.close()
        if response.status != 200 or not isinstance(payload, dict):
            raise ValueError("target_not_ready")
        migration = payload.get("migration") if isinstance(payload.get("migration"), dict) else {}
        if (payload.get("status") != "ready" or payload.get("role") != "api"
                or payload.get("generation") != backend.generation
                or (backend.artifact_digest and payload.get("artifact_digest") != backend.artifact_digest)
                or migration.get("revision") != backend.revision
                or migration.get("checksum") != backend.checksum
                or migration.get("source_checksum") != backend.source_checksum):
            raise ValueError("target_identity_or_migration_mismatch")
        return payload

    def switch(self, target: str, expected: str) -> dict:
        if target not in self.backends:
            raise ValueError("unknown_target")
        # Probe before taking the short CAS lock. Never infer readiness from a
        # config file or another process's cached observation.
        self.probe(target)
        with self.lock:
            if self.active != expected:
                raise ValueError("route_cas_conflict")
            if target != self.active:
                self._persist(target, self.active)
                self.previous, self.active = self.active, target
            return {"active": self.active, "previous": self.previous, "inflight": dict(self.inflight)}

    def drain(self, generation: str, timeout: float) -> dict:
        if generation not in self.backends or timeout < 0 or timeout > 3600:
            raise ValueError("invalid_drain_request")
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot()
            if snapshot["inflight"][generation] == 0:
                return {"drained": True, **snapshot}
            if time.monotonic() >= deadline:
                return {"drained": False, **snapshot}
            time.sleep(0.02)


def _connect(backend: Backend, timeout: float = 30) -> http.client.HTTPConnection:
    endpoint = urlsplit(backend.url)
    return http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=timeout)


def make_proxy_handler(state: RouterState):
    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self): self._forward()
        def do_HEAD(self): self._forward()
        def do_POST(self): self._forward()
        def do_PUT(self): self._forward()
        def do_PATCH(self): self._forward()
        def do_DELETE(self): self._forward()
        def do_OPTIONS(self): self._forward()

        def _forward(self):
            generation = state.claim()
            self._response_started = False
            try:
                self._forward_to(generation)
            except ValueError:
                if self._response_started:
                    self.close_connection = True
                else:
                    self.send_error(400, "invalid_request_body")
            except (OSError, TimeoutError, http.client.HTTPException):
                # Never replay a mutating request. A new request can use the
                # standby only after its independent readiness check and CAS.
                # Combination updates keep this disabled: the standby may not
                # support the currently active Provider generation.
                if state.automatic_failover:
                    other = next(key for key in state.backends if key != generation)
                    try:
                        state.switch(other, generation)
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                if self._response_started:
                    self.close_connection = True
                else:
                    try:
                        self.send_error(502, "upstream_unavailable")
                    except OSError:
                        pass
            finally:
                state.release(generation)

        def _forward_to(self, generation: str):
            backend = state.backends[generation]
            length_header = self.headers.get("Content-Length")
            transfer_encoding = self.headers.get("Transfer-Encoding", "").lower().strip()
            if transfer_encoding and transfer_encoding != "chunked":
                self.send_error(501, "unsupported_transfer_encoding")
                return
            if transfer_encoding and length_header:
                self.send_error(400, "conflicting_body_framing")
                return
            try:
                length = int(length_header) if length_header else 0
            except ValueError:
                self.send_error(400, "invalid_content_length")
                return
            if length < 0 or length > 1024 * 1024 * 1024:
                self.send_error(413)
                return
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in HOP_HEADERS and key.lower() not in
                       {"host", "x-tracemind-proxy-client-ip", "content-length"}}
            headers["Host"] = urlsplit(backend.url).netloc
            headers["X-Forwarded-For"] = self.client_address[0]
            connection = _connect(backend, timeout=120)
            asset_previous = None
            try:
                connection.putrequest(self.command, self.path, skip_host=True,
                                      skip_accept_encoding=True)
                for key, value in headers.items():
                    connection.putheader(key, value)
                if transfer_encoding:
                    connection.putheader("Transfer-Encoding", "chunked")
                elif length_header is not None:
                    connection.putheader("Content-Length", str(length))
                connection.endheaders()
                if transfer_encoding:
                    total = 0
                    while True:
                        line = self.rfile.readline(128)
                        if len(line) >= 128 or not line.endswith(b"\r\n"):
                            raise ValueError("invalid_chunk_framing")
                        size = int(line.split(b";", 1)[0].strip(), 16)
                        total += size
                        if total > 1024 * 1024 * 1024:
                            raise ValueError("request_body_too_large")
                        connection.send(line)
                        remaining = size
                        while remaining:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                raise ValueError("incomplete_request_body")
                            connection.send(chunk)
                            remaining -= len(chunk)
                        if size:
                            ending = self.rfile.read(2)
                            if ending != b"\r\n":
                                raise ValueError("invalid_chunk_framing")
                            connection.send(ending)
                        else:
                            trailer_bytes = 0
                            while True:
                                trailer = self.rfile.readline(8192)
                                if not trailer:
                                    raise ValueError("incomplete_chunk_trailer")
                                trailer_bytes += len(trailer)
                                if trailer_bytes > 32768:
                                    raise ValueError("chunk_trailer_too_large")
                                connection.send(trailer)
                                if trailer == b"\r\n":
                                    break
                            break
                else:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("incomplete_request_body")
                        connection.send(chunk)
                        remaining -= len(chunk)
                response = connection.getresponse()
                # Previous release keeps serving its original hash assets while
                # in retention. A new release's 404 can fall back to it.
                if (response.status == 404 and self.command in {"GET", "HEAD"}
                        and self.path.startswith("/assets/") and state.snapshot()["previous"]):
                    previous = state.snapshot()["previous"]
                    response.read()
                    connection.close()
                    with state.lock:
                        state.inflight[previous] += 1
                    asset_previous = previous
                    connection = _connect(state.backends[previous], timeout=120)
                    try:
                        connection.request(self.command, self.path, headers=headers)
                        response = connection.getresponse()
                    except (OSError, TimeoutError, http.client.HTTPException):
                        self.send_response_only(404)
                        self._response_started = True
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                self.send_response_only(response.status, response.reason)
                self._response_started = True
                has_length = False
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                        has_length = has_length or key.lower() == "content-length"
                if not has_length:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := response.read(65536):
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except BrokenPipeError:
                            self.close_connection = True
                            return
            finally:
                connection.close()
                if asset_previous is not None:
                    state.release(asset_previous)

        def log_message(self, fmt, *args):
            pass

    return ProxyHandler


def serve_router(state: RouterState, host: str, port: int, control_socket: Path,
                 stop: threading.Event | None = None,
                 update_lease_db: Path | None = None):
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("router listens on loopback; use a trusted edge for public access")
    state.probe(state.active)
    control_socket.parent.mkdir(parents=True, exist_ok=True)
    if control_socket.exists():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(control_socket))
            except ConnectionRefusedError:
                control_socket.unlink()
            else:
                raise FileExistsError(f"control socket already active: {control_socket}")

    class ControlHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(65536))
                command = request.get("command")
                if command == "status":
                    result = state.snapshot()
                elif command == "switch":
                    if update_lease_db is None:
                        result = state.switch(request["target"], request["expected"])
                    else:
                        with sqlite3.connect(update_lease_db, timeout=5) as lease_db:
                            lease_db.execute("BEGIN IMMEDIATE")
                            row = lease_db.execute(
                                "SELECT owner,epoch,expires FROM lease WHERE id=1"
                            ).fetchone()
                            if (row is None or row[0] != request.get("lease_owner") or
                                    row[1] != request.get("lease_epoch") or row[2] <= time.time()):
                                raise ValueError("platform_update_fence_lost")
                            result = state.switch(request["target"], request["expected"])
                elif command == "drain":
                    result = state.drain(request["generation"], float(request.get("timeout", 30)))
                else:
                    raise ValueError("unknown_command")
                self.wfile.write(json.dumps({"ok": True, "result": result}).encode() + b"\n")
            except (ValueError, KeyError, OSError, sqlite3.Error, http.client.HTTPException) as exc:
                self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")

    with ThreadingHTTPServer((host, port), make_proxy_handler(state)) as server:
        with socketserver.ThreadingUnixStreamServer(str(control_socket), ControlHandler) as control:
            os.chmod(control_socket, 0o600)
            thread = threading.Thread(target=control.serve_forever, daemon=True)
            thread.start()
            stopper = None
            if stop is not None:
                stopper = threading.Thread(target=lambda: (stop.wait(), server.shutdown()), daemon=True)
                stopper.start()
            try:
                server.serve_forever()
            finally:
                if stopper is not None:
                    stop.set()
                    stopper.join()
                control.shutdown()
                thread.join()
                control_socket.unlink(missing_ok=True)


def control_request(path: Path, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(35)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b"\n")
        output = json.loads(client.makefile("rb").readline(65536))
    if not output.get("ok"):
        raise ValueError(output.get("error", "router_control_failed"))
    return output["result"]
