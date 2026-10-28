#!/usr/bin/env python3
"""Real offline build/install/CLI/SIGKILL acceptance using generated test keys."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.engine_host.packages import build_fixture_package, describe_context  # noqa: E402
from app.runtime.engine_host.supervisor import FixtureSupervisor  # noqa: E402
from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator  # noqa: E402
from app.runtime.engine_host.store import FixtureStore  # noqa: E402
from app.runtime.engine_packs.archive import ArchiveError, _no_link_path  # noqa: E402
from app.runtime.engine_packs.repository import LabRepository  # noqa: E402

KILL_POINTS = ("after_prepare", "after_stage", "after_rename", "after_commit")
WORKER = '''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from app.runtime.engine_packs.repository import LabRepository
from tmengine import load_lab_policy
def checkpoint(point,operation):
 if point==sys.argv[6]:
  Path(sys.argv[5]).write_text(json.dumps(operation))
  while True: time.sleep(1)
repository=LabRepository(Path(sys.argv[3]),trusted_keys=load_lab_policy(sys.argv[4]),checkpoint=checkpoint)
repository.install('provider.tmengine','crash-request')
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        work = _no_link_path(Path(args.workdir))
        work.mkdir(mode=0o700, exist_ok=False)
    except (ArchiveError, OSError) as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "workdir_unavailable")}))
        return 2
    report = {"fixture_only": True, "lab_only": True, "activated_by_repository": False,
              "context": describe_context(), "commands": [], "kill_cases": [], "runtime": [], "failures": []}
    report["implementation_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("backend/app/runtime/engine_packs/archive.py", "backend/app/runtime/engine_packs/repository.py",
                     "scripts/tmengine.py", "scripts/check_engine_pack_installation.py")}
    owned_paths = []

    def run(policy, repository, *command, expected=0):
        cmd = [sys.executable, str(ROOT / "scripts/tmengine.py"), "--trust-policy", str(policy),
               "--repository", str(repository), *command]
        process = subprocess.Popen(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except BaseException:
            process.kill(); process.wait(timeout=5)
            raise
        value = json.loads(stdout or stderr)
        record = {"argv": cmd, "pid": process.pid, "exit_code": process.returncode,
                  "expected_exit_code": expected, "response": value}
        report["commands"].append(record)
        if process.returncode != expected:
            raise RuntimeError(f"unexpected_cli_exit:{command[0]}:{process.returncode}")
        return record

    try:
        build = work / "build"; build.mkdir(mode=0o700)
        artifacts = work / "artifacts"; artifacts.mkdir(mode=0o700)
        packages = [build_fixture_package(build, version) for version in ("1.0.0", "1.0.1")]
        keys = tuple(key for package in packages for key in package.trusted_keys)
        policy = work / "lab-public-trust.json"
        policy.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [
            {"public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
             "engine_scopes": list(key.engine_scopes)} for key in keys]}))
        policy.chmod(0o600)
        repository = LabRepository(work / "repository", trusted_keys=keys)
        for package in packages:
            manifest, signature = artifacts / f"{package.version}.manifest.json", artifacts / f"{package.version}.sig.json"
            manifest.write_text(json.dumps(package.manifest)); signature.write_text(json.dumps(package.signature))
            archive = artifacts / f"{package.version}.tmengine"
            result = run(policy, repository.root, "build", "--source", str(package.source),
                "--manifest", str(manifest), "--signature", str(signature), "--output", str(archive))
            run(policy, repository.root, "verify", "--archive", str(archive), "--expected-archive-sha256",
                result["response"]["result"]["archive_sha256"])
            shutil.copyfile(archive, repository.root / "incoming" / archive.name)
            installed = run(policy, repository.root, "install", "--import-name", archive.name,
                            "--request-id", "normal-" + package.version)
            replay = run(policy, repository.root, "install", "--import-name", archive.name,
                         "--request-id", "normal-" + package.version)
            assert installed["response"] == replay["response"]
            run(policy, repository.root, "inspect", "--digest", package.digest)
            run(policy, repository.root, "status", "--request-id", "normal-" + package.version)
            host = FixtureSupervisor(workdir=work / ("host-" + package.version))
            try:
                record = host.start(repository.root / "packages" / package.digest[7:],
                    package.manifest, package.signature, keys, expected_namespace="tmengine.test", expected_channel="test")
                result = host.call("invoke_read", {"echo": "installed-artifact"})
                # Raw call() returns the Provider envelope; values carries the read payload.
                assert result["values"]["marker"] == ("v1" if package.version == "1.0.0" else "v2")
                report["runtime"].append({"version": package.version, "package_digest": package.digest,
                    "provider_pid": record.pid, "host_pid": os.getpid(), "launch_id": record.launch_instance_id,
                    "result": result})
                owned_paths.extend([record.socket_path, record.ipc_root, record.staging_dir])
            finally:
                host.stop()
        conflict = run(policy, repository.root, "install", "--import-name", "1.0.1.tmengine",
                       "--request-id", "normal-1.0.0", expected=2)
        assert conflict["response"]["code"] == "repository_idempotency_conflict"
        assert len(repository.list()) == 2
        run(policy, repository.root, "audit")

        installed = [replace(package, source=repository.root / "packages" / package.digest[7:]) for package in packages]
        store = FixtureStore(work / "host-journal" / "state.db")
        try:
            host = FixtureUpdateOrchestrator(store=store, workdir=work / "update-hosts",
                effect_store_path=work / "host-effects.db",
                fixture_budget=FixtureUpdateOrchestrator.build_fixture_budget(*installed))
            updated = host.run(*installed, cycles=1)
            assert updated["active"]["generation"] == 3
            assert updated["active"]["digest"] == installed[0].digest
            cycle = updated["cycles"][0]
            report["installed_update"] = {"cycles": 1, "activated_generation": cycle["activated_generation"],
                "rollback_generation": cycle["rollback_generation"], "final_generation": updated["active"]["generation"],
                "old_digest": cycle["old_digest"], "new_digest": cycle["new_digest"],
                "tasks": [{k: task[k] for k in ("task_id", "digest", "generation", "status")} for task in updated["tasks"]],
                "old_pre_drain": cycle["old_pre_drain"],
                "capacity_gate": "explicit_fixture_harness_budget_not_formal_capacity_acceptance"}
            for launch in updated["launches"]:
                owned_paths.extend(launch[k] for k in ("socket_path", "ipc_root", "staging_dir"))
        finally:
            store.close()

        worker = work / "crashable-installer.py"; worker.write_text(WORKER)
        for point in KILL_POINTS:
            case = work / point; case.mkdir(mode=0o700)
            repo = LabRepository(case / "repository", trusted_keys=keys)
            shutil.copyfile(artifacts / "1.0.0.tmengine", repo.root / "incoming/provider.tmengine")
            marker = case / "marker.json"
            process = subprocess.Popen([sys.executable, str(worker), str(ROOT / "backend"), str(ROOT / "scripts"),
                str(repo.root), str(policy), str(marker), point], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not marker.exists():
                    raise RuntimeError("install_checkpoint_missing:" + point)
                operation = json.loads(marker.read_text())
                process.kill(); process.wait(timeout=10)
                process.communicate(timeout=5)
                assert process.returncode == -9
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
            recovered = run(policy, repo.root, "recover")
            receipt = run(policy, repo.root, "install", "--import-name", "provider.tmengine", "--request-id", "crash-request")
            assert recovered["pid"] != process.pid
            assert receipt["response"]["result"]["operation_id"] == operation["operation_id"]
            assert len(repo.list()) == 1 and not list((repo.root / ".staging").iterdir())
            records = repo.audit()
            assert sum(json.loads(x["record"])["status"] == "installed" for x in records) == 1
            report["kill_cases"].append({"point": point, "installer_pid": process.pid, "installer_exit": process.returncode,
                "new_recovery_pid": recovered["pid"], "recover_exit": recovered["exit_code"],
                "operation_id": operation["operation_id"], "package_count": len(repo.list()),
                "staging_remaining": [], "audit_installed_count": 1})
    except Exception as exc:
        report["failures"].append(getattr(exc, "code", type(exc).__name__) + ":" + str(exc))
    finally:
        import psutil
        report["cleanup"] = {"children_after": [p.pid for p in psutil.Process().children(recursive=True)],
            "socket_paths_after": [str(p) for p in work.rglob("*.sock")],
            "owned_runtime_paths_after": [str(p) for p in owned_paths if Path(p).exists()],
            "workdir": str(work), "retention_reason": "Public signed fixtures and isolated installation/audit journals"}
        if (report["cleanup"]["children_after"] or report["cleanup"]["socket_paths_after"]
                or report["cleanup"]["owned_runtime_paths_after"]):
            report["failures"].append("owned_resource_remaining")
        if (len(report["kill_cases"]) != 4 or len(report["runtime"]) != 2
                or (report.get("installed_update") or {}).get("final_generation") != 3):
            report["failures"].append("acceptance_steps_incomplete")
        report["ok"] = not report["failures"]
        output = Path(args.report).absolute(); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"ok": report["ok"], "kill_cases": len(report["kill_cases"]), "report": str(output)}))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
