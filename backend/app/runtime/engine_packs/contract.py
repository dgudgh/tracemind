"""Engine-pack payload inventory and C73 byte-oracle contracts.

The public helpers here only read caller-owned paths. They never extract,
install, execute, or fetch an engine pack. Success means only that supplied
local evidence is internally bound to bytes read by this process.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CONTROL_FILES = frozenset({"ENGINE-MANIFEST.json", "ENGINE-MANIFEST.sig"})
CLASSIFICATIONS = frozenset({"self-built", "third-party-opaque"})
VARIANTS = frozenset({"self-payload", "third-party-integrity", "incident-rebuild"})
THIRD_PARTY_FIELDS = (
    "upstream_origin", "version", "abi", "raw_artifact_hash",
    "packaged_raw_artifact_hash", "sbom_path", "sbom_digest", "license_status",
    "redistribution_status", "upstream_signature_status",
)
ALLOWED_LICENSE_STATUS = frozenset({"allowed", "reviewed-allowed", "redistributable"})
ALLOWED_REDISTRIBUTION_STATUS = frozenset({"allowed", "not-required", "public-domain"})
ALLOWED_UPSTREAM_SIGNATURE_STATUS = frozenset({"recorded-unsigned", "unverified"})
SBOM_SCHEMA = "tmengine.sbom/v1"
BUILD_PROVENANCE_SCHEMA = "tmengine.build-provenance/v1"
INCIDENT_ENDORSEMENT_SCHEMA = "tmengine.incident-rebuild-endorsement.v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^(?:https|vendor|pkg|repository)://[^\s]+$")
_VERSION_OR_ABI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,127}$")
CONTRACT_ERROR_CODES = frozenset({
    "manifest_invalid", "manifest_inventory_invalid", "manifest_signed_files_invalid",
    "manifest_classification_invalid", "manifest_file_missing", "manifest_file_extra",
    "payload_path_unsafe", "payload_path_collision", "payload_not_regular",
    "payload_link_rejected", "payload_hash_mismatch", "payload_size_mismatch",
    "manifest_digest_invalid", "third_party_metadata_missing",
    "third_party_metadata_invalid", "third_party_sbom_invalid",
    "third_party_signature_claim_unproven", "third_party_raw_hash_drift",
    "c73_variant_invalid", "c73_oracle_missing", "c73_oracle_mismatch",
    "c73_self_builder_invalid", "c73_self_payload_digest_mismatch",
    "c73_self_locked_input_missing", "c73_third_party_invalid",
    "c73_incident_invalid",
})


class EnginePackContractError(ValueError):
    """Stable, non-secret engine-pack contract failure."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def _error(code: str, detail: str = "") -> None:
    raise EnginePackContractError(code, detail)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + _sha256(data)


def _digest(value, code: str = "manifest_digest_invalid") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _error(code)
    return value


def _canonical(value, code: str = "manifest_invalid") -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EnginePackContractError(code) from exc


def _validate_json_tree(value, code: str, depth: int = 0) -> None:
    if depth > 32:
        _error(code)
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _error(code)
            _validate_json_tree(child, code, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_json_tree(child, code, depth + 1)
    elif value is None or type(value) in (str, int, float, bool):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            _error(code)
    else:
        _error(code)


def _strict_json_bytes(data: bytes, code: str) -> dict:
    if not isinstance(data, bytes):
        _error(code)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnginePackContractError(code) from exc

    def pairs(values):
        result = {}
        for key, value in values:
            if not isinstance(key, str) or key in result:
                _error(code)
            result[key] = value
        return result

    def no_constant(_value):
        _error(code)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=no_constant)
    except EnginePackContractError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise EnginePackContractError(code) from exc
    if not isinstance(value, dict):
        _error(code)
    _validate_json_tree(value, code)
    return value


def _safe_relative(raw, *, control_allowed: bool = False) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        _error("payload_path_unsafe")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        _error("payload_path_unsafe")
    if len(pure.parts) == 1 and raw in CONTROL_FILES and not control_allowed:
        _error("manifest_inventory_invalid")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        _error("payload_path_unsafe")
    return pure.as_posix()


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def _validated_unique_paths(values, code: str) -> list[str]:
    if not isinstance(values, list):
        _error(code)
    result: list[str] = []
    exact: set[str] = set()
    collisions: dict[str, str] = {}
    for raw in values:
        path = _safe_relative(raw)
        if path in exact:
            _error(code)
        collision = _collision_key(path)
        if collision in collisions and collisions[collision] != path:
            _error("payload_path_collision")
        exact.add(path)
        collisions[collision] = path
        result.append(path)
    return result


def _is_reparse(st: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(st, "st_file_attributes", 0) & marker)


def _lstat_regular(path: Path) -> os.stat_result:
    try:
        st = path.lstat()
    except OSError as exc:
        raise EnginePackContractError("payload_not_regular") from exc
    if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
        _error("payload_link_rejected")
    if not stat.S_ISREG(st.st_mode):
        _error("payload_not_regular")
    if st.st_nlink != 1:
        _error("payload_link_rejected")
    return st


def _read_regular_file(path: Path) -> bytes:
    before = _lstat_regular(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EnginePackContractError("payload_link_rejected") from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            _error("payload_link_rejected")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            _error("payload_not_regular")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_evidence_file(value, code: str) -> bytes:
    if not isinstance(value, (str, os.PathLike, Path)):
        _error(code)
    return _read_regular_file(Path(value))


def _as_path(value, code: str) -> Path:
    try:
        return Path(value)
    except TypeError as exc:
        raise EnginePackContractError(code) from exc


def scan_payload_directory(root: Path) -> list[dict]:
    """Inventory all regular payload files without following links."""
    root = _as_path(root, "payload_path_unsafe")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise EnginePackContractError("payload_path_unsafe") from exc
    if stat.S_ISLNK(root_stat.st_mode) or _is_reparse(root_stat):
        _error("payload_link_rejected")
    if not stat.S_ISDIR(root_stat.st_mode):
        _error("payload_path_unsafe")

    files: list[dict] = []
    seen: dict[str, str] = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        kept_dirs = []
        for name in sorted(dirs):
            candidate = Path(current) / name
            try:
                st = candidate.lstat()
            except OSError as exc:
                raise EnginePackContractError("payload_not_regular") from exc
            if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
                _error("payload_link_rejected")
            if not stat.S_ISDIR(st.st_mode):
                _error("payload_not_regular")
            rel_dir = candidate.relative_to(root).as_posix()
            collision = _collision_key(rel_dir)
            previous = seen.get(collision)
            if previous is not None and previous != rel_dir:
                _error("payload_path_collision")
            seen[collision] = rel_dir
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            full = Path(current) / name
            rel = full.relative_to(root).as_posix()
            if rel in CONTROL_FILES and full.parent == root:
                _lstat_regular(full)
                continue
            rel = _safe_relative(rel)
            collision = _collision_key(rel)
            previous = seen.get(collision)
            if previous is not None and previous != rel:
                _error("payload_path_collision")
            seen[collision] = rel
            data = _read_regular_file(full)
            files.append({"path": rel, "size": len(data), "sha256": _sha256(data)})
    return sorted(files, key=lambda item: item["path"])


def _validate_sbom(root: Path, sbom_path: str, actual: dict,
                   components: dict[str, dict]) -> None:
    if sbom_path in components or sbom_path not in actual:
        _error("third_party_sbom_invalid", sbom_path)
    document = _strict_json_bytes(_read_regular_file(root / sbom_path),
                                  "third_party_sbom_invalid")
    if set(document) != {"schema", "components"} or document.get("schema") != SBOM_SCHEMA:
        _error("third_party_sbom_invalid", sbom_path)
    values = document.get("components")
    if not isinstance(values, list) or not values:
        _error("third_party_sbom_invalid", sbom_path)
    by_path: dict[str, dict] = {}
    required = {"path", "version", "sha256", "license", "redistribution_status"}
    for component in values:
        if not isinstance(component, dict) or set(component) != required:
            _error("third_party_sbom_invalid", sbom_path)
        path = _safe_relative(component.get("path"))
        if path in by_path or path == sbom_path:
            _error("third_party_sbom_invalid", sbom_path)
        if not isinstance(component.get("sha256"), str) or _HEX_DIGEST.fullmatch(component["sha256"]) is None:
            _error("third_party_sbom_invalid", path)
        by_path[path] = component
    for path, metadata in components.items():
        if metadata["sbom_path"] != sbom_path:
            continue
        expected = {
            "path": path, "version": metadata["version"],
            "sha256": actual[path]["sha256"], "license": metadata["license_status"],
            "redistribution_status": metadata["redistribution_status"],
        }
        if by_path.get(path) != expected:
            _error("third_party_sbom_invalid", path)
    expected_paths = {path for path, metadata in components.items()
                      if metadata["sbom_path"] == sbom_path}
    if set(by_path) != expected_paths:
        _error("third_party_sbom_invalid", sbom_path)


def _validate_third_party(root: Path, actual: dict[str, dict], classification: dict[str, str],
                          third) -> list[str]:
    opaque = sorted(path for path, value in classification.items() if value == "third-party-opaque")
    if not isinstance(third, dict) or set(third) != set(opaque):
        _error("third_party_metadata_missing")
    sboms: set[str] = set()
    for path in opaque:
        metadata = third.get(path)
        if not isinstance(metadata, dict) or set(metadata) != set(THIRD_PARTY_FIELDS):
            _error("third_party_metadata_missing", path)
        if any(not isinstance(metadata.get(field), str) or not metadata[field]
               for field in THIRD_PARTY_FIELDS):
            _error("third_party_metadata_missing", path)
        if _SOURCE.fullmatch(metadata["upstream_origin"]) is None:
            _error("third_party_metadata_invalid", f"{path}:origin")
        if (_VERSION_OR_ABI.fullmatch(metadata["version"]) is None
                or _VERSION_OR_ABI.fullmatch(metadata["abi"]) is None):
            _error("third_party_metadata_invalid", f"{path}:version-abi")
        actual_digest = "sha256:" + actual[path]["sha256"]
        for field in ("raw_artifact_hash", "packaged_raw_artifact_hash", "sbom_digest"):
            _digest(metadata[field])
        if (metadata["raw_artifact_hash"] != actual_digest
                or metadata["packaged_raw_artifact_hash"] != actual_digest):
            _error("third_party_raw_hash_drift", path)
        sbom_path = _safe_relative(metadata["sbom_path"])
        if sbom_path == path or sbom_path not in actual or classification.get(sbom_path) != "self-built":
            _error("third_party_sbom_invalid", path)
        if metadata["sbom_digest"] != "sha256:" + actual[sbom_path]["sha256"]:
            _error("third_party_sbom_invalid", path)
        if metadata["license_status"] not in ALLOWED_LICENSE_STATUS:
            _error("third_party_metadata_invalid", f"{path}:license")
        if metadata["redistribution_status"] not in ALLOWED_REDISTRIBUTION_STATUS:
            _error("third_party_metadata_invalid", f"{path}:redistribution")
        signature_status = metadata["upstream_signature_status"]
        if signature_status.startswith("verified") or signature_status not in ALLOWED_UPSTREAM_SIGNATURE_STATUS:
            _error("third_party_signature_claim_unproven", path)
        sboms.add(sbom_path)
    for sbom_path in sboms:
        _validate_sbom(root, sbom_path, actual, third)
    return opaque


def build_payload_inventory(root: Path, manifest: dict) -> dict:
    """Bind a manifest inventory/classification to an actual payload tree."""
    if not isinstance(manifest, dict):
        _error("manifest_invalid")
    raw_inventory = manifest.get("package_file_inventory")
    if not isinstance(raw_inventory, list):
        _error("manifest_inventory_invalid")
    declared: dict[str, dict] = {}
    collision_paths: dict[str, str] = {}
    for item in raw_inventory:
        if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or _HEX_DIGEST.fullmatch(item["sha256"]) is None):
            _error("manifest_inventory_invalid")
        path = _safe_relative(item.get("path"))
        collision = _collision_key(path)
        if path in declared:
            _error("manifest_inventory_invalid")
        if collision in collision_paths and collision_paths[collision] != path:
            _error("payload_path_collision")
        collision_paths[collision] = path
        declared[path] = {"path": path, "size": item["size"], "sha256": item["sha256"]}

    actual = {item["path"]: item for item in scan_payload_directory(root)}
    missing = sorted(set(declared) - set(actual))
    extra = sorted(set(actual) - set(declared))
    if missing:
        _error("manifest_file_missing", ",".join(missing))
    if extra:
        _error("manifest_file_extra", ",".join(extra))
    for path, item in actual.items():
        if declared[path]["size"] != item["size"]:
            _error("payload_size_mismatch", path)
        if declared[path]["sha256"] != item["sha256"]:
            _error("payload_hash_mismatch", path)

    signed = _validated_unique_paths(manifest.get("whole_package_signed_files"),
                                     "manifest_signed_files_invalid")
    if set(signed) != set(actual):
        _error("manifest_signed_files_invalid")
    classification = manifest.get("file_classification")
    if not isinstance(classification, dict):
        _error("manifest_classification_invalid")
    normalized_classification: dict[str, str] = {}
    for raw_path, value in classification.items():
        path = _safe_relative(raw_path)
        if (path in normalized_classification or not isinstance(value, str)
                or value not in CLASSIFICATIONS):
            _error("manifest_classification_invalid", path)
        normalized_classification[path] = value
    if set(normalized_classification) != set(actual):
        _error("manifest_classification_invalid")

    opaque = _validate_third_party(Path(root), actual, normalized_classification,
                                   manifest.get("third_party", {}))
    self_files = [actual[path] for path in sorted(actual)
                  if normalized_classification[path] == "self-built"]
    return {
        "inventory": [actual[path] for path in sorted(actual)],
        "whole_package_signed_files": sorted(actual),
        "self_payload_digest": compute_self_payload_digest(self_files),
        "self_files": self_files,
        "third_party_paths": opaque,
    }


def compute_self_payload_digest(files: list[dict]) -> str:
    """Compute a strict digest from validated self-built inventory entries."""
    if not isinstance(files, list):
        _error("manifest_inventory_invalid")
    normalized: list[dict] = []
    exact: set[str] = set()
    collisions: dict[str, str] = {}
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or _HEX_DIGEST.fullmatch(item["sha256"]) is None):
            _error("manifest_inventory_invalid")
        path = _safe_relative(item.get("path"))
        collision = _collision_key(path)
        if path in exact:
            _error("manifest_inventory_invalid")
        if collision in collisions and collisions[collision] != path:
            _error("payload_path_collision")
        exact.add(path)
        collisions[collision] = path
        normalized.append({"path": path, "size": item["size"], "sha256": item["sha256"]})
    normalized.sort(key=lambda item: item["path"])
    return _sha256_digest(_canonical(normalized, "manifest_inventory_invalid"))


def _validate_classification_block(block) -> dict:
    if not isinstance(block, dict) or set(block) != {
            "package_file_inventory", "file_classification", "whole_package_signed_files"}:
        _error("manifest_classification_invalid")
    inventory = _validated_unique_paths(block.get("package_file_inventory"),
                                        "manifest_classification_invalid")
    signed = _validated_unique_paths(block.get("whole_package_signed_files"),
                                     "manifest_classification_invalid")
    classification = block.get("file_classification")
    if not isinstance(classification, dict):
        _error("manifest_classification_invalid")
    normalized: dict[str, str] = {}
    for raw_path, value in classification.items():
        path = _safe_relative(raw_path)
        if path in normalized or not isinstance(value, str) or value not in CLASSIFICATIONS:
            _error("manifest_classification_invalid")
        normalized[path] = value
    if set(inventory) != set(signed) or set(inventory) != set(normalized):
        _error("manifest_classification_invalid")
    return {"inventory": inventory, "classification": normalized, "signed": signed}


def _inventory_from_oracle(value, code: str) -> list[dict]:
    if not isinstance(value, list):
        _error(code)
    compute_self_payload_digest(value)
    return sorted(value, key=lambda item: item["path"])


def _actual_matches_block(actual: list[dict], block: dict, side: str) -> None:
    paths = {item["path"] for item in actual}
    if paths != set(block["inventory"]) or paths != set(block["signed"]):
        _error("c73_oracle_mismatch", f"{side}_paths")


def _evidence_digest(build_roots: dict, key: str, receipt_value: str, *,
                     missing_code: str = "c73_self_locked_input_missing") -> None:
    value = _read_evidence_file(build_roots.get(key), missing_code)
    if _sha256_digest(value) != receipt_value:
        _error("c73_oracle_mismatch", key)


def _builder_provenance(build_roots: dict, key: str, *, identity: str,
                        builder_fingerprint: str, receipt: dict) -> None:
    document = _strict_json_bytes(
        _read_evidence_file(build_roots.get(key), "c73_self_builder_invalid"),
        "c73_self_builder_invalid",
    )
    expected = {
        "schema": BUILD_PROVENANCE_SCHEMA, "builder_identity": identity,
        "builder_fingerprint": builder_fingerprint, "clean": True,
        "locked_input_digest": receipt["locked_input_digest"],
        "toolchain_digest": receipt["toolchain_digest"],
        "build_recipe_digest": receipt["build_recipe_digest"],
    }
    if document != expected or _DIGEST.fullmatch(builder_fingerprint) is None:
        _error("c73_self_builder_invalid", key)


def _validate_self_receipt(receipt: dict, block: dict, oracle: dict, build_roots: dict) -> None:
    required = (
        "locked_input_digest", "toolchain_digest", "build_recipe_digest",
        "builder_a_identity", "builder_b_identity", "builder_a_fingerprint",
        "builder_b_fingerprint", "builder_a_payload_digest",
        "builder_b_payload_digest", "normalized_payload_digest",
    )
    for field in required:
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            _error("c73_self_locked_input_missing", field)
    for field in (
        "locked_input_digest", "toolchain_digest", "build_recipe_digest",
        "builder_a_fingerprint", "builder_b_fingerprint", "builder_a_payload_digest",
        "builder_b_payload_digest", "normalized_payload_digest",
    ):
        _digest(receipt[field], "c73_self_payload_digest_mismatch")
    if (receipt["builder_a_identity"] == receipt["builder_b_identity"]
            or receipt["builder_a_fingerprint"] == receipt["builder_b_fingerprint"]):
        _error("c73_self_builder_invalid")
    if (receipt["builder_a_payload_digest"] != receipt["builder_b_payload_digest"]
            or receipt["normalized_payload_digest"] != receipt["builder_a_payload_digest"]):
        _error("c73_self_payload_digest_mismatch")

    _evidence_digest(build_roots, "locked_inputs", receipt["locked_input_digest"])
    _evidence_digest(build_roots, "toolchain", receipt["toolchain_digest"])
    _evidence_digest(build_roots, "build_recipe", receipt["build_recipe_digest"])
    _builder_provenance(build_roots, "a_provenance", identity=receipt["builder_a_identity"],
                        builder_fingerprint=receipt["builder_a_fingerprint"], receipt=receipt)
    _builder_provenance(build_roots, "b_provenance", identity=receipt["builder_b_identity"],
                        builder_fingerprint=receipt["builder_b_fingerprint"], receipt=receipt)

    if set(oracle) != {"variant_id", "computed_self_payload_digest",
                       "builder_a_inventory", "builder_b_inventory"}:
        _error("c73_oracle_mismatch")
    observed: dict[str, str] = {}
    for side, digest_field in (("a", "builder_a_payload_digest"),
                               ("b", "builder_b_payload_digest")):
        root = build_roots.get(side)
        if root is None:
            _error("c73_oracle_missing", f"builder_{side}_root")
        actual = scan_payload_directory(root)
        _actual_matches_block(actual, block, f"builder_{side}")
        if any(value == "third-party-opaque" for value in block["classification"].values()):
            manifest = receipt.get("payload_manifest")
            if not isinstance(manifest, dict):
                _error("c73_oracle_mismatch", f"builder_{side}_manifest")
            validated = build_payload_inventory(root, manifest)
            if (validated["inventory"] != actual
                    or manifest.get("file_classification") != block["classification"]
                    or set(validated["whole_package_signed_files"]) != set(block["signed"])):
                _error("c73_oracle_mismatch", f"builder_{side}_manifest")
        claimed = _inventory_from_oracle(oracle.get(f"builder_{side}_inventory"),
                                         "c73_oracle_mismatch")
        if claimed != actual:
            _error("c73_oracle_mismatch", f"builder_{side}_inventory")
        self_files = [item for item in actual
                      if block["classification"][item["path"]] == "self-built"]
        if not self_files:
            _error("c73_oracle_mismatch", f"builder_{side}_self_files")
        computed = compute_self_payload_digest(self_files)
        if computed != receipt[digest_field]:
            _error("c73_self_payload_digest_mismatch", side)
        observed[side] = computed
    if observed["a"] != observed["b"] or oracle.get("computed_self_payload_digest") != observed["a"]:
        _error("c73_oracle_mismatch")


def _validate_third_receipt(receipt: dict, block: dict, oracle: dict, build_roots: dict) -> None:
    if receipt.get("classification") != "third-party-opaque":
        _error("c73_third_party_invalid")
    manifest = receipt.get("payload_manifest")
    if not isinstance(manifest, dict):
        _error("c73_third_party_invalid", "payload_manifest")
    root = build_roots.get("payload")
    if root is None:
        _error("c73_oracle_missing", "payload_root")
    validated = build_payload_inventory(root, manifest)
    inventory_paths = [item["path"] for item in validated["inventory"]]
    if (set(inventory_paths) != set(block["inventory"])
            or set(validated["whole_package_signed_files"]) != set(block["signed"])
            or manifest.get("file_classification") != block["classification"]):
        _error("c73_oracle_mismatch", "payload_manifest")
    opaque = validated["third_party_paths"]
    if not opaque:
        _error("c73_third_party_invalid", "no_opaque_file")
    if set(oracle) != {"variant_id", "whole_package_signed_files", "opaque_file_hashes"}:
        _error("c73_oracle_mismatch")
    if oracle.get("whole_package_signed_files") != block["signed"]:
        _error("c73_oracle_mismatch")
    scanned = {item["path"]: item for item in validated["inventory"]}
    actual_hashes = {path: "sha256:" + scanned[path]["sha256"] for path in opaque}
    if oracle.get("opaque_file_hashes") != actual_hashes:
        _error("c73_oracle_mismatch", "opaque_file_hashes")
    metadata_map = receipt.get("opaque_files")
    if not isinstance(metadata_map, dict) or metadata_map != manifest.get("third_party"):
        _error("c73_third_party_invalid", "opaque_files")
    primary = receipt.get("primary_opaque_path")
    if primary not in opaque:
        _error("c73_third_party_invalid", "primary_opaque_path")
    primary_meta = metadata_map[primary]
    for field in ("upstream_origin", "raw_artifact_hash", "packaged_raw_artifact_hash",
                  "sbom_digest", "license_status", "upstream_signature_status"):
        if receipt.get(field) != primary_meta[field]:
            _error("c73_third_party_invalid", field)


def _raw_public_key(value, code: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        _error(code)
    raw = bytes(value)
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise EnginePackContractError(code) from exc
    return raw


def _key_fingerprint(raw: bytes) -> str:
    return _sha256_digest(raw)


def _key_id(namespace: str, raw: bytes) -> str:
    if namespace not in {"tmengine.test", "tmengine.production"}:
        _error("c73_incident_invalid", "namespace")
    return f"{namespace}:{_sha256(raw)}"


def _decode_signature(value) -> bytes:
    if not isinstance(value, str):
        _error("c73_incident_invalid", "endorsement")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise EnginePackContractError("c73_incident_invalid") from exc
    if len(decoded) != 64:
        _error("c73_incident_invalid", "endorsement")
    return decoded


def _validate_incident_receipt(receipt: dict, block: dict, oracle: dict, build_roots: dict) -> None:
    required_text = (
        "namespace", "compromised_namespace", "clean_builder_identity",
        "clean_builder_fingerprint", "compromised_builder_identity",
        "compromised_builder_fingerprint", "new_signing_key_id",
        "new_signing_key_fingerprint", "compromised_key_fingerprint",
        "rebuilt_payload_digest", "locked_input_digest", "toolchain_digest",
        "build_recipe_digest",
    )
    if any(not isinstance(receipt.get(field), str) or not receipt[field] for field in required_text):
        _error("c73_incident_invalid")
    compromised_ids = receipt.get("compromised_key_ids")
    revoked = receipt.get("revoked_payload_digests")
    if (not isinstance(compromised_ids, list) or not compromised_ids
            or any(not isinstance(value, str) or not value for value in compromised_ids)
            or len(compromised_ids) != len(set(compromised_ids))):
        _error("c73_incident_invalid")
    if (not isinstance(revoked, list) or not revoked
            or any(not isinstance(value, str) for value in revoked)
            or len(revoked) != len(set(revoked))):
        _error("c73_incident_invalid")
    for value in revoked:
        _digest(value, "c73_incident_invalid")
    for field in ("clean_builder_fingerprint", "compromised_builder_fingerprint",
                  "new_signing_key_fingerprint", "compromised_key_fingerprint",
                  "rebuilt_payload_digest", "locked_input_digest", "toolchain_digest",
                  "build_recipe_digest"):
        _digest(receipt[field], "c73_incident_invalid")
    if (receipt["clean_builder_identity"] == receipt["compromised_builder_identity"]
            or receipt["clean_builder_fingerprint"] == receipt["compromised_builder_fingerprint"]):
        _error("c73_incident_invalid", "builder_alias")

    _evidence_digest(build_roots, "locked_inputs", receipt["locked_input_digest"],
                     missing_code="c73_incident_invalid")
    _evidence_digest(build_roots, "toolchain", receipt["toolchain_digest"],
                     missing_code="c73_incident_invalid")
    _evidence_digest(build_roots, "build_recipe", receipt["build_recipe_digest"],
                     missing_code="c73_incident_invalid")

    clean_provenance = _strict_json_bytes(
        _read_evidence_file(build_roots.get("clean_builder_provenance"), "c73_incident_invalid"),
        "c73_incident_invalid",
    )
    if clean_provenance != {
        "schema": BUILD_PROVENANCE_SCHEMA,
        "builder_identity": receipt["clean_builder_identity"],
        "builder_fingerprint": receipt["clean_builder_fingerprint"], "clean": True,
        "locked_input_digest": receipt["locked_input_digest"],
        "toolchain_digest": receipt["toolchain_digest"],
        "build_recipe_digest": receipt["build_recipe_digest"],
    }:
        _error("c73_incident_invalid", "clean_builder_provenance")

    compromised_raw = _raw_public_key(build_roots.get("compromised_public_key"),
                                      "c73_incident_invalid")
    new_raw = _raw_public_key(build_roots.get("new_public_key"), "c73_incident_invalid")
    if compromised_raw == new_raw:
        _error("c73_incident_invalid", "key_alias")
    compromised_fingerprint = _key_fingerprint(compromised_raw)
    new_fingerprint = _key_fingerprint(new_raw)
    compromised_id = _key_id(receipt["compromised_namespace"], compromised_raw)
    new_id = _key_id(receipt["namespace"], new_raw)
    if (receipt["compromised_key_fingerprint"] != compromised_fingerprint
            or receipt["new_signing_key_fingerprint"] != new_fingerprint
            or compromised_id not in compromised_ids
            or receipt["new_signing_key_id"] != new_id
            or new_id in compromised_ids):
        _error("c73_incident_invalid", "key_identity")

    root = build_roots.get("rebuilt")
    if root is None:
        _error("c73_oracle_missing", "rebuilt_root")
    actual = scan_payload_directory(root)
    _actual_matches_block(actual, block, "rebuilt")
    if any(value == "third-party-opaque" for value in block["classification"].values()):
        manifest = receipt.get("payload_manifest")
        if not isinstance(manifest, dict):
            _error("c73_incident_invalid", "payload_manifest")
        validated = build_payload_inventory(root, manifest)
        if (validated["inventory"] != actual
                or manifest.get("file_classification") != block["classification"]
                or set(validated["whole_package_signed_files"]) != set(block["signed"])):
            _error("c73_incident_invalid", "payload_manifest")
    self_files = [item for item in actual
                  if block["classification"][item["path"]] == "self-built"]
    if not self_files:
        _error("c73_incident_invalid", "self_files")
    rebuilt_digest = compute_self_payload_digest(self_files)
    if receipt["rebuilt_payload_digest"] != rebuilt_digest or rebuilt_digest in revoked:
        _error("c73_incident_invalid", "rebuilt_payload")
    if set(oracle) != {"variant_id", "clean_rebuild", "rebuilt_inventory",
                       "new_key_endorsement"} or oracle.get("clean_rebuild") is not True:
        _error("c73_oracle_mismatch")
    if _inventory_from_oracle(oracle.get("rebuilt_inventory"), "c73_oracle_mismatch") != actual:
        _error("c73_oracle_mismatch", "rebuilt_inventory")
    endorsement = {
        "schema": INCIDENT_ENDORSEMENT_SCHEMA, "namespace": receipt["namespace"],
        "rebuilt_payload_digest": rebuilt_digest,
        "clean_builder_fingerprint": receipt["clean_builder_fingerprint"],
        "compromised_key_fingerprint": compromised_fingerprint,
        "new_signing_key_fingerprint": new_fingerprint,
    }
    try:
        Ed25519PublicKey.from_public_bytes(new_raw).verify(
            _decode_signature(oracle.get("new_key_endorsement")), _canonical(endorsement)
        )
    except InvalidSignature as exc:
        raise EnginePackContractError("c73_incident_invalid", "endorsement") from exc


def validate_c73_receipt(receipt: dict, *, build_roots: dict | None = None) -> dict:
    """Validate a C73 receipt against actual caller-controlled evidence paths."""
    if not isinstance(receipt, dict) or receipt.get("variant_id") not in VARIANTS:
        _error("c73_variant_invalid")
    block = _validate_classification_block(receipt.get("package_classification"))
    oracle = receipt.get("oracle_receipt")
    if not isinstance(oracle, dict) or not oracle:
        _error("c73_oracle_missing")
    if not isinstance(build_roots, dict) or not build_roots:
        _error("c73_oracle_missing", "build_roots")
    if oracle.get("variant_id") != receipt["variant_id"]:
        _error("c73_oracle_mismatch")
    if receipt["variant_id"] == "self-payload":
        _validate_self_receipt(receipt, block, oracle, build_roots)
    elif receipt["variant_id"] == "third-party-integrity":
        _validate_third_receipt(receipt, block, oracle, build_roots)
    else:
        _validate_incident_receipt(receipt, block, oracle, build_roots)
    return {"variant_id": receipt["variant_id"], "validated": True,
            "evidence": "actual-local-bytes"}


def validate_payload_manifest(root: Path, manifest: dict) -> dict:
    return build_payload_inventory(root, manifest)
