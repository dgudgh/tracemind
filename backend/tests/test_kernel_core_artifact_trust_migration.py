from sqlalchemy import create_engine, inspect, text

from app.migrations.versions import v20260824_03_kernel_core_artifact_trust as migration


def test_kernel_core_artifact_trust_migration_upgrades_existing_table():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE kernel_capability_report (
                id INTEGER PRIMARY KEY,
                report_id VARCHAR(64) NOT NULL
            )
        """))
        migration.upgrade(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("kernel_capability_report")}
        assert "core_artifact_status" in columns
        indexes = {index["name"] for index in inspect(connection).get_indexes("kernel_capability_report")}
        assert "ix_kernel_capability_report_core_artifact_status" in indexes
        migration.upgrade(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("kernel_capability_report")}
        assert "core_artifact_status" in columns
