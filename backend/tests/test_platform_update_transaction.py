"""Actual A/B API + Router + provider-control process transaction recovery."""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from app.runtime.platform_router import control_request
from app.runtime.platform_update import RECEIPTS, UpdateRefused, digest, plan_path, slots_from_config

ROOT = Path(__file__).resolve().parents[2]


def test_compatibility_graph_rejects_atomic_double_pointer_jump():
    old, new = ("A", "old"), ("B", "new")
    with pytest.raises(UpdateRefused, match="invalid_directed_compatibility_edge"):
        plan_path({"nodes": [list(old), list(new)], "edges": [[list(old), list(new)]]}, old, new)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_json(url, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                return json.load(response)
        except (URLError, TimeoutError):
            time.sleep(0.1)
    raise AssertionError(f"endpoint did not start: {url}")


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _write_receipts(config, slot, provider, root):
    receipts = {}
    for name in RECEIPTS:
        value = {
            "output": name, "scope": "fixture", "admitted": True, "frozen": True,
            "profile": config["profile"], "platform_artifact_digest": slot.artifact_digest,
            "provider_package_digest": provider,
            "host_context_digest": config["host_context_digest"],
            "driver_digest": config["driver_digest"], "load_digest": config["load_digest"],
            "os": platform.system().lower(), "cpu": platform.machine().lower(),
            "startup_seconds": 1, "drain_seconds": 1,
            "rss_peak_bytes": 1, "process_peak": 1, "connection_peak": 1, "fd_peak": 1,
            "thresholds": {key: 2 for key in ("startup_seconds", "drain_seconds",
                                               "rss_peak_bytes", "process_peak",
                                               "connection_peak", "fd_peak")},
        }
        if name == "platform-ab-budget-v0":
            value["required_available_memory_bytes"] = 1
            value["required_free_disk_bytes"] = 1
            value["required_fd_soft_limit"] = 1
            value["components"] = ["platform_A", "platform_B", "router", "worker", "provider"]
        value["digest"] = digest(value)
        path = root / f"{name}-{slot.generation}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        receipts[name] = str(path)
    return receipts


@pytest.mark.parametrize("crash_point", ("candidate_started", "step_0_effect", "step_1_effect"))
def test_combination_crash_recovery_compensation_and_missing_gate(tmp_path, crash_point):
    url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    env = {**os.environ, "DATABASE_URL": url, "SERVE_STATIC": "false"}
    receipt = tmp_path / "migration.json"
    migrate = subprocess.run([sys.executable, str(ROOT / "scripts/platform_role.py"), "migrator",
                              "--receipt", str(receipt), "--apply", "--backup-verified",
                              "--service-quiesced"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert migrate.returncode == 0, migrate.stderr
    migration = json.loads(migrate.stdout)["receipt"]
    slots = []
    for generation in ("A", "B"):
        release = tmp_path / "releases" / generation
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        for filename in ("_bootstrap.py", "platform_role.py"):
            shutil.copy2(ROOT / "scripts" / filename, scripts / filename)
        (release / "backend").symlink_to(ROOT / "backend", target_is_directory=True)
        (release / "build-marker").write_text(f"fixture-{generation}", encoding="utf-8")
        slots.append({"generation": generation, "release_root": str(release),
                      "python": sys.executable, "port": free_port(),
                      "release_id": f"fixture-{generation}"})
    ports = {item["generation"]: item["port"] for item in slots}
    ports["router"] = free_port()
    old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    with tempfile.TemporaryDirectory(prefix="tm-update-", dir="/tmp") as short:
        router_sock, provider_sock = Path(short) / "router.sock", Path(short) / "provider.sock"
        config = {
            "scope": "fixture", "allow_fixture": True, "slots": slots,
            "transaction_root": str(tmp_path / "update"),
            "router_socket": str(router_sock), "provider_control_socket": str(provider_sock),
            "migration_receipt": str(receipt), "profile": "fake-l2",
            "host_context_digest": "sha256:" + "c" * 64,
            "driver_digest": "sha256:" + "d" * 64,
            "load_digest": "sha256:" + "e" * 64,
            "engine_id": "mysql",
            "compatibility_graph": {
                "nodes": [["A", old], ["B", old], ["B", new]],
                "edges": [
                    [["A", old], ["B", old]], [["B", old], ["B", new]],
                    [["B", new], ["B", old]], [["B", old], ["A", old]],
                ],
            },
        }
        resolved = slots_from_config(config)
        worker_specs = {}
        for generation, package in (("A", old), ("B", new)):
            identity = {
                "core_release": resolved[generation].release_id,
                "engine_id": "mysql", "package_digest": package,
                "runtime_digest": "sha256:" + "f" * 64,
                "sdk_version": "1.0", "task_schema": "engine.task/v1",
                "result_schema": "engine.result/v1",
            }
            identity_file = tmp_path / f"worker-{generation}.json"
            identity_file.write_text(json.dumps(identity), encoding="utf-8")
            worker_specs[generation] = (identity, identity_file, tmp_path / f"worker-{generation}-status.json")
        config["worker_identity"] = worker_specs["B"][0]
        config["worker_status_file"] = str(worker_specs["B"][2])
        config["capacity_receipts"] = _write_receipts(config, resolved["B"], new, tmp_path)
        path = tmp_path / "update.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        router_config = tmp_path / "router.json"
        router_config.write_text(json.dumps({"active": "A", "backends": [
            {"generation": generation, "url": f"http://127.0.0.1:{ports[generation]}",
             "revision": migration["revision"], "checksum": migration["checksum"],
             "source_checksum": migration["source_checksum"],
             "artifact_digest": resolved[generation].artifact_digest}
            for generation in ("A", "B")]}), encoding="utf-8")
        processes = []
        try:
            for generation in ("A", "B"):
                identity, identity_file, status_file = worker_specs[generation]
                worker = subprocess.Popen([
                    sys.executable, str(Path(resolved[generation].release_root) / "scripts/platform_role.py"),
                    "worker", "--receipt", str(receipt), "--worker-identity", str(identity_file),
                    "--worker-status-file", str(status_file),
                ], cwd=resolved[generation].release_root, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processes.append(worker)
                deadline = time.monotonic() + 5
                while not status_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert status_file.exists() and worker.poll() is None
            api_a = subprocess.Popen([sys.executable, str(slots[0]["release_root"] + "/scripts/platform_role.py"),
                                      "api", "--receipt", str(receipt), "--generation", "A",
                                      "--artifact-digest", resolved["A"].artifact_digest,
                                      "--port", str(ports["A"])],
                                     cwd=slots[0]["release_root"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(api_a)
            wait_json(f"http://127.0.0.1:{ports['A']}/health/ready/platform")
            provider = subprocess.Popen([sys.executable, str(ROOT / "scripts/platform_update_provider_fixture.py"),
                                         "--socket", str(provider_sock), "--active", old, "--candidate", new,
                                         "--update-lease-db", str(tmp_path / "update/platform-update-lease.sqlite")],
                                        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(provider)
            deadline = time.monotonic() + 5
            while not provider_sock.exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            assert provider_sock.exists()
            router = subprocess.Popen([sys.executable, str(ROOT / "scripts/platform_router.py"), "serve",
                                       "--config", str(router_config), "--socket", str(router_sock),
                                       "--update-lease-db", str(tmp_path / "update/platform-update-lease.sqlite"),
                                       "--port", str(ports["router"])], cwd=ROOT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(router)
            wait_json(f"http://127.0.0.1:{ports['router']}/health/ready/platform")

            def update(*arguments, current_path=path):
                return subprocess.run([sys.executable, str(ROOT / "scripts/platform_update.py"),
                                       *arguments, "--config", str(current_path)],
                                      cwd=ROOT, env=env, capture_output=True, text=True, timeout=35)

            missing = tmp_path / RECEIPTS[0]
            old_path = Path(config["capacity_receipts"][RECEIPTS[0]])
            old_path.rename(missing)
            refused = update("apply", "--target-generation", "B", "--target-provider", new)
            assert refused.returncode == 2 and "three_exact" not in refused.stdout
            assert control_request(router_sock, {"command": "status"})["active"] == "A"
            old_path.write_bytes(missing.read_bytes())
            missing.unlink()

            crash = update("apply", "--target-generation", "B", "--target-provider", new,
                           "--crash-after", crash_point)
            assert crash.returncode == 77, crash.stderr
            expected_platform = "A" if crash_point == "candidate_started" else "B"
            expected_provider = new if crash_point == "step_1_effect" else old
            assert control_request(router_sock, {"command": "status"})["active"] == expected_platform
            assert control_request(provider_sock, {"command": "status"})["active_digest"] == expected_provider
            # A dead owner cannot resume until its durable lease expires.
            time.sleep(5.2)
            if crash_point == "step_0_effect":
                saved_receipt = receipt.read_bytes()
                tampered = json.loads(saved_receipt)
                tampered["source_checksum"] = "0" * 64
                receipt.write_text(json.dumps(tampered), encoding="utf-8")
                refused_recovery = update("recover")
                assert refused_recovery.returncode == 2
                assert control_request(router_sock, {"command": "status"})["active"] == "B"
                assert control_request(provider_sock, {"command": "status"})["active_digest"] == old
                receipt.write_bytes(saved_receipt)
            recovered = update("recover")
            assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            assert control_request(provider_sock, {"command": "status"})["active_digest"] == new
            assert control_request(router_sock, {"command": "status"})["active"] == "B"
            if crash_point == "step_0_effect":
                candidate = wait_json(f"http://127.0.0.1:{ports['B']}/health/ready/platform")
                os.kill(candidate["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with urlopen(f"http://127.0.0.1:{ports['B']}/health/ready/platform", timeout=0.5):
                            pass
                    except URLError:
                        break
                    time.sleep(0.05)
                with pytest.raises(HTTPError) as error:
                    urlopen(f"http://127.0.0.1:{ports['router']}/health/ready/platform", timeout=2)
                assert error.value.code == 502
                assert control_request(router_sock, {"command": "status"})["active"] == "B"
                assert control_request(provider_sock, {"command": "status"})["active_digest"] == new

            rollback_config = dict(config)
            rollback_config["capacity_receipts"] = _write_receipts(config, resolved["A"], old, tmp_path)
            rollback_config["worker_identity"] = worker_specs["A"][0]
            rollback_config["worker_status_file"] = str(worker_specs["A"][2])
            rollback_path = tmp_path / "rollback.json"
            rollback_path.write_text(json.dumps(rollback_config), encoding="utf-8")
            rolled = update("apply", "--target-generation", "A", "--target-provider", old,
                            current_path=rollback_path)
            assert rolled.returncode == 0, rolled.stdout + rolled.stderr
            assert control_request(router_sock, {"command": "status"})["active"] == "A"
            assert control_request(provider_sock, {"command": "status"})["active_digest"] == old
            control_request(provider_sock, {"command": "fail-next"})
            failed = update("apply", "--target-generation", "B", "--target-provider", new)
            assert failed.returncode == 2
            assert control_request(router_sock, {"command": "status"})["active"] == "A"
            assert control_request(provider_sock, {"command": "status"})["active_digest"] == old
            with pytest.raises(ValueError, match="fence_lost"):
                control_request(router_sock, {"command": "switch", "target": "B", "expected": "A",
                                              "lease_owner": "stale", "lease_epoch": 1})
            with pytest.raises(ValueError, match="fence_lost"):
                control_request(provider_sock, {"command": "switch", "target": new, "expected": old,
                                                "lease_owner": "stale", "lease_epoch": 1})
            final = wait_json(f"http://127.0.0.1:{ports['B']}/health/ready/platform")
            os.kill(final["pid"], signal.SIGTERM)
        finally:
            try:
                body = wait_json(f"http://127.0.0.1:{ports['B']}/health/ready/platform", timeout=0.3)
                os.kill(body["pid"], signal.SIGTERM)
            except (AssertionError, ProcessLookupError):
                pass
            for process in reversed(processes):
                stop(process)
