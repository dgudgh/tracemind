"""Start A/B API children from separate release roots and a stable Router.

Configuration names immutable release directories explicitly. This launcher is
for source/Python deployments; frozen EXE/App packaging has a separate gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from _bootstrap import add_backend_to_path


def _load(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    slots = config["slots"]
    if len(slots) != 2 or {slot["generation"] for slot in slots} != {"A", "B"}:
        raise ValueError("launcher requires exactly A and B slots")
    releases = [Path(slot["release_root"]).expanduser().resolve() for slot in slots]
    if releases[0] == releases[1]:
        raise ValueError("A and B require separate release roots")
    runtime = Path(config["runtime_root"]).expanduser().resolve()
    for slot, release in zip(slots, releases):
        if runtime == release or runtime in release.parents:
            raise ValueError("runtime root cannot be inside a release")
        if not (release / "scripts/platform_role.py").is_file():
            raise ValueError(f"release missing platform_role.py: {release}")
        # Keep a venv's bin/python symlink intact; resolving it selects its base
        # interpreter and silently drops the release's installed dependencies.
        python = Path(slot["python"]).expanduser().absolute()
        if not python.is_file():
            raise ValueError(f"python executable missing: {python}")
        if not 0 < int(slot["port"]) <= 65535:
            raise ValueError("invalid API port")
    ports = [int(slot["port"]) for slot in slots] + [int(config["router_port"])]
    if len(set(ports)) != 3:
        raise ValueError("A, B and Router must listen on different ports")
    return config


def _wait_ready(url: str, expected: dict, process: subprocess.Popen, timeout=30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API child exited before readiness")
        try:
            with urlopen(url, timeout=2) as response:
                body = json.load(response)
            if all(body.get(key) == value for key, value in expected.items()):
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"API child not ready: {url}")


def _control_socket(runtime: Path) -> Path:
    short = hashlib.sha256(str(runtime).encode()).hexdigest()[:16]
    parent = Path(tempfile.gettempdir()) / f"tm-router-{os.getuid()}-{short}"
    try:
        parent.mkdir(mode=0o700)
    except FileExistsError:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("router control directory is not owner private")
    return parent / "control.sock"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--migrate-with", choices=("A", "B"),
                        help="explicitly run the chosen release's dedicated migrator before API start")
    parser.add_argument("--backup-verified", action="store_true")
    parser.add_argument("--service-quiesced", action="store_true")
    args = parser.parse_args(argv)
    if args.migrate_with and not (args.backup_verified and args.service_quiesced):
        parser.error("migrator launch requires --backup-verified and --service-quiesced")
    config = _load(args.config)
    url_env = config.get("database_url_env", "DATABASE_URL")
    if not os.environ.get(url_env, "").strip():
        parser.error(f"{url_env} must be supplied in the process environment")
    os.environ["DATABASE_URL"] = os.environ[url_env]
    runtime = Path(config["runtime_root"]).expanduser().resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    control_socket = _control_socket(runtime)
    receipt = Path(config["receipt"]).expanduser().resolve()
    if args.migrate_with:
        slot = next(item for item in config["slots"] if item["generation"] == args.migrate_with)
        release = Path(slot["release_root"]).expanduser().resolve()
        migrator = subprocess.run([
            str(Path(slot["python"]).expanduser().absolute()),
            str(release / "scripts/platform_role.py"), "migrator",
            "--database-url-env", url_env, "--receipt", str(receipt),
            "--apply", "--backup-verified", "--service-quiesced",
        ], cwd=release, env=os.environ.copy(), capture_output=True, text=True)
        if migrator.returncode:
            raise RuntimeError("dedicated migrator failed; inspect migration evidence before retry")
    if not receipt.is_file():
        raise FileNotFoundError("dedicated migrator receipt missing")
    add_backend_to_path()
    from app.runtime.process_roles import verify_migration_receipt
    from app.core.database import engine, verify_schema_ready

    contract = verify_schema_ready(engine)
    verify_migration_receipt(receipt, contract, os.environ[url_env])
    children = []
    logs = []
    stopping = False

    def request_stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    old_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        router_backends = []
        for slot in config["slots"]:
            release = Path(slot["release_root"]).expanduser().resolve()
            slot_receipt = Path(slot.get("migration_receipt") or receipt).expanduser().resolve()
            slot_receipt_data = json.loads(slot_receipt.read_text(encoding="utf-8"))
            slot_migration = {key: slot_receipt_data[key] for key in
                              ("revision", "checksum", "source_checksum")}
            log = (runtime / f"api-{slot['generation']}.log").open("ab", buffering=0)
            logs.append(log)
            child_env = {**os.environ, "TRACEMIND_RUNTIME_DIR": str(runtime)}
            static = release / "backend/static"
            if static.is_dir():
                child_env["SERVE_STATIC"] = "true"
                child_env["STATIC_DIR"] = str(static)
            role_args = [
                str(Path(slot["python"]).expanduser().absolute()),
                str(release / "scripts/platform_role.py"), "api",
                "--database-url-env", url_env, "--receipt", str(slot_receipt),
                "--generation", slot["generation"], "--host", "127.0.0.1",
                "--port", str(slot["port"]),
            ]
            if slot.get("artifact_digest"):
                role_args.extend(["--artifact-digest", slot["artifact_digest"]])
            process = subprocess.Popen(role_args, cwd=release, env=child_env,
               stdout=log, stderr=subprocess.STDOUT)
            children.append(process)
            expected = {"status": "ready", "generation": slot["generation"],
                        "role": "api", "migration": slot_migration}
            if slot.get("artifact_digest"):
                expected["artifact_digest"] = slot["artifact_digest"]
            _wait_ready(f"http://127.0.0.1:{slot['port']}/health/ready/platform",
                        expected, process)
            router_backends.append({"generation": slot["generation"],
                                    "url": f"http://127.0.0.1:{slot['port']}",
                                    **slot_migration,
                                    **({"artifact_digest": slot["artifact_digest"]}
                                       if slot.get("artifact_digest") else {})})
        router_config = runtime / "router-backends.json"
        router_config.write_text(json.dumps({
            "active": config.get("active", "A"),
            "automatic_failover": config.get("automatic_failover") is True,
            "backends": router_backends,
        }, sort_keys=True), encoding="utf-8")
        log = (runtime / "router.log").open("ab", buffering=0)
        logs.append(log)
        router_args = [
            sys.executable, str(Path(__file__).with_name("platform_router.py")), "serve",
            "--config", str(router_config), "--socket", str(control_socket),
            "--state", str(runtime / "router-state.json"), "--port", str(config["router_port"]),
        ]
        if config.get("platform_update_transaction_root"):
            router_args.extend(["--update-lease-db", str(
                Path(config["platform_update_transaction_root"]).expanduser().resolve()
                / "platform-update-lease.sqlite")])
        router = subprocess.Popen(router_args, cwd=Path(__file__).resolve().parents[1],
                                  stdout=log, stderr=subprocess.STDOUT)
        children.append(router)
        _wait_ready(f"http://127.0.0.1:{config['router_port']}/health/ready/platform",
                    {"status": "ready", "role": "api"}, router)
        print(json.dumps({"status": "serving", "router_port": config["router_port"],
                          "runtime_root": str(runtime), "control_socket": str(control_socket)},
                         sort_keys=True), flush=True)
        while not stopping:
            if router.poll() is not None:
                raise RuntimeError("stable router exited; service manager must restore listener")
            time.sleep(0.2)
        return 0
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in reversed(children):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
