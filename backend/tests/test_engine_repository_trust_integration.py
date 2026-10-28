from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import shutil
import threading

import pytest

from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_packs.archive import build_archive, ArchiveError
from app.runtime.engine_packs.repository import EngineRepository
from app.runtime.engine_packs.trust import TrustedEngineKey, TrustError, canonical_manifest_bytes
from app.runtime.engine_packs.trust_metadata import TrustStore
import test_engine_trust_metadata as vectors


def ready_repository(tmp_path, monkeypatch, namespace="tmengine.test"):
    monkeypatch.setattr(vectors, "NOW", datetime.now(timezone.utc) - timedelta(seconds=5))
    root = tmp_path.resolve()
    build = root / "build"; build.mkdir()
    package = build_fixture_package(build)
    fixture = vectors.Fixture(namespace)
    manifest = dict(package.manifest, namespace=namespace,
        channel="test" if namespace == "tmengine.test" else "production",
        publisher="acme", engine_id="dm", signing_key_id=fixture.release[1])
    fixture.manifest, fixture.package_digest = manifest, vectors.digest(manifest)
    signature = {"algorithm":"ed25519", "key_id":fixture.release[1],
        "value":base64.b64encode(fixture.release[0].sign(canonical_manifest_bytes(manifest))).decode()}
    key = TrustedEngineKey.create(base64.b64decode(fixture.release[2]), namespace=namespace,
        channel=manifest["channel"], publisher="acme", engine_scopes=("dm",), fixture_only=True)
    trust = TrustStore(root / "trust", namespace)
    trust.bootstrap(fixture.root, expected_root_sha256=vectors.digest(fixture.root), fixture_only=True)
    trust.update(fixture.bundle())
    artifact = root / "source.tmengine"
    build_archive(package.source, manifest, signature, artifact, trusted_keys=(key,),
                  expected_namespace=namespace, expected_channel=manifest["channel"])
    repo = EngineRepository(root / "repository", namespace=namespace, channel=manifest["channel"], trust_guard=trust,
                            entrypoint_ids=("python.sdk.fixture.v1",))
    shutil.copyfile(artifact, repo.root / "incoming" / "provider.tmengine")
    return repo, fixture, trust


@pytest.mark.parametrize("namespace", ["tmengine.test", "tmengine.production"])
def test_managed_install_uses_lifecycle_and_keeps_fixture_provenance(tmp_path, monkeypatch, namespace):
    repo, fixture, trust = ready_repository(tmp_path, monkeypatch, namespace)
    result = repo.install("provider.tmengine", "first")
    assert result["package_digest"] == fixture.package_digest and result["fixture_only"] is True
    assert result["activated"] is False
    assert repo.inspect(fixture.package_digest)["namespace"] == namespace
    trust.update(fixture.bundle(version=2, revoked_digests=[fixture.package_digest]))
    with pytest.raises(TrustError, match="package_revoked"):
        repo.install("provider.tmengine", "after-revocation")
    with pytest.raises(TrustError, match="package_revoked"):
        with repo.guard_installed(fixture.package_digest, rollback=True):
            pytest.fail("revoked rollback authorized")


def test_mid_install_revocation_blocks_recovery_publication(tmp_path, monkeypatch):
    repo, fixture, trust = ready_repository(tmp_path, monkeypatch)
    def checkpoint(point, _operation):
        if point == "after_stage": raise RuntimeError("crashed")
    repo.checkpoint = checkpoint
    with pytest.raises(RuntimeError): repo.install("provider.tmengine", "interrupt")
    trust.update(fixture.bundle(version=2, revoked_digests=[fixture.package_digest]))
    result = repo.recover()
    assert result[0]["state"] == "blocked" and result[0]["code"] == "package_revoked"
    assert repo.list() == []
    assert not repo._target(fixture.package_digest).exists()


def test_trust_guard_covers_publication_commit_and_activation_scope(tmp_path, monkeypatch):
    repo, fixture, trust = ready_repository(tmp_path, monkeypatch)
    entered, completed = threading.Event(), threading.Event()
    def revoker():
        entered.set()
        trust.update(fixture.bundle(version=2, revoked_digests=[fixture.package_digest]))
        completed.set()
    threads = []
    def checkpoint(point, _operation):
        if point == "after_rename":
            thread = threading.Thread(target=revoker); threads.append(thread); thread.start()
            assert entered.wait(2)
            assert not completed.wait(0.05)
    repo.checkpoint = checkpoint
    # Revocation can win immediately after the commit guard releases. A receipt
    # recheck may then reject; committed files must still be consistently installed.
    try:
        repo.install("provider.tmengine", "serialized")
    except TrustError as exc:
        assert exc.code == "package_revoked"
    for thread in threads: thread.join(5)
    assert completed.is_set()
    assert repo.status("serialized")["state"] == "installed"
    assert len(repo.list()) == 1


def test_repository_cannot_reopen_under_other_domain_or_unregistered_entry(tmp_path, monkeypatch):
    repo, _fixture, trust = ready_repository(tmp_path, monkeypatch)
    with pytest.raises(ArchiveError, match="repository_lifecycle_trust_required"):
        EngineRepository(repo.root, namespace="tmengine.production", channel="production", trust_guard=trust)
    with pytest.raises(ArchiveError, match="archive_entrypoint_policy_invalid"):
        EngineRepository(tmp_path.resolve() / "other", trust_guard=trust, entrypoint_ids=("/bin/sh",))
