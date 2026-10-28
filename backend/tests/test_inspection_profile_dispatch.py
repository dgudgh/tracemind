from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analyzers.rules import Finding
from app.collectors.oracle.oracle_inspection_profile import profile_from_values
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import policy_service, runner, template_exchange, template_runtime
from app.services.inspection.execution_policy import resolve_execution_policy
from app.services.inspection.template_profiles import ORACLE_TEMPLATE_KEYS


class ProfileDispatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        DBInspectionPolicy.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        policy_service.ensure_default_policies(self.db)
        self.instance = SimpleNamespace(id=7, db_type="oracle", instance_name="test")
        self.connection = self.enterContext(patch("app.services.inspection.execution_policy.oracle_connection"))
        self.probe = self.enterContext(patch("app.services.inspection.execution_policy.detect_oracle_profile"))
        self.probe.return_value = profile_from_values("19.3", "NO")
        self.host_probe = self.enterContext(patch("app.services.inspection.execution_policy.detect_host_platform", return_value="unix"))

    def model(self, key):
        return self.db.query(DBInspectionPolicy).filter_by(policy_key=key).one()

    def custom(self, key, profile="auto", db_type="oracle", default=False):
        row = policy_service.create_policy(self.db, {
            "policy_key": key, "name": key, "db_types": [db_type],
            "item_config": [{"key": "connection.basic", "name": "基础信息", "enabled": True}],
            "report_config": {
                "oracle_profile": profile,
                "assistant_default_db_types": [db_type] if default else [],
            },
        })
        return self.model(row["policy_key"])

    def test_catalog_has_three_oracle_variants_and_one_per_other_database(self):
        rows = [row for row in policy_service.list_policies(self.db) if not row["legacy_template"]]
        self.assertEqual({row["policy_key"] for row in rows if row["db_types"] == ["oracle"]}, set(ORACLE_TEMPLATE_KEYS.values()))
        for engine in ("mysql", "postgresql", "sqlserver"):
            self.assertEqual(len([row for row in rows if row["db_types"] == [engine]]), 1)
        self.assertTrue(all(row["runtime_valid"] for row in rows))

    def test_default_selection_uses_fresh_version_and_cdb_probe(self):
        for version, cdb, expected in (("10.2", None, "oracle_10g"), ("11.2", None, "oracle_noncdb"), ("19.3", "NO", "oracle_noncdb"), ("19.3", "YES", "oracle_cdb")):
            with self.subTest(expected=expected, version=version):
                self.probe.return_value = profile_from_values(version, cdb)
                selected = resolve_execution_policy(self.db, self.instance)
                self.assertEqual(selected.policy_key, expected)
                self.assertEqual(selected.selection_mode, "automatic")
                self.assertEqual(len(selected.source_script["sha256"]), 64)

    def test_explicit_wrong_profile_rejected_before_collection(self):
        with self.assertRaisesRegex(ValueError, "模板不匹配"):
            resolve_execution_policy(self.db, self.instance, self.model("oracle_cdb"))

    def test_unknown_profile_never_defaults_to_noncdb(self):
        for profile in (profile_from_values(""), profile_from_values("19.3", None)):
            self.probe.return_value = profile
            with self.assertRaisesRegex(ValueError, "无法"):
                resolve_execution_policy(self.db, self.instance)

    def test_custom_defaults_can_be_scoped_to_different_oracle_profiles(self):
        self.custom("my_noncdb", "noncdb", default=True)
        self.custom("my_cdb", "cdb", default=True)
        self.assertEqual(resolve_execution_policy(self.db, self.instance).policy_key, "my_noncdb")
        self.probe.return_value = profile_from_values("19.3", "YES")
        self.assertEqual(resolve_execution_policy(self.db, self.instance).policy_key, "my_cdb")
        self.probe.return_value = profile_from_values("10.2")
        self.assertEqual(resolve_execution_policy(self.db, self.instance).policy_key, "oracle_10g")

    def test_all_nonoracle_defaults_and_custom_defaults_use_registry(self):
        for engine in ("mysql", "postgresql", "sqlserver"):
            self.instance.db_type = engine
            self.assertEqual(resolve_execution_policy(self.db, self.instance).policy_key, engine + "_standard")
            self.custom("company_" + engine, db_type=engine, default=True)
            self.assertEqual(resolve_execution_policy(self.db, self.instance).policy_key, "company_" + engine)
        self.connection.assert_not_called()

    def test_disabled_default_is_not_silently_replaced(self):
        selected = self.custom("custom_default", "noncdb", default=True)
        selected.enabled = 0
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "已停用"):
            resolve_execution_policy(self.db, self.instance)

    def test_ambiguous_defaults_never_depend_on_database_row_order(self):
        first = self.custom("first", "noncdb", default=True)
        second = self.custom("second", "noncdb")
        second.report_config_json = {**second.report_config_json, "assistant_default_db_types": ["oracle"]}
        self.db.commit()
        self.assertIn("oracle", first.report_config_json["assistant_default_db_types"])
        with self.assertRaisesRegex(ValueError, "多个默认"):
            resolve_execution_policy(self.db, self.instance)

    def test_malformed_version_value_is_rejected_without_server_error(self):
        document = template_exchange.export_document(self.custom("format"))
        for version in ({}, [], True, 2.0):
            document["format_version"] = version
            preview = template_exchange.preview_uploads([("invalid.json", json.dumps(document).encode())], set())
            self.assertEqual(preview[0]["status"], "invalid")

    def test_disabled_or_wrong_engine_explicit_template_does_not_probe_database(self):
        row = self.model("oracle_noncdb")
        row.enabled = 0
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "已停用"):
            resolve_execution_policy(self.db, self.instance, row)
        with self.assertRaisesRegex(ValueError, "不适用"):
            resolve_execution_policy(self.db, self.instance, self.model("mysql_standard"))
        self.connection.assert_not_called()

    def test_legacy_key_stays_usable_without_forcing_noncdb(self):
        self.probe.return_value = profile_from_values("19.3", "YES")
        selected = resolve_execution_policy(self.db, self.instance, self.model("oracle_standard"))
        self.assertEqual(selected.policy_key, "oracle_standard")
        self.assertEqual(selected.resolved_oracle_profile, "cdb")

    def test_registry_refresh_keeps_schedule_custom_items_and_thresholds(self):
        row = self.model("mysql_standard")
        row.schedule = "daily:03:00"
        row.threshold_config_json = {"space_usage_pct": 62}
        row.item_config_json = [{"key": "connection.basic", "name": "customer"}]
        row.report_config_json = {"instance_ids": [7], "assistant_default_db_types": ["mysql"]}
        self.db.commit()
        policy_service.ensure_default_policies(self.db)
        self.assertEqual(row.schedule, "daily:03:00")
        self.assertEqual(row.threshold_config_json, {"space_usage_pct": 62})
        self.assertEqual(row.item_config_json[0]["name"], "customer")
        self.assertEqual(row.report_config_json["instance_ids"], [7])
        self.assertEqual(row.report_config_json["assistant_default_db_types"], ["mysql"])
        previous = row.update_time
        policy_service.ensure_default_policies(self.db)
        self.assertEqual(row.update_time, previous)

    def test_execution_snapshot_is_immutable_and_next_execution_uses_edit(self):
        row = self.custom("snapshot", "noncdb")
        selected = resolve_execution_policy(self.db, self.instance, row)
        policy_service.update_policy(self.db, row, {"name": "updated", "threshold_config": {"space_usage_pct": 55}})
        self.assertEqual(selected.name, "snapshot")
        self.assertEqual(selected.threshold_config_json, {})
        current = resolve_execution_policy(self.db, self.instance, row)
        self.assertEqual(current.name, "updated")
        self.assertEqual(current.threshold_config_json["space_usage_pct"], 55)

    def test_editor_changes_do_not_resurrect_removed_profile_or_script(self):
        row = self.custom("editable", "cdb")
        row.report_config_json = {**row.report_config_json, "instance_ids": [7], "last_status": "success"}
        self.db.commit()
        updated = policy_service.update_policy(self.db, row, {
            "db_types": ["oracle"], "instance_ids": [],
            "report_config": {"oracle_profile": "noncdb", "include_summary": True},
        })
        self.assertEqual(updated["report_config"]["script_name"], "dbcheck_noncdb.sql")
        self.assertEqual(updated["report_config"]["last_status"], "success")
        updated = policy_service.update_policy(self.db, row, {
            "db_types": ["mysql"], "instance_ids": [],
            "report_config": {"include_summary": True},
        })
        self.assertNotIn("oracle_profile", updated["report_config"])
        self.assertNotIn("script_name", updated["report_config"])
        self.assertEqual(resolve_execution_policy(self.db, SimpleNamespace(db_type="mysql"), row).source_script["name"], "mysql_check.sh")

    def test_multi_engine_template_records_the_executed_engine_source(self):
        row = self.custom("multi_engine", "cdb")
        policy_service.update_policy(self.db, row, {"db_types": ["oracle", "mysql", "postgresql", "sqlserver"]})
        for engine, name in (("mysql", "mysql_check.sh"), ("postgresql", "pg_check.sh"), ("sqlserver", "mssql_check.sh")):
            with self.subTest(engine=engine):
                selected = resolve_execution_policy(self.db, SimpleNamespace(db_type=engine), row)
                self.assertEqual(selected.source_script["name"], name)
                self.assertEqual(selected.source_script["mode"], "collector_reference")
                self.assertIsNone(selected.resolved_oracle_profile)
        self.connection.assert_not_called()

    def test_profile_survives_clone_export_import_to_second_installation(self):
        cloned = policy_service.clone_policy(self.db, self.model("oracle_cdb"), "shared_cdb", "共享 CDB")
        source = self.model(cloned["policy_key"])
        source.report_config_json = {**source.report_config_json, "instance_ids": [99], "assistant_default_db_types": ["oracle"], "script_source": "/private/local/file.sql"}
        self.db.commit()
        document = template_exchange.export_document(source)
        self.assertEqual(document["format_version"], 2)
        config = document["template"]["report_config"]
        self.assertEqual(config["oracle_profile"], "cdb")
        self.assertEqual(config["script_name"], "dbcheck_cdb.sql")
        self.assertFalse({"instance_ids", "assistant_default_db_types", "script_source"} & config.keys())
        engine = create_engine("sqlite://")
        DBInspectionPolicy.__table__.create(engine)
        with sessionmaker(bind=engine)() as target:
            result = template_exchange.import_uploads(target, [("shared.json", json.dumps(document).encode())])
            self.assertEqual(result["imported_count"], 1)
            imported = target.query(DBInspectionPolicy).filter_by(policy_key="shared_cdb").one()
            self.assertFalse(imported.enabled)
            self.assertFalse(imported.read_only)
            imported.enabled = 1
            target.commit()
            self.probe.return_value = profile_from_values("19.3", "YES")
            selected = resolve_execution_policy(target, self.instance, imported)
            self.assertEqual(selected.resolved_oracle_profile, "cdb")
            self.assertEqual(selected.item_config_json, source.item_config_json)
        engine.dispose()

    def test_v1_cannot_carry_profile_silently_ignored_by_old_program(self):
        row = self.custom("scoped", "cdb")
        document = template_exchange.export_document(row)
        document["format_version"] = 1
        preview = template_exchange.preview_uploads([("old.json", json.dumps(document).encode())], set())
        self.assertEqual(preview[0]["status"], "invalid")
        self.assertIn("版本 2", preview[0]["reason"])

    def test_invalid_scope_or_absolute_script_path_is_rejected(self):
        for config in ({"oracle_profile": "unknown"}, {"oracle_profile": "cdb", "script_name": "/tmp/other.sql"}):
            with self.assertRaises(template_runtime.TemplateRuntimeError):
                policy_service.create_policy(self.db, {
                    "policy_key": "invalid", "name": "invalid", "db_types": ["oracle"],
                    "item_config": [{"key": "connection.basic"}], "report_config": config,
                })

    def test_unified_runner_uses_custom_default_and_freezes_context(self):
        row = self.custom("company", "noncdb", default=True)
        finding = Finding("basic", "test", "normal", "connection.basic", "1", "", "ok", "", {})
        with patch.object(runner, "collect_findings", return_value=[finding]) as collect, patch.object(runner.result_writer, "save_findings", return_value=[]) as save:
            runner.run_inspection(self.db, self.instance)
        self.assertEqual(collect.call_args.kwargs["policy"].id, row.id)
        context = save.call_args.args[3][0].raw_data["inspection_template"]
        self.assertEqual(context["policy_key"], "company")
        self.assertEqual(context["oracle_profile"], "noncdb")
        self.assertEqual(context["source_script"]["name"], "dbcheck_noncdb.sql")

    def test_failed_preflight_does_not_execute_any_template_sql(self):
        self.probe.side_effect = RuntimeError("connection failed")
        with patch.object(runner, "collect_findings") as collect, patch.object(runner.result_writer, "save_findings", return_value=[]) as save:
            runner.run_inspection(self.db, self.instance)
        collect.assert_not_called()
        findings = save.call_args.args[3]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].metric_value, "degraded")
        self.assertIn("connection failed", findings[0].finding)

    def test_scheduled_host_template_uses_host_collector_not_database_collector(self):
        with patch.object(runner, "collect_findings", return_value=[]) as collect, patch.object(runner.result_writer, "save_findings", return_value=[]):
            runner.run_inspection(self.db, self.instance, self.model("host_resource"))
        self.assertEqual(collect.call_args.args[1].db_type, "host")
        self.assertEqual(collect.call_args.args[1].source_db_type, "oracle")
        self.assertEqual(collect.call_args.kwargs["policy"].policy_key, "host_resource")
        self.assertEqual(self.instance.db_type, "oracle")
        self.connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
