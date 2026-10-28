"""Process level lab management boundary and crash recovery evidence."""
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.orchestrator import FixtureUpdateOrchestrator
from app.runtime.engine_host.store import FixtureStore, StoreError
from app.runtime.engine_packs.archive import build_archive
from app.runtime.engine_packs.management import LabManagement, ManagementError, METHODS, _recv, management_socket_path
from app.runtime.engine_packs.repository import LabRepository

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "engine_pack_manager.py"


def _run(*args):
    process = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=ROOT,
                             capture_output=True, text=True, timeout=40)
    return process.returncode, json.loads(process.stdout or process.stderr)


def _server(repo, trust, policy, *extra):
    process = subprocess.Popen([sys.executable, str(CLI), "serve", "--mode", "lab",
        "--repository", str(repo), "--trust-policy", str(trust), "--admin-policy", str(policy), *map(str, extra)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if management_socket_path(repo).exists():
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2); probe.connect(str(management_socket_path(repo)))
                    _recv(probe)
                return process
            except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                pass
        if process.poll() is not None: raise AssertionError(process.communicate())
        time.sleep(0.02)
    process.kill(); process.wait()
    raise AssertionError("management_socket_missing")


def _call(repo, method, *args, expected=0):
    code, value = _run("call", "--socket", management_socket_path(repo), "--server-uid", os.getuid(), method, *args)
    assert code == expected, value
    return value


@pytest.fixture
def lab(tmp_path):
    package = build_fixture_package(tmp_path / "fixture", "1.0.0")
    key = package.trusted_keys[0]
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [{
        "public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
        "engine_scopes": list(key.engine_scopes)}]})); trust.chmod(0o600)
    policy = tmp_path / "admin.json"
    policy.write_text(json.dumps({"schema": "tmengine.lab-management-policy/v1",
        "allowed_uids": [os.getuid()], "allowed_methods": sorted(METHODS)})); policy.chmod(0o600)
    repo = LabRepository(tmp_path / "repository", trusted_keys=package.trusted_keys).root
    archive = tmp_path / "fixture.tmengine"
    receipt = build_archive(package.source, package.manifest, package.signature, archive,
        trusted_keys=package.trusted_keys, expected_namespace="tmengine.test", expected_channel="test")
    shutil.copyfile(archive, repo / "incoming" / "fixture.tmengine")
    yield repo, trust, policy, package, receipt
    socket_root = management_socket_path(repo).parent
    if socket_root.exists(): shutil.rmtree(socket_root)


def test_live_ipc_activation_crash_recovery_and_replay(lab):
    repo, trust, policy, package, receipt = lab
    first = _server(repo, trust, policy)
    try:
        assert _call(repo, "list")["result"] == []
        assert _call(repo, "verify", "--import-name", "fixture.tmengine", "--archive-sha256",
                     receipt["archive_sha256"])["result"]["package_digest"] == package.digest
        installed = _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
                          receipt["archive_sha256"], "--request-id", "install-1")
        assert installed["result"]["package_digest"] == package.digest
        assert _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
                     receipt["archive_sha256"], "--request-id", "install-1")["result"]["operation_id"] == installed["result"]["operation_id"]
        assert _call(repo, "inspect", "--digest", package.digest)["result"]["package_digest"] == package.digest
        assert _call(repo, "initial-activate", "--digest", package.digest,
                     "--request-id", "activate-1")["result"]["state"] == "active"
        state = _call(repo, "status")["result"]
        assert state["routes"][0]["digest"] == package.digest
        assert state["live_engines"] == ["test.fake"]
        assert _call(repo, "initial-activate", "--digest", package.digest,
                     "--request-id", "activate-1")["result"]["route"]["generation"] == 1
        # A captured request from another connection cannot carry its old nonce.
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(str(management_socket_path(repo))); hello = _recv(conn)
            raw = json.dumps({"schema": hello["schema"], "nonce": "0" * 32,
                              "method": "list", "args": {}}).encode()
            conn.sendall(struct.pack("!I", len(raw)) + raw)
            assert _recv(conn)["code"] == "management_session_mismatch"
        assert _call(repo, "verify", "--import-name", "../fixture.tmengine",
                     "--archive-sha256", receipt["archive_sha256"], expected=2)["code"]
        assert _call(repo, "initial-rollback", "--request-id", "activate-1", expected=2)["code"] == "management_rollback_not_unrouted"
        first.kill(); first.wait(timeout=10)
        second = _server(repo, trust, policy)
        try:
            cold = _call(repo, "status")["result"]
            assert cold["live_engines"] == []
            assert _call(repo, "recover")["result"]["routes"][0]["digest"] == package.digest
            assert _call(repo, "status")["result"]["live_engines"] == ["test.fake"]
            assert _call(repo, "recover")["result"]["routes"][0]["generation"] == 1
        finally:
            second.terminate(); second.wait(timeout=10)
        manager = LabManagement(repo, trusted_keys=package.trusted_keys, policy_path=policy)
        try:
            audit = manager.audit()
            assert any(json.loads(x["record"])["code"] == "management_session_mismatch" for x in audit)
            with pytest.raises(ManagementError, match="management_unauthorized"):
                manager.dispatch("list", {}, peer=(os.getuid() + 10000, os.getpid()))
            assert any(json.loads(x["record"])["code"] == "management_unauthorized" for x in manager.audit())
        finally: manager.close()
    finally:
        if first.poll() is None: first.kill(); first.wait(timeout=10)


def test_production_fail_closed_and_client_server_uid(lab):
    repo, trust, policy, _, _ = lab
    code, value = _run("serve", "--mode", "production", "--repository", repo,
                       "--trust-policy", trust, "--admin-policy", policy)
    assert code == 2 and value["code"] == "production_management_admission_missing"
    process = _server(repo, trust, policy)
    try:
        code, value = _run("call", "--socket", management_socket_path(repo), "--server-uid", os.getuid() + 1, "list")
        assert code == 2 and value["code"] == "management_endpoint_descriptor_invalid"
    finally:
        process.terminate(); process.wait(timeout=10)


def test_ready_before_cas_kill_and_concurrent_initial_activation(lab):
    repo, trust, policy, package, receipt = lab
    marker = repo.parent / "ready-checkpoint.json"
    first = _server(repo, trust, policy, "--lab-pause-at", "activation_ready",
                    "--lab-checkpoint-file", marker)
    try:
        _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
              receipt["archive_sha256"], "--request-id", "install-1")
        pending = subprocess.Popen([sys.executable, str(CLI), "call", "--socket",
            str(management_socket_path(repo)), "--server-uid", str(os.getuid()),
            "initial-activate", "--digest", package.digest, "--request-id", "interrupted"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            assert first.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        first.kill(); first.wait(timeout=10)
        pending.communicate(timeout=5)
        second = _server(repo, trust, policy)
        try:
            cold = _call(repo, "status")["result"]
            assert cold["routes"] == []
            recovery = _call(repo, "recover")["result"]
            assert recovery["routes"] == []
            assert recovery["launch_cleanup"] and all(not x["blocked_reasons"] for x in recovery["launch_cleanup"])
            state = _call(repo, "status")["result"]
            assert state["activations"][0]["state"] == "failed"
            # Two callers race against one fenced initial route. Exactly one wins.
            commands = []
            for name in ("race-a", "race-b"):
                commands.append(subprocess.Popen([sys.executable, str(CLI), "call", "--socket",
                    str(management_socket_path(repo)), "--server-uid", str(os.getuid()),
                    "initial-activate", "--digest", package.digest, "--request-id", name],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            responses = [(p.wait(timeout=20), json.loads((p.stdout.read() or p.stderr.read()))) for p in commands]
            assert sorted(x[0] for x in responses) == [0, 2]
            assert next(x[1] for x in responses if x[0] == 2)["code"] == "management_initial_route_exists"
            assert _call(repo, "status")["result"]["routes"][-1]["digest"] == package.digest
        finally:
            second.terminate(); second.wait(timeout=10)
    finally:
        if first.poll() is None: first.kill(); first.wait(timeout=10)


def test_rejected_peer_revocation_frames_and_import_replacement(lab):
    repo, trust, policy, package, receipt = lab
    process = _server(repo, trust, policy)
    try:
        # The archive is replaced after a successful verify. The installer pins
        # a fresh FD and rejects the expected SHA before publication.
        _call(repo, "verify", "--import-name", "fixture.tmengine", "--archive-sha256", receipt["archive_sha256"])
        source = repo / "incoming" / "fixture.tmengine"
        changed = bytearray(source.read_bytes()); changed[-1] ^= 1; source.write_bytes(changed)
        assert _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
                     receipt["archive_sha256"], "--request-id", "replace", expected=2)["code"] == "archive_digest_mismatch"
        assert not _call(repo, "list")["result"]
        assert _call(repo, "verify", "--import-name", "/tmp/fixture.tmengine",
                     "--archive-sha256", receipt["archive_sha256"], expected=2)["code"]
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(str(management_socket_path(repo))); hello = _recv(conn)
            body = (b'{"schema":"' + hello["schema"].encode() + b'","nonce":"' +
                    hello["nonce"].encode() + b'","method":"list","method":"list","args":{}}')
            conn.sendall(struct.pack("!I", len(body)) + body)
            assert _recv(conn)["code"] == "frame_json_invalid"
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(str(management_socket_path(repo))); _recv(conn)
            conn.sendall(struct.pack("!I", 65537))
            assert _recv(conn)["code"] == "management_frame_limit"
        policy.write_text(json.dumps({"schema": "tmengine.lab-management-policy/v1",
            "allowed_uids": [os.getuid() + 10000], "allowed_methods": sorted(METHODS)}))
        assert _call(repo, "list", expected=2)["code"] == "management_unauthorized"
        policy.write_text(json.dumps({"schema": "tmengine.lab-management-policy/v1",
            "allowed_uids": [os.getuid()], "allowed_methods": ["status"]}))
        assert _call(repo, "list", expected=2)["code"] == "management_unauthorized"
        assert _call(repo, "status")["ok"] is True
    finally:
        process.terminate(); process.wait(timeout=10)
    manager = LabManagement(repo, trusted_keys=package.trusted_keys, policy_path=policy)
    try:
        codes = {json.loads(row["record"])["code"] for row in manager.audit()}
        assert {"archive_digest_mismatch", "frame_json_invalid", "management_frame_limit",
                "management_unauthorized"} <= codes
    finally: manager.close()


def test_unready_candidate_never_routes(lab):
    repo, trust, policy, _, _ = lab
    bad = build_fixture_package(repo.parent / "unready", "1.0.0",
        manifest_updates={"runtime_digest": "sha256:" + "0" * 64})
    # Use its own explicit lab trust policy for this isolated repository.
    key = bad.trusted_keys[0]
    trust.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [{
        "public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
        "engine_scopes": list(key.engine_scopes)}]}))
    # Recreate repository with that key before any package has been installed.
    archive = repo.parent / "unready.tmengine"
    receipt = build_archive(bad.source, bad.manifest, bad.signature, archive,
        trusted_keys=bad.trusted_keys, expected_namespace="tmengine.test", expected_channel="test")
    shutil.copyfile(archive, repo / "incoming" / "unready.tmengine")
    process = _server(repo, trust, policy)
    try:
        _call(repo, "install", "--import-name", "unready.tmengine", "--archive-sha256",
              receipt["archive_sha256"], "--request-id", "bad-install")
        result = _call(repo, "initial-activate", "--digest", bad.digest, "--request-id", "bad-activation", expected=2)
        assert result["code"] == "runtime_identity_mismatch"
        status = _call(repo, "status")["result"]
        assert status["routes"] == [] and status["live_engines"] == []
        assert status["activations"][0]["state"] == "failed"
        assert _call(repo, "initial-rollback", "--request-id", "bad-activation")["result"]["state"] == "rolled_back"
    finally:
        process.terminate(); process.wait(timeout=10)


def test_lab_upgrade_disable_rollback_and_reference_gc(lab):
    repo, trust, policy, v1, first_receipt = lab
    v2 = build_fixture_package(repo.parent / "v2", "1.0.1")
    keys = [v1.trusted_keys[0], v2.trusted_keys[0]]
    trust.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [
        {"public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
         "engine_scopes": list(key.engine_scopes)} for key in keys]}))
    archive = repo.parent / "v2.tmengine"
    second_receipt = build_archive(v2.source, v2.manifest, v2.signature, archive,
        trusted_keys=keys, expected_namespace="tmengine.test", expected_channel="test")
    shutil.copyfile(archive, repo / "incoming" / "v2.tmengine")
    budget = repo.parent / "budget.json"
    budget.write_text(json.dumps(FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)))
    budget.chmod(0o600)
    server = _server(repo, trust, policy, "--lab-capacity-budget", budget)
    try:
        for name, receipt in (("fixture.tmengine", first_receipt), ("v2.tmengine", second_receipt)):
            _call(repo, "install", "--import-name", name, "--archive-sha256", receipt["archive_sha256"],
                  "--request-id", "install-" + name)
        _call(repo, "initial-activate", "--digest", v1.digest, "--request-id", "initial")
        store = FixtureStore(repo / "management-host.sqlite")
        try:
            old_task = store.prepare_task(task_id="old-held", engine_id="test.fake", payload={"echo": "held"})
            assert old_task["digest"] == v1.digest
            contenders = [subprocess.Popen([sys.executable, str(CLI), "call", "--socket",
                str(management_socket_path(repo)), "--server-uid", str(os.getuid()),
                "upgrade", "--digest", v2.digest, "--expected-generation", "1",
                "--request-id", request_id], cwd=ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True) for request_id in ("upgrade-a", "upgrade-b")]
            observed = [(process.wait(timeout=30), json.loads(process.stdout.read() or process.stderr.read()))
                        for process in contenders]
            assert sorted(code for code, _ in observed) == [0, 2]
            upgraded = next(value for code, value in observed if code == 0)
            assert next(value for code, value in observed if code == 2)["code"] == "management_stale_generation"
            winning_id = "upgrade-a" if observed[0][0] == 0 else "upgrade-b"
            assert upgraded["result"]["route"]["generation"] == 2
            replay = _call(repo, "upgrade", "--digest", v2.digest,
                           "--expected-generation", 1, "--request-id", winning_id)
            assert replay["result"]["replay"] is True
            assert replay["result"]["operation_id"] == upgraded["result"]["operation_id"]
            assert upgraded["result"]["capacity_admission"]["fixture_only"] is True
            assert _call(repo, "status")["result"]["retained_launches"]
            assert _call(repo, "uninstall", "--digest", v1.digest,
                         "--request-id", "remove-old", expected=2)["code"] == "management_package_referenced"
            _call(repo, "disable", "--digest", v2.digest, "--request-id", "disable-active")
            with pytest.raises(StoreError, match="package_disabled"):
                store.prepare_task(task_id="new-disabled", engine_id="test.fake", payload={"echo": "new"})
            assert store.prepare_task(task_id="old-held", engine_id="test.fake", payload={"echo": "held"})["digest"] == v1.digest
            store.set_task_result("old-held", status="succeeded", effect_state="none", result={"echo": "held"})
            store.release_task_reference("old-held")
            _call(repo, "gc")
            assert store.read_result("old-held")["echo"] == "held"
        finally: store.close()
    finally:
        server.terminate(); server.wait(timeout=10)
    reverse = repo.parent / "reverse-budget.json"
    reverse.write_text(json.dumps(FixtureUpdateOrchestrator.build_fixture_budget(v2, v1)))
    reverse.chmod(0o600)
    server = _server(repo, trust, policy, "--lab-capacity-budget", reverse)
    try:
        _call(repo, "recover")
        rollback = _call(repo, "rollback", "--digest", v1.digest,
                         "--expected-generation", 2, "--request-id", "rollback-1")
        assert rollback["result"]["route"]["generation"] == 3
        removed = _call(repo, "uninstall", "--digest", v2.digest, "--request-id", "remove-v2")
        assert removed["result"]["state"] == "uninstalled"
        assert _call(repo, "inspect", "--digest", v2.digest, expected=2)["code"] == "repository_package_missing"
        store = FixtureStore(repo / "management-host.sqlite")
        try: assert store.read_result("old-held")["echo"] == "held"
        finally: store.close()
    finally:
        server.terminate(); server.wait(timeout=10)


def test_sigkill_uninstall_recovery(lab):
    repo, trust, policy, package, receipt = lab
    marker = repo.parent / "uninstall-checkpoint.json"
    first = _server(repo, trust, policy, "--lab-pause-at", "uninstall_removed",
                    "--lab-checkpoint-file", marker)
    try:
        _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
              receipt["archive_sha256"], "--request-id", "install-1")
        pending = subprocess.Popen([sys.executable, str(CLI), "call", "--socket",
            str(management_socket_path(repo)), "--server-uid", str(os.getuid()),
            "uninstall", "--digest", package.digest, "--request-id", "uninstall-1"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            assert first.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        first.kill(); first.wait(timeout=10)
        pending.communicate(timeout=5)
        second = _server(repo, trust, policy)
        try:
            result = _call(repo, "recover")["result"]
            assert result["uninstall"][0]["state"] == "uninstalled"
            assert _call(repo, "list")["result"] == []
            assert _call(repo, "uninstall", "--digest", package.digest,
                         "--request-id", "uninstall-1")["result"]["state"] == "uninstalled"
        finally:
            second.terminate(); second.wait(timeout=10)
    finally:
        if first.poll() is None: first.kill(); first.wait(timeout=10)


def test_upgrade_unready_stale_generation_and_rejected_rollback(lab):
    repo, trust, policy, v1, first_receipt = lab
    bad = build_fixture_package(repo.parent / "bad-upgrade", "1.0.1",
        manifest_updates={"runtime_digest": "sha256:" + "0" * 64})
    keys = [v1.trusted_keys[0], bad.trusted_keys[0]]
    trust.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [
        {"public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
         "engine_scopes": list(key.engine_scopes)} for key in keys]}))
    archive = repo.parent / "bad-upgrade.tmengine"
    second_receipt = build_archive(bad.source, bad.manifest, bad.signature, archive,
        trusted_keys=keys, expected_namespace="tmengine.test", expected_channel="test")
    shutil.copyfile(archive, repo / "incoming" / "bad-upgrade.tmengine")
    budget = repo.parent / "bad-budget.json"
    budget.write_text(json.dumps(FixtureUpdateOrchestrator.build_fixture_budget(v1, bad)))
    budget.chmod(0o600)
    process = _server(repo, trust, policy, "--lab-capacity-budget", budget)
    try:
        for name, receipt in (("fixture.tmengine", first_receipt), ("bad-upgrade.tmengine", second_receipt)):
            _call(repo, "install", "--import-name", name, "--archive-sha256", receipt["archive_sha256"],
                  "--request-id", "install-" + name)
        _call(repo, "initial-activate", "--digest", v1.digest, "--request-id", "initial")
        assert _call(repo, "upgrade", "--digest", bad.digest, "--expected-generation", 9,
                     "--request-id", "stale", expected=2)["code"] == "management_stale_generation"
        result = _call(repo, "upgrade", "--digest", bad.digest, "--expected-generation", 1,
                       "--request-id", "unready", expected=2)
        assert result["code"] == "runtime_identity_mismatch"
        status = _call(repo, "status")["result"]
        assert status["routes"][0]["digest"] == v1.digest
        assert status["live_engines"] == ["test.fake"]
        assert status["updates"][0]["state"] == "blocked"
        assert _call(repo, "rollback", "--digest", bad.digest, "--expected-generation", 1,
                     "--request-id", "no-lkg", expected=2)["code"] in {"runtime_identity_mismatch", "no_safe_rollback"}
        assert _call(repo, "status")["result"]["routes"][0]["generation"] == 1
    finally:
        process.terminate(); process.wait(timeout=10)


@pytest.mark.parametrize("point,expected_generation", [("switch_ready", 1), ("switch_committed", 2)])
def test_sigkill_switch_recovery_exact_digest(lab, point, expected_generation):
    repo, trust, policy, v1, first_receipt = lab
    v2 = build_fixture_package(repo.parent / "switch-v2", "1.0.1")
    keys = [v1.trusted_keys[0], v2.trusted_keys[0]]
    trust.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [
        {"public_key_hex": key.raw_public_key.hex(), "publisher": key.publisher,
         "engine_scopes": list(key.engine_scopes)} for key in keys]}))
    archive = repo.parent / "switch-v2.tmengine"
    second_receipt = build_archive(v2.source, v2.manifest, v2.signature, archive,
        trusted_keys=keys, expected_namespace="tmengine.test", expected_channel="test")
    shutil.copyfile(archive, repo / "incoming" / "switch-v2.tmengine")
    budget = repo.parent / "switch-budget.json"
    budget.write_text(json.dumps(FixtureUpdateOrchestrator.build_fixture_budget(v1, v2)))
    budget.chmod(0o600)
    setup = _server(repo, trust, policy, "--lab-capacity-budget", budget)
    try:
        for name, receipt in (("fixture.tmengine", first_receipt), ("switch-v2.tmengine", second_receipt)):
            _call(repo, "install", "--import-name", name, "--archive-sha256", receipt["archive_sha256"],
                  "--request-id", "install-" + name)
        _call(repo, "initial-activate", "--digest", v1.digest, "--request-id", "initial")
    finally:
        setup.terminate(); setup.wait(timeout=10)
    marker = repo.parent / (point + ".json")
    first = _server(repo, trust, policy, "--lab-capacity-budget", budget,
                    "--lab-pause-at", point, "--lab-checkpoint-file", marker)
    try:
        _call(repo, "recover")
        pending = subprocess.Popen([sys.executable, str(CLI), "call", "--socket",
            str(management_socket_path(repo)), "--server-uid", str(os.getuid()), "upgrade",
            "--digest", v2.digest, "--expected-generation", "1", "--request-id", "switch-crash"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            assert first.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        first.kill(); first.wait(timeout=10)
        pending.communicate(timeout=5)
        second = _server(repo, trust, policy, "--lab-capacity-budget", budget)
        try:
            cold = _call(repo, "status")["result"]
            assert cold["routes"][0]["generation"] == expected_generation
            result = _call(repo, "recover")["result"]
            assert all(not launch["blocked_reasons"] for launch in result["launch_cleanup"])
            live = _call(repo, "status")["result"]
            assert live["routes"][0]["generation"] == expected_generation
            assert live["routes"][0]["digest"] == (v1.digest if point == "switch_ready" else v2.digest)
            assert live["live_engines"] == ["test.fake"]
            if point == "switch_ready":
                assert live["updates"][0]["state"] == "blocked"
            else:
                assert live["updates"][0]["state"] == "complete"
        finally:
            second.terminate(); second.wait(timeout=10)
    finally:
        if first.poll() is None: first.kill(); first.wait(timeout=10)


def test_gc_requires_disabled_and_preserves_unreferenced_candidate(lab):
    repo, trust, policy, package, receipt = lab
    process = _server(repo, trust, policy)
    try:
        _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
              receipt["archive_sha256"], "--request-id", "install-1")
        before = _call(repo, "gc")["result"]
        assert before["removed"] == [] and before["retained"][0]["code"] == "management_not_gc_candidate"
        assert _call(repo, "list")["result"][0]["package_digest"] == package.digest
        _call(repo, "disable", "--digest", package.digest, "--request-id", "disable-1")
        after = _call(repo, "gc")["result"]
        assert after["removed"][0]["digest"] == package.digest
        assert _call(repo, "list")["result"] == []
    finally:
        process.terminate(); process.wait(timeout=10)


def test_disable_rejects_live_update_lease(lab):
    repo, trust, policy, package, receipt = lab
    process = _server(repo, trust, policy)
    try:
        _call(repo, "install", "--import-name", "fixture.tmengine", "--archive-sha256",
              receipt["archive_sha256"], "--request-id", "install-1")
        _call(repo, "initial-activate", "--digest", package.digest, "--request-id", "initial")
        store = FixtureStore(repo / "management-host.sqlite")
        try:
            pending = store.start_update(engine_id="test.fake", digest=package.digest,
                expected_generation=1, idempotency_key="lease-probe")
            assert pending["state"] == "prepared"
            assert _call(repo, "disable", "--digest", package.digest,
                         "--request-id", "disable-while-lease", expected=2)["code"] == "management_lease_busy"
            store._write(lambda db: db.execute("DELETE FROM leases WHERE engine_id=? AND operation_id=?",
                                               ("test.fake", pending["operation_id"])))
        finally: store.close()
        assert _call(repo, "disable", "--digest", package.digest,
                     "--request-id", "disable-after-lease")["result"]["state"] == "disabled"
    finally:
        process.terminate(); process.wait(timeout=10)
