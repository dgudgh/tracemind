"""Engine-pack trust domains and manifest signature verification.

This module is intentionally independent of the platform release/patch trust
store.  A caller must explicitly provide the engine trust policy; a manifest
can never make its own public key trusted.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

TEST_NAMESPACE = "tmengine.test"
PRODUCTION_NAMESPACE = "tmengine.production"
NAMESPACE_CHANNELS = {TEST_NAMESPACE: "test", PRODUCTION_NAMESPACE: "production"}
PACKAGE_KIND = "tmengine"
PURPOSE = "tmengine.manifest-signing.v1"
ALGORITHM = "ed25519"
SIGNATURE_BYTES = 64


class TrustError(ValueError):
    """Stable, non-secret trust failure."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class TrustedEngineKey:
    namespace: str
    channel: str
    publisher: str
    engine_scopes: tuple[str, ...]
    purposes: tuple[str, ...]
    public_key: Ed25519PublicKey
    raw_public_key: bytes
    key_id: str
    fixture_only: bool = False
    package_kind: str = PACKAGE_KIND

    def __post_init__(self):
        _validate_trusted_key(self)

    @classmethod
    def create(cls, raw_public_key: bytes, *, namespace: str, channel: str, publisher: str,
               engine_scopes: Iterable[str], purposes: Iterable[str] = (PURPOSE,),
               fixture_only: bool = False) -> "TrustedEngineKey":
        if not isinstance(raw_public_key, (bytes, bytearray)):
            raise TrustError("trust_key_length_invalid")
        raw_public_key = bytes(raw_public_key)
        if len(raw_public_key) != 32:
            raise TrustError("trust_key_length_invalid")
        try:
            engines = tuple(engine_scopes)
            purpose_values = tuple(purposes)
        except TypeError as exc:
            raise TrustError("trust_policy_invalid") from exc
        try:
            public_key = Ed25519PublicKey.from_public_bytes(raw_public_key)
        except (TypeError, ValueError) as exc:
            raise TrustError("trust_key_invalid") from exc
        return cls(
            namespace=namespace,
            channel=channel,
            publisher=publisher,
            engine_scopes=engines,
            purposes=purpose_values,
            public_key=public_key,
            raw_public_key=raw_public_key,
            key_id=key_id_for(namespace, raw_public_key),
            fixture_only=fixture_only,
        )


def _validate_trusted_key(key: TrustedEngineKey) -> bytes:
    """Re-derive key identity at every trust boundary.

    The second check is defensive against stale/deserialized policy objects; it
    is not presented as protection from arbitrary same-process memory writes.
    """
    if not isinstance(key, TrustedEngineKey):
        raise TrustError("trust_policy_invalid")
    try:
        namespace = key.namespace
        channel = key.channel
        package_kind = key.package_kind
        purposes = key.purposes
        publisher = key.publisher
        engine_scopes = key.engine_scopes
        public_key = key.public_key
        supplied_raw = key.raw_public_key
        supplied_key_id = key.key_id
    except AttributeError as exc:
        raise TrustError("trust_policy_invalid") from exc
    if not isinstance(namespace, str) or namespace not in NAMESPACE_CHANNELS:
        raise TrustError("trust_namespace_invalid")
    if channel != NAMESPACE_CHANNELS[namespace]:
        raise TrustError("trust_channel_namespace_mismatch")
    if package_kind != PACKAGE_KIND:
        raise TrustError("package_kind_mismatch")
    if purposes != (PURPOSE,):
        raise TrustError("purpose_scope_mismatch")
    if not isinstance(publisher, str) or not publisher or publisher == "*":
        raise TrustError("trust_publisher_scope_required")
    if (not isinstance(engine_scopes, tuple) or not engine_scopes
            or any(not isinstance(value, str) or not value for value in engine_scopes)
            or "*" in engine_scopes or len(engine_scopes) != len(set(engine_scopes))):
        raise TrustError("trust_engine_scope_required")
    if not isinstance(public_key, Ed25519PublicKey):
        raise TrustError("trust_key_invalid")
    if type(key.fixture_only) is not bool:
        raise TrustError("trust_policy_invalid")
    try:
        actual_raw = public_key.public_bytes_raw()
    except (TypeError, ValueError, AttributeError) as exc:
        raise TrustError("trust_key_invalid") from exc
    if not isinstance(supplied_raw, (bytes, bytearray)) or bytes(supplied_raw) != actual_raw:
        raise TrustError("trust_key_raw_mismatch")
    expected = key_id_for(namespace, actual_raw)
    if supplied_key_id != expected:
        raise TrustError("key_id_mismatch")
    return actual_raw


def key_id_for(namespace: str, raw_public_key: bytes) -> str:
    if not isinstance(namespace, str) or namespace not in NAMESPACE_CHANNELS:
        raise TrustError("trust_namespace_invalid")
    if not isinstance(raw_public_key, (bytes, bytearray)) or len(raw_public_key) != 32:
        raise TrustError("trust_key_length_invalid")
    return f"{namespace}:{hashlib.sha256(bytes(raw_public_key)).hexdigest()}"


def _validate_json(value, depth=0):
    if depth > 32:
        raise TrustError("manifest_json_invalid")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TrustError("manifest_json_invalid")
            _validate_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_json(child, depth + 1)
    elif value is None or type(value) in (str, int, float, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise TrustError("manifest_json_invalid")
    else:
        raise TrustError("manifest_json_invalid")


def parse_manifest_json(data: bytes | str) -> dict:
    """Strict JSON parser: UTF-8, unique string keys, no NaN/Infinity, bounded depth."""
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
    except UnicodeDecodeError as exc:
        raise TrustError("manifest_json_invalid") from exc
    if not isinstance(text, str) or not text.strip():
        raise TrustError("manifest_json_invalid")

    def pairs(values):
        result = {}
        for key, value in values:
            if not isinstance(key, str) or key in result:
                raise TrustError("manifest_json_invalid")
            result[key] = value
        return result

    def no_constant(_value):
        raise TrustError("manifest_json_invalid")

    try:
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=no_constant)
    except TrustError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise TrustError("manifest_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise TrustError("manifest_invalid")
    _validate_json(parsed)
    return parsed


def canonical_manifest_bytes(manifest: dict) -> bytes:
    if not isinstance(manifest, dict):
        raise TrustError("manifest_invalid")
    if "signature" in manifest:
        raise TrustError("manifest_signature_embedded")
    _validate_json(manifest)
    try:
        return json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TrustError("manifest_not_canonicalizable") from exc


@dataclass(frozen=True)
class VerifiedManifest:
    namespace: str
    channel: str
    purpose: str
    publisher: str
    engine_id: str
    key_id: str
    manifest_sha256: str


def _decode_signature(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise TrustError("signature_encoding_invalid")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise TrustError("signature_encoding_invalid") from exc
    if len(raw) != SIGNATURE_BYTES:
        raise TrustError("signature_length_invalid")
    return raw


def verify_engine_manifest_signature(manifest: dict, signature: dict, *,
                                     trusted_keys: Iterable[TrustedEngineKey],
                                     expected_namespace: str, expected_channel: str) -> VerifiedManifest:
    """Verify a detached Ed25519 signature against caller-supplied engine trust keys.

    the manifest's own ``key_id``/publisher/engine fields are never trusted until
    the corresponding public key has passed the explicit policy checks.
    """
    if not isinstance(expected_namespace, str) or expected_namespace not in NAMESPACE_CHANNELS:
        raise TrustError("trust_namespace_invalid")
    if expected_channel != NAMESPACE_CHANNELS[expected_namespace]:
        raise TrustError("trust_channel_namespace_mismatch")
    if not isinstance(manifest, dict):
        raise TrustError("manifest_invalid")
    _validate_json(manifest)
    if not isinstance(signature, dict) or set(signature) != {"algorithm", "key_id", "value"}:
        raise TrustError("signature_envelope_invalid")
    if signature.get("algorithm") != ALGORITHM:
        raise TrustError("signature_algorithm_unsupported")
    decoded_signature = _decode_signature(signature.get("value"))

    if manifest.get("package_kind") != PACKAGE_KIND:
        raise TrustError("package_kind_mismatch")
    if manifest.get("purpose") != PURPOSE:
        raise TrustError("purpose_mismatch")
    if manifest.get("namespace") != expected_namespace:
        raise TrustError("trust_domain_mismatch")
    if manifest.get("channel") != expected_channel:
        raise TrustError("trust_channel_mismatch")
    if not isinstance(manifest.get("publisher"), str) or not manifest["publisher"]:
        raise TrustError("publisher_invalid")
    if not isinstance(manifest.get("engine_id"), str) or not manifest["engine_id"]:
        raise TrustError("engine_id_invalid")

    try:
        keys = list(trusted_keys)
    except TypeError as exc:
        raise TrustError("trust_policy_invalid") from exc
    assert_no_cross_domain_key_reuse(keys)
    if signature.get("key_id") != manifest.get("signing_key_id"):
        raise TrustError("key_id_mismatch")
    for key in keys:
        actual_raw = _validate_trusted_key(key)
        if key.namespace != expected_namespace or key.channel != expected_channel:
            continue
        # A key's identity is derived from raw bytes, never copied from the package.
        if signature.get("key_id") != key.key_id:
            continue
        if manifest.get("signing_key_id") != key.key_id:
            raise TrustError("key_id_mismatch")
        if PURPOSE not in key.purposes:
            raise TrustError("purpose_scope_mismatch")
        if manifest["publisher"] != key.publisher:
            raise TrustError("publisher_scope_mismatch")
        if manifest["engine_id"] not in key.engine_scopes:
            raise TrustError("engine_scope_mismatch")
        try:
            # Verify with the public key whose raw identity was freshly derived
            # above, not with a manifest-supplied or cached public key.
            Ed25519PublicKey.from_public_bytes(actual_raw).verify(
                decoded_signature, canonical_manifest_bytes(manifest)
            )
        except InvalidSignature as exc:
            raise TrustError("signature_invalid") from exc
        return VerifiedManifest(
            namespace=manifest["namespace"], channel=manifest["channel"],
            purpose=manifest["purpose"], publisher=manifest["publisher"],
            engine_id=manifest["engine_id"], key_id=key.key_id,
            manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
        )

    # Distinguish a known key from a key only present under the wrong domain.
    raw_key_id = signature.get("key_id")
    for key in keys:
        if key.key_id == raw_key_id and (key.namespace != expected_namespace or key.channel != expected_channel):
            raise TrustError("trust_domain_mismatch")
        if key.key_id == raw_key_id and manifest.get("publisher") != key.publisher:
            raise TrustError("publisher_scope_mismatch")
        if key.key_id == raw_key_id and manifest.get("engine_id") not in key.engine_scopes:
            raise TrustError("engine_scope_mismatch")
    raise TrustError("unknown_key")


def assert_no_cross_domain_key_reuse(keys: Iterable[TrustedEngineKey]) -> None:
    seen: dict[bytes, str] = {}
    try:
        values = list(keys)
    except TypeError as exc:
        raise TrustError("trust_policy_invalid") from exc
    for key in values:
        actual_raw = _validate_trusted_key(key)
        previous = seen.get(actual_raw)
        if previous is not None and previous != key.namespace:
            raise TrustError("cross_domain_key_reuse")
        seen[actual_raw] = key.namespace
