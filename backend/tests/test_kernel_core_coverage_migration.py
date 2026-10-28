from sqlalchemy import create_engine, inspect, text

from app.migrations.versions import v20260824_02_kernel_core_coverage as migration


def test_kernel_core_coverage_migration_upgrades_existing_table():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE kernel_capability_report (
                id INTEGER PRIMARY KEY,
                report_id VARCHAR(64) NOT NULL,
                supported_sensors_json JSON
            )
        """))
        migration.upgrade(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("kernel_capability_report")}
        assert {"core_sensors_json", "core_object_sha256"}.issubset(columns)
        migration.upgrade(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("kernel_capability_report")}
        assert {"core_sensors_json", "core_object_sha256"}.issubset(columns)
