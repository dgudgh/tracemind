"""Bounded, offline .tmengine containers. No code, hooks, or downloads run here.

V1 is a regular-file-only, ZIP_STORED container whose root is the signed
inventory plus the two detached control files. Trust is supplied by the host.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path

from .contract import build_payload_inventory, CONTROL_FILES
from .trust import canonical_manifest_bytes, parse_manifest_json, verify_engine_manifest_signature

APPROVED_ENTRYPOINT_IDS = frozenset({"python.sdk.fixture.v1", "python.sdk.provider.v1"})


def approved_entrypoints(values):
    if (not isinstance(values, tuple) or not values or
            any(not isinstance(value, str) or value not in APPROVED_ENTRYPOINT_IDS for value in values)
            or len(set(values)) != len(values)):
        raise ArchiveError("archive_entrypoint_policy_invalid")
    return values


class ArchiveError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ArchiveLimits:
    archive_bytes: int = 256 * 1024 * 1024
    expanded_bytes: int = 512 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    control_bytes: int = 4 * 1024 * 1024
    entries: int = 4096
    central_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in self.__dict__.values()):
            raise ArchiveError("archive_limits_invalid")


def safe_name(name):
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 1024:
        raise ArchiveError("archive_path_unsafe")
    for part in name.split("/"):
        normalized = unicodedata.normalize("NFKC", part)
        reserved = normalized.split(".", 1)[0].upper()
        if (not part or normalized in {".", ".."} or normalized.endswith((".", " "))
                or any(ord(c) < 32 or c in "/\\:\x7f" for c in normalized)
                or reserved in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", reserved)):
            raise ArchiveError("archive_path_unsafe")
    return name


def _paths(names):
    seen, components = set(), {}
    for name in names:
        safe_name(name)
        if name in seen:
            raise ArchiveError("archive_duplicate_member")
        seen.add(name)
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = unicodedata.normalize("NFKC", prefix).casefold()
            if key in components and components[key] != prefix:
                raise ArchiveError("archive_path_collision")
            components[key] = prefix
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ArchiveError("archive_path_collision")
    for name in seen:
        parts = name.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ArchiveError("archive_path_collision")


def _no_link_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ArchiveError("archive_path_not_absolute")
    for item in (*reversed(path.parents), path):
        try:
            st = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ArchiveError("archive_link_rejected")
    return path


def _regular_open(path):
    path = _no_link_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise ArchiveError("archive_not_regular")
    return os.fdopen(fd, "rb")


def _directory_sync(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _preflight_zip(stream, size, limits):
    if size < 22:
        raise ArchiveError("archive_format_invalid")
    stream.seek(size - 22)
    footer = stream.read(22)
    signature, disk, start_disk, disk_count, count, central_size, offset, comment = struct.unpack("<4s4H2LH", footer)
    if (signature != b"PK\x05\x06" or disk or start_disk or disk_count != count or comment
            or count == 0xffff or offset == 0xffffffff or central_size == 0xffffffff):
        raise ArchiveError("archive_format_invalid")
    if count > limits.entries or central_size > limits.central_bytes:
        raise ArchiveError("archive_limit_exceeded")
    if offset + central_size != size - 22:
        raise ArchiveError("archive_format_invalid")
    stream.seek(0)
    return count, offset


class VerifiedArchive:
    """Valid only inside open_verified_archive; manifest returns a fresh copy."""

    def __init__(self, zf, manifest, signature, archive_sha256, inventory):
        self._zip = zf
        self._manifest_bytes = canonical_manifest_bytes(manifest)
        self._signature_bytes = canonical_manifest_bytes(signature)
        self.archive_sha256 = archive_sha256
        self.package_digest = "sha256:" + hashlib.sha256(self._manifest_bytes).hexdigest()
        self.payload_digest = inventory["self_payload_digest"]

    @property
    def manifest(self):
        return parse_manifest_json(self._manifest_bytes)

    @property
    def signature(self):
        return parse_manifest_json(self._signature_bytes)

    def extract(self, destination):
        destination = _no_link_path(destination)
        destination.mkdir(mode=0o700, exist_ok=False)
        try:
            for info in self._zip.infolist():
                target = destination.joinpath(*info.filename.split("/"))
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as output, self._zip.open(info) as source:
                    shutil.copyfileobj(source, output, 128 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            build_payload_inventory(destination, self.manifest)
            for directory in sorted((x for x in destination.rglob("*") if x.is_dir()), reverse=True):
                _directory_sync(directory)
            _directory_sync(destination)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return destination


@contextmanager
def open_verified_archive(path, *, trusted_keys, expected_namespace, expected_channel,
                          limits=None, expected_archive_sha256=None,
                          entrypoint_ids=("python.sdk.fixture.v1",), trust_guard=None):
    """Pin one regular source FD, snapshot once, authenticate every actual byte."""
    limits = ArchiveLimits() if limits is None else limits
    if not isinstance(limits, ArchiveLimits):
        raise ArchiveError("archive_limits_invalid")
    entrypoint_ids = approved_entrypoints(entrypoint_ids)
    with tempfile.TemporaryFile() as snapshot, ExitStack() as authorization:
        digest, size = hashlib.sha256(), 0
        with _regular_open(path) as source:
            if os.fstat(source.fileno()).st_size > limits.archive_bytes:
                raise ArchiveError("archive_limit_exceeded")
            while block := source.read(128 * 1024):
                size += len(block)
                if size > limits.archive_bytes:
                    raise ArchiveError("archive_limit_exceeded")
                digest.update(block)
                snapshot.write(block)
        archive_sha = "sha256:" + digest.hexdigest()
        if expected_archive_sha256 is not None and archive_sha != expected_archive_sha256:
            raise ArchiveError("archive_digest_mismatch")
        count, central_offset = _preflight_zip(snapshot, size, limits)
        try:
            with zipfile.ZipFile(snapshot) as zf:
                infos = zf.infolist()
                if len(infos) != count or count > limits.entries:
                    raise ArchiveError("archive_format_invalid")
                _paths([x.filename for x in infos])
                total, local_offset = 0, 0
                for info in infos:
                    mode = info.external_attr >> 16
                    if (info.orig_filename != info.filename or info.is_dir() or info.create_system != 3
                            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                            or mode & 0o7000 or info.external_attr & 0x10):
                        raise ArchiveError("archive_not_regular")
                    if (info.compress_type != zipfile.ZIP_STORED or info.flag_bits & ~0x800
                            or info.extra or info.comment or info.compress_size != info.file_size):
                        raise ArchiveError("archive_encoding_unsupported")
                    bound = limits.control_bytes if info.filename in CONTROL_FILES else limits.file_bytes
                    if info.file_size > bound:
                        raise ArchiveError("archive_limit_exceeded")
                    total += info.file_size
                    if total > limits.expanded_bytes:
                        raise ArchiveError("archive_limit_exceeded")
                    if info.header_offset != local_offset:
                        raise ArchiveError("archive_format_invalid")
                    snapshot.seek(local_offset)
                    header = snapshot.read(30)
                    if len(header) != 30 or header[:4] != b"PK\x03\x04":
                        raise ArchiveError("archive_format_invalid")
                    name_length, extra_length = struct.unpack_from("<HH", header, 26)
                    flags, method = struct.unpack_from("<HH", header, 6)
                    crc, compressed, uncompressed = struct.unpack_from("<LLL", header, 14)
                    if ((flags, method, crc, compressed, uncompressed) !=
                            (info.flag_bits, info.compress_type, info.CRC, info.compress_size, info.file_size)):
                        raise ArchiveError("archive_format_invalid")
                    if extra_length:
                        raise ArchiveError("archive_encoding_unsupported")
                    local_offset += 30 + name_length + info.compress_size
                if local_offset != central_offset or not CONTROL_FILES <= {x.filename for x in infos}:
                    raise ArchiveError("archive_format_invalid")
                manifest = parse_manifest_json(zf.read("ENGINE-MANIFEST.json"))
                signature = parse_manifest_json(zf.read("ENGINE-MANIFEST.sig"))
                if trust_guard is not None:
                    if trust_guard.namespace != expected_namespace:
                        raise ArchiveError("archive_trust_domain_mismatch")
                    digest = "sha256:" + hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
                    key = authorization.enter_context(trust_guard.guard_package(manifest, digest))
                    trusted_keys = (key,)
                verify_engine_manifest_signature(manifest, signature, trusted_keys=trusted_keys,
                    expected_namespace=expected_namespace, expected_channel=expected_channel)
                if (type(manifest.get("format")) is not int or manifest["format"] != 1
                        or not isinstance(manifest.get("package_id"), str) or not 1 <= len(manifest["package_id"]) <= 128
                        or not isinstance(manifest.get("version"), str) or len(manifest["version"]) > 128
                        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", manifest["version"]) is None):
                    raise ArchiveError("archive_manifest_identity_invalid")
                if (any(not isinstance(manifest.get(field), str) or not 1 <= len(manifest[field]) <= 128
                        for field in ("os", "cpu", "python_abi", "sdk_version", "rpc_version", "task_schema", "result_schema"))
                        or not isinstance(manifest.get("runtime_digest"), str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["runtime_digest"]) is None):
                    raise ArchiveError("archive_manifest_identity_invalid")
                if manifest.get("entrypoint_id") not in entrypoint_ids:
                    raise ArchiveError("archive_entrypoint_unsupported")
                if any(name in manifest for name in ("hooks", "install_hook", "uninstall_hook", "executable", "command", "module")):
                    raise ArchiveError("archive_hook_forbidden")
                inventory = manifest.get("package_file_inventory")
                if not isinstance(inventory, list) or len(inventory) > limits.entries:
                    raise ArchiveError("archive_inventory_invalid")
                declared = {x.get("path") for x in inventory if isinstance(x, dict) and isinstance(x.get("path"), str)}
                if len(declared) != len(inventory) or declared | CONTROL_FILES != {x.filename for x in infos}:
                    raise ArchiveError("archive_inventory_mismatch")
                private_markers = (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----",  # secret-scan: allow - public detection sentinels, no key material
                                   b"-----BEGIN EC PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----")  # secret-scan: allow - public detection sentinels, no key material
                for info in infos:
                    leaf = info.filename.rsplit("/", 1)[-1].casefold()
                    if leaf in {"id_rsa", "id_ed25519", ".env"} or leaf.startswith(".env."):
                        raise ArchiveError("archive_credentials_forbidden")
                    with zf.open(info) as source:
                        tail = b""
                        while block := source.read(128 * 1024):
                            window = tail + block
                            if any(marker in window for marker in private_markers):
                                raise ArchiveError("archive_credentials_forbidden")
                            tail = window[-64:]
                # A private extraction applies the existing full byte/SBOM/classification oracle.
                with tempfile.TemporaryDirectory(prefix="tmengine-verify-") as temp:
                    stage = Path(temp).resolve() / "payload"
                    probe = VerifiedArchive(zf, manifest, signature, archive_sha, {"self_payload_digest": ""})
                    probe.extract(stage)
                    verified_inventory = build_payload_inventory(stage, manifest)
                yield VerifiedArchive(zf, manifest, signature, archive_sha, verified_inventory)
        except (zipfile.BadZipFile, NotImplementedError, EOFError, UnicodeError, struct.error) as exc:
            raise ArchiveError("archive_format_invalid") from exc


def build_archive(source, manifest, signature, output, *, trusted_keys,
                  expected_namespace, expected_channel, limits=None,
                  entrypoint_ids=("python.sdk.fixture.v1", "python.sdk.provider.v1"), trust_guard=None):
    """Build deterministic bytes from already signed input; never create a key."""
    limits = ArchiveLimits() if limits is None else limits
    if not isinstance(limits, ArchiveLimits):
        raise ArchiveError("archive_limits_invalid")
    entrypoint_ids = approved_entrypoints(entrypoint_ids)
    source, output = _no_link_path(source), _no_link_path(output)
    if output.suffix != ".tmengine" or output.exists():
        raise ArchiveError("archive_output_invalid")
    verify_engine_manifest_signature(manifest, signature, trusted_keys=trusted_keys,
        expected_namespace=expected_namespace, expected_channel=expected_channel)
    files, total, directories = 0, 0, 0
    for base, dirs, names in os.walk(source, followlinks=False):
        directories += len(dirs)
        if directories > limits.entries * 32:
            raise ArchiveError("archive_limit_exceeded")
        for name in names:
            path = Path(base) / name
            st = path.lstat()
            if path.parent == source and name in CONTROL_FILES:
                continue  # the two authenticated control documents are generated below
            bound = limits.file_bytes
            files += 1
            total += st.st_size
            if files + 2 > limits.entries or st.st_size > bound or total > limits.expanded_bytes:
                raise ArchiveError("archive_limit_exceeded")
    inventory = build_payload_inventory(source, manifest)
    names = [x["path"] for x in inventory["inventory"]]
    declared = {x["path"]: x for x in inventory["inventory"]}
    _paths(names + sorted(CONTROL_FILES))
    controls = {"ENGINE-MANIFEST.json": canonical_manifest_bytes(manifest),
                "ENGINE-MANIFEST.sig": canonical_manifest_bytes(signature)}
    if any(len(data) > limits.control_bytes for data in controls.values()):
        raise ArchiveError("archive_limit_exceeded")
    size = 22 + sum(76 + 2 * len(name.encode("utf-8")) + len(data) for name, data in controls.items())
    size += sum(76 + 2 * len(name.encode("utf-8")) + declared[name]["size"] for name in names)
    if size > limits.archive_bytes:
        raise ArchiveError("archive_limit_exceeded")
    fd, temp_name = tempfile.mkstemp(prefix=".tmengine-build-", dir=output.parent)
    temp = Path(temp_name)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as zf:
            for name in sorted(names + list(controls)):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, (stat.S_IFREG | 0o600) << 16
                if name in controls:
                    data = controls[name]
                else:
                    with _regular_open(source / name) as stream:
                        data = stream.read(limits.file_bytes + 1)
                    if len(data) > limits.file_bytes:
                        raise ArchiveError("archive_limit_exceeded")
                    if len(data) != declared[name]["size"] or hashlib.sha256(data).hexdigest() != declared[name]["sha256"]:
                        raise ArchiveError("archive_source_changed")
                zf.writestr(info, data)
        with open_verified_archive(temp, trusted_keys=trusted_keys, expected_namespace=expected_namespace,
                                   expected_channel=expected_channel, limits=limits,
                                   entrypoint_ids=entrypoint_ids, trust_guard=trust_guard) as verified:
            receipt = {"package_digest": verified.package_digest, "archive_sha256": verified.archive_sha256,
                       "payload_digest": verified.payload_digest, "bytes": temp.stat().st_size}
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temp, output)  # exclusive publication; cannot replace an existing destination
        temp.unlink()
        _directory_sync(output.parent)
        return receipt
    finally:
        temp.unlink(missing_ok=True)
