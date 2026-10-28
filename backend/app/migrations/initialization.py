"""One startup transaction/session for version scripts, model bootstrap and runtime revisions."""

from types import SimpleNamespace

from app.migrations import runner, runtime_schema
from app.migrations.locking import schema_lock
from app.migrations.policy import auto_upgrade_enabled, require_upgrade_permission
from app.migrations.schema_contract import reconcile_bootstrap_indexes, validate_schema
from app.migrations.inspection_result_integrity import require_legacy_integrity

BOOTSTRAP_REVISION = "20260908_00_model_bootstrap"
BOOTSTRAP_DESCRIPTION = (
    "Create registered platform tables without replacing existing tables"
)


def initialize_schema(
    bind, metadata, *, allow_upgrade: bool | None = None, lock_timeout: float = 30
):
    allowed = auto_upgrade_enabled(bind, allow_upgrade)
    with schema_lock(bind, timeout=lock_timeout) as connection:
        applied = runner._applied_revisions(connection)
        require_legacy_integrity(connection, allow_initial_copy=runtime_schema.REVISION not in applied)
        bootstrap = SimpleNamespace(
            REVISION=BOOTSTRAP_REVISION,
            DESCRIPTION=BOOTSTRAP_DESCRIPTION,
            upgrade=lambda conn: reconcile_bootstrap_indexes(conn, metadata),
        )
        revisions = (*runner.MIGRATIONS, bootstrap, runtime_schema)
        pending = [item.REVISION for item in revisions if item.REVISION not in applied]
        require_upgrade_permission(pending, allowed)
        applied_now = []
        if pending:
            applied_now.extend(runner.apply_migrations(connection, runner.MIGRATIONS))
            if BOOTSTRAP_REVISION not in applied:
                metadata.create_all(bind=connection)
            applied_now.extend(
                runner.apply_migrations(connection, (runtime_schema, bootstrap))
            )
        # Known revisions may recreate downgraded tables or introduce new model tables.
        # Validate only after applying them, while still in the same lock/transaction.
        validate_schema(connection, metadata)
        require_legacy_integrity(connection)
        return applied_now


def ensure_runtime_schema(
    bind, *, allow_upgrade: bool | None = None, lock_timeout: float = 30
):
    """Legacy compatibility entrypoint; never opens another connection within initialization."""
    allowed = auto_upgrade_enabled(bind, allow_upgrade)
    with schema_lock(bind, timeout=lock_timeout) as connection:
        pending = runtime_schema.REVISION not in runner._applied_revisions(connection)
        require_legacy_integrity(connection, allow_initial_copy=pending)
        require_upgrade_permission(
            [runtime_schema.REVISION] if pending else [], allowed
        )
        applied = runner.apply_migrations(connection, (runtime_schema,)) if pending else []
        require_legacy_integrity(connection)
        return applied
