from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.runtime.database_migrations import (
    ExternalDatabaseAdapter,
    MigrationRunner,
    build_migration_manifest,
    discover_migration_plan,
    load_migration_bundle,
)
from app.runtime.release_manager import (
    ContainerDeploymentDriver,
    HealthResult,
    ManifestError,
    PreflightError,
    ReleaseError,
    ReleaseManager,
    build_release_manifest,
)
from scripts.release_manager import build_parser


def _container_package(root: Path, version: str, digest_character: str) -> Path:
    package = root / f"container-{version}"
    (package / "deploy").mkdir(parents=True)
    (package / "images").mkdir()
    (package / "deploy" / "compose.yml").write_text(
        "services:\n  platform:\n    image: ${TRACEMIND_IMAGE}\n",
        encoding="utf-8",
    )
    (package / "images" / "platform.oci.tar").write_bytes(f"oci-{version}".encode())
    build_release_manifest(
        package,
        version=version,
        deployment_type="container",
        container_runtime="docker",
        container_image=f"registry.local/tracemind/platform@sha256:{digest_character * 64}",
        container_image_archive="images/platform.oci.tar",
        container_compose_file="deploy/compose.yml",
        container_service="platform",
        container_project="tracemind",
    )
    return package


def test_container_driver_uses_digest_and_rolls_back_failed_upgrade(tmp_path: Path) -> None:
    first = _container_package(tmp_path, "1.0.0", "a")
    second = _container_package(tmp_path, "1.1.0", "b")
    commands: list[tuple[list[str], dict[str, str]]] = []

    def command_runner(arguments, _cwd, environment, _timeout):
        commands.append((list(arguments), dict(environment)))
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    driver = ContainerDeploymentDriver(command_runner=command_runner)
    manager = ReleaseManager(tmp_path / "installation")
    first_result = manager.install(first, deployment_driver=driver, health_check=lambda _path: True)

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install(
            second,
            deployment_driver=driver,
            health_check=lambda path: HealthResult(path.name == first_result.release_id, "readiness failed"),
        )

    active = manager.active()
    assert active is not None
    assert active["release_id"] == first_result.release_id
    assert active["deployment"]["image_digest"].endswith("a" * 64)
    compose_images = [
        environment["TRACEMIND_IMAGE"]
        for arguments, environment in commands
        if len(arguments) > 1 and arguments[1] == "compose" and "up" in arguments
    ]
    assert [value[-64:] for value in compose_images] == ["a" * 64, "b" * 64, "a" * 64]
    assert all("@sha256:" in value for value in compose_images)


def test_container_manifest_rejects_mutable_image_tag(tmp_path: Path) -> None:
    package = tmp_path / "container"
    (package / "deploy").mkdir(parents=True)
    (package / "deploy" / "compose.yml").write_text(
        "services:\n  platform:\n    image: ${TRACEMIND_IMAGE}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="repository@sha256"):
        build_release_manifest(
            package,
            version="1.0.0",
            deployment_type="container",
            container_image="registry.local/tracemind/platform:latest",
            container_compose_file="deploy/compose.yml",
            container_service="platform",
            container_project="tracemind",
        )


def _migration_bundle(
    root: Path,
    name: str,
    source: str,
    target: str,
    *,
    reversible: bool = True,
) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "up.sql").write_text(f"-- {source} -> {target}\nselect 1;\n", encoding="utf-8")
    (bundle / "verify.sql").write_text("select 1;\n", encoding="utf-8")
    if reversible:
        (bundle / "down.sql").write_text(f"-- {target} -> {source}\nselect 1;\n", encoding="utf-8")
    build_migration_manifest(
        bundle,
        migration_id=name,
        from_revision=source,
        to_revision=target,
        engines=["mysql", "postgresql"],
        reversible=reversible,
    )
    return bundle


def test_migration_plan_checksums_journal_and_reapply_after_rollback(tmp_path: Path) -> None:
    root = tmp_path / "migrations"
    first = _migration_bundle(root, "001-to-002", "001", "002")
    _migration_bundle(root, "002-to-003", "002", "003")
    plan = discover_migration_plan(
        root,
        from_revision="001",
        to_revision="003",
        engine="postgresql",
    )
    assert [item.migration_id for item in plan] == ["001-to-002", "002-to-003"]

    class Executor:
        engine = "postgresql"

        def __init__(self) -> None:
            self.scripts: list[str] = []

        def execute_script(self, script: Path, *, timeout_seconds: int) -> None:
            assert timeout_seconds == 900
            self.scripts.append(script.name)

    executor = Executor()
    runner = MigrationRunner(executor, tmp_path / "migration-journal.json")
    runner.apply(plan)
    runner.apply(plan)
    assert executor.scripts == ["up.sql", "verify.sql", "up.sql", "verify.sql"]

    runner.rollback(plan)
    runner.apply(plan)
    assert executor.scripts[-6:] == [
        "down.sql",
        "down.sql",
        "up.sql",
        "verify.sql",
        "up.sql",
        "verify.sql",
    ]
    journal = json.loads((tmp_path / "migration-journal.json").read_text(encoding="utf-8"))
    assert journal["current_revision"] == "003"

    (first / "up.sql").write_text("select 2;\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="integrity check failed"):
        load_migration_bundle(first, engine="postgresql")


def test_external_postgresql_backup_is_verified_and_credentials_stay_out_of_commands(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str], Path | None]] = []

    def command_runner(arguments, environment, _cwd, _timeout, stdin_path):
        arguments = list(arguments)
        calls.append((arguments, dict(environment), stdin_path))
        if Path(arguments[0]).name == "pg_dump":
            output = Path(arguments[arguments.index("--file") + 1])
            output.write_bytes(b"PGDMP\x00verified")
        return subprocess.CompletedProcess(arguments, 0, "verified", "")

    adapter = ExternalDatabaseAdapter(
        # secret-scan: allow - synthetic fixture DSN; no host is ever contacted
        "postgresql+psycopg://release_user:s3cret@db.internal:5433/platform",  # secret-scan: allow
        command_runner=command_runner,
    )
    destination = tmp_path / "backup"
    adapter.backup(destination)
    adapter.restore(destination)
    script = tmp_path / "up.sql"
    script.write_text("select 1;\n", encoding="utf-8")
    adapter.execute_script(script, timeout_seconds=30)

    assert (destination / "platform.pg.dump").is_file()
    assert any(Path(arguments[0]).name == "pg_restore" for arguments, _, _ in calls)
    assert all("s3cret" not in " ".join(arguments) for arguments, _, _ in calls)
    assert all(environment.get("PGPASSWORD") == "s3cret" for _, environment, _ in calls)


def test_external_mysql_backup_and_restore_use_verified_artifact(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str], Path | None]] = []

    def command_runner(arguments, environment, _cwd, _timeout, stdin_path):
        arguments = list(arguments)
        calls.append((arguments, dict(environment), stdin_path))
        if Path(arguments[0]).name == "mysqldump":
            output_argument = next(item for item in arguments if item.startswith("--result-file="))
            Path(output_argument.split("=", 1)[1]).write_text("-- verified dump\nselect 1;\n", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    adapter = ExternalDatabaseAdapter(
        # secret-scan: allow - synthetic fixture DSN; no host is ever contacted
        "mysql+pymysql://release_user:s3cret@db.internal:3307/platform",  # secret-scan: allow
        command_runner=command_runner,
    )
    destination = tmp_path / "backup"
    adapter.backup(destination)
    adapter.restore(destination)

    dump_arguments = next(arguments for arguments, _, _ in calls if Path(arguments[0]).name == "mysqldump")
    restore_call = next(item for item in calls if Path(item[0][0]).name == "mysql")
    assert dump_arguments[-1] == "platform"
    assert not any(item.startswith("--database=") for item in dump_arguments)
    assert restore_call[2] == destination / "platform.mysql.sql"
    assert all("s3cret" not in " ".join(arguments) for arguments, _, _ in calls)
    assert all(environment.get("MYSQL_PWD") == "s3cret" for _, environment, _ in calls)


def test_failed_readiness_prefers_reversible_down_migration_over_database_restore(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "app").write_text("1", encoding="utf-8")
    (second / "app").write_text("2", encoding="utf-8")
    build_release_manifest(first, version="1.0.0")
    build_release_manifest(second, version="1.1.0")
    manager = ReleaseManager(tmp_path / "installation")
    initial = manager.install(first)
    events: list[str] = []

    def backup(destination: Path) -> None:
        events.append("backup")
        (destination / "platform.sql").write_text("backup", encoding="utf-8")

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install(
            second,
            database_url="postgresql://configured/platform",
            external_backup=backup,
            external_restore=lambda _source: events.append("restore"),
            migration=lambda _release: events.append("up"),
            migration_rollback=lambda _release: events.append("down"),
            health_check=lambda release: release.name == initial.release_id,
        )

    assert events == ["backup", "up", "down"]


def test_cli_exposes_container_and_versioned_migration_controls() -> None:
    parser = build_parser()
    manifest = parser.parse_args(
        [
            "manifest",
            "--source",
            "/tmp/release",
            "--version",
            "1.2.3",
            "--deployment-type",
            "container",
            "--container-image",
            f"registry/platform@sha256:{'a' * 64}",
            "--container-compose-file",
            "deploy/compose.yml",
        ]
    )
    migration = parser.parse_args(
        [
            "migration-manifest",
            "--bundle",
            "/tmp/migration",
            "--migration-id",
            "001-to-002",
            "--to-revision",
            "002",
            "--engine",
            "postgresql",
            "--reversible",
        ]
    )
    install = parser.parse_args(
        [
            "install",
            "--install-root",
            "/tmp/install",
            "--package",
            "/tmp/release",
            "--database-url-env",
            "PLATFORM_DATABASE_URL",
        ]
    )
    assert manifest.deployment_type == "container"
    assert migration.reversible is True
    assert install.database_url_env == "PLATFORM_DATABASE_URL"


def test_container_activation_without_health_check_is_rejected(tmp_path: Path) -> None:
    first = _container_package(tmp_path, "1.0.0", "a")
    second = _container_package(tmp_path, "1.1.0", "b")
    commands = []

    def command_runner(arguments, _cwd, _environment, _timeout):
        commands.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    driver = ContainerDeploymentDriver(command_runner=command_runner)
    manager = ReleaseManager(tmp_path / "installation")
    with pytest.raises(PreflightError, match="readiness health check"):
        manager.install(first, deployment_driver=driver)
    assert commands == []
    assert manager.active() is None
    manager.install(first, deployment_driver=driver, health_check=lambda _path: True)
    manager.install(second, deployment_driver=driver, health_check=lambda _path: True)
    commands.clear()
    current = manager.active()
    with pytest.raises(PreflightError, match="readiness health check"):
        manager.rollback(deployment_driver=driver)
    assert commands == []
    assert manager.active() == current


@pytest.mark.parametrize("failure", ["activation", "health"])
def test_container_recovery_failure_requires_intervention(tmp_path: Path, failure: str) -> None:
    first = _container_package(tmp_path, "1.0.0", "a")
    second = _container_package(tmp_path, "1.1.0", "b")
    fail_old_activation = False
    checks = []

    def command_runner(arguments, _cwd, environment, _timeout):
        failed = (
            fail_old_activation
            and "up" in arguments
            and environment["TRACEMIND_IMAGE"].endswith("a" * 64)
        )
        return subprocess.CompletedProcess(arguments, 1 if failed else 0, "", "old container failed" if failed else "")

    driver = ContainerDeploymentDriver(command_runner=command_runner)
    manager = ReleaseManager(tmp_path / "installation")
    initial = manager.install(first, deployment_driver=driver, health_check=lambda _path: True)
    fail_old_activation = failure == "activation"

    def health(path: Path) -> HealthResult:
        checks.append(path.name)
        return HealthResult(False, "container readiness failed")

    with pytest.raises(ReleaseError, match="manual intervention") as error:
        manager.install(second, deployment_driver=driver, health_check=health)
    assert "active release was restored" not in str(error.value)
    assert manager.state()["history"][-1]["status"] == "needs_intervention"
    assert (initial.release_id in checks) == (failure == "health")


@pytest.mark.parametrize("restore_fails", [False, True])
def test_failed_down_migration_falls_back_to_backup_and_checks_recovery(tmp_path: Path, restore_fails: bool) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "app").write_text("1", encoding="utf-8")
    (second / "app").write_text("2", encoding="utf-8")
    build_release_manifest(first, version="1.0.0")
    build_release_manifest(second, version="1.1.0")
    manager = ReleaseManager(tmp_path / "installation")
    initial = manager.install(first)
    events = []

    def backup(destination: Path) -> None:
        events.append("backup")
        (destination / "platform.sql").write_text("backup", encoding="utf-8")

    def down(_release: Path) -> None:
        events.append("down")
        raise RuntimeError("down migration failed")

    def restore(_source: Path) -> None:
        events.append("restore")
        if restore_fails:
            raise RuntimeError("database restore failed")

    def health(path: Path) -> HealthResult:
        events.append("old-health" if path.name == initial.release_id else "new-health")
        return HealthResult(path.name == initial.release_id, "new readiness failed")

    with pytest.raises(ReleaseError) as error:
        manager.install(
            second,
            database_url="postgresql://configured/platform",
            external_backup=backup,
            external_restore=restore,
            migration=lambda _release: events.append("up"),
            migration_rollback=down,
            activate=lambda path: events.append("old-activate" if path.name == initial.release_id else "new-activate"),
            health_check=health,
        )
    assert events[:6] == ["backup", "up", "new-activate", "new-health", "down", "restore"]
    record = manager.state()["history"][-1]
    assert record["original_error"] == "new readiness failed"
    assert "down migration failed" in record["detail"]
    if restore_fails:
        assert events[6:] == []
        assert record["status"] == "needs_intervention"
        assert "active release was restored" not in str(error.value)
    else:
        assert events[6:] == ["old-activate", "old-health"]
        assert record["status"] == "rolled_back"
        assert record["rollback_health_status"] == "healthy"


@pytest.mark.parametrize("restore_fails", [False, True])
def test_manual_rollback_does_not_restart_new_release_after_restore_attempt(tmp_path: Path, restore_fails: bool) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "app").write_text("1", encoding="utf-8")
    (second / "app").write_text("2", encoding="utf-8")
    build_release_manifest(first, version="1.0.0", schema_revision="001")
    build_release_manifest(second, version="1.1.0", schema_revision="002")
    manager = ReleaseManager(tmp_path / "installation")
    old = manager.install(first)
    current = manager.install(second)

    def backup(destination: Path) -> None:
        (destination / "platform.sql").write_text("backup", encoding="utf-8")

    backup_id = manager.create_pre_upgrade_backup(external_backup=backup)
    events = []

    def restore(_source: Path) -> None:
        events.append("restore")
        if restore_fails:
            raise RuntimeError("restore partially failed")

    with pytest.raises(ReleaseError, match="manual intervention") as error:
        manager.rollback(
            restore_backup_id=backup_id,
            external_restore=restore,
            quiesce=lambda _path: events.append("quiesce"),
            activate=lambda path: events.append(path.name),
            health_check=lambda _path: HealthResult(False, "old readiness failed"),
        )
    assert events == (["quiesce", "restore"] if restore_fails else ["quiesce", "restore", old.release_id])
    assert current.release_id not in events
    assert "remains active" not in str(error.value)
    assert manager.state()["history"][-1]["status"] == "needs_intervention"
