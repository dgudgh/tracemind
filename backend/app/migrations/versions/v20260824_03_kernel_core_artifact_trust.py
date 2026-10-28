from __future__ import annotations

from sqlalchemy import inspect, text


REVISION = "20260824_03_kernel_core_artifact_trust"
DESCRIPTION = "Persist eBPF CO-RE object trust verification status"


def _column_names(connection, table_name: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def _index_names(connection, table_name: str) -> set[str]:
    return {index["name"] for index in inspect(connection).get_indexes(table_name)}


def upgrade(connection) -> None:
    if not inspect(connection).has_table("kernel_capability_report"):
        return
    columns = _column_names(connection, "kernel_capability_report")
    if "core_artifact_status" not in columns:
        connection.execute(text(
            "ALTER TABLE kernel_capability_report "
            "ADD COLUMN core_artifact_status VARCHAR(30) NOT NULL DEFAULT 'not_applicable'"
        ))
    if "ix_kernel_capability_report_core_artifact_status" not in _index_names(connection, "kernel_capability_report"):
        connection.execute(text(
            "CREATE INDEX ix_kernel_capability_report_core_artifact_status "
            "ON kernel_capability_report (core_artifact_status)"
        ))


def downgrade(connection) -> None:
    if not inspect(connection).has_table("kernel_capability_report"):
        return
    index_name = "ix_kernel_capability_report_core_artifact_status"
    if index_name in _index_names(connection, "kernel_capability_report"):
        if connection.dialect.name in {"mysql", "mariadb"}:
            connection.execute(text(
                f"DROP INDEX {index_name} ON kernel_capability_report"
            ))
        else:
            connection.execute(text(f"DROP INDEX {index_name}"))
    if "core_artifact_status" in _column_names(connection, "kernel_capability_report"):
        connection.execute(text(
            "ALTER TABLE kernel_capability_report DROP COLUMN core_artifact_status"
        ))
