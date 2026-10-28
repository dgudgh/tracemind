"""Source release launcher runs separate A/B API children and stable Router."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_launcher_starts_distinct_source_release_roots(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    receipt = tmp_path / "receipt.json"
    env = {**os.environ, "DATABASE_URL": db_url}
    slots = []
    for generation in ("A", "B"):
        release = tmp_path / "releases" / generation
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        for name in ("_bootstrap.py", "platform_role.py"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (release / "backend").symlink_to(ROOT / "backend", target_is_directory=True)
        slots.append({"generation": generation, "release_root": str(release),
                      "python": sys.executable, "port": free_port()})
    router_port = free_port()
    runtime = tmp_path / "shared"
    config = tmp_path / "launch.json"
    config.write_text(json.dumps({"slots": slots, "active": "A", "router_port": router_port,
                                  "runtime_root": str(runtime), "receipt": str(receipt)}), encoding="utf-8")
    launcher = subprocess.Popen([sys.executable, str(ROOT / "scripts/platform_router_launcher.py"),
                                 "--config", str(config), "--migrate-with", "A",
                                 "--backup-verified", "--service-quiesced"],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if launcher.poll() is not None:
                raise AssertionError(f"launcher exited: {launcher.stderr.read()}")
            try:
                with urlopen(f"http://127.0.0.1:{router_port}/health/ready/platform", timeout=2) as response:
                    body = json.load(response)
                if body.get("status") == "ready":
                    break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise AssertionError("router listener missing")
        assert body["generation"] == "A"
        assert receipt.is_file()
        from app.runtime.platform_router import control_request

        control = Path(json.loads(launcher.stdout.readline())["control_socket"])
        assert control_request(control, {"command": "switch", "target": "B", "expected": "A"})["active"] == "B"
        with urlopen(f"http://127.0.0.1:{router_port}/health/ready/platform", timeout=3) as response:
            assert json.load(response)["generation"] == "B"
    finally:
        launcher.terminate()
        try:
            launcher.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            launcher.kill()
            launcher.communicate(timeout=5)
