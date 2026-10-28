from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.collectors.oracle.oracle_inspection_profile import SCRIPT_DIR, profile_from_values
from app.models.inspection_policy import DBInspectionPolicy
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.services.inspection import policy_service, oracle_script_checks as checks, template_runtime, runner
from app.services.inspection.custom_metric_service import normalize_item_configs
from app.services.inspection.native_catalog import item_digest
from app.services.rule_engine import build_rule_hits


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    DBInspectionPolicy.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_mysql_table_analysis_is_not_misclassified_as_oracle_tablespace():
    policy = next(p for p in policy_service.DEFAULT_POLICIES if p["policy_key"] == "mysql_standard")
    item = next(i for i in policy["item_config"] if i["key"] == "mysql.script.table_analysis")
    assert item["name"] == "表与索引分析" and item["category"] == "objects"
    instance = DBInstance(id=6, instance_name="mysql-test", db_type="mysql", host="127.0.0.1",
                          port=3306, username="monitor", password_cipher="not-used")
    finding = DBCheckResult(
        task_id="classification-test", instance_id=6, db_type="mysql",
        check_category="10. 表与索引分析", check_item="表与索引分析 巡检", check_status="warning",
        metric_name="table_analysis", metric_value="1", threshold_value="0",
        finding="发现 1 个无主键表候选，需要核查复制和更新热点风险。",
        suggestion="优先治理大表无主键、碎片高和热点 IO 对象，变更前确认备份与窗口。",
    )
    codes = {hit.rule_code for hit in build_rule_hits(instance, [finding])}
    assert "ORACLE_TABLESPACE_001" not in codes
    assert "INDEX_STATS_001" in codes


@pytest.mark.parametrize("key,sections_count,total", [("oracle_10g", 53, 72), ("oracle_noncdb", 56, 75), ("oracle_cdb", 56, 75)])
def test_native_templates_contain_every_actual_script_section(db, key, sections_count, total):
    policy_service.ensure_default_policies(db)
    row = db.query(DBInspectionPolicy).filter_by(policy_key=key).one()
    sections = checks.parse_script_sections(SCRIPT_DIR / row.report_config_json["script_name"])
    items = row.item_config_json
    assert len(items) == total
    script_items = {item["script_section"]: item for item in items if "script_section" in item}
    assert set(script_items) == {section.key for section in sections}
    assert len(script_items) == sections_count
    for section in sections:
        assert script_items[section.key]["sql"] == section.sql
        assert script_items[section.key]["script_line"] == section.line_number
    runtime = template_runtime.inspect_template_runtime(row.db_types_json, items, row.report_config_json)
    assert runtime["valid"]
    assert runtime["by_db_type"]["oracle"]["script_section_count"] == sections_count


def test_native_seed_upgrades_only_known_factory_items_and_keeps_bindings(db):
    payload = deepcopy(next(p for p in policy_service.DEFAULT_POLICIES if p["policy_key"] == "oracle_noncdb"))
    payload["item_config"] = deepcopy(policy_service.ORACLE_STANDARD_ITEMS)
    payload["report_config"] = {"oracle_profile": "noncdb", "instance_ids": [7], "last_status": "failed"}
    row = policy_service._make_policy(payload)
    row.schedule = "daily:03:00"
    row.enabled = 0
    row.threshold_config_json = {"space_usage_pct": 77}
    db.add(row)
    db.commit()
    original_id = row.id
    policy_service.ensure_default_policies(db)
    assert len(row.item_config_json) == 75
    assert row.id == original_id and row.enabled == 0
    assert row.schedule == "daily:03:00"
    assert row.threshold_config_json == {"space_usage_pct": 77}
    assert row.report_config_json["instance_ids"] == [7]
    assert row.report_config_json["last_status"] == "failed"
    original_update = row.update_time
    policy_service.ensure_default_policies(db)
    assert row.update_time == original_update
    changed = deepcopy(row.item_config_json)
    changed[0]["enabled"] = False
    row.item_config_json = changed
    db.commit()
    policy_service.ensure_default_policies(db)
    assert row.item_config_json[0]["enabled"] is False


def test_seed_can_join_installation_transaction_and_roll_back(db):
    policy_service.ensure_default_policies(db, commit=False)
    db.flush()
    assert db.query(DBInspectionPolicy).count() == len(policy_service.DEFAULT_POLICIES)
    db.rollback()
    assert db.query(DBInspectionPolicy).count() == 0


def test_fresh_sqlite_install_has_native_templates_before_any_api_request(tmp_path, monkeypatch):
    from app.services import sqlite_init_service
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'install.db'}")
    monkeypatch.setattr(sqlite_init_service, "engine", engine)
    monkeypatch.setattr(sqlite_init_service, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(sqlite_init_service, "ensure_data_dirs", lambda: None)
    try:
        sqlite_init_service.init_sqlite_database("test-admin", "Only-for-install-test-98")
        with Session(engine) as session:
            rows = session.query(DBInspectionPolicy).all()
            assert len(rows) == len(policy_service.DEFAULT_POLICIES)
            assert len(next(row for row in rows if row.policy_key == "oracle_cdb").item_config_json) == 75
    finally:
        engine.dispose()


def test_script_item_selects_only_one_query_not_every_sibling_or_health_metric():
    profile = profile_from_values("19.3", "NO")
    items = [{"key": "oracle.script:database_version", "enabled": True}]
    with (
        patch.object(checks, "oracle_connection", return_value=nullcontext(object())),
        patch.object(checks, "detect_oracle_profile", return_value=profile),
        patch.object(checks, "_execute_sections", return_value=[]) as execute,
    ):
        checks.collect_oracle_script_findings(object(), items)
    selected = execute.call_args.args[2]
    assert [section.key for section in selected] == ["database_version"]
    assert selected[0].item_key == "oracle.script:database_version"
    assert checks.enabled_oracle_query_keys(items) == set()


def test_query_binding_does_not_execute_grouped_script_sections():
    items = [{"key": "renamed", "source_key": "oracle.query:oracle.tablespace.used_pct"}]
    assert checks.enabled_oracle_query_keys(items) == {"oracle.tablespace.used_pct"}
    sections = checks.parse_script_sections(SCRIPT_DIR / "dbcheck_noncdb.sql")
    assert checks.select_script_sections(sections, checks.enabled_oracle_item_keys(items)) == []
    summary = template_runtime.inspect_template_runtime(["oracle"], items, {"oracle_profile": "noncdb"})
    assert summary["valid"] and summary["execution_unit_count"] == 2
    assert summary["by_db_type"]["oracle"]["script_section_count"] == 0


def test_legacy_grouped_template_remains_compatible_and_native_disabled_section_stays_off():
    sections = checks.parse_script_sections(SCRIPT_DIR / "dbcheck_noncdb.sql")
    assert len(checks.select_script_sections(sections, {"oracle.basic.database_info"})) > 1
    native = next(p for p in policy_service.DEFAULT_POLICIES if p["policy_key"] == "oracle_noncdb")
    items = deepcopy(native["item_config"])
    for item in items:
        if item["key"] == "oracle.script:database_version":
            item["enabled"] = False
    selected = checks.select_script_sections(sections, checks.enabled_oracle_item_keys(items))
    assert len(selected) == 55
    assert "database_version" not in {section.key for section in selected}


def test_source_keys_survive_normalization_and_missing_sections_fail_validation():
    native = next(p for p in policy_service.DEFAULT_POLICIES if p["policy_key"] == "oracle_noncdb")
    assert item_digest(native["item_config"]) == item_digest(normalize_item_configs(native["item_config"]))
    summary = template_runtime.inspect_template_runtime(["oracle"], [{"key": "oracle.script:foreigno_index"}], {"oracle_profile": "noncdb"})
    assert not summary["valid"]


def test_renamed_native_health_metric_keeps_threshold_and_evidence_binding():
    from app.analyzers.rules import Finding

    item = {"key": "renamed", "source_key": "oracle.query:oracle.tablespace.used_pct", "warning_value": 50, "critical_value": 90}
    policy = SimpleNamespace(item_config_json=[item], threshold_config_json={})
    finding = Finding("表空间", "SYSTEM", "normal", "used_pct", 80, "", "", "", {"template_item_key": "oracle.tablespace.used_pct"})
    runner._apply_policy_thresholds([finding], policy)
    assert finding.check_status == "warning"
    runner._attach_policy_context([finding], policy)
    assert finding.raw_data["template_item_key"] == "renamed"


def test_windows_native_template_is_present_but_unsupported_items_are_not_executable(db):
    rows = policy_service.list_policies(db)
    windows = next(row for row in rows if row["policy_key"] == "host_windows")
    assert windows["read_only"] and windows["runtime_valid"]
    assert len(windows["item_config"]) == 30
    assert windows["executable_item_count"] == 20
    assert all(not item["enabled"] for item in windows["item_config"] if not item["supported"])
    items = deepcopy(windows["item_config"])
    next(item for item in items if not item["supported"])["enabled"] = True
    assert not template_runtime.inspect_template_runtime(["host"], items, windows["report_config"])["valid"]
    assert not template_runtime.inspect_template_runtime(["host"], windows["item_config"], {"host_platform": "unix"})["valid"]


def test_host_template_selection_uses_detected_platform_and_mismatch_stops(db):
    from app.services.inspection.execution_policy import resolve_execution_policy

    instance = SimpleNamespace(id=7, db_type="host")
    policy_service.ensure_default_policies(db)
    with patch("app.services.inspection.execution_policy.detect_host_platform", return_value="windows"):
        selected = resolve_execution_policy(db, instance)
        assert selected.policy_key == "host_windows"
        assert selected.source_script["name"] == "oscheck.ps1"
        unix = db.query(DBInspectionPolicy).filter_by(policy_key="host_resource").one()
        with pytest.raises(ValueError, match="不匹配"):
            resolve_execution_policy(db, instance, unix)
        with patch.object(runner, "_degraded") as degraded, patch("app.services.inspection.windows_host_checks.collect_windows_host_findings", return_value=[]) as windows:
            assert runner._collect_host(instance, selected) == []
            windows.assert_called_once()
            degraded.assert_not_called()


def test_host_platform_probe_is_bounded_and_never_guesses_on_unknown_or_timeout():
    from app.services.inspection import host_platform

    with patch.object(host_platform, "ssh_connection", return_value=nullcontext(object())), patch.object(host_platform, "_run_command") as command:
        command.return_value = {"exit_code": 0, "stdout": "Linux\n"}
        assert host_platform.detect_host_platform(object()) == "unix"
        assert command.call_count == 1
        assert command.call_args.kwargs == {"timeout": 10, "max_bytes": 4096}
        command.reset_mock()
        command.side_effect = [{"exit_code": 1}, {"exit_code": 0, "stdout": "Win32NT"}]
        assert host_platform.detect_host_platform(object()) == "windows"
        assert command.call_count == 2
        command.side_effect = [{"exit_code": 1}, {"exit_code": 0, "stdout": "Win32NT", "timed_out": True}]
        with pytest.raises(ValueError, match="失败"):
            host_platform.detect_host_platform(object())
        command.side_effect = None
        command.return_value = {"exit_code": 1, "timed_out": True}
        with pytest.raises(ValueError, match="失败"):
            host_platform.detect_host_platform(object())
        command.return_value = {"exit_code": 0, "stdout": "Unknown"}
        with pytest.raises(ValueError, match="未能识别"):
            host_platform.detect_host_platform(object())


def test_auto_oracle_scope_rechecks_sections_against_actual_database_before_collection(db):
    from app.services.inspection.execution_policy import resolve_execution_policy

    policy_service.create_policy(db, {
        "policy_key": "modern_only", "name": "modern_only", "db_types": ["oracle"],
        "item_config": [{"key": "oracle.script:alertlog_check"}],
        "report_config": {"oracle_profile": "auto"},
    })
    policy = db.query(DBInspectionPolicy).filter_by(policy_key="modern_only").one()
    instance = SimpleNamespace(id=7, db_type="oracle", instance_name="test")
    with patch("app.services.inspection.execution_policy.oracle_connection", return_value=nullcontext(object())), patch("app.services.inspection.execution_policy.detect_oracle_profile", return_value=profile_from_values("10.2")):
        with pytest.raises(ValueError, match="未绑定"):
            resolve_execution_policy(db, instance, policy)
        with patch.object(runner, "collect_findings") as collect, patch.object(runner.result_writer, "save_findings", return_value=[]):
            runner.run_inspection(db, instance, policy)
            collect.assert_not_called()
    assert policy.report_config_json["oracle_profile"] == "auto"
