"""Payload byte contracts and C73 actual-evidence oracles."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_packs.contract import (
    BUILD_PROVENANCE_SCHEMA,
    INCIDENT_ENDORSEMENT_SCHEMA,
    SBOM_SCHEMA,
    EnginePackContractError,
    build_payload_inventory,
    compute_self_payload_digest,
    scan_payload_directory,
    validate_c73_receipt,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + _sha(data)


def _digest(text: str) -> str:
    return _digest_bytes(text.encode())


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def _write(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _manifest(root: Path, classifications: dict[str, str], *, third_party=None) -> dict:
    files = scan_payload_directory(root)
    return {
        "format": 1, "package_kind": "tmengine", "package_id": "pkg-1", "version": "1.0.0",
        "namespace": "tmengine.test", "channel": "test", "purpose": "tmengine.manifest-signing.v1",
        "publisher": "acme", "engine_id": "dm", "signing_key_id": "tmengine.test:" + "0" * 64,
        "package_file_inventory": files,
        "whole_package_signed_files": [item["path"] for item in files],
        "file_classification": classifications,
        "third_party": third_party or {},
    }


def _third_party_package(root: Path, opaque: dict[str, tuple[bytes, str, str]],
                         *, self_files: dict[str, bytes] | None = None) -> dict:
    """Create an actual package and a matching manifest.

    opaque maps path to (bytes, version, abi). One independent SBOM binds all
    opaque components. The SBOM is generated metadata and is self-built.
    """
    self_files = self_files or {"runtime/provider.py": b"self"}
    _write(root, {**self_files, **{path: value[0] for path, value in opaque.items()}})
    components = [
        {"path": path, "version": version, "sha256": _sha(data), "license": "allowed",
         "redistribution_status": "allowed"}
        for path, (data, version, _abi) in sorted(opaque.items())
    ]
    sbom = _canonical({"schema": SBOM_SCHEMA, "components": components})
    _write(root, {"SBOM.json": sbom})
    classifications = {path: "self-built" for path in self_files}
    classifications["SBOM.json"] = "self-built"
    classifications.update({path: "third-party-opaque" for path in opaque})
    metadata = {
        path: {
            "upstream_origin": f"vendor://acme/{path}", "version": version, "abi": abi,
            "raw_artifact_hash": _digest_bytes(data),
            "packaged_raw_artifact_hash": _digest_bytes(data),
            "sbom_path": "SBOM.json", "sbom_digest": _digest_bytes(sbom),
            "license_status": "allowed", "redistribution_status": "allowed",
            "upstream_signature_status": "recorded-unsigned",
        }
        for path, (data, version, abi) in opaque.items()
    }
    return _manifest(root, classifications, third_party=metadata)


def _fixture_self_receipt() -> dict:
    path = Path(__file__).parent / "fixtures/engine_packs/c73_self_positive.json"
    return json.loads(path.read_text())


def _provenance(receipt: dict, side: str, *, clean: bool = True) -> bytes:
    return _canonical({
        "schema": BUILD_PROVENANCE_SCHEMA,
        "builder_identity": receipt[f"builder_{side}_identity"],
        "builder_fingerprint": receipt[f"builder_{side}_fingerprint"],
        "clean": clean,
        "locked_input_digest": receipt["locked_input_digest"],
        "toolchain_digest": receipt["toolchain_digest"],
        "build_recipe_digest": receipt["build_recipe_digest"],
    })


def _self_build_roots(tmp_path: Path, receipt: dict) -> dict:
    a, b, evidence = tmp_path / "a", tmp_path / "b", tmp_path / "evidence"
    for root in (a, b):
        _write(root, {"runtime/provider.py": b"self"})
    _write(evidence, {
        "locked-inputs.txt": b"source-lock-v1\n",
        "toolchain.txt": b"python-3.12\n",
        "recipe.txt": b"recipe-v1\n",
        "builder-a.json": _provenance(receipt, "a"),
        "builder-b.json": _provenance(receipt, "b"),
    })
    return {
        "a": a, "b": b,
        "locked_inputs": evidence / "locked-inputs.txt",
        "toolchain": evidence / "toolchain.txt",
        "build_recipe": evidence / "recipe.txt",
        "a_provenance": evidence / "builder-a.json",
        "b_provenance": evidence / "builder-b.json",
    }


def _third_receipt(root: Path, opaque: dict[str, tuple[bytes, str, str]]) -> dict:
    manifest = _third_party_package(root, opaque)
    paths = [item["path"] for item in manifest["package_file_inventory"]]
    primary = sorted(opaque)[0]
    primary_meta = manifest["third_party"][primary]
    return {
        "variant_id": "third-party-integrity", "classification": "third-party-opaque",
        "primary_opaque_path": primary,
        "upstream_origin": primary_meta["upstream_origin"],
        "raw_artifact_hash": primary_meta["raw_artifact_hash"],
        "packaged_raw_artifact_hash": primary_meta["packaged_raw_artifact_hash"],
        "sbom_digest": primary_meta["sbom_digest"],
        "license_status": primary_meta["license_status"],
        "upstream_signature_status": primary_meta["upstream_signature_status"],
        "opaque_files": copy.deepcopy(manifest["third_party"]),
        "payload_manifest": manifest,
        "package_classification": {
            "package_file_inventory": paths,
            "file_classification": copy.deepcopy(manifest["file_classification"]),
            "whole_package_signed_files": paths,
        },
        "oracle_receipt": {
            "variant_id": "third-party-integrity", "whole_package_signed_files": paths,
            "opaque_file_hashes": {path: _digest_bytes(data)
                                   for path, (data, _version, _abi) in opaque.items()},
        },
    }


def _incident(tmp_path: Path, *, reuse_key: bool = False, clean: bool = True):
    rebuilt, evidence = tmp_path / "rebuilt", tmp_path / "incident-evidence"
    _write(rebuilt, {"runtime/provider.py": b"rebuilt-good"})
    inventory = scan_payload_directory(rebuilt)
    rebuilt_digest = compute_self_payload_digest(inventory)
    compromised_private = Ed25519PrivateKey.generate()
    new_private = compromised_private if reuse_key else Ed25519PrivateKey.generate()
    compromised_raw = compromised_private.public_key().public_bytes_raw()
    new_raw = new_private.public_key().public_bytes_raw()
    clean_fingerprint = _digest("clean-builder-hardware")
    dirty_fingerprint = _digest("dirty-builder-hardware")
    lock = b"source-lock-v1\n"
    toolchain = b"python-3.12\n"
    recipe = b"recipe-v1\n"
    receipt = {
        "variant_id": "incident-rebuild", "namespace": "tmengine.test",
        "compromised_namespace": "tmengine.test",
        "compromised_key_ids": ["tmengine.test:" + _sha(compromised_raw)],
        "revoked_payload_digests": [_digest("revoked-payload")],
        "clean_builder_identity": "builder-clean",
        "clean_builder_fingerprint": clean_fingerprint,
        "compromised_builder_identity": "builder-dirty",
        "compromised_builder_fingerprint": dirty_fingerprint,
        "new_signing_key_id": "tmengine.test:" + _sha(new_raw),
        "new_signing_key_fingerprint": _digest_bytes(new_raw),
        "compromised_key_fingerprint": _digest_bytes(compromised_raw),
        "rebuilt_payload_digest": rebuilt_digest,
        "locked_input_digest": _digest_bytes(lock),
        "toolchain_digest": _digest_bytes(toolchain),
        "build_recipe_digest": _digest_bytes(recipe),
        "package_classification": {
            "package_file_inventory": [item["path"] for item in inventory],
            "file_classification": {item["path"]: "self-built" for item in inventory},
            "whole_package_signed_files": [item["path"] for item in inventory],
        },
    }
    provenance = _canonical({
        "schema": BUILD_PROVENANCE_SCHEMA, "builder_identity": "builder-clean",
        "builder_fingerprint": clean_fingerprint, "clean": clean,
        "locked_input_digest": receipt["locked_input_digest"],
        "toolchain_digest": receipt["toolchain_digest"],
        "build_recipe_digest": receipt["build_recipe_digest"],
    })
    _write(evidence, {"clean-builder.json": provenance, "lock": lock,
                      "toolchain": toolchain, "recipe": recipe})
    endorsement = {
        "schema": INCIDENT_ENDORSEMENT_SCHEMA, "namespace": receipt["namespace"],
        "rebuilt_payload_digest": rebuilt_digest,
        "clean_builder_fingerprint": clean_fingerprint,
        "compromised_key_fingerprint": receipt["compromised_key_fingerprint"],
        "new_signing_key_fingerprint": receipt["new_signing_key_fingerprint"],
    }
    receipt["oracle_receipt"] = {
        "variant_id": "incident-rebuild", "clean_rebuild": True,
        "rebuilt_inventory": inventory,
        "new_key_endorsement": base64.b64encode(new_private.sign(_canonical(endorsement))).decode(),
    }
    roots = {
        "rebuilt": rebuilt, "clean_builder_provenance": evidence / "clean-builder.json",
        "locked_inputs": evidence / "lock", "toolchain": evidence / "toolchain",
        "build_recipe": evidence / "recipe",
        "compromised_public_key": compromised_raw, "new_public_key": new_raw,
    }
    return receipt, roots


def test_full_inventory_includes_sdk_and_sbom_but_excludes_only_controls(tmp_path):
    _write(tmp_path, {
        "fake_provider.py": b"provider", "sdk_runtime/wire.py": b"wire",
        "SBOM.json": _canonical({"schema": SBOM_SCHEMA, "components": []}),
        "ENGINE-MANIFEST.json": b"manifest", "ENGINE-MANIFEST.sig": b"signature",
    })
    manifest = _manifest(tmp_path, {
        "fake_provider.py": "self-built", "sdk_runtime/wire.py": "self-built",
        "SBOM.json": "self-built",
    })
    result = build_payload_inventory(tmp_path, manifest)
    assert result["whole_package_signed_files"] == [
        "SBOM.json", "fake_provider.py", "sdk_runtime/wire.py"
    ]


def test_actual_files_and_independent_sbom_bind_opaque_bytes(tmp_path):
    manifest = _third_party_package(tmp_path, {"drivers/vendor.bin": (b"vendor-1", "1.0", "cp312")})
    first = build_payload_inventory(tmp_path, manifest)
    assert first["third_party_paths"] == ["drivers/vendor.bin"]
    (tmp_path / "drivers/vendor.bin").write_bytes(b"vendor-2")
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "payload_hash_mismatch"
    manifest["package_file_inventory"] = scan_payload_directory(tmp_path)
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "third_party_raw_hash_drift"


def test_self_bytes_change_only_self_digest(tmp_path):
    manifest = _third_party_package(tmp_path, {"vendor.bin": (b"v", "1", "abi")})
    before = build_payload_inventory(tmp_path, manifest)["self_payload_digest"]
    (tmp_path / "runtime/provider.py").write_bytes(b"changed")
    manifest["package_file_inventory"] = scan_payload_directory(tmp_path)
    after = build_payload_inventory(tmp_path, manifest)["self_payload_digest"]
    assert after != before


@pytest.mark.parametrize("mutation,code", [
    (lambda m: m["whole_package_signed_files"].pop(), "manifest_signed_files_invalid"),
    (lambda m: m["whole_package_signed_files"].append(m["whole_package_signed_files"][0]),
     "manifest_signed_files_invalid"),
    (lambda m: m["file_classification"].pop("a.bin"), "manifest_classification_invalid"),
    (lambda m: m["file_classification"].__setitem__("a.bin", "unknown"),
     "manifest_classification_invalid"),
])
def test_signed_and_classification_sets_are_exact(tmp_path, mutation, code):
    _write(tmp_path, {"a.bin": b"a"})
    manifest = _manifest(tmp_path, {"a.bin": "self-built"})
    mutation(manifest)
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == code


def test_extra_and_missing_files_are_rejected(tmp_path):
    _write(tmp_path, {"a.bin": b"a"})
    manifest = _manifest(tmp_path, {"a.bin": "self-built"})
    _write(tmp_path, {"b.bin": b"b"})
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "manifest_file_extra"
    (tmp_path / "b.bin").unlink()
    (tmp_path / "a.bin").unlink()
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "manifest_file_missing"


def test_sbom_must_be_independent_parseable_and_bind_component(tmp_path):
    manifest = _third_party_package(tmp_path, {"vendor.bin": (b"vendor", "1", "x")})
    metadata = manifest["third_party"]["vendor.bin"]
    metadata["sbom_path"] = "vendor.bin"
    metadata["sbom_digest"] = metadata["raw_artifact_hash"]
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "third_party_sbom_invalid"
    manifest = _third_party_package(tmp_path, {"vendor.bin": (b"vendor", "1", "x")})
    sbom_path = tmp_path / "SBOM.json"
    bad_sbom = b'{"schema":"tmengine.sbom/v1","components":[],"components":[]}'
    sbom_path.write_bytes(bad_sbom)
    for item in manifest["package_file_inventory"]:
        if item["path"] == "SBOM.json":
            item.update(size=len(bad_sbom), sha256=_sha(bad_sbom))
    manifest["third_party"]["vendor.bin"]["sbom_digest"] = _digest_bytes(bad_sbom)
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, manifest)
    assert error.value.code == "third_party_sbom_invalid"


def test_opaque_source_license_abi_and_unproved_signature_are_rejected(tmp_path):
    for field, value, code in (
        ("upstream_origin", "invented", "third_party_metadata_invalid"),
        ("version", "unknown value", "third_party_metadata_invalid"),
        ("license_status", "invented-license", "third_party_metadata_invalid"),
        ("upstream_signature_status", "verified-upstream", "third_party_signature_claim_unproven"),
    ):
        root = tmp_path / field
        manifest = _third_party_package(root, {"vendor.bin": (b"vendor", "1", "x")})
        manifest["third_party"]["vendor.bin"][field] = value
        with pytest.raises(EnginePackContractError) as error:
            build_payload_inventory(root, manifest)
        assert error.value.code == code


def test_file_directory_dangling_links_and_hardlinks_are_rejected(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    cases = []
    file_link = tmp_path / "file-link"
    file_link.symlink_to(target)
    cases.append(file_link)
    with pytest.raises(EnginePackContractError) as error:
        scan_payload_directory(tmp_path)
    assert error.value.code == "payload_link_rejected"
    linked_root = tmp_path.parent / f"{tmp_path.name}-linked-root"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(EnginePackContractError) as error:
        scan_payload_directory(linked_root)
    assert error.value.code == "payload_link_rejected"
    linked_root.unlink()
    file_link.unlink()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    with pytest.raises(EnginePackContractError) as error:
        scan_payload_directory(tmp_path)
    assert error.value.code == "payload_link_rejected"
    dangling.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    dir_link = tmp_path / "dir-link"
    dir_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EnginePackContractError) as error:
        scan_payload_directory(tmp_path)
    assert error.value.code == "payload_link_rejected"
    dir_link.unlink()
    hardlink = tmp_path / "hardlink"
    os.link(target, hardlink)
    with pytest.raises(EnginePackContractError) as error:
        scan_payload_directory(tmp_path)
    assert error.value.code == "payload_link_rejected"


def test_unsafe_unhashable_bool_digest_and_collision_fail_stably(tmp_path):
    _write(tmp_path, {"a.bin": b"a"})
    base = _manifest(tmp_path, {"a.bin": "self-built"})
    mutations = [
        (lambda m: m["package_file_inventory"][0].__setitem__("path", "../a.bin"),
         "payload_path_unsafe"),
        (lambda m: m["package_file_inventory"][0].__setitem__("size", True),
         "manifest_inventory_invalid"),
        (lambda m: m["package_file_inventory"][0].__setitem__("sha256", "not-a-digest"),
         "manifest_inventory_invalid"),
        (lambda m: m.__setitem__("whole_package_signed_files", [{}]),
         "payload_path_unsafe"),
    ]
    for mutate, code in mutations:
        manifest = copy.deepcopy(base)
        mutate(manifest)
        with pytest.raises(EnginePackContractError) as error:
            build_payload_inventory(tmp_path, manifest)
        assert error.value.code == code
    collision = copy.deepcopy(base)
    collision["package_file_inventory"].append({"path": "A.bin", "size": 1, "sha256": _sha(b"a")})
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, collision)
    assert error.value.code == "payload_path_collision"
    with pytest.raises(EnginePackContractError) as error:
        build_payload_inventory(tmp_path, [])
    assert error.value.code == "manifest_invalid"


def test_compute_self_digest_rejects_duplicate_and_invalid_types():
    entry = {"path": "a", "size": 1, "sha256": _sha(b"a")}
    for files in ([entry, dict(entry)], [{**entry, "size": True}], [{**entry, "sha256": "bad"}]):
        with pytest.raises(EnginePackContractError) as error:
            compute_self_payload_digest(files)
        assert error.value.code == "manifest_inventory_invalid"


def test_c73_self_uses_actual_roots_locked_inputs_and_provenance(tmp_path):
    receipt = _fixture_self_receipt()
    roots = _self_build_roots(tmp_path, receipt)
    result = validate_c73_receipt(receipt, build_roots=roots)
    assert result == {"variant_id": "self-payload", "validated": True,
                      "evidence": "actual-local-bytes"}
    (roots["locked_inputs"]).write_bytes(b"changed")
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(receipt, build_roots=roots)
    assert error.value.code == "c73_oracle_mismatch"


def test_c73_self_rejects_alias_ghost_reclassification_and_inventory_claims(tmp_path):
    receipt = _fixture_self_receipt()
    roots = _self_build_roots(tmp_path, receipt)
    alias = copy.deepcopy(receipt)
    alias["builder_b_identity"] = "alias-b"
    alias["builder_b_fingerprint"] = alias["builder_a_fingerprint"]
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(alias, build_roots=roots)
    assert error.value.code == "c73_self_builder_invalid"
    ghost = copy.deepcopy(receipt)
    ghost["package_classification"] = {
        "package_file_inventory": ["ghost.bin"],
        "file_classification": {"ghost.bin": "self-built"},
        "whole_package_signed_files": ["ghost.bin"],
    }
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(ghost, build_roots=roots)
    assert error.value.code == "c73_oracle_mismatch"
    reclassified = copy.deepcopy(receipt)
    reclassified["package_classification"]["file_classification"]["runtime/provider.py"] = "third-party-opaque"
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(reclassified, build_roots=roots)
    assert error.value.code == "c73_oracle_mismatch"
    claimed = copy.deepcopy(receipt)
    claimed["oracle_receipt"]["builder_a_inventory"][0]["size"] = 999
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(claimed, build_roots=roots)
    assert error.value.code == "c73_oracle_mismatch"


def test_c73_self_requires_actual_context_and_rejects_duplicate_paths(tmp_path):
    receipt = _fixture_self_receipt()
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(receipt)
    assert error.value.code == "c73_oracle_missing"
    duplicate = copy.deepcopy(receipt)
    duplicate["package_classification"]["package_file_inventory"].append("runtime/provider.py")
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(duplicate, build_roots=_self_build_roots(tmp_path, receipt))
    assert error.value.code == "manifest_classification_invalid"


def test_c73_third_validates_every_opaque_path_without_filename_special_cases(tmp_path):
    opaque = {
        "vendor.bin": (b"vendor", "1", "linux-x86_64"),
        "nested/client.dat": (b"client", "2", "macos-arm64"),
    }
    receipt = _third_receipt(tmp_path, opaque)
    assert validate_c73_receipt(receipt, build_roots={"payload": tmp_path})["validated"] is True
    for path in opaque:
        bad = copy.deepcopy(receipt)
        bad["oracle_receipt"]["opaque_file_hashes"][path] = _digest("other")
        with pytest.raises(EnginePackContractError) as error:
            validate_c73_receipt(bad, build_roots={"payload": tmp_path})
        assert error.value.code == "c73_oracle_mismatch"


def test_c73_third_reuses_manifest_byte_sbom_source_and_license_checks(tmp_path):
    receipt = _third_receipt(tmp_path, {"vendor.bin": (b"actual", "1", "abi")})
    for field, value, code in (
        ("raw_artifact_hash", _digest("claimed"), "third_party_raw_hash_drift"),
        ("upstream_origin", "invented", "third_party_metadata_invalid"),
        ("license_status", "invented-license", "third_party_metadata_invalid"),
    ):
        bad = copy.deepcopy(receipt)
        bad["payload_manifest"]["third_party"]["vendor.bin"][field] = value
        bad["opaque_files"]["vendor.bin"][field] = value
        if field in bad:
            bad[field] = value
        with pytest.raises(EnginePackContractError) as error:
            validate_c73_receipt(bad, build_roots={"payload": tmp_path})
        assert error.value.code == code


def test_c73_incident_requires_actual_rebuild_real_new_key_and_endorsement(tmp_path):
    receipt, roots = _incident(tmp_path)
    assert validate_c73_receipt(receipt, build_roots=roots)["validated"] is True
    tampered = copy.deepcopy(receipt)
    tampered["oracle_receipt"]["new_key_endorsement"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(tampered, build_roots=roots)
    assert error.value.code == "c73_incident_invalid"
    (roots["rebuilt"] / "runtime/provider.py").write_bytes(b"changed")
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(receipt, build_roots=roots)
    assert error.value.code == "c73_incident_invalid"


def test_c73_incident_rejects_key_alias_domain_relabel_and_dirty_builder(tmp_path):
    alias_receipt, alias_roots = _incident(tmp_path / "alias", reuse_key=True)
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(alias_receipt, build_roots=alias_roots)
    assert error.value.code == "c73_incident_invalid"
    receipt, roots = _incident(tmp_path / "domain")
    relabel = copy.deepcopy(receipt)
    relabel["new_signing_key_id"] = "tmengine.production:" + receipt["new_signing_key_id"].split(":")[-1]
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(relabel, build_roots=roots)
    assert error.value.code == "c73_incident_invalid"
    dirty_receipt, dirty_roots = _incident(tmp_path / "dirty", clean=False)
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(dirty_receipt, build_roots=dirty_roots)
    assert error.value.code == "c73_incident_invalid"
    invalid_types = copy.deepcopy(receipt)
    invalid_types["revoked_payload_digests"] = [{}]
    with pytest.raises(EnginePackContractError) as error:
        validate_c73_receipt(invalid_types, build_roots=roots)
    assert error.value.code == "c73_incident_invalid"
