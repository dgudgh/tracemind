"""Real isolated SQLite tests; MySQL/MariaDB/PostgreSQL are SQL contracts only."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, Integer, String, create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

from app.migrations import runner
from app.migrations.policy import SchemaUpgradeRequired
from app.migrations.versions import v20260910_01_slow_sql_counters as migration
from app.migrations.versions import v20260911_01_sql_history_identity as history_migration
from app.models.slow_sql import DBSlowSQL


MAX_SAFE_INTEGER = 2**53 - 1


@pytest.fixture
def legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'slow-sql.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE db_slow_sql (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                db_type VARCHAR(20) NOT NULL,
                executions INTEGER,
                logical_reads INTEGER NOT NULL DEFAULT 0,
                physical_reads INTEGER DEFAULT 7,
                rows_processed INTEGER
            )
        """)
        connection.exec_driver_sql(
            "CREATE INDEX ix_slow_instance ON db_slow_sql (instance_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_slow_reads ON db_slow_sql (logical_reads, physical_reads)"
        )
        connection.exec_driver_sql("""
            INSERT INTO db_slow_sql
                (id, instance_id, db_type, executions, rows_processed)
            VALUES (41, 7, 'oracle', 2147483647, NULL)
        """)
    yield engine
    engine.dispose()


def _sqlite_objects(connection):
    return connection.exec_driver_sql(
        "SELECT type, name, rootpage, sql FROM sqlite_master "
        "WHERE tbl_name = 'db_slow_sql' ORDER BY name"
    ).all()


def test_registration_and_model_storage_contract():
    assert [item.REVISION for item in runner.MIGRATIONS].count(migration.REVISION) == 1
    assert runner.MIGRATIONS.index(migration) < runner.MIGRATIONS.index(history_migration)
    for name in migration.COUNTERS:
        assert isinstance(DBSlowSQL.__table__.c[name].type, BigInteger)
        assert DBSlowSQL.__table__.c[name].nullable
    for dialect in (mysql.dialect(), postgresql.dialect()):
        ddl = str(CreateTable(DBSlowSQL.__table__).compile(dialect=dialect))
        for name in migration.COUNTERS:
            assert f"{name} BIGINT" in ddl


def test_explicit_permission_gate_changes_neither_schema_nor_journal(
    legacy_engine, monkeypatch,
):
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    with legacy_engine.connect() as connection:
        before = _sqlite_objects(connection)
    with pytest.raises(SchemaUpgradeRequired, match=migration.REVISION):
        runner.run_pending_migrations(legacy_engine, allow_upgrade=False)
    with legacy_engine.connect() as connection:
        assert _sqlite_objects(connection) == before
        assert not inspect(connection).has_table(runner.MIGRATION_TABLE)
    assert runner.run_pending_migrations(legacy_engine, allow_upgrade=True) == [
        migration.REVISION
    ]
    assert runner.run_pending_migrations(legacy_engine, allow_upgrade=False) == []


def test_real_sqlite_preserves_legacy_rows_ids_defaults_indexes_and_large_inserts(
    legacy_engine, monkeypatch,
):
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    with legacy_engine.connect() as connection:
        objects = _sqlite_objects(connection)
        columns = inspect(connection).get_columns(migration.TABLE)
        row = connection.exec_driver_sql("SELECT * FROM db_slow_sql").one()
    assert runner.run_pending_migrations(legacy_engine, allow_upgrade=True) == [
        migration.REVISION
    ]
    with legacy_engine.begin() as connection:
        assert _sqlite_objects(connection) == objects
        after_columns = inspect(connection).get_columns(migration.TABLE)
        assert [(c['name'], str(c['type']), c['nullable'], c['default']) for c in columns] == [
            (c['name'], str(c['type']), c['nullable'], c['default']) for c in after_columns
        ]
        assert connection.exec_driver_sql("SELECT * FROM db_slow_sql").one() == row
        assert row == (41, 7, "oracle", 2147483647, 0, 7, None)
        for offset, value in enumerate((0, 2**31, 2**32, 10**12, MAX_SAFE_INTEGER), 42):
            result = connection.execute(DBSlowSQL.__table__.insert().values(
                instance_id=7, db_type="oracle",
                **dict.fromkeys(migration.COUNTERS, value),
            ))
            assert result.inserted_primary_key == (offset,)
            assert connection.execute(text(
                "SELECT executions, logical_reads, physical_reads, rows_processed "
                "FROM db_slow_sql WHERE id = :id"
            ), {"id": offset}).one() == (value,) * 4
        migration.upgrade(connection)
        assert _sqlite_objects(connection) == objects
    assert runner.run_pending_migrations(legacy_engine, allow_upgrade=True) == []
    with legacy_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM app_schema_migration WHERE revision = :revision"
        ), {"revision": migration.REVISION}).scalar_one() == 1


def test_absent_table_revision_can_precede_model_bootstrap(monkeypatch):
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    engine = create_engine("sqlite://")
    try:
        assert runner.run_pending_migrations(engine, allow_upgrade=True) == [migration.REVISION]
        assert not inspect(engine).has_table(migration.TABLE)
        DBSlowSQL.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(DBSlowSQL.__table__.insert().values(
                instance_id=1, db_type="oracle",
                **dict.fromkeys(migration.COUNTERS, MAX_SAFE_INTEGER),
            ))
        assert runner.run_pending_migrations(engine, allow_upgrade=False) == []
    finally:
        engine.dispose()


def test_destructive_downgrade_rejected_without_losing_journal_or_data(
    legacy_engine, monkeypatch,
):
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    runner.run_pending_migrations(legacy_engine, allow_upgrade=True)
    with legacy_engine.begin() as connection:
        connection.execute(text("UPDATE db_slow_sql SET executions = :value"), {
            "value": MAX_SAFE_INTEGER,
        })
    with pytest.raises(RuntimeError, match="retain BIGINT"):
        runner.downgrade_revision(legacy_engine, migration.REVISION)
    with legacy_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT executions FROM db_slow_sql").scalar_one() == MAX_SAFE_INTEGER
        assert migration.REVISION in runner._applied_revisions(connection)


def _mysql_columns():
    return [dict(
        COLUMN_NAME=name, DATA_TYPE="int", COLUMN_TYPE="int(11)",
        IS_NULLABLE="YES", COLUMN_DEFAULT=None, EXTRA="", COLUMN_COMMENT="",
    ) for name in migration.COUNTERS]


def _pg_columns():
    return [dict(name=name, type=Integer(), nullable=True, default=None)
            for name in migration.COUNTERS]


class ContractConnection:
    """Models metadata and successful DDL, not actual server execution/locking."""

    def __init__(self, dialect, columns=None, exists=True):
        self.dialect = postgresql.dialect() if dialect == "postgresql" else mysql.dialect()
        self.dialect.name = dialect
        self.columns = columns if columns is not None else (
            _pg_columns() if dialect == "postgresql" else _mysql_columns()
        )
        self.exists = exists
        self.ddl = []
        self.fail_journal = False
        self.journal = set()

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if "information_schema.COLUMNS" in sql:
            assert parameters == {"table_name": migration.TABLE}
            assert "TABLE_SCHEMA = DATABASE()" in sql
            return SimpleNamespace(mappings=lambda: self.columns)
        assert sql.startswith("INSERT INTO app_schema_migration")
        if self.fail_journal:
            raise RuntimeError("journal insert failed")
        self.journal.add(migration.REVISION)

    def exec_driver_sql(self, sql):
        self.ddl.append(sql)
        for column in self.columns:
            if self.dialect.name == "postgresql":
                column["type"] = BigInteger()
            else:
                column["DATA_TYPE"] = "bigint"
                column["COLUMN_TYPE"] = "bigint(20)"


@pytest.fixture
def contract_inspector(monkeypatch):
    def fake_inspect(connection):
        return SimpleNamespace(
            has_table=lambda name: connection.exists,
            get_table_names=lambda: [migration.TABLE] if connection.exists else [],
            get_columns=lambda name: connection.columns,
        )
    monkeypatch.setattr(migration, "inspect", fake_inspect)


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_sql_contract_single_alter_preserves_attributes_and_repeat(
    dialect, contract_inspector,
):
    columns = _mysql_columns()
    columns[0].update(IS_NULLABLE="NO", COLUMN_DEFAULT="0", COLUMN_COMMENT="owner's :counter")
    columns[1].update(COLUMN_DEFAULT="'12'")
    columns[2].update(COLUMN_DEFAULT="NULL")
    connection = ContractConnection(dialect, columns)
    migration.upgrade(connection)
    assert connection.ddl == [
        "ALTER TABLE db_slow_sql "
        "MODIFY COLUMN executions BIGINT NOT NULL DEFAULT 0 COMMENT 'owner''s :counter', "
        "MODIFY COLUMN logical_reads BIGINT NULL DEFAULT 12, "
        "MODIFY COLUMN physical_reads BIGINT NULL DEFAULT NULL, "
        "MODIFY COLUMN rows_processed BIGINT NULL DEFAULT NULL"
    ]
    migration.upgrade(connection)
    assert len(connection.ddl) == 1


@pytest.mark.parametrize("dialect_name", ["mysql", "mariadb"])
@pytest.mark.parametrize("sql_mode", [
    "", "ANSI_QUOTES", "NO_BACKSLASH_ESCAPES", "ANSI_QUOTES,NO_BACKSLASH_ESCAPES",
])
def test_mysql_comment_actual_sqlalchemy_pymysql_rendering_boundary(
    dialect_name, sql_mode, contract_inspector, monkeypatch,
):
    """Real driver rendering, with only network I/O replaced; not server acceptance."""
    import pymysql
    from pymysql.constants import SERVER_STATUS
    from sqlalchemy.dialects.mysql.pymysql import MySQLDialect_pymysql
    from sqlalchemy.engine import Engine, URL
    from sqlalchemy.pool import StaticPool

    wire_sql = []

    class OfflineCursor(pymysql.cursors.Cursor):
        def _query(self, query):
            wire_sql.append(query)
            self.rowcount = 0
            self.description = None
            return 0

    no_backslash_escapes = "NO_BACKSLASH_ESCAPES" in sql_mode
    dbapi_connection = pymysql.Connection(defer_connect=True, cursorclass=OfflineCursor)
    dbapi_connection.server_status = (
        SERVER_STATUS.SERVER_STATUS_NO_BACKSLASH_ESCAPES if no_backslash_escapes else 0
    )
    monkeypatch.setattr(dbapi_connection, "rollback", lambda: None)
    dialect = MySQLDialect_pymysql(dbapi=pymysql)
    dialect.name = dialect_name
    dialect._sql_mode = sql_mode
    dialect._detect_ansiquotes(None)
    engine = Engine(
        StaticPool(creator=lambda: dbapi_connection), dialect,
        URL.create("mysql+pymysql"),
    )
    comment = r"100% %(counter)s %s C:\new\path owner\'s :counter"
    metadata_connection = ContractConnection(dialect_name)
    metadata_connection.columns[-1]["COLUMN_COMMENT"] = comment
    metadata_connection.dialect = dialect
    try:
        with engine.connect() as connection:
            # Keep metadata fake but execute migration DDL through a real SQLAlchemy
            # Connection and real PyMySQL Cursor.execute/mogrify implementation.
            metadata_connection.exec_driver_sql = connection.exec_driver_sql
            migration.upgrade(metadata_connection)
    finally:
        engine.dispose()
    escaped = comment if no_backslash_escapes else comment.replace("\\", "\\\\")
    expected_literal = "'" + escaped.replace("'", "''") + "'"
    assert len(wire_sql) == 1
    assert wire_sql[0].endswith(" COMMENT " + expected_literal)
    assert "100%%" not in wire_sql[0]


@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "postgresql"])
def test_sql_contract_partial_widening_and_journal_failure_can_resume(
    dialect, contract_inspector, monkeypatch,
):
    connection = ContractConnection(dialect)
    if dialect == "postgresql":
        connection.columns[0]["type"] = BigInteger()
    else:
        connection.columns[0].update(DATA_TYPE="bigint", COLUMN_TYPE="bigint(20)")
    monkeypatch.setattr(runner, "_ensure_migration_table", lambda conn: None)
    monkeypatch.setattr(runner, "_applied_revisions", lambda conn: set(conn.journal))
    connection.fail_journal = True
    with pytest.raises(RuntimeError, match="journal insert failed"):
        runner.apply_migrations(connection, (migration,))
    assert len(connection.ddl) == 1
    assert "executions" not in connection.ddl[0]
    assert not connection.journal
    connection.fail_journal = False
    assert runner.apply_migrations(connection, (migration,)) == [migration.REVISION]
    assert runner.apply_migrations(connection, (migration,)) == []
    assert len(connection.ddl) == 1


def test_postgresql_sql_contract_type_only_preserves_other_attributes(contract_inspector):
    columns = _pg_columns()
    columns[0].update(nullable=False, default="0", comment="retained")
    connection = ContractConnection("postgresql", columns)
    before = deepcopy(columns)
    migration.upgrade(connection)
    assert connection.ddl == ["ALTER TABLE db_slow_sql " + ", ".join(
        f"ALTER COLUMN {name} TYPE BIGINT" for name in migration.COUNTERS
    )]
    for old, new in zip(before, columns):
        assert {k: v for k, v in old.items() if k != "type"} == {
            k: v for k, v in new.items() if k != "type"
        }
    migration.upgrade(connection)
    assert len(connection.ddl) == 1


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize("invalid", [
    {"DATA_TYPE": "varchar", "COLUMN_TYPE": "varchar(20)"},
    {"COLUMN_TYPE": "int(11) unsigned"},
    {"COLUMN_TYPE": "int(11) zerofill"},
    {"EXTRA": "auto_increment"},
    {"EXTRA": "STORED GENERATED"},
    {"EXTRA": "INVISIBLE"},
    {"COLUMN_DEFAULT": "(rand())"},
    {"COLUMN_DEFAULT": str(2**63)},
    {"IS_NULLABLE": "unknown"},
])
def test_mysql_sql_contract_validates_last_column_before_any_ddl(
    dialect, invalid, contract_inspector,
):
    connection = ContractConnection(dialect)
    connection.columns[-1].update(invalid)
    with pytest.raises(RuntimeError, match="unexpected db_slow_sql.rows_processed"):
        migration.upgrade(connection)
    assert connection.ddl == []


@pytest.mark.parametrize("invalid", [{"type": String()}, {"computed": {}}, {"identity": {}}])
def test_postgresql_sql_contract_validates_all_columns_before_ddl(
    invalid, contract_inspector,
):
    connection = ContractConnection("postgresql")
    connection.columns[-1].update(invalid)
    with pytest.raises(RuntimeError, match="unexpected db_slow_sql.rows_processed"):
        migration.upgrade(connection)
    assert connection.ddl == []


@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "postgresql"])
def test_sql_contract_missing_column_is_not_journaled(dialect, contract_inspector, monkeypatch):
    connection = ContractConnection(dialect)
    connection.columns.pop()
    monkeypatch.setattr(runner, "_ensure_migration_table", lambda conn: None)
    monkeypatch.setattr(runner, "_applied_revisions", lambda conn: set(conn.journal))
    with pytest.raises(RuntimeError, match="missing column"):
        runner.apply_migrations(connection, (migration,))
    assert connection.ddl == []
    assert not connection.journal


@pytest.mark.parametrize("dialect", ["mysql", "mariadb", "postgresql"])
def test_sql_contract_absent_table_and_destructive_downgrade(dialect, contract_inspector):
    connection = ContractConnection(dialect, exists=False)
    migration.upgrade(connection)
    migration.downgrade(connection)
    assert connection.ddl == []
    connection.exists = True
    with pytest.raises(RuntimeError, match="destructive counter downgrade"):
        migration.downgrade(connection)
    assert connection.ddl == []


def test_unsupported_database_fails_even_without_table(contract_inspector):
    connection = ContractConnection("oracle", exists=False)
    with pytest.raises(RuntimeError, match="unsupported database dialect oracle"):
        migration.upgrade(connection)
    assert connection.ddl == []


def test_real_sqlite_unexpected_counter_type_fails_without_changes(monkeypatch):
    monkeypatch.setattr(runner, "MIGRATIONS", (migration,))
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE db_slow_sql (
                    executions INTEGER, logical_reads INTEGER,
                    physical_reads INTEGER, rows_processed TEXT
                )
            """)
            before = _sqlite_objects(connection)
        with pytest.raises(RuntimeError, match="expected INTEGER or BIGINT"):
            runner.run_pending_migrations(engine, allow_upgrade=True)
        with engine.connect() as connection:
            assert _sqlite_objects(connection) == before
            assert migration.REVISION not in runner._applied_revisions(connection)
    finally:
        engine.dispose()
