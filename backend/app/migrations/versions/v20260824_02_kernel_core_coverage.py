from __future__ import annotations

from sqlalchemy import inspect, text


REVISION = "20260824_02_kernel_core_coverage"
DESCRIPTION = "Persist per-sensor eBPF CO-RE coverage and embedded object checksum"


def _column_names(connection, table_name: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def upgrade(connection) -> None:
    if not inspect(connection).has_table("kernel_capability_report"):
        return
    columns = _column_names(connection, "kernel_capability_report")
    if "core_sensors_json" not in columns:
        connection.execute(text(
            "ALTER TABLE kernel_capability_report ADD COLUMN core_sensors_json JSON"
        ))
    if "core_object_sha256" not in columns:
        connection.execute(text(
            "ALTER TABLE kernel_capability_report ADD COLUMN core_object_sha256 VARCHAR(64)"
        ))


def downgrade(connection) -> None:
    if not inspect(connection).has_table("kernel_capability_report"):
        return
    columns = _column_names(connection, "kernel_capability_report")
    if "core_object_sha256" in columns:
        connection.execute(text(
            "ALTER TABLE kernel_capability_report DROP COLUMN core_object_sha256"
        ))
    columns = _column_names(connection, "kernel_capability_report")
    if "core_sensors_json" in columns:
        connection.execute(text(
            "ALTER TABLE kernel_capability_report DROP COLUMN core_sensors_json"
        ))
