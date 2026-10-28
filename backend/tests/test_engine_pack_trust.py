"""T03 trust-domain and detached Ed25519 signature contract."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_packs.trust import (
    PACKAGE_KIND, PRODUCTION_NAMESPACE, PURPOSE, TEST_NAMESPACE, TrustedEngineKey, TrustError,
    assert_no_cross_domain_key_reuse, canonical_manifest_bytes, key_id_for,
    parse_manifest_json, verify_engine_manifest_signature,
)


def _key(namespace=TEST_NAMESPACE, channel="test", publisher="acme", engines=("dm",)):
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes_raw()
    return private, TrustedEngineKey.create(raw, namespace=namespace, channel=channel,
                                            publisher=publisher, engine_scopes=engines)


def _manifest(key, **updates):
    value = {
        "format": 1, "package_kind": PACKAGE_KIND, "package_id": "pkg-1", "version": "1.0.0",
        "namespace": key.namespace, "channel": key.channel, "purpose": PURPOSE,
        "publisher": key.publisher, "engine_id": key.engine_scopes[0],
        "signing_key_id": key.key_id, "package_file_inventory": [],
        "whole_package_signed_files": [], "file_classification": {},
    }
    value.update(updates)
    return value


def _sign(private, manifest):
    return {"algorithm": "ed25519", "key_id": manifest["signing_key_id"],
            "value": base64.b64encode(private.sign(canonical_manifest_bytes(manifest))).decode()}


def test_key_id_is_recomputed_from_raw_bytes():
    _, key = _key()
    assert key.key_id == key_id_for(TEST_NAMESPACE, key.raw_public_key)
    assert key.key_id.startswith(TEST_NAMESPACE + ":")


def test_valid_test_signature():
    private, key = _key()
    manifest = _manifest(key)
    verified = verify_engine_manifest_signature(manifest, _sign(private, manifest),
                                                trusted_keys=[key], expected_namespace=TEST_NAMESPACE,
                                                expected_channel="test")
    assert verified.engine_id == "dm" and verified.namespace == TEST_NAMESPACE


def test_fixture_only_production_signature_and_reverse_isolation():
    private, key = _key(namespace=PRODUCTION_NAMESPACE, channel="production")
    object.__setattr__(key, "fixture_only", True)
    manifest = _manifest(key)
    verified = verify_engine_manifest_signature(
        manifest, _sign(private, manifest), trusted_keys=[key],
        expected_namespace=PRODUCTION_NAMESPACE, expected_channel="production",
    )
    assert verified.namespace == PRODUCTION_NAMESPACE
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(
            manifest, _sign(private, manifest), trusted_keys=[key],
            expected_namespace=TEST_NAMESPACE, expected_channel="test",
        )
    assert error.value.code == "trust_domain_mismatch"


@pytest.mark.parametrize("field,value,code", [
    ("namespace", PRODUCTION_NAMESPACE, "trust_domain_mismatch"),
    ("channel", "production", "trust_channel_mismatch"),
    ("purpose", "platform.release.signing", "purpose_mismatch"),
    ("package_kind", "platform-release", "package_kind_mismatch"),
    ("publisher", "other", "publisher_scope_mismatch"),
    ("engine_id", "oracle", "engine_scope_mismatch"),
])
def test_manifest_identity_fields_are_enforced(field, value, code):
    private, key = _key()
    manifest = _manifest(key, **{field: value})
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, _sign(private, manifest), trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == code


def test_test_key_cannot_verify_production_domain():
    private, key = _key()
    manifest = _manifest(key)
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, _sign(private, manifest), trusted_keys=[key],
                                          expected_namespace=PRODUCTION_NAMESPACE,
                                          expected_channel="production")
    assert error.value.code == "trust_domain_mismatch"


def test_signature_key_id_must_match_manifest():
    private, key = _key()
    manifest = _manifest(key)
    signature = _sign(private, manifest)
    signature["key_id"] = "other"
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, signature, trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == "key_id_mismatch"


def test_tampered_manifest_or_truncated_signature_rejected():
    private, key = _key()
    manifest = _manifest(key)
    signature = _sign(private, manifest)
    manifest["version"] = "1.0.1"
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, signature, trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == "signature_invalid"
    truncated = dict(signature)
    truncated["value"] = base64.b64encode(b"x").decode()
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(_manifest(key), truncated, trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == "signature_length_invalid"


def test_same_public_key_cross_domain_is_rejected():
    private, test_key = _key()
    prod_key = TrustedEngineKey.create(test_key.raw_public_key, namespace=PRODUCTION_NAMESPACE,
                                       channel="production", publisher="acme", engine_scopes=("dm",))
    with pytest.raises(TrustError) as error:
        assert_no_cross_domain_key_reuse([test_key, prod_key])
    assert error.value.code == "cross_domain_key_reuse"


def test_platform_release_key_does_not_implicitly_authorize_engine_pack():
    private, key = _key(publisher="platform", engines=("platform",))
    manifest = _manifest(key, engine_id="dm")
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, _sign(private, manifest), trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == "engine_scope_mismatch"


def test_direct_dataclass_construction_recomputes_identity():
    private, key = _key()
    with pytest.raises(TrustError) as error:
        TrustedEngineKey(namespace=TEST_NAMESPACE, channel="test", publisher="acme",
                         engine_scopes=("dm",), purposes=(PURPOSE,), public_key=key.public_key,
                         raw_public_key=b"\x00" * 32, key_id=key.key_id)
    assert error.value.code == "trust_key_raw_mismatch"
    with pytest.raises(TrustError) as error:
        TrustedEngineKey(namespace=TEST_NAMESPACE, channel="test", publisher="acme",
                         engine_scopes=("dm",), purposes=(PURPOSE,), public_key=key.public_key,
                         raw_public_key=key.raw_public_key, key_id="fake")
    assert error.value.code == "key_id_mismatch"


def test_mixed_platform_and_engine_purpose_is_rejected():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes_raw()
    with pytest.raises(TrustError) as error:
        TrustedEngineKey.create(raw, namespace=TEST_NAMESPACE, channel="test", publisher="acme",
                                engine_scopes=("dm",), purposes=(PURPOSE, "platform.release.signing"))
    assert error.value.code == "purpose_scope_mismatch"


def test_invalid_policy_container_types_fail_stably():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes_raw()
    with pytest.raises(TrustError) as error:
        TrustedEngineKey.create(raw, namespace=TEST_NAMESPACE, channel="test", publisher="acme",
                                engine_scopes=(["not-a-string"],))
    assert error.value.code == "trust_engine_scope_required"
    with pytest.raises(TrustError) as error:
        key_id_for([], raw)
    assert error.value.code == "trust_namespace_invalid"


def test_strict_json_rejects_duplicate_keys_non_string_keys_and_nan():
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '[]', '{"a":' + '[' * 40 + ']' * 40 + '}'):
        with pytest.raises(TrustError):
            parse_manifest_json(raw)


def test_programmatic_non_json_types_and_bad_roots_fail_stably():
    with pytest.raises(TrustError) as error:
        canonical_manifest_bytes({1: "x"})
    assert error.value.code == "manifest_json_invalid"
    with pytest.raises(TrustError) as error:
        canonical_manifest_bytes({"value": (1, 2)})
    assert error.value.code == "manifest_json_invalid"
    _, key = _key()
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature([], {}, trusted_keys=[key],
                                         expected_namespace=TEST_NAMESPACE,
                                         expected_channel="test")
    assert error.value.code == "manifest_invalid"


def test_unknown_key_and_invalid_base64_have_stable_errors():
    private, key = _key()
    other_private, other_key = _key()
    manifest = _manifest(key)
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, _sign(private, manifest),
                                         trusted_keys=[other_key],
                                         expected_namespace=TEST_NAMESPACE,
                                         expected_channel="test")
    assert error.value.code == "unknown_key"
    signature = _sign(private, manifest)
    signature["value"] = "***not-base64***"
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, signature, trusted_keys=[key],
                                         expected_namespace=TEST_NAMESPACE,
                                         expected_channel="test")
    assert error.value.code == "signature_encoding_invalid"


def test_verification_boundary_rederives_real_key_identity():
    private, key = _key()
    manifest = _manifest(key)
    signature = _sign(private, manifest)
    object.__setattr__(key, "raw_public_key", b"\x00" * 32)
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, signature, trusted_keys=[key],
                                         expected_namespace=TEST_NAMESPACE,
                                         expected_channel="test")
    assert error.value.code == "trust_key_raw_mismatch"


def test_signature_envelope_has_exact_fields():
    private, key = _key()
    manifest = _manifest(key)
    signature = _sign(private, manifest)
    signature["public_key"] = base64.b64encode(key.raw_public_key).decode()
    with pytest.raises(TrustError) as error:
        verify_engine_manifest_signature(manifest, signature, trusted_keys=[key],
                                          expected_namespace=TEST_NAMESPACE, expected_channel="test")
    assert error.value.code == "signature_envelope_invalid"
