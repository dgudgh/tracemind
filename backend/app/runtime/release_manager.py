from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.runtime import config_merge
from app.runtime.release_checkpoint import (
    CheckpointError, ReleaseCheckpoint, sync_directory, sync_tree,
)


MANIFEST_FILENAME = "tracemind-release.json"
PATCH_MANIFEST_FILENAME = "tracemind-patch.json"
STATE_FILENAME = "release-state.json"
ACTIVE_FILENAME = "active-release.json"
LOCK_FILENAME = ".release.lock"
MANIFEST_FORMAT_VERSION = 1
PATCH_FORMAT_VERSION = 1
UPDATER_PROTOCOL_VERSION = 1
STRICT_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
IMAGE_DIGEST_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*@sha256:[0-9a-f]{64}$"
)
CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ReleaseError(RuntimeError):
    """Base exception for release lifecycle failures."""


class ManifestError(ReleaseError):
    """Raised when a release manifest or payload cannot be trusted."""


class PreflightError(ReleaseError):
    """Raised before installation when the target is not compatible."""


class ReleaseLockError(ReleaseError):
    """Raised when another release operation owns the installation lock."""


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    detail: str = ""


@dataclass(frozen=True)
class ReleaseResult:
    action: str
    status: str
    release_id: str
    version: str
    previous_release_id: str | None
    backup_id: str | None
    detail: str
    package_type: str = "release"
    package_id: str | None = None
    deployment_type: str = "native"
    target_ref: str = ""
    schema_revision: str = ""
    health_status: str = "not_checked"


HealthCheck = Callable[[Path], HealthResult | bool]
MigrationStep = Callable[[Path], None]
BackupStep = Callable[[Path], None]
RestoreStep = Callable[[Path], None]
ActivationStep = Callable[[Path], None]
ContainerCommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], int],
    subprocess.CompletedProcess[str],
]


def _acquire_file_lock(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _check_health(health_check: HealthCheck | None, release_path: Path) -> HealthResult | None:
    if health_check is None:
        return None
    health = health_check(release_path)
    if isinstance(health, bool):
        health = HealthResult(health)
    if not isinstance(health, HealthResult) or not isinstance(health.healthy, bool):
        raise ReleaseError("health check returned an invalid result")
    if not health.healthy:
        raise ReleaseError(health.detail or "release health check failed")
    return health


@dataclass(frozen=True)
class DeploymentTarget:
    driver: str
    target_ref: str
    runtime: str = ""
    image_digest: str = ""
    compose_file: str = ""
    service: str = ""
    project: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "driver": self.driver,
                "target_ref": self.target_ref,
                "runtime": self.runtime,
                "image_digest": self.image_digest,
                "compose_file": self.compose_file,
                "service": self.service,
                "project": self.project,
            }.items()
            if value
        }

    @classmethod
    def from_dict(cls, value: object, *, fallback_path: Path) -> "DeploymentTarget":
        if not isinstance(value, dict):
            return cls("native", str(fallback_path))
        driver = str(value.get("driver") or "native")
        return cls(
            driver=driver,
            target_ref=str(value.get("target_ref") or fallback_path),
            runtime=str(value.get("runtime") or ""),
            image_digest=str(value.get("image_digest") or ""),
            compose_file=str(value.get("compose_file") or ""),
            service=str(value.get("service") or ""),
            project=str(value.get("project") or ""),
        )


class DeploymentDriver(Protocol):
    name: str

    def preflight(self, package_dir: Path, manifest: dict[str, object]) -> None: ...

    def stage(self, release_dir: Path, manifest: dict[str, object]) -> DeploymentTarget: ...

    def activate(
        self,
        release_dir: Path,
        manifest: dict[str, object],
        target: DeploymentTarget,
    ) -> None: ...

    def rollback(
        self,
        release_dir: Path,
        manifest: dict[str, object],
        target: DeploymentTarget,
    ) -> None: ...


class NativeDeploymentDriver:
    name = "native"

    def __init__(self, activation: ActivationStep | None = None) -> None:
        self.activation = activation

    def preflight(self, _package_dir: Path, manifest: dict[str, object]) -> None:
        if _manifest_deployment_type(manifest) != self.name:
            raise PreflightError("native deployment driver cannot activate a container release")

    def stage(self, release_dir: Path, _manifest: dict[str, object]) -> DeploymentTarget:
        return DeploymentTarget(self.name, str(release_dir))

    def activate(
        self,
        release_dir: Path,
        _manifest: dict[str, object],
        _target: DeploymentTarget,
    ) -> None:
        if self.activation is not None:
            self.activation(release_dir)

    def rollback(
        self,
        release_dir: Path,
        manifest: dict[str, object],
        target: DeploymentTarget,
    ) -> None:
        self.activate(release_dir, manifest, target)


def _default_container_command_runner(
    arguments: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        env=dict(environment),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


class ContainerDeploymentDriver:
    name = "container"

    def __init__(
        self,
        *,
        command_runner: ContainerCommandRunner | None = None,
        activation: ActivationStep | None = None,
        command_timeout_seconds: int = 600,
    ) -> None:
        self.command_runner = command_runner or _default_container_command_runner
        self.activation_hook = activation
        self.command_timeout_seconds = command_timeout_seconds
        self._uses_default_runner = command_runner is None

    def _metadata(self, manifest: dict[str, object]) -> dict[str, object]:
        deployment = manifest.get("deployment")
        if not isinstance(deployment, dict) or deployment.get("type") != self.name:
            raise PreflightError("container release is missing signed deployment metadata")
        return deployment

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        completed = self.command_runner(
            arguments,
            cwd,
            environment,
            self.command_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "container command failed").strip()
            raise ReleaseError(f"container command failed ({' '.join(arguments[:3])}): {detail[-2000:]}")
        return completed

    def preflight(self, package_dir: Path, manifest: dict[str, object]) -> None:
        deployment = self._metadata(manifest)
        runtime = str(deployment.get("runtime") or "")
        if runtime not in {"docker", "podman"}:
            raise PreflightError(f"unsupported container runtime: {runtime or 'missing'}")
        if self._uses_default_runner and shutil.which(runtime) is None:
            raise PreflightError(f"container runtime is not installed or not on PATH: {runtime}")
        compose_relative = _validate_relative_path(str(deployment.get("compose_file") or ""))
        compose_path = package_dir / compose_relative
        if not compose_path.is_file() or compose_path.is_symlink():
            raise PreflightError(f"container compose file is missing or unsafe: {compose_relative}")
        compose_text = compose_path.read_text(encoding="utf-8", errors="replace")
        if "TRACEMIND_IMAGE" not in compose_text:
            raise PreflightError("container compose file must reference the signed TRACEMIND_IMAGE value")
        archive_value = str(deployment.get("image_archive") or "")
        if archive_value:
            archive_relative = _validate_relative_path(archive_value)
            archive_path = package_dir / archive_relative
            if not archive_path.is_file() or archive_path.is_symlink():
                raise PreflightError(f"OCI image archive is missing or unsafe: {archive_relative}")

    def _target(self, release_dir: Path, manifest: dict[str, object]) -> DeploymentTarget:
        deployment = self._metadata(manifest)
        image_digest = str(deployment.get("image") or "")
        return DeploymentTarget(
            driver=self.name,
            target_ref=image_digest,
            runtime=str(deployment.get("runtime") or ""),
            image_digest=image_digest,
            compose_file=str(deployment.get("compose_file") or ""),
            service=str(deployment.get("service") or ""),
            project=str(deployment.get("project") or ""),
        )

    def stage(self, release_dir: Path, manifest: dict[str, object]) -> DeploymentTarget:
        target = self._target(release_dir, manifest)
        deployment = self._metadata(manifest)
        environment = os.environ.copy()
        environment.update(
            {
                "TRACEMIND_IMAGE": target.image_digest,
                "TRACEMIND_RELEASE_ID": str(manifest.get("release_id") or ""),
                "TRACEMIND_SCHEMA_REVISION": str(manifest.get("schema_revision") or ""),
            }
        )
        compose_path = release_dir / _validate_relative_path(target.compose_file)
        self._run(
            [
                target.runtime,
                "compose",
                "--project-name",
                target.project,
                "--file",
                str(compose_path),
                "config",
            ],
            cwd=release_dir,
            environment=environment,
        )
        archive_value = str(deployment.get("image_archive") or "")
        if archive_value:
            archive = release_dir / _validate_relative_path(archive_value)
            self._run(
                [target.runtime, "load", "--input", str(archive)],
                cwd=release_dir,
                environment=environment,
            )
        self._run(
            [target.runtime, "image", "inspect", target.image_digest],
            cwd=release_dir,
            environment=environment,
        )
        return target

    def activate(
        self,
        release_dir: Path,
        manifest: dict[str, object],
        target: DeploymentTarget,
    ) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "TRACEMIND_IMAGE": target.image_digest,
                "TRACEMIND_RELEASE_ID": str(manifest.get("release_id") or ""),
                "TRACEMIND_SCHEMA_REVISION": str(manifest.get("schema_revision") or ""),
            }
        )
        compose_path = release_dir / _validate_relative_path(target.compose_file)
        command = [
            target.runtime,
            "compose",
            "--project-name",
            target.project,
            "--file",
            str(compose_path),
            "up",
            "--detach",
            "--no-build",
            "--remove-orphans",
            target.service,
        ]
        self._run(command, cwd=release_dir, environment=environment)
        if self.activation_hook is not None:
            self.activation_hook(release_dir)

    def rollback(
        self,
        release_dir: Path,
        manifest: dict[str, object],
        target: DeploymentTarget,
    ) -> None:
        self.activate(release_dir, manifest, target)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        sync_directory(path.parent)
    except OSError as exc:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise CheckpointError("release metadata durability failed; inspect before recovery") from exc
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_key_bytes(value: str | Path | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    try:
        return Path(value).expanduser().read_bytes()
    except OSError as exc:
        raise ManifestError(f"signing key is unreadable: {exc}") from exc


def _load_private_key(value: str | Path | bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(_read_key_bytes(value), password=None)
    except (TypeError, ValueError) as exc:
        raise ManifestError("release signing private key is invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestError("release signing key must be Ed25519")
    return key


def _load_public_key(value: str | Path | bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(_read_key_bytes(value))
    except (TypeError, ValueError) as exc:
        raise ManifestError("trusted release public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ManifestError("trusted release key must be Ed25519")
    return key


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_release_signing_keypair(
    private_key_path: str | Path,
    public_key_path: str | Path,
) -> dict[str, str]:
    private_path = Path(private_key_path).expanduser()
    public_path = Path(public_key_path).expanduser()
    if private_path.exists() or public_path.exists():
        raise ManifestError("refusing to overwrite an existing release signing key")
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _atomic_write_bytes(private_path, private_pem, 0o600)
    _atomic_write_bytes(public_path, public_pem, 0o644)
    return {
        "algorithm": "ed25519",
        "key_id": _public_key_id(private_key.public_key()),
        "private_key": str(private_path.resolve()),
        "public_key": str(public_path.resolve()),
    }


def _unsigned_document(document: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != "signature"}


def _sign_document(document: dict[str, object], private_key_value: str | Path | bytes) -> dict[str, object]:
    private_key = _load_private_key(private_key_value)
    signed = dict(_unsigned_document(document))
    signature = private_key.sign(_json_bytes(signed))
    signed["signature"] = {
        "algorithm": "ed25519",
        "key_id": _public_key_id(private_key.public_key()),
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def _verify_document_signature(
    document: dict[str, object],
    trusted_public_keys: Sequence[str | Path | bytes],
) -> str:
    signature = document.get("signature")
    if not isinstance(signature, dict) or signature.get("algorithm") != "ed25519":
        raise ManifestError("signed release document is missing an Ed25519 signature")
    key_id = str(signature.get("key_id") or "")
    try:
        signature_bytes = base64.b64decode(str(signature.get("value") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ManifestError("release signature encoding is invalid") from exc
    if len(signature_bytes) != 64:
        raise ManifestError("release signature length is invalid")
    if not trusted_public_keys:
        raise ManifestError("at least one trusted release public key is required")
    payload = _json_bytes(_unsigned_document(document))
    matching_key_seen = False
    for raw_key in trusted_public_keys:
        key = _load_public_key(raw_key)
        if _public_key_id(key) != key_id:
            continue
        matching_key_seen = True
        try:
            key.verify(signature_bytes, payload)
            return key_id
        except InvalidSignature as exc:
            raise ManifestError("release signature verification failed") from exc
    if matching_key_seen:
        raise ManifestError("release signature verification failed")
    raise ManifestError(f"release signature key is not trusted: {key_id or 'missing'}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_relative_path(raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ManifestError(f"unsafe manifest path: {raw_path!r}")
    return relative


def _validate_symlink(root: Path, link_path: Path, target: str) -> None:
    target_path = Path(target)
    if target_path.is_absolute():
        raise ManifestError(f"absolute symlink target is forbidden: {link_path}")
    resolved = (link_path.parent / target_path).resolve(strict=False)
    if not _inside(root.resolve(), resolved):
        raise ManifestError(f"symlink escapes release root: {link_path} -> {target}")


def _payload_entries(source_dir: Path, *, require_nonempty: bool = True) -> list[dict[str, object]]:
    source_root = source_dir.resolve(strict=True)
    entries: list[dict[str, object]] = []
    for path in sorted(source_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_dir).as_posix()
        if relative == MANIFEST_FILENAME:
            continue
        if path.is_symlink():
            target = os.readlink(path)
            _validate_symlink(source_root, path, target)
            entries.append({"path": relative, "type": "symlink", "target": target})
        elif path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "mode": mode,
                }
            )
    if require_nonempty and not entries:
        raise ManifestError("release payload is empty")
    return entries


def _manifest_deployment_type(manifest: Mapping[str, object]) -> str:
    deployment = manifest.get("deployment")
    if deployment is None:
        return "native"
    if not isinstance(deployment, dict):
        raise ManifestError("release deployment metadata must be a JSON object")
    deployment_type = str(deployment.get("type") or "")
    if deployment_type not in {"native", "container"}:
        raise ManifestError(f"unsupported release deployment type: {deployment_type or 'missing'}")
    return deployment_type


def _validate_container_metadata(root: Path, deployment: Mapping[str, object]) -> None:
    image = str(deployment.get("image") or "")
    if not IMAGE_DIGEST_PATTERN.fullmatch(image):
        raise ManifestError("container image must be pinned as repository@sha256:<64 lowercase hex>")
    runtime = str(deployment.get("runtime") or "")
    if runtime not in {"docker", "podman"}:
        raise ManifestError(f"unsupported container runtime: {runtime or 'missing'}")
    for field in ("service", "project"):
        value = str(deployment.get(field) or "")
        if not CONTAINER_NAME_PATTERN.fullmatch(value):
            raise ManifestError(f"container {field} is missing or unsafe")
    for field, required in (("compose_file", True), ("image_archive", False)):
        raw_path = str(deployment.get(field) or "")
        if not raw_path and not required:
            continue
        relative = _validate_relative_path(raw_path)
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ManifestError(f"container {field} is missing or unsafe: {raw_path}")


def _validate_deployment_metadata(root: Path, manifest: Mapping[str, object]) -> None:
    deployment_type = _manifest_deployment_type(manifest)
    if deployment_type == "native":
        return
    deployment = manifest.get("deployment")
    assert isinstance(deployment, dict)
    _validate_container_metadata(root, deployment)


def build_release_manifest(
    source_dir: str | Path,
    *,
    version: str,
    product: str = "TraceMind AI",
    target_platform: str = "any",
    target_arch: str = "any",
    schema_revision: str = "",
    entrypoint: str = "",
    deployment_type: str = "native",
    container_runtime: str = "docker",
    container_image: str = "",
    container_image_archive: str = "",
    container_compose_file: str = "",
    container_service: str = "tracemind",
    container_project: str = "tracemind",
    signing_private_key: str | Path | bytes | None = None,
    output_path: str | Path | None = None,
) -> dict[str, object]:
    root = Path(source_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ManifestError(f"release source is not a directory: {root}")
    normalized_version = version.strip()
    if not normalized_version or any(character in normalized_version for character in "/\\\x00"):
        raise ManifestError("version must be a non-empty filesystem-safe value")
    entries = _payload_entries(root)
    identity = {
        "product": product.strip() or "TraceMind AI",
        "version": normalized_version,
        "platform": target_platform.strip().lower() or "any",
        "arch": target_arch.strip().lower() or "any",
        "schema_revision": schema_revision.strip(),
        "entrypoint": entrypoint.strip(),
        "files": entries,
    }
    normalized_deployment_type = deployment_type.strip().lower() or "native"
    if normalized_deployment_type == "container":
        identity["deployment"] = {
            "type": "container",
            "runtime": container_runtime.strip().lower(),
            "image": container_image.strip(),
            "image_archive": container_image_archive.strip(),
            "compose_file": container_compose_file.strip(),
            "service": container_service.strip(),
            "project": container_project.strip(),
        }
    elif normalized_deployment_type != "native":
        raise ManifestError(f"unsupported release deployment type: {normalized_deployment_type}")
    _validate_deployment_metadata(root, identity)
    release_id = f"{normalized_version}-{hashlib.sha256(_json_bytes(identity)).hexdigest()[:16]}"
    manifest: dict[str, object] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "release_id": release_id,
        "created_at": _utc_now(),
        **identity,
    }
    if signing_private_key is not None:
        manifest = _sign_document(manifest, signing_private_key)
    target = Path(output_path) if output_path else root / MANIFEST_FILENAME
    _atomic_write_json(target, manifest)
    return manifest


def load_release_manifest(source_dir: str | Path) -> dict[str, object]:
    root = Path(source_dir).expanduser().resolve(strict=True)
    path = root / MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"release manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"release manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("release manifest must be a JSON object")
    return manifest


def _release_identity(manifest: dict[str, object]) -> dict[str, object]:
    identity = {
        "product": manifest.get("product"),
        "version": manifest.get("version"),
        "platform": manifest.get("platform"),
        "arch": manifest.get("arch"),
        "schema_revision": manifest.get("schema_revision", ""),
        "entrypoint": manifest.get("entrypoint", ""),
        "files": manifest.get("files"),
    }
    if "deployment" in manifest:
        identity["deployment"] = manifest.get("deployment")
    return identity


def _verify_release_identity(manifest: dict[str, object]) -> None:
    identity = _release_identity(manifest)
    expected_release_id = f"{manifest.get('version')}-{hashlib.sha256(_json_bytes(identity)).hexdigest()[:16]}"
    if manifest.get("release_id") != expected_release_id:
        raise ManifestError("release identity checksum mismatch")


def verify_release(
    source_dir: str | Path,
    *,
    trusted_public_keys: Sequence[str | Path | bytes] = (),
    require_signature: bool = False,
) -> dict[str, object]:
    root = Path(source_dir).expanduser().resolve(strict=True)
    manifest = load_release_manifest(root)
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise ManifestError("unsupported release manifest format")
    if trusted_public_keys:
        _verify_document_signature(manifest, trusted_public_keys)
    elif require_signature:
        raise ManifestError("release signature verification requires a trusted public key")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("release manifest has no files")

    expected_paths: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise ManifestError("release manifest contains a non-object file entry")
        raw_path = str(raw_entry.get("path") or "")
        relative = _validate_relative_path(raw_path)
        if raw_path in expected_paths:
            raise ManifestError(f"duplicate manifest path: {raw_path}")
        expected_paths.add(raw_path)
        path = root / relative
        entry_type = raw_entry.get("type")
        if entry_type == "file":
            if not path.is_file() or path.is_symlink():
                raise ManifestError(f"release file missing or has wrong type: {raw_path}")
            if path.stat().st_size != raw_entry.get("size"):
                raise ManifestError(f"release file size mismatch: {raw_path}")
            if _sha256_file(path) != raw_entry.get("sha256"):
                raise ManifestError(f"release file checksum mismatch: {raw_path}")
            if stat.S_IMODE(path.stat().st_mode) != raw_entry.get("mode"):
                raise ManifestError(f"release file mode mismatch: {raw_path}")
        elif entry_type == "symlink":
            if not path.is_symlink():
                raise ManifestError(f"release symlink missing: {raw_path}")
            target = os.readlink(path)
            if target != raw_entry.get("target"):
                raise ManifestError(f"release symlink target mismatch: {raw_path}")
            _validate_symlink(root, path, target)
        else:
            raise ManifestError(f"unsupported release entry type: {entry_type!r}")

    actual_paths = {entry["path"] for entry in _payload_entries(root)}
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise ManifestError(f"release payload differs from manifest; missing={missing}, extra={extra}")

    _validate_deployment_metadata(root, manifest)
    _verify_release_identity(manifest)
    return manifest


def _strict_version(value: object) -> tuple[int, int, int]:
    match = STRICT_VERSION_PATTERN.fullmatch(str(value or ""))
    if not match:
        raise ManifestError(f"patch versions must use MAJOR.MINOR.PATCH: {value!r}")
    return tuple(int(part) for part in match.groups())


def _manifest_entry_map(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("release manifest has no files")
    mapped: dict[str, dict[str, object]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise ManifestError("release manifest contains a non-object file entry")
        path = str(raw_entry.get("path") or "")
        _validate_relative_path(path)
        if path in mapped:
            raise ManifestError(f"duplicate manifest path: {path}")
        mapped[path] = dict(raw_entry)
    return mapped


def _patch_identity(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "product": manifest.get("product"),
        "base_release_id": manifest.get("base_release_id"),
        "base_version": manifest.get("base_version"),
        "target_release_id": manifest.get("target_release_id"),
        "target_version": manifest.get("target_version"),
        "updater_protocol": manifest.get("updater_protocol"),
        "schema": manifest.get("schema"),
        "restart_required": manifest.get("restart_required"),
        "release_notes": manifest.get("release_notes"),
        "operations": manifest.get("operations"),
        "target_release_manifest": manifest.get("target_release_manifest"),
    }


def load_patch_manifest(package_dir: str | Path) -> dict[str, object]:
    root = Path(package_dir).expanduser().resolve(strict=True)
    path = root / PATCH_MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"patch manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"patch manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("patch manifest must be a JSON object")
    return manifest


def build_patch_package(
    base_release_dir: str | Path,
    target_release_dir: str | Path,
    output_dir: str | Path,
    *,
    signing_private_key: str | Path | bytes,
    release_notes: str = "",
    restart_required: bool = True,
    allow_downgrade: bool = False,
) -> dict[str, object]:
    base_root = Path(base_release_dir).expanduser().resolve(strict=True)
    target_root = Path(target_release_dir).expanduser().resolve(strict=True)
    base_manifest = verify_release(base_root)
    target_manifest = verify_release(target_root)
    if base_manifest.get("product") != target_manifest.get("product"):
        raise ManifestError("base and target releases belong to different products")
    base_version = _strict_version(base_manifest.get("version"))
    target_version = _strict_version(target_manifest.get("version"))
    if not allow_downgrade and target_version <= base_version:
        raise ManifestError("patch target version must be newer than the base version")

    output = Path(output_dir).expanduser().resolve()
    if _inside(base_root, output) or _inside(target_root, output):
        raise ManifestError("patch output must be outside the base and target release directories")
    if output.exists() and any(output.iterdir()):
        raise ManifestError(f"patch output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    payload_root = output / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    base_entries = _manifest_entry_map(base_manifest)
    target_entries = _manifest_entry_map(target_manifest)
    operations: list[dict[str, object]] = []
    for path in sorted(base_entries.keys() - target_entries.keys()):
        operations.append({"op": "delete", "path": path})
    for path in sorted(target_entries):
        target_entry = target_entries[path]
        if base_entries.get(path) == target_entry:
            continue
        source = target_root / _validate_relative_path(path)
        destination = payload_root / _validate_relative_path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if target_entry.get("type") == "file":
            if not source.is_file() or source.is_symlink():
                raise ManifestError(f"target patch file is missing: {path}")
            shutil.copy2(source, destination)
        elif target_entry.get("type") == "symlink":
            if not source.is_symlink():
                raise ManifestError(f"target patch symlink is missing: {path}")
            target = os.readlink(source)
            _validate_symlink(payload_root, destination, target)
            destination.symlink_to(target)
        else:
            raise ManifestError(f"unsupported target release entry type: {target_entry.get('type')!r}")
        operations.append({"op": "upsert", "path": path, "entry": target_entry})
    if not operations:
        raise ManifestError("base and target releases are identical; no patch is required")

    signed_target_manifest = _sign_document(target_manifest, signing_private_key)
    schema_from = str(base_manifest.get("schema_revision") or "")
    schema_to = str(target_manifest.get("schema_revision") or "")
    identity: dict[str, object] = {
        "product": target_manifest.get("product"),
        "base_release_id": base_manifest.get("release_id"),
        "base_version": base_manifest.get("version"),
        "target_release_id": target_manifest.get("release_id"),
        "target_version": target_manifest.get("version"),
        "updater_protocol": UPDATER_PROTOCOL_VERSION,
        "schema": {
            "from": schema_from,
            "to": schema_to,
            "migration_required": schema_from != schema_to,
        },
        "restart_required": bool(restart_required),
        "release_notes": release_notes.strip(),
        "operations": operations,
        "target_release_manifest": signed_target_manifest,
    }
    patch_id = (
        f"{base_manifest.get('version')}-to-{target_manifest.get('version')}-"
        f"{hashlib.sha256(_json_bytes(identity)).hexdigest()[:16]}"
    )
    manifest = _sign_document(
        {
            "format_version": PATCH_FORMAT_VERSION,
            "patch_id": patch_id,
            "created_at": _utc_now(),
            **identity,
        },
        signing_private_key,
    )
    _atomic_write_json(output / PATCH_MANIFEST_FILENAME, manifest)
    private_key = _load_private_key(signing_private_key)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return verify_patch_package(output, trusted_public_keys=[public_pem])


def verify_patch_package(
    package_dir: str | Path,
    *,
    trusted_public_keys: Sequence[str | Path | bytes],
) -> dict[str, object]:
    root = Path(package_dir).expanduser().resolve(strict=True)
    manifest = load_patch_manifest(root)
    if manifest.get("format_version") != PATCH_FORMAT_VERSION:
        raise ManifestError("unsupported patch manifest format")
    if manifest.get("updater_protocol") != UPDATER_PROTOCOL_VERSION:
        raise ManifestError("patch requires an unsupported updater protocol")
    _verify_document_signature(manifest, trusted_public_keys)
    target_manifest = manifest.get("target_release_manifest")
    if not isinstance(target_manifest, dict):
        raise ManifestError("patch does not contain a target release manifest")
    if target_manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise ManifestError("patch target uses an unsupported release manifest format")
    _verify_document_signature(target_manifest, trusted_public_keys)
    _verify_release_identity(target_manifest)
    if target_manifest.get("release_id") != manifest.get("target_release_id"):
        raise ManifestError("patch target release id does not match its release manifest")
    if target_manifest.get("version") != manifest.get("target_version"):
        raise ManifestError("patch target version does not match its release manifest")
    if target_manifest.get("product") != manifest.get("product"):
        raise ManifestError("patch product does not match its target release")

    base_version = _strict_version(manifest.get("base_version"))
    target_version = _strict_version(manifest.get("target_version"))
    if not str(manifest.get("base_release_id") or "") or not str(manifest.get("target_release_id") or ""):
        raise ManifestError("patch is missing base or target release identity")
    if not str(manifest.get("base_release_id")).startswith(f"{manifest.get('base_version')}-"):
        raise ManifestError("patch base release id does not match its version")
    if not isinstance(manifest.get("restart_required"), bool):
        raise ManifestError("patch restart_required must be a boolean")
    if target_version == base_version and manifest.get("base_release_id") == manifest.get("target_release_id"):
        raise ManifestError("patch base and target releases are identical")
    schema = manifest.get("schema")
    if not isinstance(schema, dict):
        raise ManifestError("patch schema compatibility metadata is missing")
    expected_migration = str(schema.get("from") or "") != str(schema.get("to") or "")
    if bool(schema.get("migration_required")) != expected_migration:
        raise ManifestError("patch schema migration metadata is inconsistent")

    operations = manifest.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ManifestError("patch has no file operations")
    expected_payload: set[str] = set()
    seen_paths: set[str] = set()
    target_entries = _manifest_entry_map(target_manifest)
    payload_root = root / "payload"
    if not payload_root.is_dir() or payload_root.is_symlink():
        raise ManifestError("patch payload directory is missing or unsafe")
    for operation in operations:
        if not isinstance(operation, dict):
            raise ManifestError("patch contains a non-object operation")
        path = str(operation.get("path") or "")
        relative = _validate_relative_path(path)
        if path in seen_paths:
            raise ManifestError(f"patch contains duplicate operations for {path}")
        seen_paths.add(path)
        action = operation.get("op")
        if action == "delete":
            if path in target_entries:
                raise ManifestError(f"patch deletes a file present in the target release: {path}")
            continue
        if action != "upsert":
            raise ManifestError(f"unsupported patch operation: {action!r}")
        entry = operation.get("entry")
        if not isinstance(entry, dict) or entry != target_entries.get(path):
            raise ManifestError(f"patch operation does not match target manifest: {path}")
        payload_path = payload_root / relative
        if entry.get("type") == "file":
            if not payload_path.is_file() or payload_path.is_symlink():
                raise ManifestError(f"patch payload file is missing: {path}")
            if payload_path.stat().st_size != entry.get("size"):
                raise ManifestError(f"patch payload size mismatch: {path}")
            if _sha256_file(payload_path) != entry.get("sha256"):
                raise ManifestError(f"patch payload checksum mismatch: {path}")
            if stat.S_IMODE(payload_path.stat().st_mode) != entry.get("mode"):
                raise ManifestError(f"patch payload mode mismatch: {path}")
        elif entry.get("type") == "symlink":
            if not payload_path.is_symlink() or os.readlink(payload_path) != entry.get("target"):
                raise ManifestError(f"patch payload symlink mismatch: {path}")
            _validate_symlink(payload_root, payload_path, os.readlink(payload_path))
        else:
            raise ManifestError(f"unsupported patch payload type: {entry.get('type')!r}")
        expected_payload.add(path)

    actual_payload = {
        str(entry.get("path") or "")
        for entry in _payload_entries(payload_root, require_nonempty=False)
    }
    if actual_payload != expected_payload:
        missing = sorted(expected_payload - actual_payload)
        extra = sorted(actual_payload - expected_payload)
        raise ManifestError(f"patch payload differs from operations; missing={missing}, extra={extra}")
    expected_patch_id = (
        f"{manifest.get('base_version')}-to-{manifest.get('target_version')}-"
        f"{hashlib.sha256(_json_bytes(_patch_identity(manifest))).hexdigest()[:16]}"
    )
    if manifest.get("patch_id") != expected_patch_id:
        raise ManifestError("patch identity checksum mismatch")
    return manifest


def _reject_symlink_parents(root: Path, path: Path) -> None:
    root = root.resolve()
    current = path.parent
    while current != root:
        if current.is_symlink():
            raise ManifestError(f"patch path traverses a symlinked directory: {path}")
        if not _inside(root, current.resolve(strict=False)):
            raise ManifestError(f"patch path escapes release root: {path}")
        current = current.parent


def materialize_patch_package(
    package_dir: str | Path,
    base_release_dir: str | Path,
    output_dir: str | Path,
    *,
    trusted_public_keys: Sequence[str | Path | bytes],
) -> dict[str, object]:
    package_root = Path(package_dir).expanduser().resolve(strict=True)
    base_root = Path(base_release_dir).expanduser().resolve(strict=True)
    output = Path(output_dir).expanduser().resolve()
    manifest = verify_patch_package(package_root, trusted_public_keys=trusted_public_keys)
    base_manifest = verify_release(base_root)
    if base_manifest.get("release_id") != manifest.get("base_release_id"):
        raise PreflightError(
            f"patch requires base {manifest.get('base_release_id')}, active base is {base_manifest.get('release_id')}"
        )
    if output.exists():
        raise ManifestError(f"patch materialization directory already exists: {output}")
    shutil.copytree(base_root, output, symlinks=True)
    try:
        for operation in manifest["operations"]:
            relative = _validate_relative_path(str(operation["path"]))
            target = output / relative
            _reject_symlink_parents(output, target)
            if operation["op"] == "delete":
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    raise ManifestError(f"patch cannot delete a directory payload entry: {relative}")
                continue
            entry = operation["entry"]
            source = package_root / "payload" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                raise ManifestError(f"patch cannot replace a directory payload entry: {relative}")
            if entry["type"] == "file":
                shutil.copy2(source, target)
                os.chmod(target, int(entry.get("mode") or 0o644))
            else:
                target.symlink_to(str(entry["target"]))
                _validate_symlink(output, target, str(entry["target"]))
        _atomic_write_json(output / MANIFEST_FILENAME, manifest["target_release_manifest"])
        verify_release(
            output,
            trusted_public_keys=trusted_public_keys,
            require_signature=True,
        )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _normalized_platform(value: str) -> str:
    value = value.strip().lower()
    aliases = {"win32": "windows", "cygwin": "windows", "darwin": "macos", "linux2": "linux"}
    return aliases.get(value, value)


def _normalized_arch(value: str) -> str:
    value = value.strip().lower()
    aliases = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}
    return aliases.get(value, value)


def _sqlite_path(database_url: str | None) -> Path | None:
    value = (database_url or "").strip()
    prefix = "sqlite:///"
    if not value.startswith(prefix):
        return None
    raw_path = value[len(prefix) :]
    if raw_path in {"", ":memory:"} or raw_path.startswith("file::memory:"):
        return None
    return Path(raw_path).expanduser().resolve()


def _copy_sqlite_consistently(source: Path, target: Path) -> None:
    if not source.is_file():
        raise PreflightError(f"SQLite database does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _backup_artifact_entries(root: Path) -> list[dict[str, object]]:
    resolved_root = root.resolve(strict=True)
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            _validate_symlink(resolved_root, path, target)
            entries.append({"path": relative, "type": "symlink", "target": target})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
    return entries


def _verify_backup_artifacts(root: Path, expected: object) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ReleaseError(f"backup artifact directory is missing or unsafe: {root}")
    if not isinstance(expected, list):
        raise ReleaseError(f"backup artifact inventory is missing: {root}")
    actual = _backup_artifact_entries(root)
    if actual != expected:
        raise ReleaseError(f"backup artifact integrity check failed: {root}")


class ReleaseManager:
    def __init__(self, install_root: str | Path, *, lock_timeout_seconds: int = 900) -> None:
        self.root = Path(install_root).expanduser().resolve()
        self.releases_dir = self.root / "releases"
        self.backups_dir = self.root / "backups"
        self.shared_dir = self.root / "shared"
        self.state_path = self.root / STATE_FILENAME
        self.active_path = self.root / ACTIVE_FILENAME
        self.lock_path = self.root / LOCK_FILENAME
        self.checkpoints = ReleaseCheckpoint(self.root)
        self._operation_local = threading.local()
        # Retained for callers; elapsed time never grants ownership of a live lock.
        self.lock_timeout_seconds = lock_timeout_seconds

    def _ensure_layout(self) -> None:
        for path in (
            self.root,
            self.releases_dir,
            self.backups_dir,
            self.shared_dir,
            self.shared_dir / "config",
            self.shared_dir / "data",
            self.shared_dir / "logs",
            self.shared_dir / "db",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _bootstrap_shared_config(self, release_path: Path) -> dict:
        """Bring the shared env file up to the release without discarding local edits.

        Copying once and returning (the old behaviour) meant every key added by a later release
        stayed missing, and the symptom was a setting that silently did nothing. Merging needs the
        template as of the last write, so a base copy is kept next to the file.
        """
        destination = self.shared_dir / "config" / "app.env"
        base_path = self.shared_dir / "config" / "app.env.base"
        template = release_path / "config" / "app.env.example"
        if not template.is_file() or template.is_symlink():
            return {"strategy": "skipped", "reason": "release ships no app.env.example"}

        theirs = template.read_text(encoding="utf-8")
        if not destination.exists():
            shutil.copy2(template, destination)
            try:
                base_path.write_text(theirs, encoding="utf-8")
            except OSError:
                pass  # a missing base only downgrades the next merge to two-way
            return {"strategy": "copied"}

        ours = destination.read_text(encoding="utf-8")
        merged, report = config_merge.merge_env(
            config_merge.read_text(base_path), ours, theirs
        )
        if merged == ours and not report.added and not report.updated and not report.removed:
            return report.as_dict()
        backup = destination.with_name(f"app.env.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(destination, backup)
        report.backup_path = str(backup)
        destination.write_text(merged, encoding="utf-8")
        try:
            base_path.write_text(theirs, encoding="utf-8")
        except OSError:
            pass
        return report.as_dict()

    def _read_json(self, path: Path, default: object) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"invalid release metadata {path}: {exc}") from exc

    def state(self) -> dict[str, object]:
        value = self._read_json(self.state_path, {"format_version": 1, "history": []})
        if not isinstance(value, dict):
            raise ReleaseError("release state must be a JSON object")
        history = value.get("history")
        if not isinstance(history, list):
            raise ReleaseError("release history must be a JSON array")
        return value

    def active(self) -> dict[str, object] | None:
        value = self._read_json(self.active_path, None)
        if value is None:
            return None
        if not isinstance(value, dict) or not value.get("release_id"):
            raise ReleaseError("active release metadata is invalid")
        return value

    def active_release_path(self) -> Path | None:
        active = self.active()
        if not active:
            return None
        release_path = self.releases_dir / str(active["release_id"])
        if not release_path.is_dir():
            raise ReleaseError(f"active release payload is missing: {release_path}")
        return release_path

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._ensure_layout()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ReleaseLockError(f"cannot open release lock: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or self.lock_path.is_symlink():
                raise ReleaseLockError("release lock must be a regular file")
            try:
                _acquire_file_lock(descriptor)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ReleaseLockError("another install, upgrade, or rollback operation is running") from exc
                raise ReleaseLockError(f"cannot acquire release lock: {exc}") from exc
            if not os.path.samestat(os.fstat(descriptor), self.lock_path.stat()):
                raise ReleaseLockError("release lock file changed while acquiring ownership")
            existing = os.read(descriptor, 4096)
            if existing:
                try:
                    metadata = json.loads(existing)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ReleaseLockError("unrecognized release lock; manual inspection required") from exc
                if not isinstance(metadata, dict) or metadata.get("lock_protocol") != 2:
                    raise ReleaseLockError("legacy release lock; stop old updaters before manual removal")
            payload = _json_bytes({
                "lock_protocol": 2,
                "pid": os.getpid(),
                "created_at": _utc_now(),
                "created_epoch": time.time(),
            })
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.ftruncate(descriptor, len(payload))
            self._operation_local.locked = True
            yield
        except CheckpointError as exc:
            raise ReleaseError(str(exc)) from exc
        finally:
            self._operation_local.locked = False
            # Keep one stable inode. Closing only our handle releases our OS lock,
            # including on process exit, without deleting a successor's lock file.
            os.close(descriptor)

    def inspect_transaction(self) -> dict[str, object]:
        return {"unfinished": self.checkpoints.current(), "active": self.active()}

    def transaction_history(self) -> list[dict]:
        return self.checkpoints.read()["transactions"]

    def _require_finished(self) -> None:
        pending = self.checkpoints.current()
        if pending is not None:
            raise ReleaseError(
                f"unfinished release transaction {pending['id']}; use inspect/recover before new mutation"
            )

    def _phase(self, name: str, operation):
        self.checkpoints.record(name, "started")
        result = operation()
        facts = {}
        if name == "backup":
            facts = {
                "backup_id": result,
                "backup_manifest_sha256": _sha256_file(self.backups_dir / result / "backup-manifest.json"),
            }
            try:
                sync_tree(self.backups_dir / result)
                sync_directory(self.backups_dir)
            except OSError as exc:
                raise CheckpointError("backup durability failed; recovery required") from exc
        elif name == "stage":
            facts = {"target_deployment": result.as_dict()}
        self.checkpoints.record(name, "completed", facts=facts)
        return result

    def _finish_transaction(self, status: str) -> None:
        self.checkpoints.record("finish", status, terminal=status != "needs_intervention")

    def recover(
        self,
        transaction_id: str,
        *,
        action: str,
        evidence: Callable[[Path], Mapping[str, object]] | None = None,
        health_check: HealthCheck | None = None,
        activate: ActivationStep | None = None,
        deployment_driver: DeploymentDriver | None = None,
        trusted_public_keys: Sequence[str | Path | bytes] = (),
    ) -> dict[str, object]:
        """Recover locally; never replay migration, down-migration, or restore hooks.

        Evidence is a fresh site-owned probe, not a saved operator assertion. It
        must prove the transaction identity, schema compatibility and fencing of
        all writers/old hooks. Reconcile observes an already repaired runtime.
        """
        if action not in {"cancel", "resume", "rollback", "reconcile"}:
            raise PreflightError("unknown recovery action")
        with self._lock():
            transactions = self.transaction_history()
            transaction = next((item for item in transactions if item["id"] == transaction_id), None)
            if transaction is None:
                raise PreflightError("unknown transaction id")
            if transaction["terminal"]:
                return {"transaction_id": transaction_id, "status": "already_finished"}
            facts = transaction["facts"]
            events = transaction["events"]
            started = {event["phase"] for event in events if event["status"] == "started"}
            completed = {event["phase"] for event in events if event["status"] == "completed"}
            if action == "cancel":
                if started - {"payload", "config", "stage", "backup"}:
                    raise PreflightError("cancel requires a transaction before runtime/data mutation")
                if (self.active() or {}).get("release_id") != facts.get("previous_release_id"):
                    raise PreflightError("active identity changed; cancellation is unsafe")
                result = {"transaction_id": transaction_id, "action": "recover-cancel", "status": "cancelled"}
                self._append_history({**result, "recorded_at": _utc_now()})
                self._finish_transaction("cancelled")
                return result

            if evidence is None or health_check is None:
                raise PreflightError("recovery requires fresh evidence and a readiness health check")
            if facts.get("action") == "restore":
                raise PreflightError("standalone restore requires manual reconciliation outside upgrade recovery")
            selected = facts.get("previous_release_id") if action == "rollback" else facts.get("release_id")
            if action == "reconcile":
                selected = (self.active() or {}).get("release_id")
                if selected not in {facts.get("release_id"), facts.get("previous_release_id")}:
                    raise PreflightError("active identity is outside the interrupted transaction")
            if not isinstance(selected, str) or not selected:
                raise PreflightError("recovery release is unavailable")
            target = self.releases_dir / _validate_relative_path(selected)
            if target.parent != self.releases_dir or target.is_symlink():
                raise PreflightError("unsafe recovery release path")
            manifest = verify_release(target, trusted_public_keys=trusted_public_keys,
                                      require_signature=bool(facts.get("require_signature")))
            digest = _sha256_file(target / MANIFEST_FILENAME)
            expected_digest = facts.get("manifest_sha256") if selected == facts.get("release_id") else facts.get("previous_manifest_sha256")
            if digest != expected_digest or manifest.get("release_id") != selected:
                raise PreflightError("recovery release identity/checksum changed")
            data_phases = {"migration", "down_migration", "restore"}
            ambiguous = (started - completed) & data_phases
            if action != "reconcile" and ambiguous:
                raise PreflightError("ambiguous migration/restore; manually verify and reconcile, never replay")
            if action == "resume":
                if (facts.get("migration_required") and "migration" not in completed) or (
                    facts.get("restore_required") and "restore" not in completed
                ):
                    raise PreflightError("resume never runs pending migration or restore; cancel/rollback or reconcile")
                if started & {"undo_metadata", "down_migration", "undo_activate", "recovery_activate"}:
                    raise PreflightError("compensation/recovery was entered; reconcile its runtime before proceeding")
                if "restore" in started and facts.get("action") != "rollback":
                    raise PreflightError("compensation restored data; resume is unsafe")
            if action == "rollback":
                if started & data_phases and not facts.get("database_reverted"):
                    raise PreflightError("rollback cannot replay data changes; manually repair and reconcile")
                if "recovery_activate" in started:
                    raise PreflightError("recovery activation was entered; reconcile instead of replaying")
            if action != "reconcile" and started & data_phases:
                backup_id = facts.get("backup_id")
                if not backup_id:
                    raise PreflightError("data recovery requires a durable verified backup")
                backup_path = self.backups_dir / _validate_relative_path(str(backup_id))
                if _sha256_file(backup_path / "backup-manifest.json") != facts.get("backup_manifest_sha256"):
                    raise PreflightError("recovery backup identity/checksum changed")
                self._validated_backup_items(str(backup_id))

            proof = evidence(target)
            expected = {
                "transaction_id": transaction_id, "release_id": selected,
                "manifest_sha256": digest, "schema_revision": str(manifest.get("schema_revision") or ""),
                "data_compatible": True, "writers_fenced": True, "hooks_stopped": True,
            }
            if not isinstance(proof, Mapping) or any(
                (proof.get(key) is not value if isinstance(value, bool) else proof.get(key) != value)
                for key, value in expected.items()
            ):
                raise PreflightError("fresh evidence did not prove release/schema identity, data compatibility and fencing")
            runtime_matches = proof.get("runtime_release_id") == selected
            if action == "reconcile" or (action == "resume" and "activate" in started):
                if not runtime_matches:
                    raise PreflightError("activation was entered; evidence must identify the running release, never replay")
            else:
                if activate is None and deployment_driver is None:
                    raise PreflightError("recovery activation requires an explicit deployment driver or command")
                driver = self._deployment_driver(manifest, activation=activate, requested=deployment_driver)
                if driver.name != facts.get("driver"):
                    raise PreflightError("recovery deployment driver changed")
                driver.preflight(target, manifest)
                deployment = self._phase("recovery_stage", lambda: driver.stage(target, manifest))
                self._phase("recovery_metadata", lambda: self._set_active(
                    manifest, previous_release_id=None if action == "rollback" else facts.get("previous_release_id"),
                    deployment=deployment,
                ))
                self._phase("recovery_activate", lambda: (
                    driver.rollback(target, manifest, deployment) if action == "rollback"
                    else driver.activate(target, manifest, deployment)
                ))
            self._phase("recovery_health", lambda: _check_health(health_check, target))
            # Reconciliation must not silently manufacture a new active pointer.
            if (self.active() or {}).get("release_id") != selected:
                raise PreflightError("active metadata does not match the verified runtime")
            result = {"transaction_id": transaction_id, "action": f"recover-{action}",
                      "status": "reconciled" if action == "reconcile" else "succeeded", "release_id": selected}
            self._append_history({**result, "recorded_at": _utc_now()})
            self._finish_transaction(str(result["status"]))
            return result

    def _append_history(self, record: dict[str, object]) -> None:
        state = self.state()
        history = list(state.get("history") or [])
        pending = self.checkpoints.current()
        if pending:
            record = {**record, "transaction_id": pending["id"]}
            history = [item for item in history if not (
                item.get("transaction_id") == pending["id"]
                and item.get("action") == record.get("action")
                and item.get("status") == record.get("status")
            )]
        history.append(record)
        state["history"] = history[-200:]
        state["updated_at"] = _utc_now()
        _atomic_write_json(self.state_path, state)

    def _set_active(
        self,
        manifest: dict[str, object],
        *,
        previous_release_id: str | None,
        deployment: DeploymentTarget | None = None,
    ) -> None:
        release_path = self.releases_dir / str(manifest["release_id"])
        deployment_target = deployment or DeploymentTarget("native", str(release_path))
        _atomic_write_json(
            self.active_path,
            {
                "release_id": manifest["release_id"],
                "version": manifest["version"],
                "path": str(release_path),
                "deployment": deployment_target.as_dict(),
                "previous_release_id": previous_release_id,
                "activated_at": _utc_now(),
            },
        )

    def _restore_active_metadata(self, previous: dict[str, object] | None) -> None:
        if previous is None:
            try:
                self.active_path.unlink()
                sync_directory(self.root)
            except FileNotFoundError:
                pass
            return
        _atomic_write_json(self.active_path, previous)

    def _preflight(self, package_dir: Path, manifest: dict[str, object]) -> None:
        package_platform = _normalized_platform(str(manifest.get("platform") or "any"))
        current_platform = _normalized_platform(os.sys.platform)
        if package_platform not in {"any", current_platform}:
            raise PreflightError(
                f"release targets {package_platform}, current platform is {current_platform}"
            )
        package_arch = _normalized_arch(str(manifest.get("arch") or "any"))
        current_arch = _normalized_arch(platform.machine())
        if package_arch not in {"any", current_arch}:
            raise PreflightError(f"release targets {package_arch}, current architecture is {current_arch}")
        entrypoint = str(manifest.get("entrypoint") or "").strip()
        if entrypoint:
            relative_entrypoint = _validate_relative_path(entrypoint)
            candidate = package_dir / relative_entrypoint
            if not candidate.is_file() or candidate.is_symlink():
                raise PreflightError(f"release entrypoint is missing or unsafe: {entrypoint}")
        payload_size = sum(
            int(entry.get("size") or 0)
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and entry.get("type") == "file"
        )
        usage = shutil.disk_usage(self.root if self.root.exists() else package_dir)
        required = max(payload_size * 2, 32 * 1024 * 1024)
        if usage.free < required:
            raise PreflightError(f"insufficient free space: need {required} bytes, have {usage.free} bytes")

    def _deployment_driver(
        self,
        manifest: dict[str, object],
        *,
        activation: ActivationStep | None,
        requested: DeploymentDriver | None,
    ) -> DeploymentDriver:
        deployment_type = _manifest_deployment_type(manifest)
        driver: DeploymentDriver
        if requested is not None:
            driver = requested
            if activation is not None:
                raise PreflightError(
                    "activation callback must be configured on an explicit deployment driver"
                )
        elif deployment_type == "container":
            driver = ContainerDeploymentDriver(activation=activation)
        else:
            driver = NativeDeploymentDriver(activation)
        if driver.name != deployment_type:
            raise PreflightError(
                f"release requires {deployment_type} deployment driver, received {driver.name}"
            )
        return driver

    def create_pre_upgrade_backup(
        self,
        *,
        database_url: str | None = None,
        config_paths: Sequence[str | Path] = (),
        external_backup: BackupStep | None = None,
    ) -> str:
        if not getattr(self._operation_local, "locked", False):
            with self._lock():
                self._require_finished()
                return self.create_pre_upgrade_backup(
                    database_url=database_url, config_paths=config_paths, external_backup=external_backup,
                )
        self._ensure_layout()
        backup_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        backup_dir = self.backups_dir / backup_id
        backup_dir.mkdir(parents=True, exist_ok=False)
        items: list[dict[str, object]] = []
        try:
            sqlite_source = _sqlite_path(database_url)
            if sqlite_source is not None:
                sqlite_target = backup_dir / "database" / sqlite_source.name
                _copy_sqlite_consistently(sqlite_source, sqlite_target)
                items.append(
                    {
                        "kind": "sqlite",
                        "source": str(sqlite_source),
                        "backup": str(sqlite_target.relative_to(backup_dir)),
                        "sha256": _sha256_file(sqlite_target),
                    }
                )
            if external_backup is not None:
                external_dir = backup_dir / "external-database"
                external_dir.mkdir(parents=True, exist_ok=False)
                external_backup(external_dir)
                artifacts = _backup_artifact_entries(external_dir)
                if not any(item.get("type") == "file" for item in artifacts):
                    raise PreflightError("external database backup did not create an artifact")
                items.append(
                    {
                        "kind": "external",
                        "source": "external-database",
                        "backup": str(external_dir.relative_to(backup_dir)),
                        "artifacts": artifacts,
                    }
                )
            for index, raw_path in enumerate(config_paths):
                source = Path(raw_path).expanduser().resolve()
                if not source.exists():
                    continue
                target = backup_dir / "config" / f"{index:02d}-{source.name}"
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                    kind = "directory"
                    integrity: dict[str, object] = {"artifacts": _backup_artifact_entries(target)}
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    kind = "file"
                    integrity = {
                        "size": target.stat().st_size,
                        "sha256": _sha256_file(target),
                        "mode": stat.S_IMODE(target.stat().st_mode),
                    }
                items.append(
                    {
                        "kind": kind,
                        "source": str(source),
                        "backup": str(target.relative_to(backup_dir)),
                        **integrity,
                    }
                )
            _atomic_write_json(
                backup_dir / "backup-manifest.json",
                {"format_version": 1, "backup_id": backup_id, "created_at": _utc_now(), "items": items},
            )
            return backup_id
        except Exception:
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise

    def restore_backup(
        self,
        backup_id: str,
        *,
        external_restore: RestoreStep | None = None,
        restore_external: bool = True,
    ) -> None:
        if not getattr(self._operation_local, "locked", False):
            with self._lock():
                self._require_finished()
                self.checkpoints.begin({"action": "restore", "backup_id": backup_id})
                self._phase("restore", lambda: self.restore_backup(
                    backup_id, external_restore=external_restore, restore_external=restore_external,
                ))
                self._finish_transaction("unverified")
                return
        validated_items = self._validated_backup_items(backup_id)
        self._restore_validated_items(validated_items, external_restore=external_restore, restore_external=restore_external)

    def _validated_backup_items(self, backup_id: str) -> list[tuple[dict[str, object], Path]]:
        backup_dir = (self.backups_dir / backup_id).resolve()
        if not _inside(self.backups_dir.resolve(), backup_dir) or not backup_dir.is_dir():
            raise ReleaseError(f"backup not found: {backup_id}")
        manifest_value = self._read_json(backup_dir / "backup-manifest.json", None)
        if not isinstance(manifest_value, dict) or not isinstance(manifest_value.get("items"), list):
            raise ReleaseError(f"backup manifest is invalid: {backup_id}")
        if manifest_value.get("format_version") != 1 or manifest_value.get("backup_id") != backup_id:
            raise ReleaseError(f"backup manifest identity is invalid: {backup_id}")

        validated_items: list[tuple[dict[str, object], Path]] = []
        for item in manifest_value["items"]:
            if not isinstance(item, dict):
                raise ReleaseError("backup item is invalid")
            source = backup_dir / _validate_relative_path(str(item.get("backup") or ""))
            if item.get("kind") == "sqlite":
                if not source.is_file() or source.is_symlink():
                    raise ReleaseError(f"SQLite backup artifact is missing or unsafe: {source}")
                if source.stat().st_size <= 0:
                    raise ReleaseError(f"SQLite backup artifact is empty: {source}")
                if _sha256_file(source) != item.get("sha256"):
                    raise ReleaseError(f"backup checksum mismatch: {source}")
            elif item.get("kind") == "file":
                if not source.is_file() or source.is_symlink():
                    raise ReleaseError(f"backup file is missing or unsafe: {source}")
                if source.stat().st_size != item.get("size") or _sha256_file(source) != item.get("sha256"):
                    raise ReleaseError(f"backup file integrity check failed: {source}")
                if stat.S_IMODE(source.stat().st_mode) != item.get("mode"):
                    raise ReleaseError(f"backup file mode check failed: {source}")
            elif item.get("kind") in {"directory", "external"}:
                _verify_backup_artifacts(source, item.get("artifacts"))
            else:
                raise ReleaseError(f"unsupported backup item kind: {item.get('kind')}")
            validated_items.append((item, source))
        return validated_items

    def _restore_validated_items(
        self, validated_items: list[tuple[dict[str, object], Path]], *,
        external_restore: RestoreStep | None, restore_external: bool,
    ) -> None:
        for item, source in validated_items:
            kind = item.get("kind")
            if kind == "external":
                if not restore_external:
                    continue
                if external_restore is None:
                    raise ReleaseError("external database restore hook is required")
                external_restore(source)
                continue
            raw_target = Path(str(item.get("source") or "")).expanduser()
            if not raw_target.is_absolute():
                raise ReleaseError(f"backup restore target must be absolute: {raw_target}")
            target = Path(os.path.abspath(raw_target))
            if kind == "sqlite":
                target.parent.mkdir(parents=True, exist_ok=True)
                restore_temp = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
                shutil.copy2(source, restore_temp)
                os.replace(restore_temp, target)
            elif kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                shutil.copy2(source, target)
            elif kind == "directory":
                replacement = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
                shutil.copytree(source, replacement, symlinks=True)
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                os.replace(replacement, target)

    def install(
        self,
        package_dir: str | Path,
        *,
        database_url: str | None = None,
        config_paths: Sequence[str | Path] = (),
        migration: MigrationStep | None = None,
        migration_rollback: MigrationStep | None = None,
        migration_preflight: MigrationStep | None = None,
        health_check: HealthCheck | None = None,
        external_backup: BackupStep | None = None,
        external_restore: RestoreStep | None = None,
        quiesce: ActivationStep | None = None,
        activate: ActivationStep | None = None,
        deployment_driver: DeploymentDriver | None = None,
        trusted_public_keys: Sequence[str | Path | bytes] = (),
        require_signature: bool = False,
        expected_previous_release_id: str | None = None,
        allow_downgrade: bool = False,
        package_type: str = "release",
        package_id: str | None = None,
    ) -> ReleaseResult:
        package = Path(package_dir).expanduser().resolve(strict=True)
        manifest = verify_release(
            package,
            trusted_public_keys=trusted_public_keys,
            require_signature=require_signature,
        )
        self._ensure_layout()
        self._preflight(package, manifest)
        driver = self._deployment_driver(
            manifest,
            activation=activate,
            requested=deployment_driver,
        )
        driver.preflight(package, manifest)
        if migration_preflight is not None:
            migration_preflight(package)
        release_id = str(manifest["release_id"])
        version = str(manifest["version"])
        database_value = (database_url or "").strip()
        if migration is not None and database_value and _sqlite_path(database_value) is None:
            if external_backup is None or external_restore is None:
                raise PreflightError(
                    "non-SQLite schema migration requires both external backup and restore hooks"
                )
        if migration_rollback is not None and migration is None:
            raise PreflightError("migration rollback requires a matching forward migration")
        if migration_preflight is not None and migration is None:
            raise PreflightError("migration preflight requires a matching forward migration")
        if (
            quiesce is not None
            and driver.name == "native"
            and activate is None
            and deployment_driver is None
        ):
            raise PreflightError("a quiesce/stop-write command requires an activation/restart command")
        if health_check is None and (
            activate is not None or deployment_driver is not None or driver.name != "native"
        ):
            raise PreflightError("runtime activation requires a readiness health check")

        with self._lock():
            self._require_finished()
            previous = self.active()
            previous_release_id = str(previous["release_id"]) if previous else None
            previous_path = self.releases_dir / previous_release_id if previous_release_id else None
            previous_manifest: dict[str, object] | None = None
            previous_target: DeploymentTarget | None = None
            if previous is not None and previous_path is not None:
                previous_manifest = verify_release(
                    previous_path,
                )
                previous_target = DeploymentTarget.from_dict(
                    previous.get("deployment"),
                    fallback_path=previous_path,
                )
                if previous_target.driver != driver.name:
                    raise PreflightError(
                        "changing deployment mode in-place is not supported; use a parallel installation and cutover"
                    )
            if expected_previous_release_id is not None and previous_release_id != expected_previous_release_id:
                raise PreflightError(
                    f"release base changed; expected {expected_previous_release_id}, active is {previous_release_id or 'none'}"
                )
            if previous_release_id == release_id:
                return ReleaseResult(
                    "install",
                    "unchanged",
                    release_id,
                    version,
                    previous_release_id,
                    None,
                    "already active",
                    package_type,
                    package_id,
                    driver.name,
                    str((previous or {}).get("deployment", {}).get("target_ref") or "")
                    if isinstance((previous or {}).get("deployment"), dict)
                    else "",
                    str(manifest.get("schema_revision") or ""),
                )
            if previous is not None and not allow_downgrade:
                previous_version = STRICT_VERSION_PATTERN.fullmatch(str(previous.get("version") or ""))
                target_version = STRICT_VERSION_PATTERN.fullmatch(version)
                if previous_version and target_version:
                    previous_tuple = tuple(int(part) for part in previous_version.groups())
                    target_tuple = tuple(int(part) for part in target_version.groups())
                    if target_tuple <= previous_tuple:
                        raise PreflightError(
                            f"release version {version} is not newer than active version {previous.get('version')}"
                        )

            backup_id = None
            effective_config_paths = list(config_paths)
            shared_config = self.shared_dir / "config"
            if shared_config.exists() and shared_config not in [Path(item).expanduser().resolve() for item in effective_config_paths]:
                effective_config_paths.append(shared_config)
            target = self.releases_dir / release_id
            stage = self.releases_dir / f".stage-{release_id}-{uuid.uuid4().hex[:8]}"
            activated = False
            quiesced = False
            deployment_activated = False
            migration_started = False
            migration_completed = False
            target_deployment: DeploymentTarget | None = None
            self.checkpoints.begin({
                "action": "install" if previous is None else ("patch" if package_type == "patch" else "upgrade"),
                "release_id": release_id, "version": version,
                "previous_release_id": previous_release_id,
                "manifest_sha256": _sha256_file(package / MANIFEST_FILENAME),
                "previous_manifest_sha256": _sha256_file(previous_path / MANIFEST_FILENAME) if previous_path else None,
                "driver": driver.name, "migration_required": migration is not None,
                "require_signature": require_signature,
                "runtime_activation": activate is not None or deployment_driver is not None or driver.name != "native",
            })
            try:
                self.checkpoints.record("payload", "started")
                if target.exists():
                    verify_release(
                        target,
                        trusted_public_keys=trusted_public_keys,
                        require_signature=require_signature,
                    )
                else:
                    shutil.copytree(package, stage, symlinks=True)
                    verify_release(
                        stage,
                        trusted_public_keys=trusted_public_keys,
                        require_signature=require_signature,
                    )
                    os.replace(stage, target)
                try:
                    sync_tree(target)
                    sync_directory(self.releases_dir)
                except OSError as exc:
                    raise CheckpointError("release payload durability failed; recovery required") from exc
                self.checkpoints.record("payload", "completed")
                self._phase("config", lambda: self._bootstrap_shared_config(target))
                target_deployment = self._phase("stage", lambda: driver.stage(target, manifest))
                if quiesce is not None and previous is not None:
                    quiesced = True
                    self._phase("quiesce", lambda: quiesce(self.releases_dir / previous_release_id))
                if previous is not None and (database_url or effective_config_paths or external_backup):
                    backup_id = self._phase("backup", lambda: self.create_pre_upgrade_backup(
                        database_url=database_url,
                        config_paths=effective_config_paths,
                        external_backup=external_backup,
                    ))
                activated = True
                self._phase("metadata", lambda: self._set_active(
                    manifest,
                    previous_release_id=previous_release_id,
                    deployment=target_deployment,
                ))
                if migration is not None:
                    migration_started = True
                    self._phase("migration", lambda: migration(target))
                    migration_completed = True
                deployment_activated = True
                self._phase("activate", lambda: driver.activate(target, manifest, target_deployment))
                health = self._phase("health", lambda: _check_health(health_check, target))
                action = "install" if previous is None else ("patch" if package_type == "patch" else "upgrade")
                result = ReleaseResult(
                    action,
                    "succeeded" if health is not None else "unverified",
                    release_id,
                    version,
                    previous_release_id,
                    backup_id,
                    health.detail if health is not None else "health check not configured; runtime health is unverified",
                    package_type,
                    package_id,
                    driver.name,
                    target_deployment.target_ref,
                    str(manifest.get("schema_revision") or ""),
                    health_status="healthy" if health is not None else "not_checked",
                )
                self._append_history({**result.__dict__, "recorded_at": _utc_now()})
                self._finish_transaction(result.status)
                return result
            except CheckpointError:
                # A failed post-phase write is indistinguishable from a crash.
                # Never compensate with a migration/restore on uncertain storage.
                raise
            except Exception as exc:
                original_error = str(exc)
                recovery_errors: list[str] = []
                rollback_verified = False
                if activated:
                    try:
                        self._phase("undo_metadata", lambda: self._restore_active_metadata(previous))
                    except CheckpointError:
                        raise
                    except Exception as metadata_error:
                        recovery_errors.append(f"active metadata restore failed: {metadata_error}")
                database_reverted_by_migration = False
                if migration_completed and migration_rollback is not None:
                    try:
                        self._phase("down_migration", lambda: migration_rollback(target))
                        database_reverted_by_migration = True
                    except CheckpointError:
                        raise
                    except Exception as migration_rollback_error:
                        original_error += (
                            "; down migration failed, restoring database backup: "
                            f"{migration_rollback_error}"
                        )
                backup_restored = False
                if backup_id:
                    try:
                        self._phase("restore", lambda: self.restore_backup(
                            backup_id,
                            external_restore=external_restore,
                            restore_external=not database_reverted_by_migration,
                        ))
                        # A config-only backup cannot prove that schema/data was reverted.
                        backup_restored = _sqlite_path(database_value) is not None or external_backup is not None
                        self.checkpoints.record("database_reverted", "completed", facts={
                            "database_reverted": database_reverted_by_migration or backup_restored,
                        })
                    except CheckpointError:
                        raise
                    except Exception as restore_error:
                        recovery_errors.append(f"backup restore failed: {restore_error}")
                if migration_started and not (database_reverted_by_migration or backup_restored):
                    recovery_errors.append("migration may have changed data without a verified recovery path")
                if previous is not None and previous_path is not None and previous_manifest is not None and (
                    activated or quiesced or deployment_activated
                ) and not recovery_errors:
                    try:
                        self._phase("undo_activate", lambda: driver.rollback(
                            previous_path,
                            previous_manifest,
                            previous_target or DeploymentTarget("native", str(previous_path)),
                        ))
                        if self._phase("undo_health", lambda: _check_health(health_check, previous_path)) is None:
                            raise ReleaseError("rollback health check not configured")
                        rollback_verified = True
                    except CheckpointError:
                        raise
                    except Exception as recovery_error:
                        recovery_errors.append(f"previous release recovery failed: {recovery_error}")
                shutil.rmtree(stage, ignore_errors=True)
                status = "rolled_back" if rollback_verified else "failed"
                if recovery_errors or (previous is None and deployment_activated):
                    status = "needs_intervention"
                detail = "; ".join([original_error, *recovery_errors])
                failure = {
                    "action": "install" if previous is None else ("patch" if package_type == "patch" else "upgrade"),
                    "status": status,
                    "release_id": release_id,
                    "version": version,
                    "previous_release_id": previous_release_id,
                    "backup_id": backup_id,
                    "detail": detail,
                    "original_error": str(exc),
                    "rollback_errors": recovery_errors,
                    "rollback_health_status": "healthy" if rollback_verified else "not_verified",
                    "package_type": package_type,
                    "package_id": package_id,
                    "deployment_type": driver.name,
                    "target_ref": target_deployment.target_ref if target_deployment else "",
                    "schema_revision": str(manifest.get("schema_revision") or ""),
                    "recorded_at": _utc_now(),
                }
                self._append_history(failure)
                self._finish_transaction(status)
                outcome = "active release was restored and health verified" if rollback_verified else (
                    "recovery is not verified; manual intervention required"
                    if status == "needs_intervention" else "no verified runtime rollback performed"
                )
                raise ReleaseError(
                    f"release {release_id} failed; {outcome}: {detail}"
                ) from exc

    def preflight_patch(
        self,
        package_dir: str | Path,
        *,
        trusted_public_keys: Sequence[str | Path | bytes],
        allow_downgrade: bool = False,
        migration_preflight: MigrationStep | None = None,
    ) -> dict[str, object]:
        package = Path(package_dir).expanduser().resolve(strict=True)
        patch = verify_patch_package(package, trusted_public_keys=trusted_public_keys)
        self._ensure_layout()
        with self._lock():
            current = self.active()
            if current is None:
                raise PreflightError("a patch cannot be applied before the initial full installation")
            active_release_id = str(current.get("release_id") or "")
            if active_release_id != patch.get("base_release_id"):
                raise PreflightError(
                    f"patch requires base {patch.get('base_release_id')}, active base is {active_release_id}"
                )
            current_version = _strict_version(current.get("version"))
            target_version = _strict_version(patch.get("target_version"))
            if not allow_downgrade and target_version <= current_version:
                raise PreflightError(
                    f"patch target {patch.get('target_version')} is not newer than active {current.get('version')}"
                )
            base_release_path = self.releases_dir / active_release_id
            with tempfile.TemporaryDirectory(prefix=".patch-preflight-", dir=str(self.root)) as temporary:
                materialized = Path(temporary) / "release"
                materialize_patch_package(
                    package,
                    base_release_path,
                    materialized,
                    trusted_public_keys=trusted_public_keys,
                )
                target_manifest = verify_release(
                    materialized,
                    trusted_public_keys=trusted_public_keys,
                    require_signature=True,
                )
                self._preflight(materialized, target_manifest)
                if migration_preflight is not None:
                    migration_preflight(materialized)

            operations = patch.get("operations") or []
            upserts = [item for item in operations if isinstance(item, dict) and item.get("op") == "upsert"]
            deletes = [item for item in operations if isinstance(item, dict) and item.get("op") == "delete"]
            payload_bytes = sum(
                int((item.get("entry") or {}).get("size") or 0)
                for item in upserts
                if isinstance(item.get("entry"), dict) and item["entry"].get("type") == "file"
            )
            schema = patch.get("schema") if isinstance(patch.get("schema"), dict) else {}
            signature = patch.get("signature") if isinstance(patch.get("signature"), dict) else {}
            target_deployment = target_manifest.get("deployment")
            return {
                "status": "ready",
                "patch_id": patch.get("patch_id"),
                "signature_key_id": signature.get("key_id"),
                "active_release_id": active_release_id,
                "active_version": current.get("version"),
                "target_release_id": patch.get("target_release_id"),
                "target_version": patch.get("target_version"),
                "restart_required": patch.get("restart_required"),
                "deployment_type": _manifest_deployment_type(target_manifest),
                "target_ref": str(target_deployment.get("image") or "")
                if isinstance(target_deployment, dict)
                else str(self.releases_dir / str(patch.get("target_release_id") or "")),
                "schema": schema,
                "required_hooks": [
                    hook
                    for hook, required in (
                        ("activation", bool(patch.get("restart_required"))),
                        ("readiness", True),
                        ("migration", bool(schema.get("migration_required"))),
                        ("quiesce", bool(schema.get("migration_required"))),
                        ("database_backup", bool(schema.get("migration_required"))),
                    )
                    if required
                ],
                "operations": {"upsert": len(upserts), "delete": len(deletes)},
                "payload_bytes": payload_bytes,
            }

    def install_patch(
        self,
        package_dir: str | Path,
        *,
        trusted_public_keys: Sequence[str | Path | bytes],
        database_url: str | None = None,
        config_paths: Sequence[str | Path] = (),
        migration: MigrationStep | None = None,
        migration_rollback: MigrationStep | None = None,
        migration_preflight: MigrationStep | None = None,
        health_check: HealthCheck | None = None,
        external_backup: BackupStep | None = None,
        external_restore: RestoreStep | None = None,
        quiesce: ActivationStep | None = None,
        activate: ActivationStep | None = None,
        deployment_driver: DeploymentDriver | None = None,
        allow_downgrade: bool = False,
    ) -> ReleaseResult:
        package = Path(package_dir).expanduser().resolve(strict=True)
        patch = verify_patch_package(package, trusted_public_keys=trusted_public_keys)
        self._ensure_layout()
        current = self.active()
        if current is None:
            raise PreflightError("a patch cannot be applied before the initial full installation")
        active_release_id = str(current.get("release_id") or "")
        if active_release_id != patch.get("base_release_id"):
            raise PreflightError(
                f"patch requires base {patch.get('base_release_id')}, active base is {active_release_id}"
            )
        current_version = _strict_version(current.get("version"))
        target_version = _strict_version(patch.get("target_version"))
        if not allow_downgrade and target_version <= current_version:
            raise PreflightError(
                f"patch target {patch.get('target_version')} is not newer than active {current.get('version')}"
            )
        target_release_manifest = patch.get("target_release_manifest")
        if not isinstance(target_release_manifest, dict):
            raise ManifestError("patch target release manifest is missing")
        target_deployment_type = _manifest_deployment_type(target_release_manifest)
        if (
            patch.get("restart_required")
            and target_deployment_type == "native"
            and activate is None
            and deployment_driver is None
        ):
            raise PreflightError("this patch requires an activation/restart command")
        if health_check is None:
            raise PreflightError("patch installation requires a readiness health check")
        schema = patch.get("schema")
        migration_required = isinstance(schema, dict) and bool(schema.get("migration_required"))
        if migration_required:
            if migration is None:
                raise PreflightError("this patch changes the platform schema and requires a migration command")
            if not (database_url or "").strip():
                raise PreflightError("schema-changing patches require the platform database URL for backup")
            if quiesce is None:
                raise PreflightError("schema-changing patches require a quiesce/stop-write command")

        base_release_path = self.releases_dir / active_release_id
        with tempfile.TemporaryDirectory(prefix=".patch-materialize-", dir=str(self.root)) as temporary:
            materialized = Path(temporary) / "release"
            materialize_patch_package(
                package,
                base_release_path,
                materialized,
                trusted_public_keys=trusted_public_keys,
            )
            return self.install(
                materialized,
                database_url=database_url,
                config_paths=config_paths,
                migration=migration,
                migration_rollback=migration_rollback,
                migration_preflight=migration_preflight,
                health_check=health_check,
                external_backup=external_backup,
                external_restore=external_restore,
                quiesce=quiesce,
                activate=activate,
                deployment_driver=deployment_driver,
                trusted_public_keys=trusted_public_keys,
                require_signature=True,
                expected_previous_release_id=active_release_id,
                allow_downgrade=allow_downgrade,
                package_type="patch",
                package_id=str(patch.get("patch_id") or ""),
            )

    def rollback(
        self,
        *,
        target_release_id: str | None = None,
        restore_backup_id: str | None = None,
        health_check: HealthCheck | None = None,
        external_restore: RestoreStep | None = None,
        quiesce: ActivationStep | None = None,
        activate: ActivationStep | None = None,
        deployment_driver: DeploymentDriver | None = None,
        trusted_public_keys: Sequence[str | Path | bytes] = (),
        require_signature: bool = False,
    ) -> ReleaseResult:
        with self._lock():
            self._require_finished()
            current = self.active()
            if current is None:
                raise ReleaseError("no active release to roll back")
            selected = target_release_id or str(current.get("previous_release_id") or "")
            if not selected:
                if quiesce is not None and activate is None and deployment_driver is None:
                    raise PreflightError(
                        "a rollback quiesce/stop-write command requires an activation/restart command"
                    )
                raise ReleaseError("no previous release is recorded")
            target_path = (self.releases_dir / selected).resolve()
            if not _inside(self.releases_dir.resolve(), target_path) or not target_path.is_dir():
                raise ReleaseError(f"rollback release not found: {selected}")
            manifest = verify_release(
                target_path,
                trusted_public_keys=trusted_public_keys,
                require_signature=require_signature,
            )
            previous_target = str(current["release_id"])
            current_path = self.releases_dir / previous_target
            current_manifest = verify_release(current_path)
            schema_changes = str(current_manifest.get("schema_revision") or "") != str(
                manifest.get("schema_revision") or ""
            )
            if schema_changes and not restore_backup_id:
                raise PreflightError(
                    "schema-changing rollback requires the matching pre-upgrade backup id"
                )
            if schema_changes and quiesce is None:
                raise PreflightError(
                    "schema-changing rollback requires a quiesce/stop-write command"
                )
            driver = self._deployment_driver(
                manifest,
                activation=activate,
                requested=deployment_driver,
            )
            driver.preflight(target_path, manifest)
            current_deployment = DeploymentTarget.from_dict(
                current.get("deployment"),
                fallback_path=current_path,
            )
            if current_deployment.driver != driver.name:
                raise PreflightError(
                    "changing deployment mode during rollback is not supported"
                )
            if (
                quiesce is not None
                and driver.name == "native"
                and activate is None
                and deployment_driver is None
            ):
                raise PreflightError(
                    "a rollback quiesce/stop-write command requires an activation/restart command"
                )
            if health_check is None and (
                activate is not None or deployment_driver is not None or driver.name != "native"
            ):
                raise PreflightError("runtime rollback requires a readiness health check")
            recovery_required = False
            restore_started = False
            target_deployment = None
            self.checkpoints.begin({
                "action": "rollback", "release_id": str(manifest["release_id"]),
                "version": str(manifest["version"]), "previous_release_id": previous_target,
                "manifest_sha256": _sha256_file(target_path / MANIFEST_FILENAME),
                "previous_manifest_sha256": _sha256_file(current_path / MANIFEST_FILENAME),
                "driver": driver.name, "migration_required": False,
                "restore_required": bool(restore_backup_id), "backup_id": restore_backup_id,
                "require_signature": require_signature,
                "runtime_activation": activate is not None or deployment_driver is not None or driver.name != "native",
            })
            try:
                target_deployment = self._phase("stage", lambda: driver.stage(target_path, manifest))
                if quiesce is not None:
                    recovery_required = True
                    self._phase("quiesce", lambda: quiesce(self.releases_dir / previous_target))
                recovery_required = True
                self._phase("metadata", lambda: self._set_active(
                    manifest,
                    previous_release_id=previous_target,
                    deployment=target_deployment,
                ))
                if restore_backup_id:
                    restore_started = True
                    self._phase("restore", lambda: self.restore_backup(restore_backup_id, external_restore=external_restore))
                self._phase("activate", lambda: driver.activate(target_path, manifest, target_deployment))
                health = self._phase("health", lambda: _check_health(health_check, target_path))
            except CheckpointError:
                raise
            except Exception as exc:
                recovery_errors: list[str] = []
                recovery_verified = False
                if restore_started:
                    # The pre-upgrade database may already be restored, even when
                    # restore raised. Restarting the newer application is unsafe.
                    recovery_errors.append(
                        "backup restore was attempted; original release database compatibility is unknown"
                    )
                elif recovery_required:
                    try:
                        self._phase("undo_metadata", lambda: self._restore_active_metadata(current))
                        self._phase("undo_activate", lambda: driver.rollback(current_path, current_manifest, current_deployment))
                        if self._phase("undo_health", lambda: _check_health(health_check, current_path)) is None:
                            raise ReleaseError("original release health check not configured")
                        recovery_verified = True
                    except CheckpointError:
                        raise
                    except Exception as recovery_error:
                        recovery_errors.append(f"original release recovery failed: {recovery_error}")
                detail = "; ".join([str(exc), *recovery_errors])
                status = "needs_intervention" if recovery_errors else "failed"
                self._append_history({
                    "action": "rollback",
                    "status": status,
                    "release_id": str(manifest["release_id"]),
                    "version": str(manifest["version"]),
                    "previous_release_id": previous_target,
                    "backup_id": restore_backup_id,
                    "detail": detail,
                    "original_error": str(exc),
                    "rollback_errors": recovery_errors,
                    "rollback_health_status": "healthy" if recovery_verified else "not_verified",
                    "deployment_type": driver.name,
                    "target_ref": target_deployment.target_ref if target_deployment else "",
                    "schema_revision": str(manifest.get("schema_revision") or ""),
                    "recorded_at": _utc_now(),
                })
                self._finish_transaction(status)
                outcome = "original release was restored and health verified" if recovery_verified else (
                    "recovery is not verified; manual intervention required"
                    if status == "needs_intervention" else "active release was not changed"
                )
                raise ReleaseError(f"rollback failed; {outcome}: {detail}") from exc
            result = ReleaseResult(
                "rollback",
                "succeeded" if health is not None else "unverified",
                str(manifest["release_id"]),
                str(manifest["version"]),
                previous_target,
                restore_backup_id,
                health.detail if health is not None else "health check not configured; runtime health is unverified",
                deployment_type=driver.name,
                target_ref=target_deployment.target_ref,
                schema_revision=str(manifest.get("schema_revision") or ""),
                health_status="healthy" if health is not None else "not_checked",
            )
            self._append_history({**result.__dict__, "recorded_at": _utc_now()})
            self._finish_transaction(result.status)
            return result
