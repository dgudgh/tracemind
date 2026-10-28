from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator
from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.store import FixtureStore, StoreError
from app.runtime.engine_host.supervisor import SupervisorError


def test_two_real_packages_update_drain_rollback_and_core_readback(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    v1 = build_fixture_package(packages, "1.0.0")
    v2 = build_fixture_package(packages, "1.0.1")
    store = FixtureStore(tmp_path / "state.db")
    budget = FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)
    samples = []
    orchestrator = FixtureUpdateOrchestrator(
        store=store, workdir=tmp_path / "hosts", effect_store_path=tmp_path / "effects.db",
        fixture_budget=budget)
    report = orchestrator.run(v1, v2, cycles=1, observer=samples.append)

    assert report["active"]["generation"] == 3
    assert report["active"]["digest"] == v1.digest
    assert report["cycles"][0]["activated_generation"] == 2
    assert report["cycles"][0]["rollback_generation"] == 3
    assert report["cycles"][0]["old_digest"] != report["cycles"][0]["new_digest"]
    assert report["cycles"][0]["running_observed_inflight"] > 0
    assert report["cycles"][0]["old_pre_drain"] == {
        "references": 0, "inflight": 0, "reconcile_effect_state": "completed"}
    assert report["cycles"][0]["capacity_admission"]["admitted"] is True
    assert report["cycles"][0]["rollback_capacity_admission"]["admitted"] is True
    rollback_operation = report["cycles"][0]["operations"]["rollback"]
    rollback_phases = [row["phase"] for row in store.journal(rollback_operation)]
    assert rollback_phases == [
        "prepared", "verified", "staged", "candidate_started", "handshake_complete",
        "selftest_complete", "candidate_ready", "activate_generation"]
    assert {sample["phase"] for sample in samples} == {
        "dual_ready", "new_task_complete", "old_drained", "rollback_complete"}
    assert all("monotonic_ns" in sample and "utc" in sample for sample in samples)

    old_tasks = [task for task in report["tasks"] if "-old-" in task["task_id"]]
    assert {task["status"] for task in old_tasks} == {"succeeded"}
    assert next(task for task in old_tasks if "reconcile_required" in task["task_id"])[
        "effect_state"] == "completed"
    assert all(task["generation"] == 1 and task["digest"] == v1.digest for task in old_tasks)
    new_task = next(task for task in report["tasks"] if "-new-" in task["task_id"])
    assert new_task["generation"] == 2 and new_task["digest"] == v2.digest
    rollback_task = next(task for task in report["tasks"] if "-rollback-" in task["task_id"])
    assert rollback_task["generation"] == 3 and rollback_task["digest"] == v1.digest
    assert store.read_result(new_task["task_id"])["marker"] == "v2"
    assert store.read_result(rollback_task["task_id"])["marker"] == "v1"
    assert all(item["state"] == "cleaned" for item in store.launch_artifacts())
    assert all(json.loads(launch["members_json"]) for launch in store.launches())


def test_missing_or_wrong_capacity_gate_refuses_activation(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    v1 = build_fixture_package(packages, "1.0.0")
    v2 = build_fixture_package(packages, "1.0.1")
    bad = FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)
    bad["package_digests"] = list(reversed(bad["package_digests"]))
    # Re-signing the modified fixture budget proves field matching is enforced,
    # not merely the outer budget digest.
    from app.runtime.engine_host.metrics import digest_json
    unsigned = dict(bad); unsigned.pop("budget_digest")
    bad["budget_digest"] = digest_json(unsigned)
    for index, budget in enumerate((None, bad)):
        store = FixtureStore(tmp_path / f"state-{index}.db")
        orchestrator = FixtureUpdateOrchestrator(
            store=store, workdir=tmp_path / f"hosts-{index}",
            effect_store_path=tmp_path / f"effects-{index}.db", fixture_budget=budget)
        with pytest.raises(StoreError, match="capacity_admission_missing|capacity_budget_package_mismatch"):
            orchestrator.run(v1, v2)
        active = store.active("test.fake")
        assert active.generation == 1 and active.digest == v1.digest


def test_verify_not_found_keeps_reconcile_reference(tmp_path, monkeypatch):
    packages = tmp_path / "packages"
    packages.mkdir()
    package = build_fixture_package(packages, "1.0.0")
    store = FixtureStore(tmp_path / "state.db")
    orchestrator = FixtureUpdateOrchestrator(
        store=store, workdir=tmp_path / "hosts", effect_store_path=tmp_path / "effects.db")
    store.bootstrap_active(orchestrator._package_record(package, 1, "active"), generation=1)
    store.prepare_task(task_id="reconcile-missing", engine_id="test.fake",
                       payload={"value": "missing"}, status="reconcile_required",
                       effect_state="unknown", operation_id="missing-effect")
    supervisor = SimpleNamespace(record=SimpleNamespace(package_digest=package.digest))
    monkeypatch.setattr(orchestrator, "_call",
                        lambda *_args, **_kwargs: {"found": False, "result": None,
                                                  "effect_state": "none"})
    receipt = orchestrator.reconcile_task(supervisor, "reconcile-missing")
    assert receipt["found"] is False
    assert store.task("reconcile-missing")["status"] == "reconcile_required"
    assert len(store.generation_references("test.fake", 1, package.digest)) == 1


@pytest.mark.parametrize(
    ("method", "reported_effect", "expected_status", "expected_effect"),
    (("execute", "completed", "reconcile_required", "completed"),
     ("execute", "unknown", "reconcile_required", "unknown"),
     ("invoke_read", "none", "retry", "none")),
)
def test_rpc_failure_is_persisted_before_propagation(
        tmp_path, monkeypatch, method, reported_effect, expected_status, expected_effect):
    packages = tmp_path / "packages"
    packages.mkdir()
    package = build_fixture_package(packages, "1.0.0")
    store = FixtureStore(tmp_path / "state.db")
    orchestrator = FixtureUpdateOrchestrator(
        store=store, workdir=tmp_path / "hosts", effect_store_path=tmp_path / "effects.db")
    store.bootstrap_active(orchestrator._package_record(package, 1, "active"), generation=1)
    task_id = f"failure-{method}-{reported_effect}"
    store.prepare_task(task_id=task_id, engine_id="test.fake", payload={"value": 1},
                       operation_id=f"operation-{task_id}")
    supervisor = SimpleNamespace(record=SimpleNamespace(package_digest=package.digest))

    def fail(*_args, **_kwargs):
        raise SupervisorError("connection_lost", category="transport",
                              effect_state=reported_effect)

    monkeypatch.setattr(orchestrator, "_call", fail)
    with pytest.raises(SupervisorError, match="connection_lost"):
        orchestrator.execute_task(supervisor, task_id, method=method)
    task = store.task(task_id)
    assert task["status"] == expected_status
    assert task["effect_state"] == expected_effect
    assert store.result(task_id) is None
    failures = store.task_failures(task_id)
    assert len(failures) == 1
    assert failures[0]["error_code"] == "connection_lost"
    assert failures[0]["effect_state"] == expected_effect


@pytest.mark.parametrize("mode", ["removed", "owner_mismatch", "active_resource"])
def test_recovery_snapshot_cleanup_requires_exact_private_ownership(
        tmp_path, monkeypatch, mode):
    from app.runtime.engine_host import ownership

    workdir = ownership.ensure_private_workdir((tmp_path / "host").resolve())
    owner = json.loads((workdir / "OWNERSHIP").read_text())
    package_digest = "sha256:" + "a" * 64
    staging = workdir / "runtime" / ("a" * 64)
    staging.mkdir(parents=True)
    (staging / "provider.py").write_text("# fixture\n")
    ipc_root = tmp_path / "ipc"
    ipc_root.mkdir()
    record = {
        "pid": os.getpid(), "start_time": 1.0, "pgid": os.getpgid(os.getpid()),
        "uid": os.getuid(), "launch_instance_id": "owned-launch",
        "socket_path": str(ipc_root / "provider.sock"), "ipc_root": str(ipc_root),
        "staging_dir": str(staging), "workdir": str(workdir),
        "manifest_sha256": "b" * 64, "provider_version": "1.0.0",
        "owner_token": owner["token"], "members": [],
    }
    store = FixtureStore(tmp_path / "state.db")
    store.save_launch(engine_id="test.fake", generation=1,
                      package_digest=package_digest, record=record)
    orchestrator = FixtureUpdateOrchestrator(store=store, workdir=tmp_path / "recovery")
    monkeypatch.setattr(
        ownership, "cleanup_owned",
        lambda *_args, **_kwargs: {"identity_matched": True, "processes_remaining": [],
                                  "sockets_remaining": [], "removed": [],
                                  "blocked_reasons": []})
    if mode == "owner_mismatch":
        marker = json.loads((workdir / "OWNERSHIP").read_text())
        marker["token"] = "different-owner"
        (workdir / "OWNERSHIP").write_text(json.dumps(marker))
    elif mode == "active_resource":
        monkeypatch.setattr(
            ownership, "cleanup_owned",
            lambda *_args, **_kwargs: {"identity_matched": True,
                                      "processes_remaining": [os.getpid()],
                                      "sockets_remaining": [], "removed": [],
                                      "blocked_reasons": []})

    reports = orchestrator.recover_owned_launches()
    artifact = store.launch_artifacts()[0]
    if mode == "owner_mismatch":
        assert staging.exists()
        assert artifact["state"] == "retained_audit"
        assert artifact["retained_reason"] == "runtime_snapshot_owner_mismatch"
        assert reports[0]["blocked_reasons"] == ["runtime_snapshot_owner_mismatch"]
    elif mode == "active_resource":
        assert staging.exists()
        assert artifact["state"] == "retained_audit"
        assert artifact["retained_reason"] == "owned_resource_cleanup_incomplete"
        assert reports[0]["blocked_reasons"] == ["owned_resource_cleanup_incomplete"]
    else:
        assert not staging.exists()
        assert artifact["state"] == "cleaned"
        assert artifact["retained_reason"] == (
            "crash_recovery_identity_matched_snapshot_removed")
        assert str(staging) in reports[0]["removed"]
