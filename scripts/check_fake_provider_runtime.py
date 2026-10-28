#!/usr/bin/env python3
"""Offline fixture acceptance: 20 dual-generation cycles and eight Host kills."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator  # noqa: E402
from fixture_host_worker import public_evidence  # noqa: E402
from app.runtime.engine_host.packages import (  # noqa: E402
    build_fixture_package, load_package, save_package,
)
from app.runtime.engine_host.store import FixtureStore, SyntheticEffectLedger  # noqa: E402

KILL_POINTS = (
    "stage_complete", "provider_started_before_handshake", "selftest_before_cas",
    "cas_after_commit", "dual_generation_inflight", "effect_after_commit_before_receipt",
    "drain_midway", "rollback_after_cas",
)


def child_count():
    try:
        import psutil
        return len(psutil.Process().children(recursive=True))
    except Exception:
        return None


def capacity_inputs(args, v1, v2):
    if args.fixture_calibration:
        if args.single_profile or args.single_candidate:
            raise ValueError("fixture calibration cannot consume formal capacity evidence")
        return None, None, FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)
    if not args.single_profile or not args.single_candidate:
        raise ValueError("single profile and candidate are required without --fixture-calibration")
    profile = json.loads(Path(args.single_profile).read_text())
    candidate = json.loads(Path(args.single_candidate).read_text())
    paths = {
        "rss_bytes": "metrics.resources.rss_bytes.max",
        "process_count": "metrics.resources.process_count.max",
        "fd_count": "metrics.resources.fd_count.max",
        "ipc_connections": "metrics.resources.ipc_connections.max",
        "queue_depth": "metrics.resources.queue_depth.max",
        "inflight": "metrics.resources.inflight.max",
    }
    limits = {}
    for name, path in paths.items():
        value = (profile.get("thresholds", {}).get(path) or {}).get("value")
        if type(value) not in (int, float) or value <= 0:
            raise ValueError(f"single profile threshold missing: {path}")
        limits[name] = max(1, int(value * 2 + 0.999999))
    budget = FixtureUpdateOrchestrator.build_fixture_budget(
        v1, v2, profile["profile_digest"], candidate["raw_digest"], limits)
    return profile, candidate, budget


def packages_for_run(args, work: Path):
    """Use the frozen V1 artifact in formal mode; never silently rebuild its identity."""
    packages_root = work / "packages"
    packages_root.mkdir(parents=True)
    if args.fixture_calibration:
        v1 = build_fixture_package(packages_root, "1.0.0")
        v1_descriptor = work / "v1.json"
        save_package(v1, v1_descriptor)
    else:
        if not args.single_profile:
            raise ValueError("single profile is required without --fixture-calibration")
        profile = json.loads(Path(args.single_profile).read_text())
        package_bindings = profile.get("packages")
        if not isinstance(package_bindings, list) or len(package_bindings) != 1:
            raise ValueError("single profile must bind exactly one package")
        binding = package_bindings[0]
        v1_descriptor = Path(str(binding.get("artifact_path", ""))).resolve()
        v1 = load_package(v1_descriptor, expected_digest=binding.get("package_digest"))
    v2 = build_fixture_package(packages_root, "1.0.1")
    v2_descriptor = work / "v2.json"
    save_package(v2, v2_descriptor)
    return v1, v2, v1_descriptor, v2_descriptor


def _wait_marker(marker: Path, process: subprocess.Popen, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        if marker.exists():
            return json.loads(marker.read_text())
        time.sleep(0.025)
    stdout, stderr = process.communicate(timeout=2) if process.poll() is not None else ("", "")
    raise RuntimeError(f"kill marker missing; exit={process.poll()} stdout={stdout[-500:]} stderr={stderr[-500:]}")


def _worker_command(case_root: Path, point: str, action: str,
                    v1_descriptor: Path, v2_descriptor: Path, report: Path,
                    args) -> list[str]:
    command = [
        sys.executable, str(ROOT / "scripts/fixture_host_worker.py"),
        "--workdir", str(case_root / ("inject-hosts" if action == "inject" else "recover-hosts")),
        "--store", str(case_root / "state.db"), "--effects", str(case_root / "effects.db"),
        "--package-v1", str(v1_descriptor), "--package-v2", str(v2_descriptor),
        "--action", action, "--kill-point", point, "--case-id", point,
        "--marker", str(case_root / "KILL-MARKER.json"), "--report", str(report),
    ]
    if args.fixture_calibration:
        command.append("--fixture-calibration")
    else:
        command.extend(["--single-profile", str(Path(args.single_profile).resolve()),
                        "--single-candidate", str(Path(args.single_candidate).resolve())])
    return command


def run_kill_case(root: Path, point: str, v1_descriptor: Path,
                  v2_descriptor: Path, args) -> dict:
    case_root = root / "kill-cases" / point
    case_root.mkdir(parents=True)
    report_path = case_root / "recovery.json"
    inject_cmd = _worker_command(case_root, point, "inject", v1_descriptor,
                                 v2_descriptor, report_path, args)
    process = subprocess.Popen(inject_cmd, cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    marker = _wait_marker(case_root / "KILL-MARKER.json", process)
    process.kill()  # genuine SIGKILL of the Host; Provider is intentionally left for recovery
    process.wait(timeout=10)

    wrong_package = None
    if point == "dual_generation_inflight":
        # A valid signed V2 descriptor is still the wrong package for the old V1 task.
        wrong_report = case_root / "wrong-package-recovery.json"
        wrong_cmd = _worker_command(case_root, point, "recover", v2_descriptor,
                                    v2_descriptor, wrong_report, args)
        wrong = subprocess.run(wrong_cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
        wrong_package = {"exit": wrong.returncode, "stdout": wrong.stdout[-500:],
                         "stderr": wrong.stderr[-500:]}
        if wrong.returncode == 0:
            raise RuntimeError("wrong recovery package was accepted")

    recover_cmd = _worker_command(case_root, point, "recover", v1_descriptor,
                                  v2_descriptor, report_path, args)
    recovered = subprocess.run(recover_cmd, cwd=ROOT, capture_output=True,
                               text=True, timeout=45)
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    return {"kill_point": point, "marker": marker, "host_exit": process.returncode,
            "recover_exit": recovered.returncode, "recover_stdout": recovered.stdout[-500:],
            "recover_stderr": recovered.stderr[-1000:], "wrong_package": wrong_package,
            "report": report}


def validate_kill_case(case: dict) -> list[str]:
    failures = []
    point, report = case["kill_point"], case.get("report") or {}
    if case["host_exit"] != -9:
        failures.append("host_not_sigkilled")
    if case["recover_exit"] != 0 or not report.get("ok"):
        failures.append("recovery_failed")
        return failures
    active = report.get("active") or {}
    expected = {
        "stage_complete": (1, "1.0.0"),
        "provider_started_before_handshake": (1, "1.0.0"),
        "selftest_before_cas": (2, "1.0.1"),
        "cas_after_commit": (2, "1.0.1"),
        "dual_generation_inflight": (2, "1.0.1"),
        "effect_after_commit_before_receipt": (2, "1.0.1"),
        "drain_midway": (2, "1.0.1"),
        "rollback_after_cas": (3, "1.0.0"),
    }
    expected_generation, expected_version = expected[point]
    if active.get("generation") != expected_generation:
        failures.append("wrong_recovered_generation")
    packages_by_digest = {task["digest"]: task["package_version"] for task in report.get("tasks", [])}
    if active.get("digest") in packages_by_digest and packages_by_digest[active["digest"]] != expected_version:
        failures.append("wrong_recovered_package")
    for cleanup in report.get("cleanup", []):
        if cleanup.get("processes_remaining") or cleanup.get("sockets_remaining"):
            failures.append("owned_resource_remaining")
    for artifact in report.get("launch_artifacts", []):
        if artifact["state"] == "cleaned" and Path(artifact["path"]).exists():
            failures.append("cleaned_runtime_snapshot_still_present")
        if artifact["state"] == "retained_audit" and not artifact.get("retained_reason"):
            failures.append("retained_runtime_snapshot_without_reason")
    if point == "dual_generation_inflight":
        if not case.get("wrong_package") or case["wrong_package"]["exit"] == 0:
            failures.append("wrong_package_not_rejected")
        old_tasks = [task for task in report.get("tasks", []) if "-old-" in task["task_id"]]
        if len(old_tasks) != 4 or any(task["generation"] != 1 or
                                      task["package_version"] != "1.0.0" for task in old_tasks):
            failures.append("old_task_rebound")
        statuses = {task["task_id"].rsplit("-old-", 1)[1]: task["status"] for task in old_tasks}
        if statuses != {"queued": "succeeded", "running": "succeeded",
                        "retry": "succeeded", "reconcile_required": "reconcile_required"}:
            failures.append("old_task_recovery_state_invalid")
    if point == "effect_after_commit_before_receipt":
        counts = report.get("effect_counts", {})
        if list(counts.values()) != [1]:
            failures.append("effect_not_exactly_once")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-profile", default="local-fake")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--kill-case", choices=("all",) + KILL_POINTS, default="all")
    parser.add_argument("--single-profile")
    parser.add_argument("--single-candidate")
    parser.add_argument("--fixture-calibration", action="store_true")
    args = parser.parse_args(argv)
    if args.cycles < 20:
        raise SystemExit("cycles must be at least 20 for this acceptance runner")
    requested_work = Path(args.workdir)
    if not requested_work.is_absolute() or requested_work.is_symlink():
        raise SystemExit("workdir must be an absolute private directory, without a symlink")
    work = requested_work.resolve()
    if work.exists() and any(work.iterdir()):
        raise SystemExit("workdir must be new/private")
    work.mkdir(parents=True, mode=0o700, exist_ok=True)
    before = child_count()
    report = {"fixture_profile": args.fixture_profile, "fixture_only": True,
              "qa_ops_acceptance": "pending", "os": sys.platform,
              "cycles_requested": args.cycles, "normal": None, "kill_cases": [],
              "failures": [], "children_before": before}
    exit_code = 2
    try:
        v1, v2, v1_descriptor, v2_descriptor = packages_for_run(args, work)
        capacity_profile, capacity_candidate, fixture_budget = capacity_inputs(args, v1, v2)

        normal_root = work / "normal"
        store = FixtureStore(normal_root / "state.db")
        samples = []
        try:
            orchestrator = FixtureUpdateOrchestrator(
                store=store, workdir=normal_root / "hosts",
                effect_store_path=normal_root / "effects.db",
                capacity_profile=capacity_profile, capacity_candidate=capacity_candidate,
                fixture_budget=fixture_budget)
            normal = orchestrator.run(v1, v2, cycles=args.cycles, observer=samples.append)
            normal["capacity_samples"] = samples
            ledger = SyntheticEffectLedger(normal_root / "effects.db")
            try:
                effect_operations = [task["operation_id"] for task in normal["tasks"]
                                     if task["operation_id"] and task["operation_id"].startswith("cycle-effect-")]
                normal["effect_counts"] = {operation: ledger.count(operation)
                                           for operation in effect_operations}
            finally:
                ledger.close()
            report["normal"] = normal
            if len(normal["cycles"]) != args.cycles:
                report["failures"].append("cycle_count_mismatch")
            if normal["active"]["generation"] != 1 + 2 * args.cycles:
                report["failures"].append("generation_not_monotonic")
            task_ids = [task["task_id"] for task in normal["tasks"]]
            if len(task_ids) != len(set(task_ids)):
                report["failures"].append("duplicate_task_id")
            if (len(normal["effect_counts"]) != args.cycles or
                    any(count != 1 for count in normal["effect_counts"].values())):
                report["failures"].append("normal_effect_not_exactly_once")
            for cycle in normal["cycles"]:
                if cycle["old_digest"] == cycle["new_digest"]:
                    report["failures"].append("cycle_same_payload")
                if cycle["rollback_generation"] != cycle["activated_generation"] + 1:
                    report["failures"].append("rollback_generation_invalid")
        finally:
            store.close()

        selected = KILL_POINTS if args.kill_case == "all" else (args.kill_case,)
        for point in selected:
            case = run_kill_case(work, point, v1_descriptor, v2_descriptor, args)
            case["failures"] = validate_kill_case(case)
            report["kill_cases"].append(case)
            report["failures"].extend(f"{point}:{failure}" for failure in case["failures"])
        exit_code = 0 if not report["failures"] else 2
    except Exception as exc:
        report["failures"].append(f"runner_exception:{type(exc).__name__}:{exc}")
    finally:
        after = child_count()
        sockets = [str(path) for path in work.rglob("*.sock")]
        normal_launches = (report.get("normal") or {}).get("launches", [])
        ipc_roots = sorted({row["ipc_root"] for row in normal_launches
                            if row.get("ipc_root") and Path(row["ipc_root"]).exists()})
        snapshots = sorted({row["staging_dir"] for row in normal_launches
                            if row.get("staging_dir") and Path(row["staging_dir"]).exists()})
        retained = [artifact for case in report["kill_cases"]
                    for artifact in (case.get("report") or {}).get("launch_artifacts", [])
                    if artifact["state"] == "retained_audit"]
        report["cleanup"] = {"children_before": before, "children_after": after,
                             "socket_files_after": sockets, "ipc_roots_after": ipc_roots,
                             "runtime_snapshots_after": snapshots,
                             "retained_audit_artifacts": retained, "workdir": str(work)}
        if sockets:
            report["failures"].append("socket_files_remaining")
        if ipc_roots:
            report["failures"].append("owned_ipc_roots_remaining")
        if snapshots:
            report["failures"].append("normal_runtime_snapshots_remaining")
        if before is not None and after is not None and after > before:
            report["failures"].append("child_processes_remaining")
        report["ok"] = not report["failures"]
        if not report["ok"]:
            exit_code = 2
        output = Path(args.report).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(public_evidence(report), ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"ok": report["ok"], "report": str(output),
                          "cycles": len((report.get("normal") or {}).get("cycles", [])),
                          "kill_cases": len(report["kill_cases"]),
                          "failures": report["failures"]}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
