"""Every revision module under ``versions/`` must actually be applied.

Two migrations (``20261019_01``, ``20261019_02``) shipped with their model columns but were
never added to ``runner.MIGRATIONS``. Nothing failed, because every test builds a *fresh*
database with ``metadata.create_all`` and therefore never walks the upgrade path. A real
installation, by contrast, would have been refused at startup by ``validate_schema``
("Registered schema objects are missing") - the platform would simply not boot after an
upgrade, which is the worst possible way to discover a missing registry entry.

The assertions below are sets, not counts: dropping one revision and adding another keeps the
count identical, so a count check cannot see the deletion this file exists to catch.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 - registers every model table on Base.metadata
from app.core.database import Base
from app.migrations import runner
from app.migrations import versions as versions_package
from app.core.database import initialize_schema
from app.migrations.policy import SchemaUpgradeRequired

# Columns introduced by the three revisions this batch is about. A database without them is a
# pre-upgrade database; after ``initialize_schema`` the columns must exist.
REVISION_COLUMNS = {
    "observability_data_quality_policy": (
        "evaluate_interval_seconds", "retention_interval_seconds",
        "retention_auto_execute", "last_retention_at",
    ),
    "db_capacity_policy": ("sustained_minutes", "recovery_margin_pct"),
    "db_capacity_alert": ("notified_at",),
    "db_inspection_job": ("cancel_requested_at", "cancel_requested_by"),
}
NEW_REVISIONS = (
    "20261019_01_data_quality_schedule",
    "20261019_02_capacity_anti_flap",
    "20261020_01_inspection_job_control",
    "20261020_02_capability_registry",
    "20261020_03_capability_plan",
)
JOB_CREATED_AT_INDEX = "ix_db_inspection_job_created_at"


def revision_module_names() -> set[str]:
    """Names of every revision module on disk (``v`` prefix, excluding the package itself)."""
    directory = Path(versions_package.__file__).parent
    return {
        module.name
        for module in pkgutil.iter_modules([str(directory)])
        if module.name.startswith("v")
    }


def registered_module_names() -> set[str]:
    return {migration.__name__.rsplit(".", 1)[-1] for migration in runner.MIGRATIONS}


def test_every_revision_module_on_disk_is_registered():
    missing = revision_module_names() - registered_module_names()
    assert missing == set(), (
        "revision modules exist but are never applied: "
        f"{sorted(missing)}. Add them to runner.MIGRATIONS in chronological order."
    )


def test_registry_contains_no_module_missing_from_disk():
    """A registry entry whose module was deleted is the same defect from the other side."""
    stale = registered_module_names() - revision_module_names()
    assert stale == set(), f"runner.MIGRATIONS references deleted modules: {sorted(stale)}"


def test_revisions_are_unique_and_match_their_module():
    revisions = [migration.REVISION for migration in runner.MIGRATIONS]
    assert len(revisions) == len(set(revisions))
    for migration in runner.MIGRATIONS:
        module = importlib.import_module(migration.__name__)
        assert module.REVISION == migration.REVISION
        assert module is migration


def test_the_three_control_plane_revisions_are_registered():
    revisions = {migration.REVISION for migration in runner.MIGRATIONS}
    for revision in NEW_REVISIONS:
        assert revision in revisions, f"{revision} would never be applied"


@pytest.fixture
def upgraded_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    yield engine
    engine.dispose()


def _downgrade_to_pre_batch_schema(engine) -> None:
    """Turn a current database into one that predates the three revisions.

    Dropping the columns is the only honest way to reproduce the defect: the models already
    declare them, so a database lacking them is exactly what an installation upgraded to this
    code without the migrations would look like.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {JOB_CREATED_AT_INDEX}")
        for table, columns in REVISION_COLUMNS.items():
            for column in columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
        for revision in NEW_REVISIONS:
            connection.execute(
                text(f"DELETE FROM {runner.MIGRATION_TABLE} WHERE revision = :revision"),
                {"revision": revision},
            )


def _columns(engine, table: str) -> set[str]:
    return {item["name"] for item in inspect(engine).get_columns(table)}


def test_pre_batch_database_upgrades_instead_of_refusing_to_start(upgraded_engine):
    initialize_schema(upgraded_engine, allow_upgrade=True)
    _downgrade_to_pre_batch_schema(upgraded_engine)
    # The fixture really is a pre-upgrade database; otherwise this test proves nothing.
    for table, columns in REVISION_COLUMNS.items():
        assert not (set(columns) & _columns(upgraded_engine, table)), table
    assert JOB_CREATED_AT_INDEX not in {
        index["name"] for index in inspect(upgraded_engine).get_indexes("db_inspection_job")
    }

    applied = initialize_schema(upgraded_engine, allow_upgrade=True)

    assert set(applied) == set(NEW_REVISIONS)
    for table, columns in REVISION_COLUMNS.items():
        assert set(columns) <= _columns(upgraded_engine, table), table
    assert JOB_CREATED_AT_INDEX in {
        index["name"] for index in inspect(upgraded_engine).get_indexes("db_inspection_job")
    }
    # Second run is a no-op; the fix must not rely on re-running DDL every boot.
    assert initialize_schema(upgraded_engine, allow_upgrade=False) == []


def test_pre_batch_database_is_refused_when_the_migrations_are_withheld(
    upgraded_engine, monkeypatch
):
    """The regression guard in the other direction: without these revisions startup fails.

    This is the behaviour a real installation saw. Keeping it asserted means the columns can
    never again be introduced "by the model alone".
    """
    initialize_schema(upgraded_engine, allow_upgrade=True)
    _downgrade_to_pre_batch_schema(upgraded_engine)
    withheld = tuple(
        migration for migration in runner.MIGRATIONS if migration.REVISION not in NEW_REVISIONS
    )
    monkeypatch.setattr(runner, "MIGRATIONS", withheld)
    with pytest.raises(SchemaUpgradeRequired, match="Registered schema objects are missing"):
        initialize_schema(upgraded_engine, allow_upgrade=True)


def test_migration_status_lists_every_registered_revision(upgraded_engine):
    initialize_schema(upgraded_engine, allow_upgrade=True)
    status = runner.migration_status(upgraded_engine)
    listed = {item["revision"] for item in status}
    assert {migration.REVISION for migration in runner.MIGRATIONS} <= listed
    assert set(NEW_REVISIONS) <= listed
    assert all(item["applied"] for item in status)


def test_control_plane_columns_exist_in_the_model_contract():
    table = Base.metadata.tables["db_inspection_job"]
    assert set(REVISION_COLUMNS["db_inspection_job"]) <= {column.name for column in table.columns}
    assert JOB_CREATED_AT_INDEX in {index.name for index in table.indexes}
