from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.supervisor import FixtureSupervisor
from app.runtime.engine_packs.archive import ArchiveError, build_archive
from app.runtime.engine_packs.repository import LabRepository

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/tmengine.py"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path.resolve()
    v1, v2 = build_fixture_package(root, "1.0.0"), build_fixture_package(root, "1.0.1")
    keys = (*v1.trusted_keys, *v2.trusted_keys)
    policy = root / "trust.json"
    policy.write_text(json.dumps({"schema": "tmengine.lab-trust/v1", "keys": [
        {"public_key_hex": k.raw_public_key.hex(), "publisher": k.publisher,
         "engine_scopes": list(k.engine_scopes)} for k in keys]}))
    repository = LabRepository(root / "repo", trusted_keys=keys)
    for v in (v1, v2):
        build_archive(v.source, v.manifest, v.signature,
            repository.root / "incoming" / f"{v.version}.tmengine", trusted_keys=keys,
            expected_namespace="tmengine.test", expected_channel="test")
    return repository, policy, v1, v2


def cli(repo, policy, *args):
    return subprocess.run([sys.executable, str(CLI), "--trust-policy", str(policy),
                           "--repository", str(repo.root), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)


def test_install_list_inspect_idempotency_and_no_activation(repo):
    repository, policy, v1, v2 = repo
    first = repository.install("1.0.0.tmengine", "request-1")
    assert first == repository.install("1.0.0.tmengine", "request-1")
    assert first["package_digest"] == v1.digest and first["activated"] is False
    assert len(repository.list()) == 1
    inspect = repository.inspect(v1.digest)
    assert inspect["actual_payload_digest"] == v1.payload_digest
    assert inspect["activated"] is False
    assert repository.status("request-1")["state"] == "installed"
    installed = repository.root / "packages" / v1.digest[7:]
    assert installed.stat().st_mode & 0o777 == 0o500
    assert all(p.stat().st_mode & 0o222 == 0 for p in installed.rglob("*"))
    with pytest.raises(ArchiveError, match="repository_idempotency_conflict"):
        repository.install("1.0.1.tmengine", "request-1")
    assert len(repository.list()) == 1 and not (repository.root / "packages" / v2.digest[7:]).exists()
    assert any(json.loads(r["record"])["status"] == "rejected" for r in repository.audit())
    result = cli(repository, policy, "list")
    assert result.returncode == 0 and len(json.loads(result.stdout)["result"]) == 1


@pytest.mark.parametrize("name", ["../outside.tmengine", "/tmp/x.tmengine", "dir/a.tmengine", "a.zip", "a\\b.tmengine"])
def test_controlled_import_names_reject_arbitrary_paths_and_audit(repo, name):
    repository, _, _, _ = repo
    with pytest.raises(ArchiveError): repository.install(name, "bad-path")
    assert repository.list() == []
    assert len(repository.audit()) == 1


def test_repository_link_owner_unknown_root_and_audit_tamper_refused(repo, tmp_path):
    repository, _, v1, _ = repo
    link = tmp_path.resolve() / "linked-repo"
    link.symlink_to(repository.root)
    with pytest.raises(ArchiveError): LabRepository(link, trusted_keys=v1.trusted_keys)
    unknown = tmp_path.resolve() / "unknown"
    unknown.mkdir(mode=0o700)
    (unknown / "existing.db").write_text("do not modify")
    with pytest.raises(ArchiveError, match="repository_owner_missing"):
        LabRepository(unknown, trusted_keys=v1.trusted_keys)
    assert (unknown / "existing.db").read_text() == "do not modify"
    repository.install("1.0.0.tmengine", "one")
    db = sqlite3.connect(repository.root / "repository.sqlite")
    with db: db.execute("UPDATE audit SET record='{}' WHERE sequence=1")
    db.close()
    with pytest.raises(ArchiveError, match="repository_audit_chain_invalid"): repository.audit()


def test_valid_signature_same_version_different_payload_does_not_overwrite(repo, tmp_path):
    import base64
    import hashlib
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from app.runtime.engine_packs.contract import scan_payload_directory
    from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes
    repository, _, v1, _ = repo
    repository.install("1.0.0.tmengine", "first")
    other_root = tmp_path.resolve() / "different-build"
    other_root.mkdir()
    other = build_fixture_package(other_root, "1.0.0")
    source = other.source / "fake_provider.py"
    source.write_bytes(source.read_bytes() + b"\n# alternate signed source build\n")
    sbom = other.source / "SBOM.json"
    components = json.loads(sbom.read_text())
    for component in components["components"]:
        if component["path"] == "fake_provider.py": component["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    sbom.write_text(json.dumps(components))
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(), namespace="tmengine.test",
        channel="test", publisher="fixture", engine_scopes=("test.fake",), fixture_only=True)
    files = scan_payload_directory(other.source)
    manifest = dict(other.manifest, signing_key_id=key.key_id, package_file_inventory=files)
    signature = {"algorithm":"ed25519", "key_id":key.key_id,
                 "value":base64.b64encode(private.sign(canonical_manifest_bytes(manifest))).decode()}
    digest = "sha256:" + hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    repository.keys = (*repository.keys, key)
    build_archive(other.source, manifest, signature, repository.root / "incoming/other.tmengine",
        trusted_keys=repository.keys, expected_namespace="tmengine.test", expected_channel="test")
    with pytest.raises(ArchiveError, match="repository_version_conflict"):
        repository.install("other.tmengine", "other-request")
    assert len(repository.list()) == 1
    assert repository.inspect(v1.digest)["package_digest"] == v1.digest
    assert not (repository.root / "packages" / digest[7:]).exists()


@pytest.mark.parametrize("point", ["after_prepare", "after_stage", "after_rename", "after_commit"])
def test_real_install_sigkill_new_cli_recovers_exact_operation(repo, tmp_path, point):
    repository, policy, v1, _ = repo
    marker = tmp_path.resolve() / "kill-marker.json"
    worker = tmp_path.resolve() / "install-worker.py"
    worker.write_text('''import json,sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nsys.path.insert(0,sys.argv[2])\nfrom app.runtime.engine_packs.repository import LabRepository\nfrom tmengine import load_lab_policy\ndef checkpoint(point,op):\n if point==sys.argv[6]:\n  Path(sys.argv[5]).write_text(json.dumps(op))\n  while True: time.sleep(1)\nrepo=LabRepository(Path(sys.argv[3]),trusted_keys=load_lab_policy(sys.argv[4]),checkpoint=checkpoint)\nrepo.install('1.0.0.tmengine','killed-request')\n''')
    process = subprocess.Popen([sys.executable, str(worker), str(ROOT / "backend"), str(ROOT / "scripts"),
        str(repository.root), str(policy), str(marker), point], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), process.communicate(timeout=1)
        operation = json.loads(marker.read_text())
        process.kill()
        assert process.wait(timeout=10) == -9
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=10)
    result = cli(repository, policy, "recover")
    assert result.returncode == 0, result.stderr + result.stdout
    retry = cli(repository, policy, "install", "--import-name", "1.0.0.tmengine", "--request-id", "killed-request")
    assert retry.returncode == 0, retry.stderr
    receipt = json.loads(retry.stdout)["result"]
    assert receipt["operation_id"] == operation["operation_id"]
    assert receipt["package_digest"] == v1.digest and receipt["state"] == "installed"
    assert len(repository.list()) == 1
    assert not list((repository.root / ".staging").iterdir())
    assert len([x for x in repository.audit() if json.loads(x["record"])["status"] == "installed"]) == 1


def test_recovery_rejects_replaced_import_and_preserves_failed_operation(repo, tmp_path):
    repository, policy, _, _ = repo
    def checkpoint(point, _op):
        if point == "after_prepare": raise RuntimeError("interrupted")
    repository.checkpoint = checkpoint
    with pytest.raises(RuntimeError): repository.install("1.0.0.tmengine", "replace")
    shutil.copyfile(repository.root / "incoming/1.0.1.tmengine", repository.root / "incoming/1.0.0.tmengine")
    result = cli(repository, policy, "recover")
    assert result.returncode == 2
    assert json.loads(result.stdout)["result"][0]["code"] == "archive_digest_mismatch"
    assert repository.status("replace")["state"] == "blocked"
    assert repository.list() == []


def test_two_real_cli_processes_claim_same_request_once(repo):
    repository, policy, _, _ = repo
    command = [sys.executable, str(CLI), "--trust-policy", str(policy), "--repository", str(repository.root),
               "install", "--import-name", "1.0.0.tmengine", "--request-id", "concurrent"]
    processes = [subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    receipts = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        receipts.append(json.loads(stdout)["result"])
    assert receipts[0] == receipts[1]
    assert len(repository.list()) == 1
    assert len([x for x in repository.audit() if json.loads(x["record"])["status"] == "installed"]) == 1


def test_cli_requires_external_trust_and_rejects_production_policy(repo, tmp_path):
    repository, _, _, _ = repo
    policy = tmp_path.resolve() / "untrusted.json"
    policy.write_text(json.dumps({"schema": "tmengine.production", "keys": []}))
    result = cli(repository, policy, "list")
    assert result.returncode == 2 and json.loads(result.stderr)["code"] == "lab_trust_policy_invalid"


def test_real_cli_build_verify_install_inspect_and_status(repo, tmp_path):
    repository, policy, v1, _ = repo
    manifest, signature = tmp_path.resolve() / "manifest.json", tmp_path.resolve() / "signature.json"
    manifest.write_text(json.dumps(v1.manifest)); signature.write_text(json.dumps(v1.signature))
    artifact = repository.root / "incoming" / "cli-built.tmengine"
    result = cli(repository, policy, "build", "--source", str(v1.source), "--manifest", str(manifest),
                 "--signature", str(signature), "--output", str(artifact))
    assert result.returncode == 0, result.stderr
    digest = json.loads(result.stdout)["result"]["archive_sha256"]
    verified = cli(repository, policy, "verify", "--archive", str(artifact), "--expected-archive-sha256", digest)
    assert verified.returncode == 0, verified.stderr
    installed = cli(repository, policy, "install", "--import-name", artifact.name, "--request-id", "cli-workflow")
    assert installed.returncode == 0, installed.stderr
    assert cli(repository, policy, "inspect", "--digest", v1.digest).returncode == 0
    assert json.loads(cli(repository, policy, "status", "--request-id", "cli-workflow").stdout)["result"]["state"] == "installed"


def test_installed_readonly_artifact_runs_via_existing_supervisor(repo, tmp_path):
    repository, _, v1, _ = repo
    receipt = repository.install("1.0.0.tmengine", "runtime")
    source = repository.root / "packages" / receipt["package_digest"][7:]
    host = FixtureSupervisor(workdir=tmp_path.resolve() / "installed-host")
    try:
        record = host.start(source, v1.manifest, v1.signature, repository.keys,
            expected_namespace="tmengine.test", expected_channel="test")
        assert record.pid != os.getpid()
        # Same envelope as test_engine_pack_archive: the raw call() reply keeps the
        # Provider's own {schema, provider_version, values} shape.
        assert host.call("invoke_read", {"echo": "installed"})["values"]["echo"] == "installed"
    finally:
        host.stop()
    assert not Path(record.socket_path).exists()
    assert source.exists()  # stop removes its own runtime copy, not the installed artifact


def test_mutable_external_policy_is_rejected_before_install(repo):
    repository, policy, _, _ = repo
    policy.chmod(0o666)
    result = cli(repository, policy, "install", "--import-name", "1.0.0.tmengine", "--request-id", "mutable-policy")
    assert result.returncode == 2 and json.loads(result.stderr)["code"] == "trust_policy_owner_mismatch"
    assert repository.list() == []


def test_whole_installation_runner_records_actual_kills_and_cleanup(tmp_path):
    report = tmp_path.resolve() / "acceptance.json"
    process = subprocess.run([sys.executable, str(ROOT / "scripts/check_engine_pack_installation.py"),
        "--workdir", str(tmp_path.resolve() / "acceptance-work"), "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(report.read_text())
    assert result["ok"] and len(result["runtime"]) == 2
    assert result["installed_update"]["final_generation"] == 3
    assert result["installed_update"]["activated_generation"] == 2
    assert result["installed_update"]["rollback_generation"] == 3
    assert len(result["kill_cases"]) == 4
    assert all(case["installer_exit"] == -9 and case["recover_exit"] == 0 for case in result["kill_cases"])
    assert not result["cleanup"]["children_after"]
    assert not result["cleanup"]["owned_runtime_paths_after"]
