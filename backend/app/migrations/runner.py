from __future__ import annotations

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select, text

from app.migrations.locking import schema_lock
from app.migrations.policy import auto_upgrade_enabled, require_upgrade_permission
from app.migrations.versions import (
    v20260824_01_scheduler_fencing,
    v20260824_02_kernel_core_coverage,
    v20260824_03_kernel_core_artifact_trust,
    v20260908_02_inspection_jobs,
    v20260908_03_inspection_template_versions,
    v20260910_01_slow_sql_counters,
    v20260911_01_sql_history_identity,
    v20261019_01_data_quality_schedule,
    v20261019_02_capacity_anti_flap,
    v20261020_01_inspection_job_control,
    v20261020_02_capability_registry,
    v20261020_03_capability_plan,
    v20261020_04_capability_execution,
    v20261020_05_capability_control,
    v20261020_06_capability_preparation,
    v20261020_07_capability_control_runtime,
    v20261020_08_capability_proposal,
    v20261023_01_capability_effect_receipt,
    v20261027_01_engine_execution,
)

#: Ordered registry. A revision module that exists under ``versions/`` but is missing here is
#: never applied, while its model columns already are - so an existing installation fails
#: ``validate_schema`` at startup instead of upgrading. ``tests/test_migration_registration.py``
#: compares this registry with the directory as *sets* to catch both additions and removals.
MIGRATIONS = (
    v20260824_01_scheduler_fencing,
    v20260824_02_kernel_core_coverage,
    v20260824_03_kernel_core_artifact_trust,
    v20260908_02_inspection_jobs,
    v20260908_03_inspection_template_versions,
    v20260910_01_slow_sql_counters,
    v20260911_01_sql_history_identity,
    v20261019_01_data_quality_schedule,
    v20261019_02_capacity_anti_flap,
    v20261020_01_inspection_job_control,
    v20261020_02_capability_registry,
    v20261020_03_capability_plan,
    v20261020_04_capability_execution,
    v20261020_05_capability_control,
    v20261020_06_capability_preparation,
    v20261020_07_capability_control_runtime,
    v20261020_08_capability_proposal,
    v20261023_01_capability_effect_receipt,
    v20261027_01_engine_execution,
)
MIGRATION_TABLE = "app_schema_migration"
JOURNAL = Table(
    MIGRATION_TABLE,
    MetaData(),
    Column("revision", String(64), primary_key=True),
    Column("description", String(500), nullable=False),
    Column(
        "applied_at",
        DateTime(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)


def _ensure_migration_table(connection) -> None:
    JOURNAL.create(connection, checkfirst=True)


def _applied_revisions(connection) -> set[str]:
    if not inspect(connection).has_table(MIGRATION_TABLE):
        return set()
    return set(connection.execute(select(JOURNAL.c.revision)).scalars())


def apply_migrations(connection, migrations) -> list[str]:
    """Apply a revision list on a connection already owned by schema_lock."""
    _ensure_migration_table(connection)
    applied_now: list[str] = []
    applied = _applied_revisions(connection)
    for migration in migrations:
        if migration.REVISION in applied:
            continue
        migration.upgrade(connection)
        connection.execute(
            JOURNAL.insert().values(
                revision=migration.REVISION,
                description=migration.DESCRIPTION,
            )
        )
        applied.add(migration.REVISION)
        applied_now.append(migration.REVISION)
    return applied_now


def run_pending_migrations(
    engine, *, allow_upgrade: bool | None = None, lock_timeout: float = 30
) -> list[str]:
    """Compatibility entrypoint for version scripts; startup must use initialize_schema."""
    allowed = auto_upgrade_enabled(engine, allow_upgrade)
    with schema_lock(engine, timeout=lock_timeout) as connection:
        applied = _applied_revisions(connection)
        from app.migrations.inspection_result_integrity import require_legacy_integrity

        # This entrypoint never runs the runtime copy; only full initialization may
        # defer an empty-target check until that copy is actually performed.
        require_legacy_integrity(connection)
        pending = [item.REVISION for item in MIGRATIONS if item.REVISION not in applied]
        require_upgrade_permission(pending, allowed)
        return apply_migrations(connection, MIGRATIONS) if pending else []


def downgrade_revision(engine, revision: str) -> bool:
    migration = next((item for item in MIGRATIONS if revision == item.REVISION), None)
    if migration is None:
        raise LookupError(f"未知数据库迁移版本：{revision}")
    with schema_lock(engine) as connection:
        if not inspect(connection).has_table(MIGRATION_TABLE):
            return False
        applied = _applied_revisions(connection)
        if revision not in applied:
            return False
        migration.downgrade(connection)
        connection.execute(
            text(f"DELETE FROM {MIGRATION_TABLE} WHERE revision = :revision"),
            {"revision": revision},
        )
    return True


def migration_status(engine) -> list[dict[str, str | bool]]:
    from app.migrations import runtime_schema
    from app.migrations.initialization import BOOTSTRAP_DESCRIPTION, BOOTSTRAP_REVISION

    with schema_lock(engine) as connection:
        applied = _applied_revisions(connection)
    result = [
        {
            "revision": migration.REVISION,
            "description": migration.DESCRIPTION,
            "applied": migration.REVISION in applied,
        }
        for migration in (*MIGRATIONS, runtime_schema)
    ]
    result.insert(
        len(MIGRATIONS),
        {
            "revision": BOOTSTRAP_REVISION,
            "description": BOOTSTRAP_DESCRIPTION,
            "applied": BOOTSTRAP_REVISION in applied,
        },
    )
    return result
