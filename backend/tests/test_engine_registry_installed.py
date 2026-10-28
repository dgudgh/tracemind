"""Registry admission consumes actual signed files, not caller verification flags."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.engine_registry import Catalog
from app.runtime.engine_host.provider_packages import build_extension_package
from app.runtime.engine_packs.archive import build_archive
from app.runtime.engine_packs.repository import LabRepository
from app.runtime.engine_packs.trust import TrustedEngineKey, TrustError
from test_engine_repository_trust_integration import ready_repository


def extension(tmp_path, publisher='vendor'):
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(),
        namespace='tmengine.test', channel='test', publisher=publisher,
        engine_scopes=('demo.engine',), fixture_only=True)
    package = build_extension_package(tmp_path, engine_id='demo.engine',
        adapter_source=b'ENGINE_ID="demo.engine"\n', driver='demo_driver', operations=('identify',),
        private_key=private, trusted_key=key, development_fixture=True)
    return package


def test_new_extension_installs_without_core_engine_list_edit(tmp_path):
    package = extension(tmp_path)
    repo = LabRepository(tmp_path/'repo', trusted_keys=package.trusted_keys)
    build_archive(package.source, package.manifest, package.signature,
        repo.root/'incoming'/'extension.tmengine', trusted_keys=package.trusted_keys,
        expected_namespace='tmengine.test', expected_channel='test')
    repo.install('extension.tmengine', 'install')
    before = Catalog.empty()
    after = before.register_installed(repo, package.digest)
    entry = after.get('demo.engine')
    assert before.get('demo.engine') is None
    assert entry.publisher == 'vendor' and entry.capabilities == ('identify',)
    assert entry.package_digest == package.digest and entry.fixture_only
    assert not entry.available and entry.reason == 'requires_runtime_validation'
    assert after.register_installed(repo, package.digest) is after


def test_managed_registry_rechecks_revocation_after_install(tmp_path, monkeypatch):
    repo, fixture, trust = ready_repository(tmp_path, monkeypatch)
    repo.install('provider.tmengine', 'install')
    after = Catalog.empty().register_installed(repo, fixture.package_digest)
    assert after.get('dm').publisher == 'acme'
    trust.update(fixture.bundle(version=2, revoked_digests=[fixture.package_digest]))
    with pytest.raises(TrustError, match='package_revoked'):
        after.register_installed(repo, fixture.package_digest)


def test_signed_payload_tampering_is_not_catalog_metadata(tmp_path):
    package = extension(tmp_path)
    repo = LabRepository(tmp_path/'repo', trusted_keys=package.trusted_keys)
    build_archive(package.source, package.manifest, package.signature,
        repo.root/'incoming'/'extension.tmengine', trusted_keys=package.trusted_keys,
        expected_namespace='tmengine.test', expected_channel='test')
    repo.install('extension.tmengine', 'install')
    path = repo.root/'packages'/package.digest.removeprefix('sha256:')/'engine_provider.py'
    path.chmod(0o600)
    path.write_text('ENGINE_ID="oracle"\n')
    with pytest.raises(ValueError):
        Catalog.empty().register_installed(repo, package.digest)
