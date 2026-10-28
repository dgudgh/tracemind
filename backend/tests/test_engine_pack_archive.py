from __future__ import annotations

import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from app.runtime.engine_host.packages import build_fixture_package
from app.runtime.engine_host.supervisor import FixtureSupervisor
from app.runtime.engine_packs.archive import ArchiveError, ArchiveLimits, build_archive, open_verified_archive
from app.runtime.engine_packs.contract import EnginePackContractError
from app.runtime.engine_packs.trust import TrustError


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path.resolve()
    package = build_fixture_package(root, "1.0.0")
    output = root / "provider.tmengine"
    options = dict(trusted_keys=package.trusted_keys, expected_namespace="tmengine.test", expected_channel="test")
    receipt = build_archive(package.source, package.manifest, package.signature, output, **options)
    return package, output, receipt, options


def _rewrite(source, target, mutate):
    with zipfile.ZipFile(source) as zf:
        members = [(x, zf.read(x)) for x in zf.infolist()]
    members = mutate(members)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as zf:
        for info, data in members:
            zf.writestr(info, data)
    return target


def _member(name, data=b"malicious", *, mode=stat.S_IFREG | 0o600, compression=zipfile.ZIP_STORED):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = compression
    return info, data


def test_deterministic_archive_actual_bytes_and_installed_source_executes(bundle, tmp_path):
    package, archive, receipt, options = bundle
    second = tmp_path.resolve() / "same.tmengine"
    assert build_archive(package.source, package.manifest, package.signature, second, **options) == receipt
    assert archive.read_bytes() == second.read_bytes()
    source = tmp_path.resolve() / "extracted"
    with open_verified_archive(archive, **options) as verified:
        assert verified.package_digest == package.digest
        verified.extract(source)
    host = FixtureSupervisor(workdir=tmp_path.resolve() / "host")
    try:
        host.start(source, package.manifest, package.signature, package.trusted_keys,
                   expected_namespace="tmengine.test", expected_channel="test")
        # The framed reply keeps the Provider envelope (schema/provider_version/values);
        # only Host.invoke_read() unwraps `values`, and this test drives the raw call.
        assert host.call("invoke_read", {"echo": "from-archive"})["values"]["marker"] == "v1"
    finally:
        host.stop()
    assert source.joinpath("ENGINE-MANIFEST.json").is_file()


@pytest.mark.parametrize("name", [
    "../outside", "/outside", "a/../../outside", "a//b", "a/./b", "C:/outside",
    "a\\outside", "a\x00b", "a\nb", "a:b", "CON", "NUL.txt", "COM1.bin",
    "LPT9", "COM¹", "dir./file", "dir /file", "．．/outside", "x／y",
])
def test_unsafe_archive_paths_rejected_before_extract(bundle, tmp_path, name):
    _, source, _, options = bundle
    # zipfile truncates NUL itself; use direct safe-name boundary for that case.
    if "\x00" in name:
        from app.runtime.engine_packs.archive import safe_name
        with pytest.raises(ArchiveError):
            safe_name(name)
        return
    bad = _rewrite(source, tmp_path.resolve() / "bad.tmengine", lambda rows: rows + [_member(name)])
    with pytest.raises(ArchiveError):
        with open_verified_archive(bad, **options):
            pytest.fail("unsafe archive accepted")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("pair", [("A/x", "a/y"), ("x", "x/y"), ("é", "e\u0301"), ("x", "x")])
def test_colliding_members_and_file_directory_aliases_rejected(bundle, tmp_path, pair):
    _, source, _, options = bundle
    bad = _rewrite(source, tmp_path.resolve() / "bad.tmengine", lambda rows: rows + [_member(x) for x in pair])
    with pytest.raises(ArchiveError):
        with open_verified_archive(bad, **options):
            pytest.fail("colliding archive accepted")


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o700, stat.S_IFIFO | 0o600, stat.S_IFREG | 0o4600])
def test_link_directory_fifo_setuid_members_rejected(bundle, tmp_path, mode):
    _, source, _, options = bundle
    bad = _rewrite(source, tmp_path.resolve() / "bad.tmengine", lambda rows: rows + [_member("link", mode=mode)])
    with pytest.raises(ArchiveError, match="archive_not_regular"):
        with open_verified_archive(bad, **options):
            pass


@pytest.mark.parametrize("kind", ["extra", "missing", "bytes", "manifest", "signature", "duplicate_json", "deflate", "comment", "prefix", "truncated", "huge_count"])
def test_tamper_and_unsupported_encoding_fail_closed(bundle, tmp_path, kind):
    _, source, _, options = bundle
    bad = tmp_path.resolve() / "bad.tmengine"

    def mutate(rows):
        if kind == "extra": return rows + [_member("extra.bin")]
        if kind == "missing": return rows[:-1]
        if kind == "bytes": return [(i, b"changed" if i.filename.endswith("fake_provider.py") else b) for i, b in rows]
        if kind == "manifest": return [(i, b.replace(b'"version":"1.0.0"', b'"version":"1.9.9"') if i.filename == "ENGINE-MANIFEST.json" else b) for i, b in rows]
        if kind == "signature": return [(i, b.replace(b'"ed25519"', b'"bad"') if i.filename == "ENGINE-MANIFEST.sig" else b) for i, b in rows]
        if kind == "duplicate_json": return [(i, b'{"a":1,"a":2}' if i.filename == "ENGINE-MANIFEST.json" else b) for i, b in rows]
        if kind == "deflate":
            rows[0][0].compress_type = zipfile.ZIP_DEFLATED
        return rows

    _rewrite(source, bad, mutate)
    data = bad.read_bytes()
    if kind == "comment": data += b"trailing bytes"
    if kind == "prefix": data = b"junk" + data
    if kind == "truncated": data = data[:-8]
    if kind == "huge_count":
        data = bytearray(data)
        struct.pack_into("<HH", data, len(data) - 14, 6000, 6000)
    bad.write_bytes(data)
    with pytest.raises((ArchiveError, EnginePackContractError, TrustError)):
        with open_verified_archive(bad, **options):
            pytest.fail("tamper accepted")


@pytest.mark.parametrize("limit", ["archive_bytes", "expanded_bytes", "file_bytes", "control_bytes", "entries", "central_bytes"])
def test_all_archive_budgets_apply_before_publication(bundle, limit):
    _, source, _, options = bundle
    with pytest.raises(ArchiveError, match="archive_limit_exceeded"):
        with open_verified_archive(source, limits=ArchiveLimits(**{limit: 1}), **options):
            pass


def test_source_pinning_link_rejection_no_overwrite_and_target_guard(bundle, tmp_path):
    package, source, _, options = bundle
    link = tmp_path.resolve() / "link.tmengine"
    link.symlink_to(source)
    with pytest.raises(ArchiveError, match="archive_link_rejected"):
        with open_verified_archive(link, **options): pass
    hard = tmp_path.resolve() / "hard.tmengine"
    os.link(source, hard)
    with pytest.raises(ArchiveError, match="archive_not_regular"):
        with open_verified_archive(source, **options): pass
    hard.unlink()
    with pytest.raises(ArchiveError, match="archive_digest_mismatch"):
        with open_verified_archive(source, expected_archive_sha256="sha256:" + "0" * 64, **options): pass
    with pytest.raises(ArchiveError, match="archive_output_invalid"):
        build_archive(package.source, package.manifest, package.signature, source, **options)
    dest = tmp_path.resolve() / "existing"
    dest.mkdir()
    with open_verified_archive(source, **options) as verified:
        with pytest.raises(FileExistsError): verified.extract(dest)
        # Replacing the import pathname does not change the pinned verified bytes.
        source.rename(source.with_suffix(".old"))
        source.write_bytes(b"replacement")
        verified.extract(tmp_path.resolve() / "pinned")
    assert (tmp_path / "pinned/fake_provider.py").read_bytes() == (package.source / "fake_provider.py").read_bytes()


@pytest.mark.parametrize("update,code", [
    ({"hooks": ["curl bad"]}, "archive_hook_forbidden"),
    ({"install_hook": "bad"}, "archive_hook_forbidden"),
    ({"executable": "/bin/sh"}, "archive_hook_forbidden"),
    ({"module": "os"}, "archive_hook_forbidden"),
    ({"entrypoint_id": "../../shell"}, "archive_entrypoint_unsupported"),
    ({"format": True}, "archive_manifest_identity_invalid"),
    ({"os": {"value": "darwin"}}, "archive_manifest_identity_invalid"),
    ({"rpc_version": True}, "archive_manifest_identity_invalid"),
])
def test_signed_package_cannot_authorize_hooks_or_arbitrary_entry(tmp_path, update, code):
    package = build_fixture_package(tmp_path.resolve(), manifest_updates=update)
    output = tmp_path.resolve() / "rejected.tmengine"
    with pytest.raises(ArchiveError, match=code):
        build_archive(package.source, package.manifest, package.signature, output,
                      trusted_keys=package.trusted_keys, expected_namespace="tmengine.test", expected_channel="test")
    assert not output.exists()
    assert not list(tmp_path.glob(".tmengine-build-*"))


@pytest.mark.parametrize("name,contents", [("id_rsa", b"credential"), (".env", b"secret=value"),
    ("data.bin", b"x" * (128 * 1024 - 10) + b"-----BEGIN PRIVATE KEY-----\nsecret")])  # secret-scan: allow - synthetic rejection marker, not a key
def test_even_trusted_signed_private_key_or_environment_payload_is_rejected(tmp_path, name, contents):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from app.runtime.engine_packs.contract import scan_payload_directory
    from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes
    package = build_fixture_package(tmp_path.resolve())
    (package.source / name).write_bytes(contents)
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(), namespace="tmengine.test",
        channel="test", publisher="fixture", engine_scopes=("test.fake",), fixture_only=True)
    files = scan_payload_directory(package.source)
    manifest = dict(package.manifest, signing_key_id=key.key_id, package_file_inventory=files,
        whole_package_signed_files=[x["path"] for x in files], file_classification={x["path"]:"self-built" for x in files})
    signature = {"algorithm":"ed25519", "key_id":key.key_id,
                 "value":base64.b64encode(private.sign(canonical_manifest_bytes(manifest))).decode()}
    output = tmp_path.resolve() / "rejected.tmengine"
    with pytest.raises(ArchiveError, match="archive_credentials_forbidden"):
        build_archive(package.source, manifest, signature, output, trusted_keys=(key,),
                      expected_namespace="tmengine.test", expected_channel="test")
    assert not output.exists()
