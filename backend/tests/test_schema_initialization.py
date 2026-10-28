import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from app.core import database
from app.migrations import runner, runtime_schema
from app.migrations.initialization import BOOTSTRAP_REVISION, initialize_schema
from app.migrations.locking import schema_lock
from app.migrations.policy import SchemaUpgradeRequired, auto_upgrade_enabled
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateTable


@pytest.fixture
def isolated_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.db'}")
    yield engine
    engine.dispose()


def test_complete_registered_schema_initializes_idempotently_on_one_connection(
    isolated_engine,
):
    statements = []

    @event.listens_for(isolated_engine, "before_cursor_execute")
    def capture(connection, _cursor, statement, *_):
        statements.append((id(connection), statement))

    applied = database.initialize_schema(isolated_engine, allow_upgrade=True)
    assert set(applied) == {item.REVISION for item in runner.MIGRATIONS} | {
        BOOTSTRAP_REVISION,
        runtime_schema.REVISION,
    }
    assert len({connection for connection, _ in statements}) == 1
    assert database.initialize_schema(isolated_engine, allow_upgrade=False) == []
    assert set(database.Base.metadata.tables).issubset(
        inspect(isolated_engine).get_table_names()
    )
    assert all(item["applied"] for item in runner.migration_status(isolated_engine))


def test_concurrent_startup_serializes_inspection_ddl_backfills_and_journal(
    tmp_path, monkeypatch
):
    path = tmp_path / "parallel.db"
    metadata = MetaData()
    Table("db_instance", metadata, Column("id", Integer, primary_key=True))
    Table(
        "notify_log",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("retry_count", Integer),
    )

    def upgrade(connection):
        time.sleep(0.05)
        connection.execute(text("CREATE TABLE once_only (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO once_only VALUES (1)"))

    migration = SimpleNamespace(
        REVISION="concurrency_test", DESCRIPTION="test", upgrade=upgrade
    )
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    barrier = Barrier(6)

    def start(_):
        engine = create_engine(f"sqlite:///{path}")
        try:
            barrier.wait(timeout=10)
            return initialize_schema(engine, metadata, allow_upgrade=True)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(start, range(6)))
    assert sorted(map(len, results)) == [0, 0, 0, 0, 0, 3]
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM once_only")) == 1
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM app_schema_migration"))
                == 3
            )
        assert "ssh_password_rotated_at" in {
            item["name"] for item in inspect(engine).get_columns("db_instance")
        }
    finally:
        engine.dispose()


def test_failure_rolls_back_ddl_journal_and_releases_lock(isolated_engine, monkeypatch):
    original = runtime_schema.upgrade

    def fail(connection):
        connection.execute(text("CREATE TABLE partial_runtime (id INTEGER)"))
        connection.execute(text("INSERT INTO partial_runtime VALUES (1)"))
        raise RuntimeError("injected runtime failure")

    monkeypatch.setattr(runtime_schema, "upgrade", fail)
    with pytest.raises(RuntimeError, match="injected runtime failure"):
        initialize_schema(isolated_engine, MetaData(), allow_upgrade=True)
    assert inspect(isolated_engine).get_table_names() == []
    monkeypatch.setattr(runtime_schema, "upgrade", original)
    assert runtime_schema.REVISION in initialize_schema(
        isolated_engine, MetaData(), allow_upgrade=True
    )


def test_disabled_upgrade_and_status_do_not_create_journal_or_tables(isolated_engine):
    assert not any(item["applied"] for item in runner.migration_status(isolated_engine))
    with pytest.raises(SchemaUpgradeRequired, match="verified backup"):
        initialize_schema(isolated_engine, MetaData(), allow_upgrade=False)
    with pytest.raises(SchemaUpgradeRequired):
        runner.run_pending_migrations(isolated_engine, allow_upgrade=False)
    with pytest.raises(SchemaUpgradeRequired):
        database.ensure_runtime_schema(isolated_engine, allow_upgrade=False)
    assert inspect(isolated_engine).get_table_names() == []


def test_registered_table_drift_is_not_silently_repaired(isolated_engine):
    metadata = MetaData()
    initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    Table("new_unversioned_table", metadata, Column("id", Integer))
    with pytest.raises(SchemaUpgradeRequired, match="explicit versioned migration"):
        initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    assert not inspect(isolated_engine).has_table("new_unversioned_table")


def test_actual_identity_downgrade_can_be_reapplied_before_model_validation(
    isolated_engine,
):
    from app.migrations.versions.v20260823_01_incident_agent_identity import REVISION

    database.initialize_schema(isolated_engine, allow_upgrade=True)
    assert runner.downgrade_revision(isolated_engine, REVISION)
    assert not inspect(isolated_engine).has_table("incident_agent_identity_key")
    assert database.initialize_schema(isolated_engine, allow_upgrade=True) == [REVISION]
    assert database.initialize_schema(isolated_engine, allow_upgrade=False) == []


def test_pending_explicit_revision_can_create_registered_new_table(
    isolated_engine, monkeypatch
):
    metadata = MetaData()
    initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    table = Table(
        "new_versioned_table", metadata, Column("id", Integer, primary_key=True)
    )
    migration = SimpleNamespace(
        REVISION="test_explicit_table",
        DESCRIPTION="Create registered new table",
        upgrade=lambda connection: table.create(connection),
    )
    monkeypatch.setattr(runner, "MIGRATIONS", (*runner.MIGRATIONS, migration))
    assert initialize_schema(isolated_engine, metadata, allow_upgrade=True) == [
        migration.REVISION
    ]
    assert initialize_schema(isolated_engine, metadata, allow_upgrade=False) == []


def test_registered_column_drift_requires_explicit_revision(
    isolated_engine, monkeypatch
):
    metadata = MetaData()
    table = Table("column_contract", metadata, Column("id", Integer, primary_key=True))
    initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    table.append_column(Column("new_required_value", String(100)))
    for allowed in (False, True):
        with pytest.raises(
            SchemaUpgradeRequired, match="column_contract.new_required_value"
        ):
            initialize_schema(isolated_engine, metadata, allow_upgrade=allowed)
    migration = SimpleNamespace(
        REVISION="test_explicit_column",
        DESCRIPTION="Create registered new column",
        upgrade=lambda connection: connection.execute(
            text(
                "ALTER TABLE column_contract ADD COLUMN new_required_value VARCHAR(100)"
            )
        ),
    )
    monkeypatch.setattr(runner, "MIGRATIONS", (*runner.MIGRATIONS, migration))
    assert initialize_schema(isolated_engine, metadata, allow_upgrade=True) == [
        migration.REVISION
    ]


def test_failed_post_migration_validation_rolls_back_revision_and_ddl(
    isolated_engine, monkeypatch
):
    metadata = MetaData()
    initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    Table("missing_unversioned_table", metadata, Column("id", Integer))
    migration = SimpleNamespace(
        REVISION="test_other_change",
        DESCRIPTION="Unrelated migration must not mask unversioned changes",
        upgrade=lambda connection: connection.execute(
            text("CREATE TABLE unrelated_change (id INTEGER)")
        ),
    )
    monkeypatch.setattr(runner, "MIGRATIONS", (*runner.MIGRATIONS, migration))
    with pytest.raises(SchemaUpgradeRequired, match="missing_unversioned_table"):
        initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    assert not inspect(isolated_engine).has_table("unrelated_change")
    with isolated_engine.connect() as connection:
        assert migration.REVISION not in runner._applied_revisions(connection)


def _session_values():
    from datetime import datetime, timezone

    return {
        "jti_hash": "same-jti",
        "user_id": 1,
        "username": "test",
        "expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
    }


def test_partial_auth_session_table_reconciles_unique_indexes_then_detects_drift(
    isolated_engine,
):
    from app.models.access_control import AuthSession

    with isolated_engine.begin() as connection:
        connection.execute(CreateTable(AuthSession.__table__))
    assert inspect(isolated_engine).get_indexes(AuthSession.__tablename__) == []
    database.initialize_schema(isolated_engine, allow_upgrade=True)
    with isolated_engine.begin() as connection:
        connection.execute(AuthSession.__table__.insert().values(**_session_values()))
    with pytest.raises(IntegrityError), isolated_engine.begin() as connection:
        connection.execute(AuthSession.__table__.insert().values(**_session_values()))
    with isolated_engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_security_auth_session_jti_hash"))
    with pytest.raises(
        SchemaUpgradeRequired, match="ix_security_auth_session_jti_hash"
    ):
        database.initialize_schema(isolated_engine, allow_upgrade=False)


def test_partial_auth_table_duplicate_jti_fails_without_losing_records(isolated_engine):
    from app.models.access_control import AuthSession

    with isolated_engine.begin() as connection:
        connection.execute(CreateTable(AuthSession.__table__))
        connection.execute(
            AuthSession.__table__.insert(), [_session_values(), _session_values()]
        )
    with pytest.raises(IntegrityError):
        database.initialize_schema(isolated_engine, allow_upgrade=True)
    with isolated_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM security_auth_session")) == 2
        )
        assert BOOTSTRAP_REVISION not in runner._applied_revisions(connection)


def test_bootstrap_restores_missing_unique_constraint_additively(isolated_engine):
    metadata = MetaData()
    Table(
        "unique_contract",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("business_key", String(100)),
        UniqueConstraint("business_key", name="uq_unique_contract_business_key"),
    )
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE unique_contract (id INTEGER PRIMARY KEY, business_key VARCHAR(100))"
            )
        )
        connection.execute(text("INSERT INTO unique_contract VALUES (1, 'one')"))
    initialize_schema(isolated_engine, metadata, allow_upgrade=True)
    with pytest.raises(IntegrityError), isolated_engine.begin() as connection:
        connection.execute(text("INSERT INTO unique_contract VALUES (2, 'one')"))
    with isolated_engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_unique_contract_business_key"))
    with pytest.raises(SchemaUpgradeRequired, match="uq_unique_contract_business_key"):
        initialize_schema(isolated_engine, metadata, allow_upgrade=False)


def test_bootstrap_rejects_nonunique_index_with_required_unique_name(isolated_engine):
    metadata = MetaData()
    table = Table(
        "wrong_index",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("business_key", String(100), unique=True, index=True),
    )
    with isolated_engine.begin() as connection:
        connection.execute(CreateTable(table))
        connection.execute(
            text(
                "CREATE INDEX ix_wrong_index_business_key ON wrong_index (business_key)"
            )
        )
    with pytest.raises(SchemaUpgradeRequired, match="incompatible definition"):
        initialize_schema(isolated_engine, metadata, allow_upgrade=True)


def test_external_and_production_databases_default_to_check_only(monkeypatch):
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    monkeypatch.setattr(database.settings, "APP_MODE", "dev")
    for dialect in (mysql.dialect(), postgresql.dialect()):
        assert not auto_upgrade_enabled(SimpleNamespace(dialect=dialect))
    assert auto_upgrade_enabled(SimpleNamespace(dialect=sqlite.dialect()))
    monkeypatch.setattr(database.settings, "APP_MODE", "production")
    assert not auto_upgrade_enabled(SimpleNamespace(dialect=sqlite.dialect()))
    monkeypatch.setenv("TRACEMIND_AUTO_MIGRATE", "true")
    assert auto_upgrade_enabled(SimpleNamespace(dialect=postgresql.dialect()))
    assert not auto_upgrade_enabled(
        SimpleNamespace(dialect=postgresql.dialect()), False
    )
    monkeypatch.setenv("TRACEMIND_AUTO_MIGRATE", "typo")
    with pytest.raises(ValueError, match="boolean"):
        auto_upgrade_enabled(SimpleNamespace(dialect=sqlite.dialect()))


def test_sqlite_lock_timeout_then_retry_restores_connection_settings(isolated_engine):
    with isolated_engine.connect() as blocker:
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        with (
            pytest.raises(OperationalError, match="locked"),
            schema_lock(isolated_engine, timeout=0.01),
        ):
            pytest.fail("must not enter lock body")
        blocker.rollback()
    with (
        schema_lock(isolated_engine, timeout=0.01) as connection,
        schema_lock(connection) as nested,
    ):
        assert nested is connection
        connection.execute(text("CREATE TABLE retry_ok (id INTEGER)"))
    with isolated_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_existing_caller_transaction_is_not_committed_or_rolled_back(isolated_engine):
    with isolated_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="fresh"), schema_lock(connection):
            pytest.fail("must not enter lock body")
        assert connection.in_transaction()
        connection.rollback()


@pytest.mark.parametrize(
    "dialect", [sqlite.dialect(), mysql.dialect(), postgresql.dialect()]
)
def test_runtime_columns_and_revision_journal_compile_for_each_dialect(dialect):
    statements = [str(CreateTable(runner.JOURNAL).compile(dialect=dialect))]
    for table_name, columns in runtime_schema.COLUMNS.items():
        statements.extend(
            runtime_schema.add_column_sql(dialect, table_name, column)
            for column in columns.values()
        )
    sql = "\n".join(statements)
    assert "CURRENT_TIMESTAMP" in sql
    if dialect.name == "postgresql":
        assert "TIMESTAMP" in sql
        assert "DATETIME" not in sql
        assert "MODIFY COLUMN" not in sql
    else:
        assert "DATETIME" in sql


def test_task_id_conversion_preserves_postgres_values_and_mysql_nullability():
    postgres = runtime_schema.task_id_type_sql(postgresql.dialect(), nullable=True)
    assert (
        "ALTER COLUMN task_id TYPE VARCHAR(30) USING CAST(task_id AS VARCHAR(30))"
        in postgres
    )
    assert "MODIFY" not in postgres
    assert runtime_schema.task_id_type_sql(mysql.dialect(), nullable=True).endswith(
        " NULL"
    )
    assert runtime_schema.task_id_type_sql(mysql.dialect(), nullable=False).endswith(
        " NOT NULL"
    )
    assert runtime_schema.task_id_type_sql(sqlite.dialect(), nullable=False) is None


def test_runtime_backfills_resume_nulls_without_overwriting_existing_values(
    isolated_engine,
):
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notify_log (id INTEGER PRIMARY KEY, retry_count INTEGER, attempt_count INTEGER)"
            )
        )
        connection.execute(
            text("INSERT INTO notify_log VALUES (1, 3, NULL), (2, 4, 99)")
        )
        connection.execute(
            text(
                "CREATE TABLE db_object_size_daily (id INTEGER PRIMARY KEY, db_type TEXT, capacity_limit_known INTEGER)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO db_object_size_daily VALUES (1, 'mysql', NULL), (2, 'oracle', NULL), (3, 'mysql', 1)"
            )
        )
    database.ensure_runtime_schema(isolated_engine, allow_upgrade=True)
    assert database.ensure_runtime_schema(isolated_engine, allow_upgrade=False) == []
    with isolated_engine.connect() as connection:
        assert list(
            connection.execute(
                text("SELECT attempt_count FROM notify_log ORDER BY id")
            ).scalars()
        ) == [4, 99]
        assert list(
            connection.execute(
                text(
                    "SELECT capacity_limit_known FROM db_object_size_daily ORDER BY id"
                )
            ).scalars()
        ) == [0, 1, 1]


def test_new_backfilled_columns_keep_insert_defaults(isolated_engine):
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notify_log (id INTEGER PRIMARY KEY, retry_count INTEGER)"
            )
        )
        connection.execute(text("INSERT INTO notify_log VALUES (1, 3)"))
        connection.execute(
            text(
                "CREATE TABLE db_object_size_daily (id INTEGER PRIMARY KEY, db_type TEXT)"
            )
        )
        connection.execute(text("INSERT INTO db_object_size_daily VALUES (1, 'mysql')"))
    database.ensure_runtime_schema(isolated_engine, allow_upgrade=True)
    with isolated_engine.begin() as connection:
        connection.execute(text("INSERT INTO notify_log (id) VALUES (2)"))
        connection.execute(
            text("INSERT INTO db_object_size_daily (id, db_type) VALUES (2, 'oracle')")
        )
        assert list(
            connection.execute(
                text("SELECT attempt_count FROM notify_log ORDER BY id")
            ).scalars()
        ) == [4, 1]
        assert list(
            connection.execute(
                text(
                    "SELECT capacity_limit_known FROM db_object_size_daily ORDER BY id"
                )
            ).scalars()
        ) == [0, 1]


@pytest.mark.parametrize("managed", [False, True])
def test_static_install_guard_requires_explicit_managed_initialization(
    tmp_path, monkeypatch, managed
):
    from scripts import check_backend_static

    source = "def init_sqlite_database(username, password):\n"
    if managed:
        source += "    from app.core.database import initialize_schema\n    initialize_schema(engine, allow_upgrade=True)\n"
    else:
        source += "    from app.core.database import ensure_runtime_schema\n    Base.metadata.create_all(bind=engine)\n    ensure_runtime_schema()\n"
    path = tmp_path / "sqlite_init.py"
    path.write_text(source)
    monkeypatch.setattr(check_backend_static, "SQLITE_INIT_SERVICE", path)
    monkeypatch.setattr(check_backend_static, "ROOT", tmp_path)
    findings = check_backend_static.check_sqlite_init_schema_guards()
    assert bool(findings) is not managed


def test_duplicate_business_keys_fail_without_clearing_data(isolated_engine):
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notify_event (id INTEGER PRIMARY KEY, event_key VARCHAR(160))"
            )
        )
        connection.execute(
            text("INSERT INTO notify_event VALUES (1, 'same'), (2, 'same')")
        )
    with pytest.raises(IntegrityError):
        database.ensure_runtime_schema(isolated_engine, allow_upgrade=True)
    with isolated_engine.connect() as connection:
        assert list(
            connection.execute(
                text("SELECT event_key FROM notify_event ORDER BY id")
            ).scalars()
        ) == ["same", "same"]
    assert not inspect(isolated_engine).has_table(runner.MIGRATION_TABLE)


def test_legacy_result_copy_retains_source_and_is_journaled(isolated_engine):
    metadata = MetaData()
    names = (
        "instance_id",
        "db_type",
        "check_category",
        "check_item",
        "check_status",
        "metric_name",
        "metric_value",
        "threshold_value",
        "finding",
        "suggestion",
        "raw_data",
        "create_time",
    )
    source = Table(
        "db_check_result_new",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("task_id", Integer),
        *(Column(name, String) for name in names),
    )
    target = Table(
        "db_check_result",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("task_id", String(30)),
        *(Column(name, String) for name in names),
    )
    metadata.create_all(isolated_engine)
    with isolated_engine.begin() as connection:
        connection.execute(
            source.insert().values(id=42, task_id=123456789, finding="preserve")
        )
    assert database.ensure_runtime_schema(isolated_engine, allow_upgrade=True) == [
        runtime_schema.REVISION
    ]
    assert database.ensure_runtime_schema(isolated_engine, allow_upgrade=True) == []
    with isolated_engine.connect() as connection:
        assert connection.execute(source.select()).one().task_id == 123456789
        row = connection.execute(target.select()).one()
        assert row.task_id == "123456789"
        assert row.finding == "preserve"


def test_contract_runtime_checker_compatibility_retains_source(tmp_path, monkeypatch):
    from scripts.check_runtime_schema import check_runtime_schema, seed_legacy_sqlite

    path = tmp_path / "contract.db"
    seed_legacy_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.settings, "APP_MODE", "schema_check")
    monkeypatch.delenv("TRACEMIND_AUTO_MIGRATE", raising=False)
    try:
        database.ensure_runtime_schema()
        database.ensure_runtime_schema()
        report = check_runtime_schema(path)
        assert report["missing_instance_columns"] == []
        assert report["missing_check_result_columns"] == []
        assert not any(report["missing_policy_status_columns"].values())
        assert report["legacy_result_table_exists"] is True
        assert report["migrated_check_result"] == [
            "2026061401",
            7,
            "oracle",
            "legacy result",
        ]
        with engine.connect() as connection:
            assert runtime_schema.REVISION in runner._applied_revisions(connection)
    finally:
        engine.dispose()


def test_create_all_then_runtime_compatibility_can_finish_full_initialization(
    isolated_engine,
):
    import app.models  # noqa: F401

    database.Base.metadata.create_all(isolated_engine)
    assert database.ensure_runtime_schema(isolated_engine, allow_upgrade=True) == [
        runtime_schema.REVISION
    ]
    applied = database.initialize_schema(isolated_engine, allow_upgrade=True)
    assert BOOTSTRAP_REVISION in applied
    assert runtime_schema.REVISION not in applied
    assert database.initialize_schema(isolated_engine, allow_upgrade=False) == []


def test_release_cli_check_apply_and_concurrent_processes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    cli = root / "scripts" / "migrate_schema.py"
    target = tmp_path / "cli.db"
    environment = dict(
        os.environ,
        DATABASE_URL="sqlite:///" + str(target),
        APP_MODE="production",
        TRACEMIND_AUTO_MIGRATE="0",
        PYTHONPATH=os.pathsep.join(filter(None, [str(root / "backend"), os.environ.get("PYTHONPATH")])),
    )
    command = [sys.executable, str(cli)]
    check = subprocess.run(
        [*command, "check"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert check.returncode == 2, check.stderr
    assert json.loads(check.stdout)["upgrade_required"]
    processes = [
        subprocess.Popen(
            [*command, "apply"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert [process.returncode for process in processes] == [0, 0, 0], outputs
        assert sorted(len(json.loads(stdout)["applied"]) for stdout, _ in outputs) == [
            0,
            0,
            len(runner.MIGRATIONS) + 2,
        ]
        applied_revisions = {revision for stdout, _ in outputs for revision in json.loads(stdout)["applied"]}
        assert applied_revisions == {item.REVISION for item in runner.MIGRATIONS} | {
            BOOTSTRAP_REVISION, runtime_schema.REVISION,
        }
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    check = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert all(item["applied"] for item in json.loads(check.stdout)["revisions"])
    verification = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import logging; logging.disable(logging.CRITICAL); "
                "from app.main import app; "
                "from app.core.database import initialize_schema; "
                "assert initialize_schema(allow_upgrade=False) == []"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verification.returncode == 0, verification.stderr


def test_release_custom_migration_command_uses_selected_environment(
    tmp_path, monkeypatch
):
    import shlex

    from scripts.release_manager import _command_step

    cli = Path(__file__).parents[2] / "scripts" / "migrate_schema.py"
    target = tmp_path / "release-hook.db"
    monkeypatch.setenv("SELECTED_PLATFORM_DB", "sqlite:///" + str(target))
    command = shlex.join(
        [
            sys.executable,
            str(cli),
            "apply",
            "--database-url-env",
            "SELECTED_PLATFORM_DB",
            "--backup-verified",
            "--service-quiesced",
        ]
    )
    _command_step(command, 30)(tmp_path)
    engine = create_engine("sqlite:///" + str(target))
    try:
        assert database.initialize_schema(engine, allow_upgrade=False) == []
    finally:
        engine.dispose()


def test_external_cli_requires_backup_and_quiescence_before_connecting(monkeypatch):
    import sqlalchemy

    from scripts.migrate_schema import main

    monkeypatch.setenv(
        "TARGET_TEST_DB", "mysql+pymysql://unused:replace-with-test-password@unreachable.invalid/platform"
    )
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])

    def unexpected_connection(*args, **kwargs):
        pytest.fail(
            "external engine must not be created before explicit maintenance attestation"
        )

    monkeypatch.setattr(sqlalchemy, "create_engine", unexpected_connection)
    with pytest.raises(SystemExit) as error:
        main(["apply", "--database-url-env", "TARGET_TEST_DB"])
    assert error.value.code == 2


def test_sqlite_install_uses_serialized_initializer_and_rejects_external_engine(
    isolated_engine, monkeypatch
):
    from app.services import sqlite_init_service
    from app.services.inspection import policy_service

    calls = []
    monkeypatch.setattr(sqlite_init_service, "engine", isolated_engine)
    monkeypatch.setattr(sqlite_init_service, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(
        sqlite_init_service,
        "SessionLocal",
        lambda: SimpleNamespace(
            commit=lambda: None,
            rollback=lambda: None,
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(
        sqlite_init_service, "_init_module_config", lambda db: calls.append("modules")
    )
    monkeypatch.setattr(
        sqlite_init_service, "_init_admin_user", lambda *args: calls.append("admin")
    )
    monkeypatch.setattr(policy_service, "ensure_default_policies", lambda db, commit=True: calls.append(("templates", commit)))
    sqlite_init_service.init_sqlite_database("admin", "test-only")
    assert calls == ["modules", "admin", ("templates", False)]
    assert database.initialize_schema(isolated_engine, allow_upgrade=False) == []
    monkeypatch.setattr(
        sqlite_init_service, "engine", SimpleNamespace(dialect=mysql.dialect())
    )
    with pytest.raises(RuntimeError, match="external database"):
        sqlite_init_service.init_sqlite_database("admin", "test-only")


@pytest.mark.parametrize("populated", [False, True])
def test_mysql_install_only_bootstraps_empty_target_under_same_lock(
    isolated_engine, tmp_path, monkeypatch, populated
):
    from contextlib import contextmanager

    from app.api.v1 import setup

    if populated:
        with isolated_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE existing_business (id INTEGER PRIMARY KEY)")
            )
            connection.execute(text("INSERT INTO existing_business VALUES (1)"))

    class Server:
        @contextmanager
        def connect(self):
            yield SimpleNamespace(
                execute=lambda statement: SimpleNamespace(
                    fetchall=lambda: [("platform",)]
                )
            )

        def dispose(self):
            pass

    engines = iter([Server(), isolated_engine])
    monkeypatch.setattr(setup, "create_engine", lambda *args, **kwargs: next(engines))
    monkeypatch.setattr(setup, "SETUP_FLAG", str(tmp_path / "setup.env"))
    from app.services import sqlite_init_service

    monkeypatch.setattr(sqlite_init_service, "_init_module_config", lambda db: None)
    monkeypatch.setattr(sqlite_init_service, "_init_admin_user", lambda *args: None)
    result = setup.init_database(
        setup.MySQLConfig(
            host="not-contacted.invalid",
            username="test",
            password="test-only",
            database="platform",
            admin_username="admin",
            admin_password="OnlyForTest123!",
        )
    )
    assert result["ok"] is not populated, result
    if populated:
        assert "already contains tables" in result["message"]
        with isolated_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM existing_business")) == 1
            )
        assert not inspect(isolated_engine).has_table(runner.MIGRATION_TABLE)
        assert not Path(setup.SETUP_FLAG).exists()
    else:
        assert database.initialize_schema(isolated_engine, allow_upgrade=False) == []
        assert Path(setup.SETUP_FLAG).exists()


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def one(self):
        return self.value


class FakeLockConnection:
    def __init__(self, dialect, *, acquire=True, release=True):
        self.dialect = SimpleNamespace(name=dialect)
        self.info = {}
        self.calls = []
        self.acquire = acquire
        self.release = release

    def in_transaction(self):
        return False

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters))
        if sql == "SELECT DATABASE()":
            return FakeResult("platform")
        if "current_database()" in sql:
            return FakeResult("platform")
        if "RELEASE_LOCK" in sql or "pg_advisory_unlock" in sql:
            return FakeResult(self.release)
        return FakeResult(self.acquire)

    def commit(self):
        self.calls.append(("commit", None))

    def rollback(self):
        self.calls.append(("rollback", None))

    def close(self):
        self.calls.append(("close", None))

    def invalidate(self):
        self.calls.append(("invalidate", None))


@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "postgresql"])
@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_external_session_lock_commit_and_failure_release_protocol(dialect, failure):
    connection = FakeLockConnection(dialect)
    engine = SimpleNamespace(connect=lambda: connection)
    if failure:
        with pytest.raises(failure), schema_lock(engine):
            raise failure("injected")
    else:
        with schema_lock(engine):
            connection.execute(text("DDL under lock"))
    statements = [sql for sql, _ in connection.calls]
    release = (
        "SELECT pg_advisory_unlock(:key)"
        if dialect == "postgresql"
        else "SELECT RELEASE_LOCK(:key)"
    )
    assert release in statements
    assert statements[-1] == "close"
    assert "invalidate" not in statements
    assert statements[statements.index(release) - 1] == "rollback"
    assert not connection.info


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_external_lock_timeout_and_unlock_failure_discard_pool_connection(dialect):
    connection = FakeLockConnection(dialect, acquire=False)
    with (
        pytest.raises(TimeoutError),
        schema_lock(SimpleNamespace(connect=lambda: connection), timeout=0),
    ):
        pytest.fail("lock body must not run")
    assert ("invalidate", None) in connection.calls
    connection = FakeLockConnection(dialect, release=False)
    with (
        pytest.raises(RuntimeError, match="release failed"),
        schema_lock(SimpleNamespace(connect=lambda: connection)),
    ):
        pass
    assert ("invalidate", None) in connection.calls


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_lost_lock_connection_is_not_reconnected_for_unlock(dialect):
    connection = FakeLockConnection(dialect)
    with (
        pytest.raises(RuntimeError, match="lost session"),
        schema_lock(SimpleNamespace(connect=lambda: connection)),
    ):
        connection.invalidated = True
        raise RuntimeError("lost session")
    assert not any(
        "RELEASE_LOCK" in sql or "pg_advisory_unlock" in sql
        for sql, _ in connection.calls
    )
    assert connection.calls[-1] == ("close", None)
