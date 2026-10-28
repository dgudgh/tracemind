"""Router drains a long request to the old independent HTTP child."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from app.runtime.platform_router import Backend, RouterState, control_request, serve_router
import pytest


CHILD = '''
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
name, port = sys.argv[1], int(sys.argv[2])
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        if self.path == "/health/ready/platform":
            body = json.dumps({"status":"ready","role":"api","generation":name,"migration":{"revision":"r","checksum":"c","source_checksum":"s"}}).encode()
        elif self.path == "/assets/old-hash.js" and name == "B":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            if self.path == "/slow": time.sleep(1.0)
            body = name.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        result = (str(len(body)) + ":" + name).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)
    def log_message(self, *args): pass
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url):
    for _ in range(100):
        try:
            with urlopen(url, timeout=1) as response:
                return response.read()
        except OSError:
            time.sleep(0.03)
    raise AssertionError(f"no listener at {url}")


def test_inflight_drains_after_real_process_switch():
    ports = {name: free_port() for name in ("A", "B", "router")}
    children = [subprocess.Popen([sys.executable, "-c", CHILD, name, str(ports[name])],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for name in ("A", "B")]
    try:
        for name in ("A", "B"):
            wait_for(f"http://127.0.0.1:{ports[name]}/health/ready/platform")
        state = RouterState({name: Backend(name, f"http://127.0.0.1:{ports[name]}", "r", "c", "s")
                             for name in ("A", "B")}, "A")
        with tempfile.TemporaryDirectory(prefix="tm-router-", dir="/tmp") as root:
            control = Path(root) / "control.sock"
            stop = threading.Event()
            router = threading.Thread(target=serve_router, args=(state, "127.0.0.1", ports["router"], control, stop), daemon=True)
            router.start()
            try:
                wait_for(f"http://127.0.0.1:{ports['router']}/health/ready/platform")
                answer = []
                def slow_request():
                    with urlopen(f"http://127.0.0.1:{ports['router']}/slow", timeout=3) as response:
                        answer.append(response.read())

                slow = threading.Thread(target=slow_request)
                slow.start()
                deadline = time.monotonic() + 2
                while state.snapshot()["inflight"]["A"] == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert state.snapshot()["inflight"]["A"] == 1
                control_request(control, {"command": "switch", "target": "B", "expected": "A"})
                assert control_request(control, {"command": "drain", "generation": "A", "timeout": 0.02})["drained"] is False
                assert wait_for(f"http://127.0.0.1:{ports['router']}/health/ready/platform")
                assert wait_for(f"http://127.0.0.1:{ports['router']}/assets/old-hash.js") == b"A"
                with urlopen(Request(f"http://127.0.0.1:{ports['router']}/upload",
                                     data=b"x" * 100_000), timeout=3) as response:
                    assert response.read() == b"100000:B"
                slow.join(3)
                assert answer == [b"A"]
                assert control_request(control, {"command": "drain", "generation": "A", "timeout": 2})["drained"] is True
            finally:
                stop.set()
                router.join(3)
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=3)


def test_route_state_survives_router_restart_but_rejects_changed_backend(tmp_path):
    backends = {name: Backend(name, f"http://127.0.0.1:{free_port()}", "r", "c", "s")
                for name in ("A", "B")}
    path = tmp_path / "route.json"
    state = RouterState(backends, "A", path)
    state.probe = lambda name: {"generation": name}
    state.switch("B", "A")
    recovered = RouterState(backends, "A", path)
    assert recovered.snapshot()["active"] == "B"
    changed = dict(backends)
    changed["B"] = Backend("B", backends["B"].url, "different", "c", "s")
    with pytest.raises(ValueError, match="router_state_invalid"):
        RouterState(changed, "A", path)
