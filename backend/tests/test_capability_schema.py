"""Schema contract for the capability registry and typed-plan tables (batch-2 T01).

The point of this file is the *upgrade* path, not the fresh-database path. Every other test
builds a brand-new database where ``metadata.create_all`` creates these tables before any
revision is applied; that path cannot catch "the migration was never registered" or "the
migration's DDL diverged from the model". A real installation already applied the bootstrap
revision, so its only route to the new tables is the two revisions below - and if those are
missing from ``runner.MIGRATIONS``, ``validate_schema`` refuses startup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 - registers every model table on Base.metadata
from app.core.database import Base, initialize_schema
from app.migrations import runner
from app.migrations.policy import SchemaUpgradeRequired

NEW_TABLES = (
    "db_capability_definition",
    "db_capability_plan",
    "db_capability_plan_step",
    "db_capability_plan_revision",
)
NEW_REVISIONS = (
    "20261020_02_capability_registry",
    "20261020_03_capability_plan",
)

# Index names are part of the contract: SQLAlchemy generates ix_<table>_<column>, and
# validate_schema compares them against what the database actually has.
EXPECTED_INDEXES = {
    "db_capability_definition": {
        "ix_db_capability_definition_domain",
        "ix_db_capability_definition_enabled",
    },
    "db_capability_plan": {
        "ix_db_capability_plan_instance_id",
        "ix_db_capability_plan_status",
        "ix_db_capability_plan_created_at",
    },
    "db_capability_plan_step": {
        "ix_db_capability_plan_step_plan_id",
        "ix_db_capability_plan_step_seq",
    },
    "db_capability_plan_revision": {"ix_db_capability_plan_revision_plan_id"},
}
# Unique constraints are compared by *column set* because column-level ``unique=True`` comes
# back unnamed from SQLite reflection; the two explicitly named constraints are asserted on
# the model side where names are reliable.
EXPECTED_UNIQUE_COLUMN_SETS = {
    "db_capability_definition": set(),
    "db_capability_plan": {
        frozenset({"idempotency_key"}),
        frozenset({"parent_plan_id"}),
    },
    "db_capability_plan_step": {frozenset({"plan_id", "seq"})},
    "db_capability_plan_revision": {frozenset({"plan_id", "revision"})},
}
NAMED_UNIQUES = {
    "db_capability_plan_step": {"uq_db_capability_plan_step_plan_seq"},
    "db_capability_plan_revision": {"uq_db_capability_plan_revision_plan_revision"},
}
CHECKS = {
    "ck_db_capability_plan_status",
    "ck_db_capability_plan_outcome",
    "ck_db_capability_plan_settled_outcome",
}


@pytest.fixture
def upgraded_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'capability-upgrade.db'}")
    yield engine
    engine.dispose()


def _drop_capability_schema(engine) -> None:
    """Produce an honest pre-upgrade database: old revisions applied, new tables absent."""
    with engine.begin() as connection:
        for table in NEW_TABLES:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        for revision in NEW_REVISIONS:
            connection.execute(
                text(f"DELETE FROM {runner.MIGRATION_TABLE} WHERE revision = :revision"),
                {"revision": revision},
            )


def _index_names(engine, table: str) -> set[str]:
    return {item["name"] for item in inspect(engine).get_indexes(table)}


def _unique_column_sets(engine, table: str) -> set[frozenset]:
    inspector = inspect(engine)
    result = {
        frozenset(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(table)
    }
    result.update(
        frozenset(index["column_names"])
        for index in inspector.get_indexes(table)
        if index["unique"]
    )
    return result


def test_a_pre_batch_database_upgrades_instead_of_refusing_to_start(upgraded_engine):
    initialize_schema(upgraded_engine, allow_upgrade=True)
    _drop_capability_schema(upgraded_engine)
    inspector = inspect(upgraded_engine)
    # The fixture really is a pre-upgrade database; otherwise the test below proves nothing.
    for table in NEW_TABLES:
        assert not inspector.has_table(table), table

    applied = initialize_schema(upgraded_engine, allow_upgrade=True)

    assert set(applied) == set(NEW_REVISIONS)
    inspector = inspect(upgraded_engine)
    for table in NEW_TABLES:
        assert inspector.has_table(table), table
        # Columns and index names must match the model exactly - a hand-written DDL drift is
        # what makes old installations refuse to start.
        model_columns = {column.name for column in Base.metadata.tables[table].columns}
        actual_columns = {item["name"] for item in inspector.get_columns(table)}
        assert model_columns <= actual_columns, table
        assert _index_names(upgraded_engine, table) == EXPECTED_INDEXES[table], table
        assert _unique_column_sets(upgraded_engine, table) == EXPECTED_UNIQUE_COLUMN_SETS[table], table
    # Second run is a no-op: startup must not re-run DDL every boot.
    assert initialize_schema(upgraded_engine, allow_upgrade=False) == []


def test_a_pre_batch_database_is_refused_when_the_migrations_are_withheld(
    upgraded_engine, monkeypatch
):
    """The other direction: a model table without its registered migration must stop startup.

    Without this assertion the test above is vacuous - it would pass even if the migrations
    did not exist, because ``validate_schema`` would simply see no missing tables.
    """
    initialize_schema(upgraded_engine, allow_upgrade=True)
    _drop_capability_schema(upgraded_engine)
    withheld = tuple(
        migration for migration in runner.MIGRATIONS if migration.REVISION not in NEW_REVISIONS
    )
    monkeypatch.setattr(runner, "MIGRATIONS", withheld)
    with pytest.raises(SchemaUpgradeRequired, match="Registered schema objects are missing"):
        initialize_schema(upgraded_engine, allow_upgrade=True)


def test_the_model_declares_the_contract_we_expect():
    """Pin the model itself: named uniques, column-level uniques, and the three CHECKs."""
    plan = Base.metadata.tables["db_capability_plan"]
    uniques = {frozenset(column.name for column in constraint.columns)
               for constraint in plan.constraints
               if type(constraint).__name__ == "UniqueConstraint"}
    assert frozenset({"idempotency_key"}) in uniques
    assert frozenset({"parent_plan_id"}) in uniques
    checks = {
        constraint.name
        for constraint in plan.constraints
        if type(constraint).__name__ == "CheckConstraint"
    }
    assert checks == CHECKS

    for table, names in NAMED_UNIQUES.items():
        declared = {
            constraint.name
            for constraint in Base.metadata.tables[table].constraints
            if type(constraint).__name__ == "UniqueConstraint"
        }
        assert names <= declared, table


def test_the_status_and_outcome_sets_are_disjoint_and_settled_requires_an_outcome(
    upgraded_engine,
):
    """The CHECKs are enforced where the dialect enforces them (SQLite, PG, MySQL >= 8.0.16).

    MySQL < 8.0.16 parses CHECK but ignores it, so this test is the SQLite evidence only; the
    plan_service write funnel (T03) is the real enforcement. What this still proves is that
    the DDL was actually emitted, and that "settled without an outcome" is structurally
    impossible on any dialect that honours CHECK.
    """
    from sqlalchemy.exc import IntegrityError

    initialize_schema(upgraded_engine, allow_upgrade=True)
    base = (
        "INSERT INTO db_capability_plan "
        "(plan_id, capability_id, definition_version, capability_snapshot_json, requested_by, "
        " trigger_type, status, approval_state, approval_binding_hash, idempotency_key, "
        " attempt, created_at{extra_cols}) "
        "VALUES ('{pid}','x',1,'{{}}',1,'manual','{status}','not_required','h','{pid}',1,"
        "'2026-01-01 00:00:00'{extra_vals})"
    )

    def insert(pid: str, status: str, outcome: str | None = None) -> None:
        sql = base.format(
            pid=pid,
            status=status,
            extra_cols=", outcome" if outcome is not None else "",
            extra_vals=f", '{outcome}'" if outcome is not None else "",
        )
        with upgraded_engine.begin() as connection:
            connection.exec_driver_sql(sql)

    insert("p-ok-draft", "draft")
    insert("p-ok-settled", "settled", outcome="unknown")
    # plan_only must never be expressible as a *status*.
    with pytest.raises(IntegrityError):
        insert("p-bad-status", "plan_only")
    # settled without an outcome is not a legal state.
    with pytest.raises(IntegrityError):
        insert("p-bad-settled", "settled")
    # An outcome must come from the closed vocabulary.
    with pytest.raises(IntegrityError):
        insert("p-bad-outcome", "settled", outcome="completed")


def test_the_four_tables_are_frozen_in_the_reviewed_schema_baseline():
    """The baseline file must carry exactly this batch's tables - a surgical edit, not
    ``--update`` (ISS-50 forbids re-freezing somebody else's uncommitted drift)."""
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "check_backend_contract_baseline", root / "scripts" / "check_backend_contract_baseline.py"
    )
    assert spec and spec.loader
    contracts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contracts)
    schema = json.loads(contracts.SCHEMA_BASELINE.read_text(encoding="utf-8"))

    for table in NEW_TABLES:
        assert table in schema["tables"], table
        frozen = schema["tables"][table]
        model_table = Base.metadata.tables[table]
        frozen_columns = {column["name"]: column for column in frozen["columns"]}
        assert set(frozen_columns) == {column.name for column in model_table.columns}, table
        # Nullability is part of the reviewed contract too: flipping a column from NOT NULL to
        # nullable (or back) without touching the frozen file must be visible here.
        for name, frozen_column in frozen_columns.items():
            assert frozen_column["nullable"] == bool(model_table.c[name].nullable), f"{table}.{name}"
        assert {index["name"] for index in frozen["indexes"]} == {
            index.name for index in model_table.indexes
        }, table
        frozen_unique = {
            column["name"] for column in frozen["columns"] if column["unique"]
        }
        model_unique = {column.name for column in model_table.columns if column.unique}
        assert frozen_unique == model_unique, table


def test_a_not_required_plan_carries_no_binding_hash(upgraded_engine):
    """``approval_state='not_required'`` means there is nothing to bind: the hash is NULL.

    A hash computed over nothing would be fabricated evidence - the same shape as
    "score 100 for zero rows". This test pins two things: the row really stores NULL, and the
    model has no Python-side or server-side default for the column, so no code path can sneak
    a placeholder in.
    """
    initialize_schema(upgraded_engine, allow_upgrade=True)
    column = Base.metadata.tables["db_capability_plan"].c.approval_binding_hash
    assert column.nullable is True
    assert column.default is None, "a default hash would be fabricated evidence"
    assert column.server_default is None, "a server default would be fabricated evidence"

    with upgraded_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO db_capability_plan (plan_id, capability_id, definition_version, "
            "capability_snapshot_json, requested_by, trigger_type, status, approval_state, "
            "idempotency_key, attempt, created_at) "
            "VALUES ('p-nr','x',1,'{}',1,'manual','draft','not_required','nr-1',1,"
            "'2026-01-01 00:00:00')"
        )
        row = connection.exec_driver_sql(
            "SELECT approval_state, approval_binding_hash FROM db_capability_plan "
            "WHERE plan_id = 'p-nr'"
        ).first()
    assert row.approval_state == "not_required"
    assert row.approval_binding_hash is None
