"""Copied SQLite maintenance scenarios; never uses a configured customer database."""
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, event, inspect, select, text

from app.core import database
from app.migrations import runner, runtime_schema
from app.migrations.inspection_result_integrity import (
    BATCH_SIZE, FIELDS, MAX_ROW_BYTES, InspectionResultIntegrityError, copy_initial_legacy_results,
    inspect_legacy_results, reconcile_missing_legacy_results,
    _require_transactional_copy,
)
from app.migrations.locking import schema_lock
from app.models.check_result import DBCheckResult


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "legacy.sqlite"
    engine = create_engine("sqlite:///" + str(path))
    metadata = MetaData()
    source = Table("db_check_result_new", metadata,
        Column("id", Integer, primary_key=True), Column("task_id", String(80)),
        Column("instance_id", Integer),
        *(Column(name, Text) for name in FIELDS if name not in {"id", "task_id", "instance_id"}))
    metadata.create_all(engine)
    DBCheckResult.__table__.create(engine)
    try:
        yield engine, source, DBCheckResult.__table__, path
    finally:
        engine.dispose()


def source_row(identity=1, **patch):
    return {"id": identity, "task_id": str(1000 + identity), "instance_id": 7, "db_type": "mysql",
            "check_category": "connection", "check_item": "fixture", "check_status": "normal",
            "metric_name": "connections", "metric_value": "1", "threshold_value": "80",
            "finding": "private fixture evidence must not appear in diagnostics", "suggestion": "fixture",
            "raw_data": '{"b":2,"a":1}', "create_time": "2026-10-20 10:11:12.123456", **patch}


def target_row(identity=1, **patch):
    value = source_row(identity)
    value.update(raw_data={"a": 1, "b": 2}, create_time=datetime(2026, 10, 20, 10, 11, 12, 123456))
    return {**value, **patch}


def seed(env, source_rows=(), target_rows=()):
    engine, source, target, _ = env
    with engine.begin() as connection:
        if source_rows:
            connection.execute(source.insert(), list(source_rows))
        if target_rows:
            connection.execute(target.insert(), list(target_rows))


def snapshots(env):
    engine, _, _, _ = env
    with engine.connect() as connection:
        return {name: connection.exec_driver_sql(f"SELECT * FROM {name} ORDER BY id").all()
                for name in ("db_check_result_new", "db_check_result")}


def test_matching_rows_allow_resume_with_newer_target_rows_without_writing_source_or_target(env):
    seed(env, [source_row()], [target_row(), target_row(9)])
    before = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    assert report["state"] == "consistent" and report["matched_rows"] == 1 and report["target_rows"] == 2
    assert database.ensure_runtime_schema(env[0], allow_upgrade=True) == [runtime_schema.REVISION]
    assert database.ensure_runtime_schema(env[0], allow_upgrade=False) == []
    assert snapshots(env) == before


def test_equal_counts_are_not_content_equivalence_and_conflicts_are_never_overwritten(env):
    seed(env, [source_row()], [target_row(finding="different evidence")])
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError) as failure:
        database.ensure_runtime_schema(env[0], allow_upgrade=True)
    report = failure.value.report
    assert report["state"] == "conflict" and report["sample_conflicting_ids"] == [1]
    assert "inspection-results-check" in str(failure.value)
    assert "private fixture" not in str(failure.value) + json.dumps(report)
    assert snapshots(env) == before and not inspect(env[0]).has_table(runner.MIGRATION_TABLE)
    with pytest.raises(InspectionResultIntegrityError), schema_lock(env[0]) as connection:
        reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])
    assert snapshots(env) == before


def test_missing_ids_need_explicit_reconciliation_and_preserve_unrelated_new_results(env):
    seed(env, [source_row(1), source_row(2), source_row(3)], [target_row(1), target_row(9)])
    original = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    assert report["state"] == "missing" and report["missing_rows"] == 2
    with pytest.raises(InspectionResultIntegrityError):
        database.ensure_runtime_schema(env[0], allow_upgrade=True)
    assert snapshots(env) == original
    with schema_lock(env[0]) as connection:
        result = reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])
    assert result["copied_rows"] == 2 and result["after"]["state"] == "consistent"
    assert snapshots(env)["db_check_result_new"] == original["db_check_result_new"]
    assert snapshots(env)["db_check_result"][-1] == original["db_check_result"][-1]
    assert database.ensure_runtime_schema(env[0], allow_upgrade=True) == [runtime_schema.REVISION]
    with schema_lock(env[0]) as connection:
        again = reconcile_missing_legacy_results(connection, expected_snapshot_hash=result["after"]["snapshot_hash"])
    assert again["copied_rows"] == 0


def test_initial_copy_verifies_content_and_retains_original_schema_and_rows(env):
    seed(env, [source_row()])
    before = snapshots(env)["db_check_result_new"]
    assert database.ensure_runtime_schema(env[0], allow_upgrade=True) == [runtime_schema.REVISION]
    assert snapshots(env)["db_check_result_new"] == before
    with env[0].connect() as connection:
        assert inspect_legacy_results(connection)["matched_rows"] == 1


def test_postcopy_mutation_with_same_count_rolls_back_instead_of_journaling_success(env):
    seed(env, [source_row()])
    with env[0].begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER corrupt_copy AFTER INSERT ON db_check_result "
                                  "BEGIN UPDATE db_check_result SET finding='wrong' WHERE id=NEW.id; END")
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError):
        database.ensure_runtime_schema(env[0], allow_upgrade=True)
    assert snapshots(env) == before and not inspect(env[0]).has_table(runner.MIGRATION_TABLE)


@pytest.mark.parametrize("change", ["new_id", "changed_source", "changed_target"])
def test_retained_source_drift_is_detected_even_after_the_revision_was_applied(env, change):
    seed(env, [source_row()])
    database.ensure_runtime_schema(env[0], allow_upgrade=True)
    with env[0].begin() as connection:
        if change == "new_id":
            connection.execute(env[1].insert().values(**source_row(2)))
        else:
            table = env[1] if change == "changed_source" else env[2]
            connection.execute(table.update().where(table.c.id == 1).values(finding="changed after upgrade"))
    before = snapshots(env)
    for allow_upgrade in (False, True):
        with pytest.raises(InspectionResultIntegrityError):
            database.ensure_runtime_schema(env[0], allow_upgrade=allow_upgrade)
    assert snapshots(env) == before
    with env[0].connect() as connection:
        assert runtime_schema.REVISION in runner._applied_revisions(connection)


def test_snapshot_change_blocks_repair_before_any_insert(env):
    seed(env, [source_row(1)], [target_row(9)])
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    with env[0].begin() as connection:
        connection.execute(env[1].insert().values(**source_row(2)))
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError, match="snapshot_changed"), schema_lock(env[0]) as connection:
        reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])
    assert snapshots(env) == before


def test_write_helpers_refuse_an_unlocked_connection_before_any_insert(env):
    seed(env, [source_row()])
    before = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
        for call in (lambda: copy_initial_legacy_results(connection),
                     lambda: reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])):
            with pytest.raises(RuntimeError, match="schema_lock"):
                call()
    assert snapshots(env) == before


def test_version_only_compatibility_entrypoint_does_not_hide_retained_source_drift(env):
    seed(env, [source_row()])
    database.ensure_runtime_schema(env[0], allow_upgrade=True)
    with env[0].begin() as connection:
        connection.execute(env[1].insert().values(**source_row(2)))
    with pytest.raises(InspectionResultIntegrityError):
        runner.run_pending_migrations(env[0], allow_upgrade=False)


def test_version_only_entrypoint_cannot_defer_a_runtime_copy_it_does_not_perform(env, monkeypatch):
    seed(env, [source_row()])
    monkeypatch.setattr(runner, "MIGRATIONS", ())
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError, match="missing"):
        runner.run_pending_migrations(env[0], allow_upgrade=True)
    assert snapshots(env) == before and not inspect(env[0]).has_table(runner.MIGRATION_TABLE)


@pytest.mark.parametrize("patch", [{"task_id": "1" * 31}, {"raw_data": '{"a":1,"a":2}'},
                                  {"raw_data": '{"a":NaN}'}, {"create_time": "not-a-timestamp"},
                                  {"raw_data": '{"x":1e9999999999999999999999999}'},
                                  {"create_time": "2026-10-20 10:11:12.1234567"},
                                  {"create_time": "2026-10-20 10:11:12+01:00"},
                                  {"instance_id": "\u0661"},
                                  {"check_item": "x" * 101}, {"instance_id": None}])
def test_lossy_or_invalid_legacy_values_are_reported_without_copying_or_payload_leaks(env, patch):
    seed(env, [source_row(**patch)])
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError) as failure:
        database.ensure_runtime_schema(env[0], allow_upgrade=True)
    assert failure.value.report["state"] == "invalid_rows"
    assert failure.value.report["invalid_rows"] == 1
    assert "private fixture" not in str(failure.value) + json.dumps(failure.value.report)
    assert snapshots(env) == before


def test_comparison_is_read_only_and_batched_with_bounded_samples(env):
    seed(env, [source_row(index) for index in range(1, 252)], [target_row(999)])
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(env[0], "before_cursor_execute", observe)
    try:
        with env[0].connect() as connection:
            report = inspect_legacy_results(connection)
    finally:
        event.remove(env[0], "before_cursor_execute", observe)
    assert report["missing_rows"] == 251 and len(report["sample_missing_ids"]) == 20
    assert not any(statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"}
                   for statement in statements)
    assert sum("LIMIT" in statement for statement in statements) == math.ceil(251 / BATCH_SIZE) + 1


@pytest.mark.parametrize("effect", ["DELETE FROM db_check_result WHERE id=9",
                                   "UPDATE db_check_result SET finding='changed' WHERE id=9",
                                   "UPDATE db_check_result_new SET finding='changed' WHERE id=1",
                                   "UPDATE db_check_result_new SET finding='changed' WHERE id=1; "
                                   "UPDATE db_check_result SET finding='changed' WHERE id=1"])
def test_reconcile_rolls_back_trigger_side_effects_to_source_or_unrelated_target(env, effect):
    seed(env, [source_row()], [target_row(9)])
    with env[0].begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER unexpected_side_effect AFTER INSERT ON db_check_result "
                                   f"WHEN NEW.id=1 BEGIN {effect}; END")
    before = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    with pytest.raises(InspectionResultIntegrityError), schema_lock(env[0]) as connection:
        reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])
    assert snapshots(env) == before


def test_initial_copy_detects_changes_to_retained_source_extra_columns(env):
    seed(env, [source_row()])
    with env[0].begin() as connection:
        connection.exec_driver_sql("ALTER TABLE db_check_result_new ADD COLUMN legacy_note TEXT")
        connection.exec_driver_sql("UPDATE db_check_result_new SET legacy_note='keep'")
        connection.exec_driver_sql("CREATE TRIGGER mutate_extra AFTER INSERT ON db_check_result "
                                   "BEGIN UPDATE db_check_result_new SET legacy_note='changed'; END")
    before = snapshots(env)
    with pytest.raises(InspectionResultIntegrityError), schema_lock(env[0]) as connection:
        copy_initial_legacy_results(connection)
    assert snapshots(env) == before


def test_reconciliation_refuses_unmodelled_target_columns_without_changing_rows(env):
    seed(env, [source_row()], [target_row(9)])
    with env[0].begin() as connection:
        connection.exec_driver_sql("ALTER TABLE db_check_result ADD COLUMN custom_note TEXT")
    before = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    assert report["target_extra_columns"] == ["custom_note"]
    with pytest.raises(InspectionResultIntegrityError, match="unsupported_target_columns"), schema_lock(env[0]) as connection:
        reconcile_missing_legacy_results(connection, expected_snapshot_hash=report["snapshot_hash"])
    assert snapshots(env) == before


def test_database_side_size_guard_refuses_large_payload_before_loading_rows(env):
    seed(env, [source_row(finding="x" * (MAX_ROW_BYTES + 1))])
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(env[0], "before_cursor_execute", observe)
    try:
        with env[0].connect() as connection:
            report = inspect_legacy_results(connection)
    finally:
        event.remove(env[0], "before_cursor_execute", observe)
    assert report["state"] == "evidence_too_large" and report["oversized_rows"] == {"db_check_result_new": 1}
    assert not any("ORDER BY db_check_result_new.id" in statement for statement in statements)
    with pytest.raises(InspectionResultIntegrityError), schema_lock(env[0]) as connection:
        copy_initial_legacy_results(connection)
    with env[0].connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM db_check_result").scalar_one() == 0


def test_submicrosecond_source_and_microsecond_target_are_not_reported_equivalent(env):
    seed(env, [source_row(create_time="2026-10-20 10:11:12.1234567")], [target_row()])
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    assert report["state"] == "invalid_rows" and report["matched_rows"] == 0


def test_sqlite_64bit_ids_and_references_are_preserved_without_false_overflow(env):
    identity = 2 ** 40
    seed(env, [source_row(identity, instance_id=identity)], [target_row(identity, instance_id=identity)])
    before = snapshots(env)
    with env[0].connect() as connection:
        report = inspect_legacy_results(connection)
    assert report["state"] == "consistent" and report["matched_rows"] == 1
    assert snapshots(env) == before


@pytest.mark.parametrize(("old", "new", "state"), [
    ('{"n":9007199254740993.1}', '{"n":9007199254740992.9}', "conflict"),
    ('{"n":1.2}', '{"n":"1.2"}', "conflict"),
    ('{"n":1.0}', '{"n":1}', "consistent"),
    ('{"n":0.00}', '{"n":0}', "consistent"),
    ('null', None, "conflict"),
])
def test_json_comparison_preserves_precision_and_distinguishes_numbers_from_strings(env, old, new, state):
    seed(env, [source_row(raw_data=old)], [target_row()])
    with env[0].begin() as connection:
        connection.exec_driver_sql("UPDATE db_check_result SET raw_data=? WHERE id=1", (new,))
        report = inspect_legacy_results(connection)
    assert report["state"] == state


@pytest.mark.parametrize(("dialect", "storage", "triggers", "state"), [
    ("mysql", [("db_check_result", "MyISAM"), ("db_check_result_new", "InnoDB")], 0, "unsupported_storage"),
    ("mysql", [("db_check_result", "InnoDB"), ("db_check_result_new", "InnoDB")], 1, "unsupported_target_triggers"),
    ("mariadb", [("db_check_result", "InnoDB"), ("db_check_result_new", "InnoDB")], 0, None),
    ("postgresql", ["f", "r"], 0, "unsupported_storage"),
    ("postgresql", ["r", "r"], 1, "unsupported_target_triggers"),
    ("postgresql", ["r", "r"], 0, None),
])
def test_storage_guard_decisions_refuse_unrollbackable_copies(dialect, storage, triggers, state):
    # This tests branching only, not a live engine/trigger/lock acceptance claim.
    result = SimpleNamespace(all=lambda: storage)
    result.scalars = lambda: result
    def scalar(statement, *args):
        if "CURRENT_USER" in str(statement):
            return "fixture@localhost"
        if "PRIVILEGE_TYPE" in str(statement):
            return 1
        return triggers
    connection = SimpleNamespace(dialect=SimpleNamespace(name=dialect), execute=lambda *args: result,
                                 scalar=scalar)
    report = {"state": "missing", "missing_rows": 1, "conflicting_rows": 0, "invalid_rows": 0}
    if state:
        with pytest.raises(InspectionResultIntegrityError) as failure:
            _require_transactional_copy(connection, report)
        assert failure.value.report["state"] == state
    else:
        _require_transactional_copy(connection, report)


def test_mysql_hidden_trigger_catalog_is_not_treated_as_an_empty_trigger_catalog():
    calls = []
    result = SimpleNamespace(all=lambda: [("db_check_result", "InnoDB"), ("db_check_result_new", "InnoDB")])
    def scalar(statement, *args):
        calls.append(str(statement))
        return "fixture@localhost" if "CURRENT_USER" in str(statement) else None
    connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), execute=lambda *args: result, scalar=scalar)
    report = {"state": "missing", "missing_rows": 1, "conflicting_rows": 0, "invalid_rows": 0}
    with pytest.raises(InspectionResultIntegrityError) as failure:
        _require_transactional_copy(connection, report)
    assert failure.value.report["state"] == "trigger_visibility_unverified"
    assert not any("information_schema.triggers" in statement for statement in calls)
    privilege_query = next(statement for statement in calls if "PRIVILEGE_TYPE" in statement)
    assert privilege_query.count("CAST(GRANTEE AS BINARY)=CAST(:grantee AS BINARY)") == 2
    assert "CAST(TABLE_NAME AS BINARY)=CAST(:target AS BINARY)" in privilege_query


@pytest.mark.parametrize("partial,allowed", [([("partial_revokes", "ON")], False),
                                            ([], False), ([("partial_revokes", "OFF")], True)])
def test_mysql_global_trigger_grant_requires_positive_exclusion_of_partial_revokes(partial, allowed):
    calls = []
    def execute(statement, *args):
        rows = partial if "SHOW GLOBAL" in str(statement) else [("db_check_result", "InnoDB"), ("db_check_result_new", "InnoDB")]
        return SimpleNamespace(all=lambda: rows)
    def scalar(statement, *args):
        calls.append(str(statement))
        if "CURRENT_USER" in str(statement):
            return "fixture@localhost"
        if "USER_PRIVILEGES" in str(statement):
            return 1
        return 0
    connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), execute=execute, scalar=scalar)
    report = {"state": "missing", "missing_rows": 1, "conflicting_rows": 0, "invalid_rows": 0}
    if allowed:
        _require_transactional_copy(connection, report)
        assert any("information_schema.triggers" in statement for statement in calls)
    else:
        with pytest.raises(InspectionResultIntegrityError, match="trigger_visibility_unverified"):
            _require_transactional_copy(connection, report)
        assert not any("information_schema.triggers" in statement for statement in calls)


def cli(env, *arguments):
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "DATABASE_URL": "sqlite:///" + str(env[3]), "DB_TYPE": "sqlite",
                   "APP_MODE": "production", "TRACEMIND_AUTO_MIGRATE": "0", "PYTHONPATH": str(root / "backend")}
    result = subprocess.run([sys.executable, str(root / "scripts/migrate_schema.py"), *arguments],
                            cwd=root, env=environment, text=True, capture_output=True, timeout=30)
    assert "private fixture" not in result.stdout + result.stderr
    return result


def test_actual_cli_exposes_readonly_report_flags_and_snapshot_guard(env):
    seed(env, [source_row(1), source_row(2)], [target_row(9)])
    before = snapshots(env)
    check = cli(env, "inspection-results-check")
    assert check.returncode == 3, check.stderr
    report = json.loads(check.stdout)["inspection_results"]
    assert report["state"] == "missing" and snapshots(env) == before
    for flags in ([], ["--backup-verified"], ["--service-quiesced"]):
        refused = cli(env, "inspection-results-reconcile", "--expected-snapshot-hash", report["snapshot_hash"], *flags)
        assert refused.returncode == 2 and snapshots(env) == before
    stale = cli(env, "inspection-results-reconcile", "--expected-snapshot-hash", "0" * 64,
                "--backup-verified", "--service-quiesced")
    assert stale.returncode == 3 and json.loads(stale.stdout)["inspection_results"]["state"] == "snapshot_changed"
    repaired = cli(env, "inspection-results-reconcile", "--expected-snapshot-hash", report["snapshot_hash"],
                   "--backup-verified", "--service-quiesced")
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout)["reconciliation"]["copied_rows"] == 2
    check = cli(env, "inspection-results-check")
    assert check.returncode == 0 and json.loads(check.stdout)["inspection_results"]["state"] == "consistent"
    assert snapshots(env)["db_check_result_new"] == before["db_check_result_new"]


def test_actual_cli_reports_applied_revision_but_blocks_new_orphan_rows(env):
    seed(env, [source_row()])
    database.ensure_runtime_schema(env[0], allow_upgrade=True)
    with env[0].begin() as connection:
        connection.execute(env[1].insert().values(**source_row(2)))
    before = snapshots(env)
    status = cli(env, "status")
    assert status.returncode == 3, status.stderr
    assert json.loads(status.stdout)["code"] == InspectionResultIntegrityError.code
    assert snapshots(env) == before


def test_real_lifespan_integrity_failure_is_actionable_and_does_not_start_schedulers(env, monkeypatch):
    from app import main
    from app.tasks import master_scheduler

    seed(env, [source_row()], [target_row(finding="conflicting source")])
    before = snapshots(env)
    calls = []
    monkeypatch.setattr(main, "engine", env[0])
    monkeypatch.setattr(main.settings, "DATABASE_URL", "sqlite:///" + str(env[3]))
    monkeypatch.setattr(main.settings, "DB_TYPE", "sqlite")
    monkeypatch.setattr(master_scheduler, "start_master_scheduler", lambda: calls.append("scheduler"))

    async def start():
        async with main.lifespan(main.app):
            calls.append("started")

    with pytest.raises(RuntimeError, match="inspection-results-check") as failure:
        asyncio.run(start())
    assert isinstance(failure.value.__cause__, InspectionResultIntegrityError)
    assert "network" not in str(failure.value) and "网络" not in str(failure.value)
    assert calls == [] and snapshots(env) == before
