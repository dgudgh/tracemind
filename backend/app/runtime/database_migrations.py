from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.runtime.release_manager import ManifestError, PreflightError, ReleaseError


MIGRATION_FILENAME = "migration.json"
MIGRATION_FORMAT_VERSION = 1
MIGRATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _normalize_engine(value: str) -> str:
    normalized = value.strip().lower().split("+", 1)[0]
    aliases = {"postgres": "postgresql", "mariadb": "mysql"}
    return aliases.get(normalized, normalized)


def _safe_script(root: Path, raw_path: object, *, required: bool) -> Path | None:
    value = str(raw_path or "")
    if not value and not required:
        return None
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ManifestError(f"unsafe migration script path: {value!r}")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ManifestError(f"migration script is missing or unsafe: {value}")
    if path.stat().st_size <= 0:
        raise ManifestError(f"migration script is empty: {value}")
    return path


@dataclass(frozen=True)
class MigrationBundle:
    root: Path
    migration_id: str
    from_revision: str
    to_revision: str
    engines: tuple[str, ...]
    reversible: bool
    backup_required: bool
    timeout_seconds: int
    up_script: Path
    verify_script: Path
    down_script: Path | None
    down_verify_script: Path | None
    checksum: str


def build_migration_manifest(
    bundle_dir: str | Path,
    *,
    migration_id: str,
    from_revision: str,
    to_revision: str,
    engines: Sequence[str],
    reversible: bool = False,
    backup_required: bool = True,
    timeout_seconds: int = 900,
    up_script: str = "up.sql",
    verify_script: str = "verify.sql",
    down_script: str = "down.sql",
    down_verify_script: str = "",
) -> dict[str, object]:
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    normalized_id = migration_id.strip()
    if not MIGRATION_ID_PATTERN.fullmatch(normalized_id):
        raise ManifestError("migration id is missing or unsafe")
    normalized_from = from_revision.strip()
    normalized_to = to_revision.strip()
    if normalized_from and not REVISION_PATTERN.fullmatch(normalized_from):
        raise ManifestError("migration source revision is unsafe")
    if not REVISION_PATTERN.fullmatch(normalized_to) or normalized_from == normalized_to:
        raise ManifestError("migration target revision is missing, unsafe, or unchanged")
    normalized_engines = tuple(sorted({_normalize_engine(item) for item in engines if item.strip()}))
    if not normalized_engines or any(item not in {"mysql", "postgresql"} for item in normalized_engines):
        raise ManifestError("migration engines must contain mysql and/or postgresql")
    if timeout_seconds < 1 or timeout_seconds > 86400:
        raise ManifestError("migration timeout must be between 1 and 86400 seconds")

    scripts: dict[str, dict[str, object]] = {}
    effective_down_script = down_script if reversible or (root / down_script).is_file() else ""
    for name, raw_path, required in (
        ("up", up_script, True),
        ("verify", verify_script, True),
        ("down", effective_down_script, reversible),
        ("down_verify", down_verify_script, False),
    ):
        path = _safe_script(root, raw_path, required=required)
        if path is not None:
            scripts[name] = {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }

    manifest: dict[str, object] = {
        "format_version": MIGRATION_FORMAT_VERSION,
        "migration_id": normalized_id,
        "from_revision": normalized_from,
        "to_revision": normalized_to,
        "engines": list(normalized_engines),
        "reversible": bool(reversible),
        "backup_required": bool(backup_required),
        "timeout_seconds": timeout_seconds,
        "scripts": scripts,
    }
    manifest["checksum"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _atomic_write_json(root / MIGRATION_FILENAME, manifest)
    return manifest


def load_migration_bundle(
    bundle_dir: str | Path,
    *,
    engine: str | None = None,
) -> MigrationBundle:
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    try:
        value = json.loads((root / MIGRATION_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"migration manifest not found: {root / MIGRATION_FILENAME}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"migration manifest is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("format_version") != MIGRATION_FORMAT_VERSION:
        raise ManifestError("unsupported migration manifest format")
    migration_id = str(value.get("migration_id") or "")
    from_revision = str(value.get("from_revision") or "")
    to_revision = str(value.get("to_revision") or "")
    if not MIGRATION_ID_PATTERN.fullmatch(migration_id):
        raise ManifestError("migration id is missing or unsafe")
    if from_revision and not REVISION_PATTERN.fullmatch(from_revision):
        raise ManifestError("migration source revision is unsafe")
    if not REVISION_PATTERN.fullmatch(to_revision) or from_revision == to_revision:
        raise ManifestError("migration target revision is missing, unsafe, or unchanged")
    raw_engines = value.get("engines")
    if not isinstance(raw_engines, list):
        raise ManifestError("migration engine list is missing")
    engines = tuple(sorted({_normalize_engine(str(item)) for item in raw_engines}))
    if not engines or any(item not in {"mysql", "postgresql"} for item in engines):
        raise ManifestError("migration engine list is invalid")
    requested_engine = _normalize_engine(engine or "")
    if requested_engine and requested_engine not in engines:
        raise PreflightError(
            f"migration {migration_id} does not support database engine {requested_engine}"
        )
    reversible = value.get("reversible") is True
    backup_required = value.get("backup_required") is not False
    timeout_seconds = int(value.get("timeout_seconds") or 0)
    if timeout_seconds < 1 or timeout_seconds > 86400:
        raise ManifestError("migration timeout is invalid")
    scripts = value.get("scripts")
    if not isinstance(scripts, dict):
        raise ManifestError("migration script inventory is missing")

    resolved: dict[str, Path | None] = {}
    for name, required in (
        ("up", True),
        ("verify", True),
        ("down", reversible),
        ("down_verify", False),
    ):
        entry = scripts.get(name)
        if entry is None and not required:
            resolved[name] = None
            continue
        if not isinstance(entry, dict):
            raise ManifestError(f"migration script entry is invalid: {name}")
        path = _safe_script(root, entry.get("path"), required=True)
        assert path is not None
        if path.stat().st_size != entry.get("size") or _sha256(path) != entry.get("sha256"):
            raise ManifestError(f"migration script integrity check failed: {name}")
        resolved[name] = path

    unsigned = {key: item for key, item in value.items() if key != "checksum"}
    expected_checksum = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if value.get("checksum") != expected_checksum:
        raise ManifestError("migration manifest checksum mismatch")
    return MigrationBundle(
        root=root,
        migration_id=migration_id,
        from_revision=from_revision,
        to_revision=to_revision,
        engines=engines,
        reversible=reversible,
        backup_required=backup_required,
        timeout_seconds=timeout_seconds,
        up_script=resolved["up"],  # type: ignore[arg-type]
        verify_script=resolved["verify"],  # type: ignore[arg-type]
        down_script=resolved["down"],
        down_verify_script=resolved["down_verify"],
        checksum=expected_checksum,
    )


def discover_migration_plan(
    migrations_root: str | Path,
    *,
    from_revision: str,
    to_revision: str,
    engine: str,
) -> list[MigrationBundle]:
    source_revision = from_revision.strip()
    target_revision = to_revision.strip()
    if source_revision == target_revision:
        return []
    root = Path(migrations_root).expanduser().resolve(strict=True)
    candidates: list[MigrationBundle] = []
    manifest_paths = sorted(root.glob(f"*/{MIGRATION_FILENAME}"))
    if (root / MIGRATION_FILENAME).is_file():
        manifest_paths.insert(0, root / MIGRATION_FILENAME)
    normalized_engine = _normalize_engine(engine)
    for manifest_path in manifest_paths:
        bundle = load_migration_bundle(manifest_path.parent)
        if normalized_engine in bundle.engines:
            candidates.append(bundle)
    if not candidates:
        raise PreflightError(f"no migration bundles were found under {root}")

    paths: list[list[MigrationBundle]] = []

    def walk(revision: str, selected: list[MigrationBundle], visited: set[str]) -> None:
        if len(paths) > 1:
            return
        if revision == target_revision:
            paths.append(list(selected))
            return
        for bundle in candidates:
            if bundle.from_revision != revision or bundle.to_revision in visited:
                continue
            walk(
                bundle.to_revision,
                [*selected, bundle],
                {*visited, bundle.to_revision},
            )

    walk(source_revision, [], {source_revision})
    if not paths:
        raise PreflightError(
            f"no migration path exists from {source_revision or '<empty>'} to {target_revision}"
        )
    if len(paths) > 1:
        raise PreflightError(
            f"multiple migration paths exist from {source_revision or '<empty>'} to {target_revision}"
        )
    plan = paths[0]
    if any(bundle.backup_required is False for bundle in plan):
        # Backup remains mandatory at the release transaction level; this flag only documents the bundle.
        pass
    return plan


DatabaseCommandRunner = Callable[
    [Sequence[str], Mapping[str, str], Path | None, int, Path | None],
    subprocess.CompletedProcess[str],
]


def _default_database_command_runner(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path | None,
    timeout: int,
    stdin_path: Path | None,
) -> subprocess.CompletedProcess[str]:
    input_handle = stdin_path.open("r", encoding="utf-8") if stdin_path is not None else None
    try:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            env=dict(environment),
            stdin=input_handle,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    finally:
        if input_handle is not None:
            input_handle.close()


class SqlScriptExecutor(Protocol):
    engine: str

    def execute_script(self, script: Path, *, timeout_seconds: int) -> None: ...


class ExternalDatabaseAdapter:
    def __init__(
        self,
        database_url: str,
        *,
        command_runner: DatabaseCommandRunner | None = None,
        command_timeout_seconds: int = 1800,
        client_bin_dir: str | Path | None = None,
    ) -> None:
        try:
            url = make_url(database_url)
        except (ArgumentError, TypeError, ValueError) as exc:
            raise PreflightError("external database URL is invalid") from exc
        self.engine = _normalize_engine(url.drivername)
        if self.engine not in {"mysql", "postgresql"}:
            raise PreflightError("automatic database protection supports MySQL and PostgreSQL only")
        if not url.database:
            raise PreflightError("external database URL must include a database name")
        self.host = url.host or "127.0.0.1"
        self.port = int(url.port or (5432 if self.engine == "postgresql" else 3306))
        self.username = url.username or ""
        self.password = url.password or ""
        self.database = url.database
        self.query = {str(key): str(value) for key, value in url.query.items()}
        self.command_runner = command_runner or _default_database_command_runner
        self.command_timeout_seconds = command_timeout_seconds
        self.client_bin_dir = Path(client_bin_dir).expanduser().resolve() if client_bin_dir else None
        self._uses_default_runner = command_runner is None

    def _executable(self, name: str) -> str:
        if self.client_bin_dir is not None:
            return str(self.client_bin_dir / name)
        return name

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.engine == "postgresql":
            if self.password:
                environment["PGPASSWORD"] = self.password
            if self.query.get("sslmode"):
                environment["PGSSLMODE"] = self.query["sslmode"]
        elif self.password:
            environment["MYSQL_PWD"] = self.password
        return environment

    def _connection_arguments(self) -> list[str]:
        if self.engine == "postgresql":
            arguments = [
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--dbname",
                self.database,
            ]
            if self.username:
                arguments.extend(["--username", self.username])
            return arguments
        arguments = [
            f"--host={self.host}",
            f"--port={self.port}",
            "--protocol=TCP",
            f"--database={self.database}",
        ]
        if self.username:
            arguments.append(f"--user={self.username}")
        return arguments

    def _mysql_dump_connection_arguments(self) -> list[str]:
        arguments = [
            f"--host={self.host}",
            f"--port={self.port}",
            "--protocol=TCP",
        ]
        if self.username:
            arguments.append(f"--user={self.username}")
        arguments.append(self.database)
        return arguments

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: int | None = None,
        stdin_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = self.command_runner(
            arguments,
            self._environment(),
            cwd,
            timeout_seconds or self.command_timeout_seconds,
            stdin_path,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "database command failed").strip()
            raise ReleaseError(f"database command failed ({Path(arguments[0]).name}): {detail[-2000:]}")
        return completed

    def preflight(self) -> None:
        names = ("pg_dump", "pg_restore", "psql") if self.engine == "postgresql" else ("mysqldump", "mysql")
        if not self._uses_default_runner:
            return
        missing = [name for name in names if shutil.which(self._executable(name)) is None]
        if missing:
            raise PreflightError(f"required database client tools are missing: {', '.join(missing)}")

    def identity(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "host": self.host,
            "port": self.port,
            "database": self.database,
        }

    def backup(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        if not destination.is_dir() or destination.is_symlink():
            raise PreflightError("external database backup directory is unsafe")
        self.preflight()
        if self.engine == "postgresql":
            artifact = destination / "platform.pg.dump"
            self._run(
                [
                    self._executable("pg_dump"),
                    "--format=custom",
                    "--no-owner",
                    "--file",
                    str(artifact),
                    *self._connection_arguments(),
                ],
                cwd=destination,
            )
            self._run(
                [self._executable("pg_restore"), "--list", str(artifact)],
                cwd=destination,
            )
        else:
            artifact = destination / "platform.mysql.sql"
            self._run(
                [
                    self._executable("mysqldump"),
                    "--single-transaction",
                    "--routines",
                    "--events",
                    "--triggers",
                    "--hex-blob",
                    f"--result-file={artifact}",
                    *self._mysql_dump_connection_arguments(),
                ],
                cwd=destination,
            )
        if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size <= 0:
            raise PreflightError("external database backup did not produce a readable artifact")
        metadata = {
            "format_version": 1,
            "created_at": _utc_now(),
            **self.identity(),
            "artifact": artifact.name,
            "size": artifact.stat().st_size,
            "sha256": _sha256(artifact),
            "verified": True,
        }
        _atomic_write_json(destination / "database-backup.json", metadata)

    def _verified_artifact(self, source: Path) -> Path:
        if not source.is_dir() or source.is_symlink():
            raise ReleaseError("external database backup directory is missing or unsafe")
        try:
            metadata = json.loads((source / "database-backup.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ReleaseError("external database backup metadata is missing or unreadable") from exc
        if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
            raise ReleaseError("external database backup metadata is invalid")
        for key, expected in self.identity().items():
            if metadata.get(key) != expected:
                raise ReleaseError(f"external database backup target does not match: {key}")
        artifact_name = str(metadata.get("artifact") or "")
        if Path(artifact_name).name != artifact_name:
            raise ReleaseError("external database backup artifact path is unsafe")
        artifact = source / artifact_name
        if (
            not artifact.is_file()
            or artifact.is_symlink()
            or artifact.stat().st_size != metadata.get("size")
            or _sha256(artifact) != metadata.get("sha256")
            or metadata.get("verified") is not True
        ):
            raise ReleaseError("external database backup artifact integrity check failed")
        return artifact

    def restore(self, source: Path) -> None:
        self.preflight()
        artifact = self._verified_artifact(source)
        if self.engine == "postgresql":
            self._run(
                [
                    self._executable("pg_restore"),
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--exit-on-error",
                    *self._connection_arguments(),
                    str(artifact),
                ],
                cwd=source,
            )
        else:
            self._run(
                [self._executable("mysql"), "--show-warnings", *self._connection_arguments()],
                cwd=source,
                stdin_path=artifact,
            )

    def execute_script(self, script: Path, *, timeout_seconds: int) -> None:
        if not script.is_file() or script.is_symlink() or script.stat().st_size <= 0:
            raise ManifestError(f"migration script is missing or unsafe: {script}")
        self.preflight()
        if self.engine == "postgresql":
            self._run(
                [
                    self._executable("psql"),
                    "--set=ON_ERROR_STOP=1",
                    f"--file={script}",
                    *self._connection_arguments(),
                ],
                cwd=script.parent,
                timeout_seconds=timeout_seconds,
            )
        else:
            self._run(
                [self._executable("mysql"), "--show-warnings", *self._connection_arguments()],
                cwd=script.parent,
                timeout_seconds=timeout_seconds,
                stdin_path=script,
            )


class MigrationRunner:
    def __init__(self, executor: SqlScriptExecutor, journal_path: str | Path) -> None:
        self.executor = executor
        self.journal_path = Path(journal_path).expanduser().resolve()

    def _journal(self) -> dict[str, object]:
        try:
            value = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"format_version": 1, "entries": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"migration journal is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("format_version") != 1 or not isinstance(value.get("entries"), list):
            raise ReleaseError("migration journal is invalid")
        return value

    def _record(self, entry: dict[str, object]) -> None:
        journal = self._journal()
        entries = list(journal.get("entries") or [])
        entries.append(entry)
        journal["entries"] = entries[-500:]
        journal["updated_at"] = _utc_now()
        if entry.get("status") == "succeeded":
            journal["current_revision"] = entry.get("to_revision")
        elif entry.get("status") == "rolled_back":
            journal["current_revision"] = entry.get("from_revision")
        _atomic_write_json(self.journal_path, journal)

    def _already_applied(self, bundle: MigrationBundle) -> bool:
        entries = self._journal().get("entries") or []
        for entry in reversed(entries):
            if (
                isinstance(entry, dict)
                and entry.get("migration_id") == bundle.migration_id
                and entry.get("checksum") == bundle.checksum
            ):
                return entry.get("status") == "succeeded"
        return False

    def apply(self, plan: Sequence[MigrationBundle]) -> None:
        for bundle in plan:
            if self._already_applied(bundle):
                continue
            base = {
                "migration_id": bundle.migration_id,
                "checksum": bundle.checksum,
                "engine": self.executor.engine,
                "from_revision": bundle.from_revision,
                "to_revision": bundle.to_revision,
            }
            self._record({**base, "status": "running", "started_at": _utc_now()})
            try:
                self.executor.execute_script(bundle.up_script, timeout_seconds=bundle.timeout_seconds)
                self.executor.execute_script(bundle.verify_script, timeout_seconds=bundle.timeout_seconds)
            except Exception as exc:
                self._record({**base, "status": "failed", "failed_at": _utc_now(), "detail": str(exc)})
                raise ReleaseError(f"migration {bundle.migration_id} failed: {exc}") from exc
            self._record({**base, "status": "succeeded", "completed_at": _utc_now()})

    def rollback(self, plan: Sequence[MigrationBundle]) -> None:
        for bundle in reversed(plan):
            if not bundle.reversible or bundle.down_script is None:
                raise PreflightError(
                    f"migration {bundle.migration_id} is not reversible; restore the pre-upgrade backup"
                )
            base = {
                "migration_id": bundle.migration_id,
                "checksum": bundle.checksum,
                "engine": self.executor.engine,
                "from_revision": bundle.from_revision,
                "to_revision": bundle.to_revision,
            }
            self._record({**base, "status": "rollback_running", "started_at": _utc_now()})
            try:
                self.executor.execute_script(bundle.down_script, timeout_seconds=bundle.timeout_seconds)
                if bundle.down_verify_script is not None:
                    self.executor.execute_script(
                        bundle.down_verify_script,
                        timeout_seconds=bundle.timeout_seconds,
                    )
            except Exception as exc:
                self._record({**base, "status": "rollback_failed", "failed_at": _utc_now(), "detail": str(exc)})
                raise ReleaseError(f"migration rollback {bundle.migration_id} failed: {exc}") from exc
            self._record({**base, "status": "rolled_back", "completed_at": _utc_now()})
