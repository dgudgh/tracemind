#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.runtime.release_manager import (  # noqa: E402
    HealthResult,
    PreflightError,
    ReleaseError,
    ReleaseManager,
    build_patch_package,
    build_release_manifest,
    generate_release_signing_keypair,
    verify_patch_package,
    verify_release,
)
from app.runtime.database_migrations import (  # noqa: E402
    ExternalDatabaseAdapter,
    MigrationRunner,
    build_migration_manifest,
    discover_migration_plan,
)


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _command_step(command: str, timeout: int, *, directory_env: str = ""):
    arguments = shlex.split(command, posix=sys.platform != "win32")
    if not arguments:
        raise ValueError("command cannot be empty")

    def run(release_dir: Path) -> None:
        environment = None
        if directory_env:
            import os

            environment = os.environ.copy()
            environment[directory_env] = str(release_dir)
        completed = subprocess.run(
            arguments,
            cwd=release_dir,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "migration command failed").strip()
            raise ReleaseError(detail[-2000:])

    return run


def _http_health_check(url: str, timeout: int, attempts: int, interval: float):
    def check(_release_dir: Path) -> HealthResult:
        last_error = ""
        for attempt in range(attempts):
            try:
                request = Request(url, headers={"User-Agent": "TraceMind-Release-Manager/1"})
                with urlopen(request, timeout=timeout) as response:
                    payload = response.read(8192).decode("utf-8", errors="replace")
                    if 200 <= response.status < 300:
                        return HealthResult(True, f"{url} returned HTTP {response.status}")
                    last_error = f"HTTP {response.status}: {payload[:300]}"
            except (HTTPError, URLError, OSError) as exc:
                last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(interval)
        return HealthResult(False, f"{url} did not become ready: {last_error}")

    return check


def _manager(args: argparse.Namespace) -> ReleaseManager:
    return ReleaseManager(args.install_root)


def _result_payload(result) -> dict[str, object]:
    return dict(result.__dict__)


def _database_url(args: argparse.Namespace) -> str:
    value = str(getattr(args, "database_url", "") or "").strip()
    environment_name = str(getattr(args, "database_url_env", "") or "").strip()
    if value:
        return value
    return str(os.environ.get(environment_name, "") if environment_name else "").strip()


def _migration_steps(
    manager: ReleaseManager,
    adapter: ExternalDatabaseAdapter,
    *,
    from_revision: str,
    to_revision: str,
    migrations_directory: str,
):
    relative = Path(migrations_directory)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("migration directory must be a safe release-relative path")
    runner = MigrationRunner(adapter, manager.shared_dir / "db" / "migration-journal.json")
    cached: dict[str, object] = {}

    def plan(release_dir: Path):
        key = str(release_dir.resolve())
        if key not in cached:
            root = (release_dir / relative).resolve(strict=True)
            try:
                root.relative_to(release_dir.resolve())
            except ValueError as exc:
                raise ReleaseError("migration directory escapes the signed release") from exc
            cached[key] = discover_migration_plan(
                root,
                from_revision=from_revision,
                to_revision=to_revision,
                engine=adapter.engine,
            )
        return cached[key]

    def apply(release_dir: Path) -> None:
        runner.apply(plan(release_dir))

    def rollback(release_dir: Path) -> None:
        runner.rollback(plan(release_dir))

    def preflight(release_dir: Path) -> None:
        plan(release_dir)

    return apply, rollback, preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TraceMind versioned install, upgrade and rollback manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen", help="create an Ed25519 release signing key pair")
    keygen.add_argument("--private-key", required=True)
    keygen.add_argument("--public-key", required=True)

    manifest = subparsers.add_parser("manifest", help="create a release manifest")
    manifest.add_argument("--source", required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--product", default="TraceMind AI")
    manifest.add_argument("--platform", default="any")
    manifest.add_argument("--arch", default="any")
    manifest.add_argument("--schema-revision", default="")
    manifest.add_argument("--entrypoint", default="")
    manifest.add_argument("--deployment-type", choices=("native", "container"), default="native")
    manifest.add_argument("--container-runtime", choices=("docker", "podman"), default="docker")
    manifest.add_argument("--container-image", default="")
    manifest.add_argument("--container-image-archive", default="")
    manifest.add_argument("--container-compose-file", default="")
    manifest.add_argument("--container-service", default="tracemind")
    manifest.add_argument("--container-project", default="tracemind")
    manifest.add_argument("--signing-private-key", default="")

    migration_manifest = subparsers.add_parser(
        "migration-manifest",
        help="create and checksum a versioned database migration bundle",
    )
    migration_manifest.add_argument("--bundle", required=True)
    migration_manifest.add_argument("--migration-id", required=True)
    migration_manifest.add_argument("--from-revision", default="")
    migration_manifest.add_argument("--to-revision", required=True)
    migration_manifest.add_argument("--engine", action="append", required=True)
    migration_manifest.add_argument("--reversible", action="store_true")
    migration_manifest.add_argument("--no-backup-required", action="store_true")
    migration_manifest.add_argument("--timeout", type=int, default=900)
    migration_manifest.add_argument("--up-script", default="up.sql")
    migration_manifest.add_argument("--verify-script", default="verify.sql")
    migration_manifest.add_argument("--down-script", default="down.sql")
    migration_manifest.add_argument("--down-verify-script", default="")

    migration_plan = subparsers.add_parser(
        "migration-plan",
        help="verify migration checksums and resolve one unambiguous revision path",
    )
    migration_plan.add_argument("--root", required=True)
    migration_plan.add_argument("--from-revision", default="")
    migration_plan.add_argument("--to-revision", required=True)
    migration_plan.add_argument("--engine", required=True)

    verify = subparsers.add_parser("verify", help="verify every file against a release manifest")
    verify.add_argument("--source", required=True)
    verify.add_argument("--trusted-public-key", action="append", default=[])
    verify.add_argument("--require-signature", action="store_true")

    patch_build = subparsers.add_parser("patch-build", help="build a signed delta patch")
    patch_build.add_argument("--base", required=True)
    patch_build.add_argument("--target", required=True)
    patch_build.add_argument("--output", required=True)
    patch_build.add_argument("--signing-private-key", required=True)
    patch_build.add_argument("--release-notes", default="")
    patch_build.add_argument("--no-restart", action="store_true")
    patch_build.add_argument("--allow-downgrade", action="store_true")

    patch_verify = subparsers.add_parser("patch-verify", help="verify a signed delta patch")
    patch_verify.add_argument("--package", required=True)
    patch_verify.add_argument("--trusted-public-key", action="append", required=True)

    patch_preflight = subparsers.add_parser(
        "patch-preflight",
        help="verify compatibility and materialize a patch without changing the active release",
    )
    patch_preflight.add_argument("--install-root", required=True)
    patch_preflight.add_argument("--package", required=True)
    patch_preflight.add_argument("--trusted-public-key", action="append", required=True)
    patch_preflight.add_argument("--allow-downgrade", action="store_true")

    for name in ("status", "install", "patch-install", "rollback", "active-path"):
        command = subparsers.add_parser(name)
        command.add_argument("--install-root", required=True)
        if name in {"install", "patch-install"}:
            command.add_argument("--package", required=True)
            command.add_argument("--database-url", default="")
            command.add_argument("--database-url-env", default="DATABASE_URL")
            command.add_argument("--database-client-bin-dir", default="")
            command.add_argument("--config-path", action="append", default=[])
            command.add_argument("--migration-command", default="")
            command.add_argument("--migrations-directory", default="migrations")
            command.add_argument("--migration-timeout", type=int, default=300)
            command.add_argument("--quiesce-command", default="")
            command.add_argument("--activation-command", default="")
            command.add_argument("--activation-timeout", type=int, default=120)
            command.add_argument("--backup-command", default="")
            command.add_argument("--restore-command", default="")
            command.add_argument("--backup-timeout", type=int, default=1800)
            command.add_argument("--health-url", default="")
            command.add_argument("--health-timeout", type=int, default=5)
            command.add_argument("--health-attempts", type=int, default=12)
            command.add_argument("--health-interval", type=float, default=2.0)
            command.add_argument(
                "--trusted-public-key",
                action="append",
                default=[],
                required=name == "patch-install",
            )
            if name == "install":
                command.add_argument("--require-signature", action="store_true")
            command.add_argument("--allow-downgrade", action="store_true")
        elif name == "rollback":
            command.add_argument("--target-release-id", default="")
            command.add_argument("--restore-backup-id", default="")
            command.add_argument("--restore-command", default="")
            command.add_argument("--database-url", default="")
            command.add_argument("--database-url-env", default="DATABASE_URL")
            command.add_argument("--database-client-bin-dir", default="")
            command.add_argument("--backup-timeout", type=int, default=1800)
            command.add_argument("--quiesce-command", default="")
            command.add_argument("--activation-command", default="")
            command.add_argument("--activation-timeout", type=int, default=120)
            command.add_argument("--health-url", default="")
            command.add_argument("--health-timeout", type=int, default=5)
            command.add_argument("--health-attempts", type=int, default=12)
            command.add_argument("--health-interval", type=float, default=2.0)
            command.add_argument("--trusted-public-key", action="append", default=[])
            command.add_argument("--require-signature", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "keygen":
            _json_print(generate_release_signing_keypair(args.private_key, args.public_key))
            return 0
        if args.command == "manifest":
            _json_print(
                build_release_manifest(
                    args.source,
                    version=args.version,
                    product=args.product,
                    target_platform=args.platform,
                    target_arch=args.arch,
                    schema_revision=args.schema_revision,
                    entrypoint=args.entrypoint,
                    deployment_type=args.deployment_type,
                    container_runtime=args.container_runtime,
                    container_image=args.container_image,
                    container_image_archive=args.container_image_archive,
                    container_compose_file=args.container_compose_file,
                    container_service=args.container_service,
                    container_project=args.container_project,
                    signing_private_key=args.signing_private_key or None,
                )
            )
            return 0
        if args.command == "migration-manifest":
            _json_print(
                build_migration_manifest(
                    args.bundle,
                    migration_id=args.migration_id,
                    from_revision=args.from_revision,
                    to_revision=args.to_revision,
                    engines=args.engine,
                    reversible=args.reversible,
                    backup_required=not args.no_backup_required,
                    timeout_seconds=args.timeout,
                    up_script=args.up_script,
                    verify_script=args.verify_script,
                    down_script=args.down_script,
                    down_verify_script=args.down_verify_script,
                )
            )
            return 0
        if args.command == "migration-plan":
            plan = discover_migration_plan(
                args.root,
                from_revision=args.from_revision,
                to_revision=args.to_revision,
                engine=args.engine,
            )
            _json_print(
                {
                    "status": "ready",
                    "from_revision": args.from_revision,
                    "to_revision": args.to_revision,
                    "engine": args.engine,
                    "migrations": [
                        {
                            "migration_id": item.migration_id,
                            "from_revision": item.from_revision,
                            "to_revision": item.to_revision,
                            "checksum": item.checksum,
                            "reversible": item.reversible,
                        }
                        for item in plan
                    ],
                }
            )
            return 0
        if args.command == "verify":
            _json_print(
                verify_release(
                    args.source,
                    trusted_public_keys=args.trusted_public_key,
                    require_signature=args.require_signature,
                )
            )
            return 0
        if args.command == "patch-build":
            _json_print(
                build_patch_package(
                    args.base,
                    args.target,
                    args.output,
                    signing_private_key=args.signing_private_key,
                    release_notes=args.release_notes,
                    restart_required=not args.no_restart,
                    allow_downgrade=args.allow_downgrade,
                )
            )
            return 0
        if args.command == "patch-verify":
            _json_print(
                verify_patch_package(
                    args.package,
                    trusted_public_keys=args.trusted_public_key,
                )
            )
            return 0

        manager = _manager(args)
        if args.command == "patch-preflight":
            _json_print(
                manager.preflight_patch(
                    args.package,
                    trusted_public_keys=args.trusted_public_key,
                    allow_downgrade=args.allow_downgrade,
                )
            )
            return 0
        if args.command == "status":
            _json_print({"active": manager.active(), "state": manager.state()})
            return 0
        if args.command == "active-path":
            active_path = manager.active_release_path()
            if active_path is None:
                raise ReleaseError("no active release")
            print(active_path)
            return 0

        health_check = None
        if args.health_url:
            health_check = _http_health_check(
                args.health_url,
                args.health_timeout,
                args.health_attempts,
                args.health_interval,
            )
        if args.command in {"install", "patch-install"}:
            database_url = _database_url(args)
            if args.command == "patch-install":
                patch_manifest = verify_patch_package(
                    args.package,
                    trusted_public_keys=args.trusted_public_key,
                )
                schema = patch_manifest.get("schema")
                if not isinstance(schema, dict):
                    raise ReleaseError("patch schema transition metadata is missing")
                from_revision = str(schema.get("from") or "")
                to_revision = str(schema.get("to") or "")
            else:
                target_manifest = verify_release(
                    args.package,
                    trusted_public_keys=args.trusted_public_key,
                    require_signature=args.require_signature,
                )
                active_path = manager.active_release_path()
                previous_manifest = verify_release(active_path) if active_path is not None else {}
                from_revision = str(previous_manifest.get("schema_revision") or "")
                to_revision = str(target_manifest.get("schema_revision") or "")

            migration_required = from_revision != to_revision
            if migration_required and not database_url:
                raise PreflightError(
                    "schema revision changes require the platform database URL for backup"
                )
            adapter = None
            is_sqlite = database_url.startswith("sqlite:///")
            if database_url and not is_sqlite and (migration_required or args.migration_command):
                adapter = ExternalDatabaseAdapter(
                    database_url,
                    client_bin_dir=args.database_client_bin_dir or None,
                )
                adapter.preflight()

            migration_rollback = None
            migration_preflight = None
            if args.migration_command:
                migration = _command_step(args.migration_command, args.migration_timeout)
            elif migration_required:
                if adapter is None:
                    raise PreflightError(
                        "automatic migration bundles require an external MySQL or PostgreSQL database"
                    )
                migration, migration_rollback, migration_preflight = _migration_steps(
                    manager,
                    adapter,
                    from_revision=from_revision,
                    to_revision=to_revision,
                    migrations_directory=args.migrations_directory,
                )
            else:
                migration = None
            quiesce = (
                _command_step(args.quiesce_command, args.activation_timeout, directory_env="TRACEMIND_RELEASE_DIR")
                if args.quiesce_command
                else None
            )
            if migration_required and manager.active() is not None and quiesce is None:
                raise PreflightError(
                    "schema revision changes require --quiesce-command before backup and migration"
                )
            activation = (
                _command_step(args.activation_command, args.activation_timeout, directory_env="TRACEMIND_RELEASE_DIR")
                if args.activation_command
                else None
            )
            backup_step = (
                _command_step(args.backup_command, args.backup_timeout, directory_env="TRACEMIND_BACKUP_DIR")
                if args.backup_command
                else (adapter.backup if adapter is not None else None)
            )
            restore_step = (
                _command_step(args.restore_command, args.backup_timeout, directory_env="TRACEMIND_BACKUP_DIR")
                if args.restore_command
                else (adapter.restore if adapter is not None else None)
            )
            if bool(args.backup_command) != bool(args.restore_command):
                raise PreflightError(
                    "custom external database protection requires both backup and restore commands"
                )
            install_arguments = {
                "database_url": database_url,
                "config_paths": args.config_path,
                "migration": migration,
                "migration_rollback": migration_rollback,
                "migration_preflight": migration_preflight,
                "health_check": health_check,
                "external_backup": backup_step,
                "external_restore": restore_step,
                "quiesce": quiesce,
                "activate": activation,
                "trusted_public_keys": args.trusted_public_key,
                "allow_downgrade": args.allow_downgrade,
            }
            if args.command == "patch-install":
                result = manager.install_patch(args.package, **install_arguments)
            else:
                result = manager.install(
                    args.package,
                    require_signature=args.require_signature,
                    **install_arguments,
                )
            _json_print(_result_payload(result))
            return 0
        if args.command == "rollback":
            database_url = _database_url(args)
            adapter = None
            if database_url and not database_url.startswith("sqlite:///"):
                adapter = ExternalDatabaseAdapter(
                    database_url,
                    client_bin_dir=args.database_client_bin_dir or None,
                )
                adapter.preflight()
            restore_step = (
                _command_step(args.restore_command, args.backup_timeout, directory_env="TRACEMIND_BACKUP_DIR")
                if args.restore_command
                else (adapter.restore if adapter is not None else None)
            )
            activation = (
                _command_step(args.activation_command, args.activation_timeout, directory_env="TRACEMIND_RELEASE_DIR")
                if args.activation_command
                else None
            )
            quiesce = (
                _command_step(args.quiesce_command, args.activation_timeout, directory_env="TRACEMIND_RELEASE_DIR")
                if args.quiesce_command
                else None
            )
            result = manager.rollback(
                target_release_id=args.target_release_id or None,
                restore_backup_id=args.restore_backup_id or None,
                health_check=health_check,
                external_restore=restore_step,
                quiesce=quiesce,
                activate=activation,
                trusted_public_keys=args.trusted_public_key,
                require_signature=args.require_signature,
            )
            _json_print(_result_payload(result))
            return 0
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release operation failed: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
