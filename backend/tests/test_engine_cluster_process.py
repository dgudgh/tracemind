"""Real two-Supervisor cluster, membership and result-fence exercise."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

from app.runtime.engine_cluster import ClusterRefused, EngineCluster
from app.runtime.engine_host.packages import build_fixture_package, save_package
from app.runtime.engine_host.store import FixtureStore, SyntheticEffectLedger


ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / "scripts/engine_cluster_node.py"


def wait_for(predicate, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    pytest.fail("cluster process condition timed out")


def read(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def start_node(tmp_path, cluster, store, effects, package_path, node_id, generation):
    launch = uuid.uuid4().hex[:8]
    status = tmp_path / f"{node_id}-{generation}-{launch}.json"
    process = subprocess.Popen(
        [sys.executable, str(NODE), "--cluster", str(cluster), "--store", str(store),
         "--effects", str(effects), "--package", str(package_path),
         "--workdir", str(tmp_path / f"host-{node_id}-{generation}-{launch}"),
         "--status", str(status), "--node-id", node_id,
         "--generation", str(generation), "--lease-seconds", "1.5"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    def ready():
        value = read(status)
        if value.get("state") == "failed" or process.poll() is not None:
            out, err = process.communicate(timeout=1)
            pytest.fail(f"node start failed: {value} {out} {err}")
        return value if value.get("state") == "ready" else None
    value = wait_for(ready)
    assert psutil.Process(value["provider_pid"]).ppid() == process.pid
    return process, status, value


def close_node(process):
    if process.poll() is not None:
        return
    if psutil.Process(process.pid).status() == psutil.STATUS_STOPPED:
        os.kill(process.pid, signal.SIGCONT)
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def test_real_two_node_pinned_recovery_and_fenced_old_result(tmp_path):
    pkg1 = build_fixture_package(tmp_path, "1.0.0")
    pkg2 = build_fixture_package(tmp_path, "1.0.1")
    p1, p2 = tmp_path / "v1.json", tmp_path / "v2.json"
    save_package(pkg1, p1)
    save_package(pkg2, p2)
    cluster_path, store_path, effects = (tmp_path / name for name in
                                          ("cluster.db", "host-store.db", "effects.db"))
    cluster = EngineCluster(cluster_path, store_path, lease_seconds=1.5)
    identity1 = cluster.bootstrap(pkg1)
    with pytest.raises(ClusterRefused, match="host_probe_evidence_missing|host_probe_identity_mismatch"):
        cluster.join("untrusted", pkg1, 1, 1, lambda: {"ready": True})
    processes = []
    try:
        with pytest.raises(ClusterRefused, match="matching_ready_node_missing"):
            cluster.switch_active("test.fake", pkg2, expected_generation=1)
        a, a_status, a_ready = start_node(tmp_path, cluster_path, store_path, effects, p1, "a", 1)
        processes.append(a)
        b, b_status, b_ready = start_node(tmp_path, cluster_path, store_path, effects, p2, "b", 2)
        processes.append(b)
        snapshot = cluster.snapshot("test.fake")
        assert snapshot["membership_epoch"] == 2
        assert {n["identity"]["package_digest"] for n in snapshot["nodes"]} == {pkg1.digest, pkg2.digest}
        assert all(n["capacity"] == 1 for n in snapshot["nodes"])

        old = cluster.enqueue("old-read", "test.fake", "invoke_read", {"echo": "old", "sleep": 1.1})
        assert old["identity"] == identity1 and old["generation"] == 1
        wait_for(lambda: cluster.task("old-read")["state"] == "claimed")
        claimed = cluster.task("old-read")
        assert claimed["node_id"] == "a"
        os.kill(a.pid, signal.SIGSTOP)
        time.sleep(1.7)
        assert cluster.expire("test.fake") == ["a"]
        queued = cluster.task("old-read")
        assert queued["state"] == "queued" and queued["identity"] == identity1
        assert queued["claim_token"] > claimed["claim_token"]
        with pytest.raises(ClusterRefused, match="claim_fence_stale"):
            cluster.commit_result("old-read", "a", a_ready["launch_id"],
                                  claimed["claim_token"], claimed["claim_epoch"],
                                  result={"fake": True}, effect_state="none")
        assert cluster.task("old-read")["state"] == "queued"
        assert not any(n["node_id"] == "a" for n in cluster.snapshot("test.fake")["nodes"])
        assert cluster.task("old-read")["identity"]["package_digest"] != b_ready["identity"]["package_digest"]

        a2, a2_status, a2_ready = start_node(tmp_path, cluster_path, store_path, effects, p1, "a", 1)
        processes.append(a2)
        assert a2_ready["launch_id"] != a_ready["launch_id"]
        os.kill(a.pid, signal.SIGCONT)
        wait_for(lambda: cluster.task("old-read")["state"] == "succeeded")
        assert cluster.task("old-read")["node_id"] == "a"
        assert cluster.task("old-read")["launch_id"] == a2_ready["launch_id"]
        assert any(e["event"] == "result_refused" for e in wait_for(
            lambda: read(a_status) if any(e.get("event") == "result_refused" for e in read(a_status).get("events", [])) else None)["events"])
        host_store = FixtureStore(store_path)
        try:
            assert host_store.read_result("old-read")["echo"] == "old"
        finally:
            host_store.close()

        switched = cluster.switch_active("test.fake", pkg2, expected_generation=1)
        assert switched["generation"] == 2 and switched["identity"]["package_digest"] == pkg2.digest
        newer = cluster.enqueue("new-read", "test.fake", "invoke_read", {"echo": "new"})
        assert newer["identity"]["package_digest"] == pkg2.digest
        wait_for(lambda: cluster.task("new-read")["state"] == "succeeded")
        assert cluster.task("new-read")["node_id"] == "b"
        assert cluster.task("old-read")["identity"]["package_digest"] == pkg1.digest
        assert b.poll() is None and a2.poll() is None

        # A rollback is a new fenced generation with a freshly probed V1 Host.
        c, _, c_ready = start_node(tmp_path, cluster_path, store_path, effects, p1, "c", 3)
        processes.append(c)
        rolled = cluster.switch_active("test.fake", pkg1, expected_generation=2)
        assert rolled["generation"] == 3 and rolled["identity"] == identity1
        cluster.enqueue("rolled-read", "test.fake", "invoke_read", {"echo": "rolled"})
        wait_for(lambda: cluster.task("rolled-read")["state"] == "succeeded")
        assert cluster.task("rolled-read")["launch_id"] == c_ready["launch_id"]
        assert cluster.task("rolled-read")["generation"] == 3
        assert cluster.task("old-read")["generation"] == 1

        previous_epoch = cluster.snapshot("test.fake")["membership_epoch"]
        close_node(c)
        wait_for(lambda: cluster.snapshot("test.fake")["membership_epoch"] > previous_epoch)
        with pytest.raises(ClusterRefused, match="matching_ready_node_missing"):
            cluster.enqueue("no-ready-after-leave", "test.fake", "invoke_read", {"echo": "none"})
    finally:
        for process in reversed(processes):
            close_node(process)


def test_effect_unknown_after_node_partition_is_not_replayed(tmp_path):
    package = build_fixture_package(tmp_path, "1.0.0")
    path = tmp_path / "v1.json"
    save_package(package, path)
    cluster_path, store_path, effects = (tmp_path / name for name in
                                          ("cluster.db", "host-store.db", "effects.db"))
    cluster = EngineCluster(cluster_path, store_path, lease_seconds=1.5)
    cluster.bootstrap(package)
    a, a_status, ready = start_node(tmp_path, cluster_path, store_path, effects, path, "a", 1)
    ledger = SyntheticEffectLedger(effects)
    try:
        task = cluster.enqueue("effect-old", "test.fake", "execute",
                               {"value": "once", "reply_delay": 2.2}, operation_id="op-once")
        wait_for(lambda: cluster.task("effect-old")["state"] == "claimed")
        claimed = cluster.task("effect-old")
        wait_for(lambda: ledger.count("op-once") == 1)
        os.kill(a.pid, signal.SIGSTOP)
        time.sleep(1.7)
        assert cluster.expire("test.fake") == ["a"]
        uncertain = cluster.task("effect-old")
        assert uncertain["state"] == "reconcile_required" and uncertain["effect_state"] == "unknown"
        assert uncertain["identity"] == task["identity"]
        with pytest.raises(ClusterRefused, match="claim_fence_stale"):
            cluster.commit_result("effect-old", "a", ready["launch_id"],
                                  claimed["claim_token"], claimed["claim_epoch"],
                                  result={"fake": True}, effect_state="completed")
        os.kill(a.pid, signal.SIGCONT)
        wait_for(lambda: any(e.get("event") == "result_refused" for e in read(a_status).get("events", [])), 5)
        assert cluster.task("effect-old")["state"] == "reconcile_required"
        assert ledger.count("op-once") == 1
    finally:
        ledger.close()
        close_node(a)


def test_killed_host_lease_expires_and_read_recovers_on_new_launch(tmp_path):
    package = build_fixture_package(tmp_path, "1.0.0")
    path = tmp_path / "v1.json"
    save_package(package, path)
    cluster_path, store_path, effects = (tmp_path / name for name in
                                          ("cluster.db", "host-store.db", "effects.db"))
    cluster = EngineCluster(cluster_path, store_path, lease_seconds=1.5)
    cluster.bootstrap(package)
    a, _, old = start_node(tmp_path, cluster_path, store_path, effects, path, "a", 1)
    old_provider_created = psutil.Process(old["provider_pid"]).create_time()
    newer = None
    try:
        cluster.enqueue("killed-read", "test.fake", "invoke_read", {"echo": "recover", "sleep": 1})
        claimed = wait_for(lambda: cluster.task("killed-read") if
                           cluster.task("killed-read")["state"] == "claimed" else None)
        os.kill(a.pid, signal.SIGKILL)
        a.communicate(timeout=3)
        time.sleep(1.7)
        assert cluster.expire("test.fake") == ["a"]
        assert cluster.task("killed-read")["state"] == "queued"
        with pytest.raises(ClusterRefused, match="node_lease_or_launch_stale"):
            cluster.claim("a", old["launch_id"])
        with pytest.raises(ClusterRefused, match="matching_ready_node_missing"):
            cluster.enqueue("new-unroutable", "test.fake", "invoke_read", {"echo": "none"})
        newer, _, fresh = start_node(tmp_path, cluster_path, store_path, effects, path, "a", 1)
        assert fresh["launch_id"] != old["launch_id"]
        wait_for(lambda: cluster.task("killed-read")["state"] == "succeeded")
        final = cluster.task("killed-read")
        assert final["identity"] == claimed["identity"]
        assert final["launch_id"] == fresh["launch_id"]
        assert final["attempt"] == 2
    finally:
        if newer is not None:
            close_node(newer)
        close_node(a)
        if psutil.pid_exists(old["provider_pid"]):
            orphan = psutil.Process(old["provider_pid"])
            if abs(orphan.create_time() - old_provider_created) < .1:
                orphan.terminate()
                try:
                    orphan.wait(timeout=3)
                except psutil.TimeoutExpired:
                    orphan.kill()
