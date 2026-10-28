"""Managed roles and router against isolated SQLite and real API subprocesses."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url: str):
    try:
        with urlopen(url, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        data = exc.read()
        return exc.code, json.loads(data) if exc.headers.get_content_type() == "application/json" else data


def wait_http(url: str, process: subprocess.Popen, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"process exited early: {process.returncode}: {process.stderr.read()}")
        try:
            return request(url)
        except (URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)
    raise AssertionError(f"server did not start: {url}")


def run_role(role, url, receipt, *extra):
    env = {**os.environ, "DATABASE_URL": url, "SERVE_STATIC": "false"}
    return subprocess.run(
        [PYTHON, str(ROOT / "scripts/platform_role.py"), role, "--receipt", str(receipt), *extra],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )


def terminate(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=8)


def test_managed_api_refuses_unmigrated_schema_without_ddl(tmp_path):
    from sqlalchemy import create_engine, inspect

    url = f"sqlite:///{tmp_path / 'unmigrated.sqlite'}"
    result = run_role("api", url, tmp_path / "missing.json", "--generation", "A", "--port", str(port()))
    assert result.returncode != 0
    engine = create_engine(url)
    try:
        assert not inspect(engine).has_table("db_instance")
    finally:
        engine.dispose()


def test_managed_api_rejects_source_checksum_receipt_mismatch(tmp_path):
    url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    receipt = tmp_path / "receipt.json"
    assert run_role("migrator", url, receipt, "--apply", "--backup-verified", "--service-quiesced").returncode == 0
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["source_checksum"] = "0" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    rejected = run_role("api", url, receipt, "--generation", "A", "--port", str(port()))
    assert rejected.returncode != 0
    assert "migration_receipt_schema_mismatch" in rejected.stderr


def test_managed_worker_passes_exact_identity_to_queue(monkeypatch):
    from app.tasks import plan_worker

    identity = {"core_release": "P", "engine_id": "mysql",
                "package_digest": "sha256:" + "a" * 64,
                "runtime_digest": "sha256:" + "b" * 64,
                "sdk_version": "1.0", "task_schema": "engine.task/v1",
                "result_schema": "engine.result/v1"}
    captured = []
    monkeypatch.setattr(plan_worker, "process_one",
                        lambda _factory, **kwargs: captured.append(kwargs) or None)
    assert plan_worker.run_worker(object(), worker_id="managed-test", once=True,
                                  worker_identity=identity) == 0
    assert captured == [{"worker_id": "managed-test", "worker_identity": identity}]


def test_managed_worker_rejects_missing_identity_before_claim(tmp_path):
    result = run_role("worker", f"sqlite:///{tmp_path / 'db.sqlite'}", tmp_path / "receipt.json", "--once")
    assert result.returncode != 0
    assert "requires an exact --worker-identity" in result.stderr


def test_real_api_ab_route_receipt_and_failover(tmp_path):
    url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    receipt = tmp_path / "receipt.json"
    migration = run_role("migrator", url, receipt, "--apply", "--backup-verified", "--service-quiesced")
    assert migration.returncode == 0, migration.stderr
    contract = json.loads(migration.stdout)["receipt"]
    ports = {"A": port(), "B": port(), "router": port()}
    processes = []
    with tempfile.TemporaryDirectory(prefix="tm-router-", dir="/tmp") as socket_dir:
      control = Path(socket_dir) / "control.sock"
      try:
        for generation in ("A", "B"):
            process = subprocess.Popen(
                [PYTHON, str(ROOT / "scripts/platform_role.py"), "api", "--receipt", str(receipt),
                 "--generation", generation, "--port", str(ports[generation])],
                cwd=ROOT, env={**os.environ, "DATABASE_URL": url, "SERVE_STATIC": "false"},
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            status, body = wait_http(f"http://127.0.0.1:{ports[generation]}/health/ready/platform", process)
            assert status == 200 and body["generation"] == generation
        with sqlite3.connect(tmp_path / "platform.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM scheduler_lease").fetchone()[0] == 0
        config = tmp_path / "router.json"
        config.write_text(json.dumps({
            "active": "A",
            "automatic_failover": True,
            "backends": [
                {"generation": generation, "url": f"http://127.0.0.1:{ports[generation]}",
                     "revision": contract["revision"], "checksum": contract["checksum"],
                     "source_checksum": contract["source_checksum"]}
                for generation in ("A", "B")
            ],
        }), encoding="utf-8")
        router = subprocess.Popen(
            [PYTHON, str(ROOT / "scripts/platform_router.py"), "serve", "--config", str(config),
             "--socket", str(control), "--port", str(ports["router"])],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        processes.append(router)
        route_url = f"http://127.0.0.1:{ports['router']}/health/ready/platform"
        assert wait_http(route_url, router)[1]["generation"] == "A"
        from app.runtime.platform_router import control_request

        assert control_request(control, {"command": "switch", "target": "B", "expected": "A"})["active"] == "B"
        assert request(route_url)[1]["generation"] == "B"
        assert control_request(control, {"command": "drain", "generation": "A", "timeout": 2})["drained"]
        receipt.unlink()
        with pytest.raises(ValueError, match="target_not_ready"):
            control_request(control, {"command": "switch", "target": "A", "expected": "B"})
        # The old API process is still alive, but cannot be selected without
        # its verifiable migrator receipt. Restore receipt only via migrator.
        migration = run_role("migrator", url, receipt, "--apply", "--backup-verified", "--service-quiesced")
        assert migration.returncode == 0, migration.stderr
        processes[1].terminate()
        processes[1].wait(timeout=8)
        assert request(route_url)[0] == 502
        assert request(route_url)[1]["generation"] == "A"
      finally:
          for process in reversed(processes):
              terminate(process)


def test_scheduler_role_acquires_database_lease_in_separate_process(tmp_path):
    url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    receipt = tmp_path / "receipt.json"
    migration = run_role("migrator", url, receipt, "--apply", "--backup-verified", "--service-quiesced")
    assert migration.returncode == 0, migration.stderr
    scheduler = subprocess.Popen(
        [PYTHON, str(ROOT / "scripts/platform_role.py"), "scheduler", "--receipt", str(receipt)],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if scheduler.poll() is not None:
                raise AssertionError(f"scheduler exited: {scheduler.stderr.read()}")
            with sqlite3.connect(tmp_path / "platform.sqlite") as db:
                row = db.execute("SELECT owner_id FROM scheduler_lease WHERE lease_key='master-due-tasks'").fetchone()
            if row and row[0]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("scheduler did not acquire persistent lease")
    finally:
        terminate(scheduler)


def test_managed_worker_once_uses_validated_identity(tmp_path):
    url = f"sqlite:///{tmp_path / 'platform.sqlite'}"
    receipt = tmp_path / "receipt.json"
    assert run_role("migrator", url, receipt, "--apply", "--backup-verified", "--service-quiesced").returncode == 0
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({
        "core_release": "test-core", "engine_id": "mysql",
        "package_digest": "sha256:" + "a" * 64,
        "runtime_digest": "sha256:" + "b" * 64,
        "sdk_version": "1.0", "task_schema": "engine.task/v1",
        "result_schema": "engine.result/v1",
    }), encoding="utf-8")
    worker = run_role("worker", url, receipt, "--worker-identity", str(identity), "--once")
    assert worker.returncode == 0, worker.stderr
