from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.runtime.release_manager import (
    ACTIVE_FILENAME,
    HealthResult,
    ManifestError,
    PreflightError,
    ReleaseError,
    ReleaseLockError,
    ReleaseManager,
    build_patch_package,
    build_release_manifest,
    generate_release_signing_keypair,
    materialize_patch_package,
    verify_patch_package,
    verify_release,
)
from scripts.release_manager import build_parser


def _package(root: Path, version: str, content: str, *, schema_revision: str = "") -> Path:
    package = root / f"package-{version}"
    package.mkdir()
    executable = package / "TraceMindAI"
    executable.write_text(content, encoding="utf-8")
    executable.chmod(0o755)
    (package / "static").mkdir()
    (package / "static" / "index.html").write_text(f"<h1>{version}</h1>", encoding="utf-8")
    build_release_manifest(
        package,
        version=version,
        target_platform="any",
        target_arch="any",
        schema_revision=schema_revision,
        entrypoint="TraceMindAI",
    )
    return package


def _signing_keys(root: Path, name: str = "release") -> tuple[Path, Path]:
    private_key = root / f"{name}.key"
    public_key = root / f"{name}.pub"
    generate_release_signing_keypair(private_key, public_key)
    return private_key, public_key


def _sqlite_database(path: Path, value: str) -> str:
    connection = sqlite3.connect(path)
    connection.execute("create table marker(value text not null)")
    connection.execute("insert into marker(value) values (?)", (value,))
    connection.commit()
    connection.close()
    return f"sqlite:///{path}"


def _read_marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("select value from marker").fetchone()[0])
    finally:
        connection.close()


def test_manifest_verifies_and_detects_tampering(tmp_path: Path) -> None:
    package = _package(tmp_path, "1.0.0", "first")
    manifest = verify_release(package)
    assert manifest["version"] == "1.0.0"

    (package / "TraceMindAI").write_text("tampered", encoding="utf-8")
    with pytest.raises(ManifestError, match="checksum mismatch|size mismatch"):
        verify_release(package)


def test_manifest_rejects_symlink_escape(tmp_path: Path) -> None:
    package = tmp_path / "unsafe"
    package.mkdir()
    (package / "payload").symlink_to("../../etc/passwd")
    with pytest.raises(ManifestError, match="escapes release root"):
        build_release_manifest(package, version="1.0.0")


def test_signed_release_requires_a_trusted_ed25519_key(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    package = _package(tmp_path, "1.0.0", "first")
    build_release_manifest(
        package,
        version="1.0.0",
        entrypoint="TraceMindAI",
        signing_private_key=private_key,
    )
    verified = verify_release(
        package,
        trusted_public_keys=[public_key],
        require_signature=True,
    )
    assert verified["signature"]["algorithm"] == "ed25519"

    _, wrong_public_key = _signing_keys(tmp_path, "wrong")
    with pytest.raises(ManifestError, match="not trusted"):
        verify_release(
            package,
            trusted_public_keys=[wrong_public_key],
            require_signature=True,
        )


def test_patch_build_verify_materialize_and_install(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    base = _package(tmp_path, "1.0.0", "base")
    (base / "unchanged.txt").write_text("stable", encoding="utf-8")
    (base / "obsolete.txt").write_text("remove me", encoding="utf-8")
    build_release_manifest(base, version="1.0.0", entrypoint="TraceMindAI")
    target = _package(tmp_path, "1.0.1", "patched")
    (target / "unchanged.txt").write_text("stable", encoding="utf-8")
    (target / "new.txt").write_text("new payload", encoding="utf-8")
    build_release_manifest(target, version="1.0.1", entrypoint="TraceMindAI")

    patch_dir = tmp_path / "patch"
    patch = build_patch_package(
        base,
        target,
        patch_dir,
        signing_private_key=private_key,
        release_notes="critical customer hotfix",
    )
    verified = verify_patch_package(patch_dir, trusted_public_keys=[public_key])
    assert verified["patch_id"] == patch["patch_id"]
    assert {item["path"] for item in verified["operations"]} == {
        "TraceMindAI",
        "new.txt",
        "obsolete.txt",
        "static/index.html",
    }
    assert not (patch_dir / "payload" / "unchanged.txt").exists()

    materialized = tmp_path / "materialized"
    materialize_patch_package(
        patch_dir,
        base,
        materialized,
        trusted_public_keys=[public_key],
    )
    assert (materialized / "TraceMindAI").read_text(encoding="utf-8") == "patched"
    assert (materialized / "new.txt").read_text(encoding="utf-8") == "new payload"
    assert (materialized / "unchanged.txt").read_text(encoding="utf-8") == "stable"
    assert not (materialized / "obsolete.txt").exists()

    manager = ReleaseManager(tmp_path / "installation")
    manager.install(base)
    activations: list[str] = []
    result = manager.install_patch(
        patch_dir,
        trusted_public_keys=[public_key],
        activate=lambda release: activations.append(str(verify_release(release)["version"])),
        health_check=lambda _release: HealthResult(True, "ready"),
    )
    assert result.action == "patch"
    assert result.package_type == "patch"
    assert result.package_id == patch["patch_id"]
    assert activations == ["1.0.1"]
    target_manifest = verify_release(target)
    assert manager.active()["release_id"] == target_manifest["release_id"]
    active_path = manager.active_release_path()
    assert active_path is not None
    assert (active_path / "new.txt").read_text(encoding="utf-8") == "new payload"
    assert not (active_path / "obsolete.txt").exists()
    assert manager.state()["history"][-1]["package_id"] == patch["patch_id"]


def test_patch_rejects_tampering_and_the_wrong_active_base(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    base = _package(tmp_path, "1.0.0", "base")
    target = _package(tmp_path, "1.0.1", "target")
    patch_dir = tmp_path / "patch"
    build_patch_package(base, target, patch_dir, signing_private_key=private_key)

    payload = patch_dir / "payload" / "TraceMindAI"
    payload.write_text("tampered", encoding="utf-8")
    with pytest.raises(ManifestError, match="checksum mismatch|size mismatch"):
        verify_patch_package(patch_dir, trusted_public_keys=[public_key])

    shutil_target = tmp_path / "clean-patch"
    build_patch_package(base, target, shutil_target, signing_private_key=private_key)
    manager = ReleaseManager(tmp_path / "installation")
    other_base = _package(tmp_path, "0.9.0", "other")
    manager.install(other_base)
    with pytest.raises(PreflightError, match="requires base"):
        manager.install_patch(
            shutil_target,
            trusted_public_keys=[public_key],
            activate=lambda _release: None,
            health_check=lambda _release: True,
        )


def test_patch_preflight_materializes_without_changing_the_active_release(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    base = _package(tmp_path, "1.0.0", "base", schema_revision="001")
    target = _package(tmp_path, "1.1.0", "target", schema_revision="002")
    patch_dir = tmp_path / "patch"
    patch = build_patch_package(base, target, patch_dir, signing_private_key=private_key)
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(base)
    history_before = list(manager.state()["history"])

    result = manager.preflight_patch(patch_dir, trusted_public_keys=[public_key])

    assert result["status"] == "ready"
    assert result["patch_id"] == patch["patch_id"]
    assert result["active_release_id"] == first.release_id
    assert result["target_version"] == "1.1.0"
    assert result["required_hooks"] == [
        "activation",
        "readiness",
        "migration",
        "quiesce",
        "database_backup",
    ]
    assert manager.active()["release_id"] == first.release_id
    assert manager.state()["history"] == history_before
    assert not (manager.releases_dir / str(patch["target_release_id"])).exists()


def test_schema_patch_requires_backup_context_and_rolls_back_on_failed_readiness(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    base = _package(tmp_path, "1.0.0", "base", schema_revision="001")
    target = _package(tmp_path, "1.1.0", "target", schema_revision="002")
    patch_dir = tmp_path / "patch"
    build_patch_package(base, target, patch_dir, signing_private_key=private_key)
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(base)

    with pytest.raises(PreflightError, match="requires a migration command"):
        manager.install_patch(
            patch_dir,
            trusted_public_keys=[public_key],
            activate=lambda _release: None,
            health_check=lambda _release: True,
        )
    with pytest.raises(PreflightError, match="database URL"):
        manager.install_patch(
            patch_dir,
            trusted_public_keys=[public_key],
            migration=lambda _release: None,
            activate=lambda _release: None,
            health_check=lambda _release: True,
        )
    with pytest.raises(PreflightError, match="quiesce"):
        manager.install_patch(
            patch_dir,
            trusted_public_keys=[public_key],
            database_url="sqlite:////tmp/platform.db",
            migration=lambda _release: None,
            activate=lambda _release: None,
            health_check=lambda _release: True,
        )

    database = tmp_path / "platform.db"
    database_url = _sqlite_database(database, "before")

    def migration(_release: Path) -> None:
        connection = sqlite3.connect(database)
        connection.execute("update marker set value='after'")
        connection.commit()
        connection.close()

    activations: list[str] = []
    quiesced: list[str] = []

    def activate(release: Path) -> None:
        activations.append(str(verify_release(release)["version"]))

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install_patch(
            patch_dir,
            trusted_public_keys=[public_key],
            database_url=database_url,
            migration=migration,
            quiesce=lambda release: quiesced.append(str(verify_release(release)["version"])),
            activate=activate,
            health_check=lambda release: HealthResult(release.name == first.release_id, "readiness failed"),
        )

    assert manager.active()["release_id"] == first.release_id
    assert _read_marker(database) == "before"
    assert quiesced == ["1.0.0"]
    assert activations == ["1.1.0", "1.0.0"]
    assert manager.state()["history"][-1]["action"] == "patch"
    assert manager.state()["history"][-1]["status"] == "rolled_back"


def test_schema_patch_quiesces_before_external_backup_and_migration(tmp_path: Path) -> None:
    private_key, public_key = _signing_keys(tmp_path)
    base = _package(tmp_path, "1.0.0", "base", schema_revision="001")
    target = _package(tmp_path, "1.1.0", "target", schema_revision="002")
    patch_dir = tmp_path / "patch"
    build_patch_package(base, target, patch_dir, signing_private_key=private_key)
    manager = ReleaseManager(tmp_path / "installation")
    manager.install(base)
    events: list[str] = []

    def backup(destination: Path) -> None:
        events.append("backup")
        (destination / "platform.sql").write_text("consistent dump", encoding="utf-8")

    result = manager.install_patch(
        patch_dir,
        trusted_public_keys=[public_key],
        database_url="postgresql://configured-by-environment/platform",
        migration=lambda _release: events.append("migration"),
        external_backup=backup,
        external_restore=lambda _source: events.append("restore"),
        quiesce=lambda _release: events.append("quiesce"),
        activate=lambda _release: events.append("activate"),
        health_check=lambda _release: events.append("readiness") or True,
    )

    assert result.status == "succeeded"
    assert events == ["quiesce", "backup", "migration", "activate", "readiness"]


def test_patch_and_full_upgrade_block_unintentional_downgrades(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    current = _package(tmp_path, "2.0.0", "current")
    older = _package(tmp_path, "1.9.9", "older")
    manager.install(current)
    with pytest.raises(PreflightError, match="not newer"):
        manager.install(older)


def test_install_upgrade_and_manual_rollback(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")

    first = manager.install(package_one, health_check=lambda _path: HealthResult(True, "ready"))
    second = manager.install(package_two, health_check=lambda _path: True)
    assert first.action == "install"
    assert second.action == "upgrade"
    assert manager.active()["release_id"] == second.release_id

    rolled_back = manager.rollback(health_check=lambda _path: True)
    assert rolled_back.release_id == first.release_id
    assert manager.active()["release_id"] == first.release_id
    assert len(manager.state()["history"]) == 3


def test_failed_upgrade_restores_binary_config_and_sqlite(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(package_one)

    database = tmp_path / "platform.db"
    database_url = _sqlite_database(database, "before")
    config = tmp_path / "app.env"
    config.write_text("MODE=before\n", encoding="utf-8")

    def migration(_release_path: Path) -> None:
        connection = sqlite3.connect(database)
        connection.execute("update marker set value='after'")
        connection.commit()
        connection.close()
        config.write_text("MODE=after\n", encoding="utf-8")

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install(
            package_two,
            database_url=database_url,
            config_paths=[config],
            migration=migration,
            health_check=lambda path: HealthResult(path.name == first.release_id, "readiness failed"),
        )

    assert manager.active()["release_id"] == first.release_id
    assert _read_marker(database) == "before"
    assert config.read_text(encoding="utf-8") == "MODE=before\n"
    assert manager.state()["history"][-1]["status"] == "rolled_back"


def test_idempotent_install_does_not_duplicate_history(tmp_path: Path) -> None:
    package = _package(tmp_path, "1.0.0", "one")
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(package)
    second = manager.install(package)
    assert second.status == "unchanged"
    assert second.release_id == first.release_id
    assert len(manager.state()["history"]) == 1


def test_invalid_active_metadata_fails_closed(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    manager.root.mkdir(parents=True)
    (manager.root / ACTIVE_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(ReleaseError, match="active release metadata"):
        manager.active()


def test_release_state_contains_no_database_credentials(tmp_path: Path) -> None:
    package = _package(tmp_path, "1.0.0", "one")
    manager = ReleaseManager(tmp_path / "installation")
    manager.install(package)
    serialized = json.dumps(manager.state())
    assert "DATABASE_URL" not in serialized
    assert "password" not in serialized.lower()


def test_external_database_migration_requires_backup_and_restore_hooks(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")
    manager.install(package_one)

    with pytest.raises(PreflightError, match="both external backup and restore hooks"):
        manager.install(
            package_two,
            database_url="mysql+pymysql://redacted@database/platform",
            migration=lambda _path: None,
        )


def test_external_database_artifact_is_restored_after_failed_health_check(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(package_one)
    restored: list[str] = []

    def backup(target: Path) -> None:
        (target / "platform.sql").write_text("consistent dump", encoding="utf-8")

    def restore(source: Path) -> None:
        restored.append((source / "platform.sql").read_text(encoding="utf-8"))

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install(
            package_two,
            database_url="postgresql://redacted@database/platform",
            migration=lambda _path: None,
            external_backup=backup,
            external_restore=restore,
            health_check=lambda path: path.name == first.release_id,
        )

    assert restored == ["consistent dump"]
    assert manager.active()["release_id"] == first.release_id


def test_tampered_external_backup_is_rejected_before_restore_hook_runs(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")

    def backup(target: Path) -> None:
        (target / "platform.sql").write_text("consistent dump", encoding="utf-8")

    backup_id = manager.create_pre_upgrade_backup(external_backup=backup)
    artifact = manager.backups_dir / backup_id / "external-database" / "platform.sql"
    artifact.write_text("tampered dump", encoding="utf-8")
    restored: list[Path] = []

    with pytest.raises(ReleaseError, match="integrity check failed"):
        manager.restore_backup(backup_id, external_restore=lambda path: restored.append(path))
    assert restored == []


def test_config_restore_replaces_a_symlink_without_following_it(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    config = tmp_path / "app.env"
    config.write_text("MODE=original\n", encoding="utf-8")
    victim = tmp_path / "unrelated.env"
    victim.write_text("DO_NOT_CHANGE\n", encoding="utf-8")
    backup_id = manager.create_pre_upgrade_backup(config_paths=[config])

    config.unlink()
    config.symlink_to(victim)
    manager.restore_backup(backup_id)

    assert not config.is_symlink()
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert victim.read_text(encoding="utf-8") == "DO_NOT_CHANGE\n"


def test_quiesce_requires_a_matching_activation_hook(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")
    manager.install(package_one)

    with pytest.raises(PreflightError, match="requires an activation"):
        manager.install(package_two, quiesce=lambda _release: None)
    with pytest.raises(PreflightError, match="requires an activation"):
        manager.rollback(quiesce=lambda _release: None)


def test_initial_install_bootstraps_shared_config_and_upgrade_preserves_it(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    config_dir = package_one / "config"
    config_dir.mkdir()
    (config_dir / "app.env.example").write_text("MODE=initial\n", encoding="utf-8")
    build_release_manifest(package_one, version="1.0.0", entrypoint="TraceMindAI")
    package_two = _package(tmp_path, "1.1.0", "two")
    (package_two / "config").mkdir()
    (package_two / "config" / "app.env.example").write_text("MODE=replacement\n", encoding="utf-8")
    build_release_manifest(package_two, version="1.1.0", entrypoint="TraceMindAI")

    manager = ReleaseManager(tmp_path / "installation")
    manager.install(package_one)
    shared_config = manager.shared_dir / "config" / "app.env"
    assert shared_config.read_text(encoding="utf-8") == "MODE=initial\n"
    shared_config.write_text("MODE=operator-value\n", encoding="utf-8")

    manager.install(package_two)
    assert shared_config.read_text(encoding="utf-8") == "MODE=operator-value\n"


def test_failed_activation_reactivates_previous_release(tmp_path: Path) -> None:
    package_one = _package(tmp_path, "1.0.0", "one")
    package_two = _package(tmp_path, "1.1.0", "two")
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(package_one)
    activations: list[str] = []

    def activate(release_path: Path) -> None:
        version = str(verify_release(release_path)["version"])
        activations.append(version)
        if version == "1.1.0":
            raise RuntimeError("service did not start")

    with pytest.raises(ReleaseError, match="active release was restored"):
        manager.install(package_two, activate=activate, health_check=lambda _path: True)

    assert activations == ["1.1.0", "1.0.0"]
    assert manager.active()["release_id"] == first.release_id


def test_release_cli_parser_accepts_install_and_rollback_activation_hooks() -> None:
    parser = build_parser()
    install = parser.parse_args(
        [
            "install",
            "--install-root",
            "/tmp/install",
            "--package",
            "/tmp/package",
            "--activation-command",
            "restart-service",
        ]
    )
    rollback = parser.parse_args(
        [
            "rollback",
            "--install-root",
            "/tmp/install",
            "--activation-command",
            "restart-service",
        ]
    )
    patch_build = parser.parse_args(
        [
            "patch-build",
            "--base",
            "/tmp/base",
            "--target",
            "/tmp/target",
            "--output",
            "/tmp/patch",
            "--signing-private-key",
            "/tmp/signing.key",
        ]
    )
    patch_install = parser.parse_args(
        [
            "patch-install",
            "--install-root",
            "/tmp/install",
            "--package",
            "/tmp/patch",
            "--trusted-public-key",
            "/tmp/signing.pub",
            "--activation-command",
            "restart-service",
            "--health-url",
            "http://127.0.0.1:8800/health/ready",
        ]
    )
    patch_preflight = parser.parse_args(
        [
            "patch-preflight",
            "--install-root",
            "/tmp/install",
            "--package",
            "/tmp/patch",
            "--trusted-public-key",
            "/tmp/signing.pub",
        ]
    )
    assert install.activation_command == "restart-service"
    assert rollback.activation_command == "restart-service"
    assert patch_build.signing_private_key == "/tmp/signing.key"
    assert patch_install.trusted_public_key == ["/tmp/signing.pub"]
    assert patch_preflight.package == "/tmp/patch"


def _lock_worker(root: str, connection) -> None:
    try:
        with ReleaseManager(root, lock_timeout_seconds=1)._lock():
            connection.send("acquired")
            connection.recv()
    except ReleaseLockError:
        connection.send("blocked")
    finally:
        connection.close()


@pytest.mark.parametrize("crash", [False, True])
def test_process_lock_survives_age_and_releases_on_owner_exit(tmp_path: Path, crash: bool) -> None:
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "installation"
    processes = []
    connections = []

    def start_worker(expected: str):
        parent, child = context.Pipe()
        process = context.Process(target=_lock_worker, args=(str(root), child))
        processes.append(process)
        connections.append(parent)
        process.start()
        child.close()
        assert parent.poll(20), "lock worker did not respond"
        assert parent.recv() == expected
        return process, parent

    try:
        owner, owner_connection = start_worker("acquired")
        manager = ReleaseManager(root, lock_timeout_seconds=1)
        before = manager.lock_path.stat()
        old_time = time.time() - 3600
        os.utime(manager.lock_path, (old_time, old_time))
        contender, _ = start_worker("blocked")
        contender.join(10)
        assert contender.exitcode == 0
        after = manager.lock_path.stat()
        assert os.path.samestat(before, after)
        assert before.st_size == after.st_size
        assert after.st_mtime == old_time
        with pytest.raises(ReleaseLockError, match="operation is running"):
            with manager._lock():
                pytest.fail("live owner was displaced")
        if crash:
            owner.terminate()
        else:
            owner_connection.send("release")
        owner.join(10)
        assert not owner.is_alive()
        assert manager.lock_path.exists()
        successor, successor_connection = start_worker("acquired")
        with pytest.raises(ReleaseLockError):
            with manager._lock():
                pytest.fail("successor lock was released by another handle")
        successor_connection.send("release")
        successor.join(10)
        assert successor.exitcode == 0
        with manager._lock():
            pass
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        for connection in connections:
            connection.close()


def test_lock_excludes_other_threads_and_releases_after_exception(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path)

    def contend() -> None:
        with ReleaseManager(tmp_path)._lock():
            pytest.fail("another thread entered the critical section")

    with pytest.raises(RuntimeError, match="operation failed"):
        with manager._lock():
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(contend) for _ in range(4)]
                for future in futures:
                    with pytest.raises(ReleaseLockError):
                        future.result(timeout=10)
            raise RuntimeError("operation failed")
    with manager._lock():
        pass


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming the open lock file")
def test_exiting_owner_does_not_delete_replacement_lock(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path)
    original = manager._lock()
    original.__enter__()
    try:
        manager.lock_path.rename(tmp_path / "old.lock")
        with ReleaseManager(tmp_path)._lock():
            original.__exit__(None, None, None)
            assert manager.lock_path.exists()
            with pytest.raises(ReleaseLockError):
                with ReleaseManager(tmp_path)._lock():
                    pytest.fail("old owner released its successor")
    finally:
        original.__exit__(None, None, None)


@pytest.mark.parametrize("payload", ['{"pid": 1234}', "invalid metadata"])
def test_legacy_or_corrupt_lock_fails_closed_regardless_of_age(tmp_path: Path, payload: str) -> None:
    manager = ReleaseManager(tmp_path, lock_timeout_seconds=1)
    manager.lock_path.write_text(payload, encoding="utf-8")
    os.utime(manager.lock_path, (1, 1))
    with pytest.raises(ReleaseLockError, match="manual"):
        with manager._lock():
            pytest.fail("unrecognized lock was reclaimed")
    assert manager.lock_path.read_text(encoding="utf-8") == payload


def test_windows_lock_uses_nonblocking_byte_lock(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from app.runtime.release_manager import _acquire_file_lock

    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "lseek", lambda *args: calls.append(("seek", *args)))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=2,
        locking=lambda *args: calls.append(("lock", *args)),
    ))
    _acquire_file_lock(42)
    assert calls == [("seek", 42, 0, os.SEEK_SET), ("lock", 42, 2, 1)]


def test_missing_health_is_unverified_for_metadata_only_operations(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = manager.install(_package(tmp_path, "1.1.0", "second"))
    rollback = manager.rollback()
    for result in (first, second, rollback):
        assert result.status == "unverified"
        assert result.health_status == "not_checked"
        assert "not configured" in result.detail
    assert all(record["status"] == "unverified" for record in manager.state()["history"])


@pytest.mark.parametrize("explicit_driver", [False, True])
def test_runtime_activation_without_health_is_rejected_before_mutation(tmp_path: Path, explicit_driver: bool) -> None:
    from app.runtime.release_manager import NativeDeploymentDriver

    manager = ReleaseManager(tmp_path / "installation")
    first = _package(tmp_path, "1.0.0", "first")
    second = _package(tmp_path, "1.1.0", "second")
    calls = []
    def activation(path: Path) -> None:
        calls.append(path)

    options = {"deployment_driver": NativeDeploymentDriver(activation)} if explicit_driver else {"activate": activation}
    with pytest.raises(PreflightError, match="requires a readiness health check"):
        manager.install(first, **options)
    assert manager.active() is None
    manager.install(first)
    manager.install(second)
    previous = manager.active()
    history = manager.state()
    with pytest.raises(PreflightError, match="requires a readiness health check"):
        manager.rollback(**options)
    assert manager.active() == previous
    assert manager.state() == history
    assert calls == []


@pytest.mark.parametrize("failure", ["activation", "health", "health_exception"])
def test_failed_automatic_recovery_never_claims_rolled_back(tmp_path: Path, failure: str) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = _package(tmp_path, "1.1.0", "second")
    events = []

    def activate(path: Path) -> None:
        events.append(("activate", path.name))
        if path.name == first.release_id and failure == "activation":
            raise RuntimeError("old service restart failed")

    def health(path: Path) -> HealthResult:
        events.append(("health", path.name))
        if path.name != first.release_id:
            return HealthResult(False, "new readiness failed")
        if failure == "health_exception":
            raise RuntimeError("old readiness raised")
        return HealthResult(False, "old readiness failed")

    with pytest.raises(ReleaseError, match="manual intervention") as error:
        manager.install(second, activate=activate, health_check=health)
    assert "active release was restored" not in str(error.value)
    record = manager.state()["history"][-1]
    assert record["status"] == "needs_intervention"
    assert record["original_error"] == "new readiness failed"
    assert record["rollback_errors"]
    assert record["rollback_health_status"] == "not_verified"
    assert ("activate", first.release_id) in events
    assert (("health", first.release_id) in events) == (failure != "activation")


@pytest.mark.parametrize("failure", ["backup", "metadata"])
def test_restore_errors_are_recorded_and_prevent_unsafe_reactivation(tmp_path: Path, monkeypatch, failure: str) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = _package(tmp_path, "1.1.0", "second")
    config = tmp_path / "app.env"
    config.write_text("MODE=old", encoding="utf-8")
    activations = []

    def fail(*_args, **_kwargs):
        raise OSError(f"{failure} restore failed")

    monkeypatch.setattr(manager, "restore_backup" if failure == "backup" else "_restore_active_metadata", fail)
    with pytest.raises(ReleaseError, match="manual intervention") as error:
        manager.install(
            second,
            config_paths=[config],
            activate=lambda path: activations.append(path.name),
            health_check=lambda _path: HealthResult(False, "upgrade failed"),
        )
    assert "active release was restored" not in str(error.value)
    record = manager.state()["history"][-1]
    assert record["status"] == "needs_intervention"
    assert record["original_error"] == "upgrade failed"
    assert f"{failure} restore failed" in record["detail"]
    assert first.release_id not in activations


def test_partial_quiesce_failure_restarts_and_checks_previous_release(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = _package(tmp_path, "1.1.0", "second")
    events = []

    def quiesce(_path):
        events.append("quiesce")
        raise RuntimeError("stopped service but drain timed out")

    with pytest.raises(ReleaseError, match="restored and health verified"):
        manager.install(
            second,
            quiesce=quiesce,
            activate=lambda path: events.append(("activate", path.name)),
            health_check=lambda path: events.append(("health", path.name)) or True,
        )
    assert events == ["quiesce", ("activate", first.release_id), ("health", first.release_id)]
    assert manager.state()["history"][-1]["status"] == "rolled_back"


@pytest.mark.parametrize("recovery_failure", [None, "activation", "health", "health_exception"])
def test_manual_rollback_failure_records_verified_or_failed_recovery(tmp_path: Path, recovery_failure: str | None) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = manager.install(_package(tmp_path, "1.1.0", "second"))
    events = []

    def activate(path: Path) -> None:
        events.append(("activate", path.name))
        if path.name == second.release_id and recovery_failure == "activation":
            raise RuntimeError("original restart failed")

    def health(path: Path) -> HealthResult:
        events.append(("health", path.name))
        if path.name == first.release_id:
            return HealthResult(False, "rollback target unhealthy")
        if recovery_failure == "health_exception":
            raise RuntimeError("original readiness raised")
        return HealthResult(recovery_failure is None, "original readiness")

    with pytest.raises(ReleaseError) as error:
        manager.rollback(activate=activate, health_check=health)
    record = manager.state()["history"][-1]
    assert record["action"] == "rollback"
    assert record["original_error"] == "rollback target unhealthy"
    if recovery_failure is None:
        assert record["status"] == "failed"
        assert record["rollback_health_status"] == "healthy"
        assert "original release was restored and health verified" in str(error.value)
    else:
        assert record["status"] == "needs_intervention"
        assert "restored" not in str(error.value)
        assert "remains active" not in str(error.value)
    assert ("activate", second.release_id) in events
    assert (("health", second.release_id) in events) == (recovery_failure != "activation")


def test_initial_activation_failure_does_not_claim_a_previous_release_was_restored(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")

    def activate(_path):
        raise RuntimeError("initial activation failed")

    with pytest.raises(ReleaseError) as error:
        manager.install(_package(tmp_path, "1.0.0", "first"), activate=activate, health_check=lambda _path: True)
    assert "active release was restored" not in str(error.value)
    assert manager.active() is None
    assert manager.state()["history"][-1]["status"] == "needs_intervention"


def test_concurrent_upgrade_and_rollback_cannot_enter_during_long_quiesce(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation", lock_timeout_seconds=1)
    manager.install(_package(tmp_path, "1.0.0", "first"))
    second = _package(tmp_path, "1.1.0", "second")
    third = _package(tmp_path, "1.2.0", "third")
    entered = threading.Event()
    finish = threading.Event()

    def quiesce(_path: Path) -> None:
        entered.set()
        assert finish.wait(10), "test did not release the running upgrade"

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(
            manager.install, second, quiesce=quiesce,
            activate=lambda _path: None, health_check=lambda _path: True,
        )
        try:
            assert entered.wait(10)
            os.utime(manager.lock_path, (1, 1))
            contender = ReleaseManager(manager.root, lock_timeout_seconds=1)
            with pytest.raises(ReleaseLockError):
                contender.install(third, health_check=lambda _path: True)
            with pytest.raises(ReleaseLockError):
                contender.rollback(health_check=lambda _path: True)
            assert not (manager.releases_dir / str(verify_release(third)["release_id"])).exists()
        finally:
            finish.set()
        result = running.result(timeout=10)
    assert result.status == "succeeded"
    assert manager.active()["release_id"] == result.release_id
    assert len(manager.state()["history"]) == 2


@pytest.mark.parametrize("partial_failure", [False, True])
def test_config_only_backup_cannot_prove_migration_rollback(tmp_path: Path, partial_failure: bool) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    first = manager.install(_package(tmp_path, "1.0.0", "first"))
    second = _package(tmp_path, "1.1.0", "second")
    activations = []

    def migration(_path: Path) -> None:
        if partial_failure:
            raise RuntimeError("migration partially applied")

    with pytest.raises(ReleaseError, match="without a verified recovery path") as error:
        manager.install(
            second,
            migration=migration,
            activate=lambda path: activations.append(path.name),
            health_check=lambda _path: False,
        )
    assert first.release_id not in activations
    assert "active release was restored" not in str(error.value)
    assert manager.state()["history"][-1]["status"] == "needs_intervention"


def test_failure_before_active_changes_is_not_reported_as_rolled_back(tmp_path: Path, monkeypatch) -> None:
    from app.runtime.release_manager import NativeDeploymentDriver

    manager = ReleaseManager(tmp_path / "installation")
    manager.install(_package(tmp_path, "1.0.0", "first"))
    before = manager.active()
    second = _package(tmp_path, "1.1.0", "second")
    driver = NativeDeploymentDriver()

    def fail_stage(*_args):
        raise RuntimeError("stage failed")

    monkeypatch.setattr(driver, "stage", fail_stage)
    with pytest.raises(ReleaseError, match="stage failed") as error:
        manager.install(second, deployment_driver=driver, health_check=lambda _path: True)
    assert "active release was restored" not in str(error.value)
    assert manager.active() == before
    assert manager.state()["history"][-1]["status"] == "failed"


def test_missing_rollback_health_never_counts_as_verified_recovery(tmp_path: Path) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    manager.install(_package(tmp_path, "1.0.0", "first"))

    def migration(_path: Path) -> None:
        raise RuntimeError("migration failed")

    database_url = _sqlite_database(tmp_path / "platform.db", "before")
    with pytest.raises(ReleaseError, match="rollback health check not configured"):
        manager.install(
            _package(tmp_path, "1.1.0", "second"),
            database_url=database_url,
            migration=migration,
        )
    assert manager.state()["history"][-1]["status"] == "needs_intervention"


@pytest.mark.parametrize("invalid", [None, "healthy", HealthResult("yes")])
def test_invalid_health_results_are_not_accepted(tmp_path: Path, invalid) -> None:
    manager = ReleaseManager(tmp_path / "installation")
    with pytest.raises(ReleaseError, match="invalid result"):
        manager.install(_package(tmp_path, "1.0.0", "first"), health_check=lambda _path: invalid)
    assert manager.state()["history"][-1]["status"] != "succeeded"
