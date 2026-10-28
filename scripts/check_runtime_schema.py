from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def seed_legacy_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE db_instance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_name VARCHAR(100) NOT NULL,
                db_type VARCHAR(20) NOT NULL,
                host VARCHAR(100) NOT NULL,
                port INTEGER NOT NULL,
                oracle_connect_type VARCHAR(20),
                service_name VARCHAR(100),
                sid VARCHAR(100),
                database_name VARCHAR(100),
                username VARCHAR(100) NOT NULL,
                password_cipher TEXT NOT NULL,
                env_type VARCHAR(30),
                factory_name VARCHAR(100),
                line_name VARCHAR(100),
                enabled INTEGER DEFAULT 1,
                remark VARCHAR(500),
                create_time DATETIME,
                update_time DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE db_check_result_new (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                instance_id INTEGER NOT NULL,
                db_type VARCHAR(20) NOT NULL,
                check_category VARCHAR(50),
                check_item VARCHAR(100),
                check_status VARCHAR(20),
                metric_name VARCHAR(100),
                metric_value VARCHAR(500),
                threshold_value VARCHAR(500),
                finding TEXT,
                suggestion TEXT,
                raw_data TEXT,
                create_time DATETIME
            )
            """
        )
        conn.execute(
            """
            INSERT INTO db_check_result_new (
                id, task_id, instance_id, db_type, check_category, check_item,
                check_status, metric_name, metric_value, threshold_value,
                finding, suggestion, raw_data, create_time
            )
            VALUES (
                1, 2026061401, 7, 'oracle', 'basic', 'legacy result',
                'normal', 'status', '1', '1',
                'ok', 'none', '{"source":"legacy"}', '2026-06-14 00:00:00'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE db_capacity_policy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                frequency_minutes INTEGER DEFAULT 1440,
                forecast_days_threshold INTEGER DEFAULT 15,
                abnormal_growth_mb DECIMAL(18, 2),
                abnormal_growth_pct DECIMAL(8, 2),
                retention_days INTEGER DEFAULT 180,
                last_run_time DATETIME,
                next_run_time DATETIME,
                create_time DATETIME,
                update_time DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE db_awr_collect_policy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                enabled INTEGER DEFAULT 0,
                interval_hours INTEGER DEFAULT 24,
                days_window INTEGER DEFAULT 1,
                report_format VARCHAR(20),
                report_type VARCHAR(50),
                auto_analyze INTEGER DEFAULT 1,
                last_run_time DATETIME,
                next_run_time DATETIME,
                create_time DATETIME,
                update_time DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE db_perf_metric_policy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                interval_minutes INTEGER DEFAULT 30,
                last_run_time DATETIME,
                next_run_time DATETIME,
                create_time DATETIME,
                update_time DATETIME
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def run_runtime_migration(db_path: Path) -> list[str]:
    os.environ["DATABASE_URL"] = "sqlite:///" + str(db_path).replace("\\", "/")
    os.environ["DB_TYPE"] = "sqlite"
    os.environ["DEBUG"] = "false"
    os.environ["APP_MODE"] = "schema_check"
    sys.path.insert(0, str(BACKEND))

    import app.models  # noqa: F401
    from sqlalchemy import create_engine
    from app.core.database import ensure_runtime_schema

    engine = create_engine("sqlite:///" + str(db_path))
    try:
        return ensure_runtime_schema(engine, allow_upgrade=True)
    finally:
        engine.dispose()


def expected_model_columns(model) -> set[str]:
    return {column.name for column in model.__table__.columns}


def expected_instance_columns() -> set[str]:
    from app.models.db_instance import DBInstance

    return expected_model_columns(DBInstance)


def expected_check_result_columns() -> set[str]:
    from app.models.check_result import DBCheckResult

    return expected_model_columns(DBCheckResult)


def check_runtime_schema(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    try:
        instance_columns = table_columns(conn, "db_instance")
        check_result_columns = table_columns(conn, "db_check_result")
        policy_status_columns = {
            table_name: sorted({"last_status", "last_message"} - table_columns(conn, table_name))
            for table_name in ("db_capacity_policy", "db_awr_collect_policy", "db_perf_metric_policy")
        }
        migrated = conn.execute(
            "SELECT task_id, instance_id, db_type, check_item FROM db_check_result WHERE id=1"
        ).fetchone()
        from app.migrations.runtime_schema import REVISION

        source_rows = conn.execute("SELECT * FROM db_check_result_new ORDER BY id").fetchall()
        target_rows = conn.execute("SELECT * FROM db_check_result ORDER BY id").fetchall()
        normalized_source = [(row[0], str(row[1]), *row[2:]) for row in source_rows]
        revision_count = conn.execute(
            "SELECT COUNT(*) FROM app_schema_migration WHERE revision = ?", (REVISION,),
        ).fetchone()[0]
        return {
            "missing_instance_columns": sorted(expected_instance_columns() - instance_columns),
            "missing_check_result_columns": sorted(expected_check_result_columns() - check_result_columns),
            "missing_policy_status_columns": policy_status_columns,
            "legacy_result_table_exists": table_exists(conn, "db_check_result_new"),
            "migrated_check_result": list(migrated) if migrated else None,
            "legacy_source_rows": len(source_rows),
            "copied_target_rows": len(target_rows),
            "copied_rows_match": normalized_source == target_rows,
            "runtime_revision_count": revision_count,
        }
    finally:
        conn.close()


def source_snapshot(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'db_check_result_new' ORDER BY type, name"
        ).fetchall()
        rows = conn.execute("SELECT * FROM db_check_result_new ORDER BY id").fetchall()
        return schema, rows


def database_snapshot(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        return tuple(conn.iterdump())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mesguard-schema-") as tmp:
        db_path = Path(tmp) / "legacy.sqlite"
        seed_legacy_sqlite(db_path)
        original_source = source_snapshot(db_path)
        first_applied = run_runtime_migration(db_path)
        first_snapshot = database_snapshot(db_path)
        second_applied = run_runtime_migration(db_path)
        report = check_runtime_schema(db_path)
        report["legacy_source_unchanged"] = source_snapshot(db_path) == original_source
        report["idempotent"] = not second_applied and first_snapshot == database_snapshot(db_path)
        report["first_applied"] = first_applied

    failures = []
    if report["missing_instance_columns"]:
        failures.append("missing instance columns")
    if report["missing_check_result_columns"]:
        failures.append("missing check result columns")
    missing_policy_status = {
        table: columns
        for table, columns in report["missing_policy_status_columns"].items()
        if columns
    }
    if missing_policy_status:
        failures.append("missing scheduler policy status columns")
    if not report["legacy_result_table_exists"] or not report["legacy_source_unchanged"]:
        failures.append("legacy source table or records were not preserved")
    if not report["copied_rows_match"] or report["legacy_source_rows"] != report["copied_target_rows"]:
        failures.append("legacy result copy differs from source")
    if report["runtime_revision_count"] != 1 or not report["first_applied"]:
        failures.append("runtime migration was not recorded exactly once")
    if not report["idempotent"]:
        failures.append("repeated runtime migration changed database state")
    if report["migrated_check_result"] != ["2026061401", 7, "oracle", "legacy result"]:
        failures.append("legacy check result was not migrated correctly")

    summary = {"ok": not failures, "failures": failures, **report}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
