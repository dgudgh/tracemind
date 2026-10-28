from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.analyzers.rules import Finding
from app.collectors.oracle.oracle_health_collector import ORACLE_QUERIES
from app.models.inspection_policy import DBInspectionPolicy
from app.services import host_overview
from app.services.inspection import (
    policy_service,
    runner,
    template_exchange,
    template_runtime,
)


class InspectionTemplateRuntimeMatrixTest(unittest.TestCase):
    def test_every_builtin_template_is_executable_for_every_declared_type(self):
        expected = {
            "oracle_standard": {"oracle": 28},
            "oracle_10g": {"oracle": 72},
            "oracle_noncdb": {"oracle": 75},
            "oracle_cdb": {"oracle": 75},
            "mysql_standard": {"mysql": 26},
            "postgresql_standard": {"postgresql": 19},
            "sqlserver_standard": {"sqlserver": 23},
            "daily_standard": {
                "oracle": 5,
                "mysql": 5,
                "postgresql": 5,
                "sqlserver": 5,
            },
            "host_resource": {"host": 37},
            "host_windows": {"host": 20},
        }
        for template in policy_service.DEFAULT_POLICIES:
            with self.subTest(template=template["policy_key"]):
                summary = template_runtime.inspect_template_runtime(
                    template["db_types"],
                    template["item_config"],
                    template.get("report_config"),
                )
                self.assertTrue(summary["valid"], summary["errors"])
                self.assertEqual(summary["unmapped_items"], [])
                self.assertEqual(
                    {
                        db_type: detail["executable_item_count"]
                        for db_type, detail in summary["by_db_type"].items()
                    },
                    expected[template["policy_key"]],
                )

    def test_unknown_collector_key_is_rejected(self):
        summary = template_runtime.inspect_template_runtime(
            ["mysql"],
            [{"key": "custom.unknown.metric", "name": "不存在的采集项", "enabled": True}],
        )
        self.assertFalse(summary["valid"])
        self.assertEqual(summary["executable_item_count"], 0)
        self.assertEqual(summary["unmapped_items"][0]["key"], "custom.unknown.metric")
        with self.assertRaises(template_runtime.TemplateRuntimeError):
            template_runtime.require_template_runtime(
                ["mysql"],
                [{"key": "custom.unknown.metric", "name": "不存在的采集项", "enabled": True}],
            )

    def test_alias_keeps_source_key_binding(self):
        summary = template_runtime.inspect_template_runtime(
            ["mysql"],
            [
                {
                    "key": "company.mysql.connections",
                    "source_key": "mysql.connection.used_pct",
                    "name": "公司连接率",
                    "enabled": True,
                }
            ],
        )
        self.assertTrue(summary["valid"], summary["errors"])
        self.assertEqual(summary["executable_item_count"], 1)
        self.assertGreaterEqual(
            summary["by_db_type"]["mysql"]["execution_unit_count"],
            1,
        )

    def test_custom_sql_only_counts_for_its_database(self):
        item = {
            "key": "custom.mysql.answer",
            "name": "自定义 MySQL 指标",
            "source_type": "database_sql",
            "db_type": "mysql",
            "sql": "SELECT 1 AS metric_value",
            "result_field": "metric_value",
            "value_type": "number",
            "threshold_direction": "higher_is_worse",
            "warning_value": 80,
            "critical_value": 90,
            "enabled": True,
        }
        valid = template_runtime.inspect_template_runtime(["mysql"], [item])
        invalid = template_runtime.inspect_template_runtime(
            ["mysql", "postgresql"],
            [item],
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["by_db_type"]["mysql"]["custom_sql_count"], 1)
        self.assertFalse(invalid["valid"])
        self.assertIn(
            "PostgreSQL 没有可执行检查项",
            invalid["errors"],
        )

    def test_oracle_top_sql_metrics_use_their_own_sort_order(self):
        self.assertIn("ORDER BY elapsed_time DESC", ORACLE_QUERIES["top_sql_elapsed"])
        self.assertIn("ORDER BY buffer_gets DESC", ORACLE_QUERIES["top_sql_buffer_gets"])
        self.assertIn("ORDER BY disk_reads DESC", ORACLE_QUERIES["top_sql_disk_reads"])

    def test_oracle_empty_query_results_still_produce_auditable_findings(self):
        query_names = {
            "locks": "oracle.blocking.seconds",
            "stale_statistics": "oracle.objects.stats_stale",
            "top_sql_elapsed": "oracle.performance.top_sql_elapsed",
            "top_sql_buffer_gets": "oracle.performance.top_sql_buffer_gets",
            "top_sql_disk_reads": "oracle.performance.top_sql_disk_reads",
        }
        metrics = {
            "_query_evidence": {
                query_name: {
                    "status": "normal",
                    "sql": f"SELECT '{query_name}' FROM dual",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "read_only": True,
                }
                for query_name in query_names
            }
        }
        findings = []
        runner._ensure_oracle_query_findings(
            findings,
            metrics,
            set(query_names.values()),
        )
        self.assertEqual(len(findings), len(query_names))
        self.assertEqual(
            {
                finding.raw_data["template_item_key"]
                for finding in findings
            },
            set(query_names.values()),
        )
        self.assertTrue(all(finding.check_status == "normal" for finding in findings))
        self.assertTrue(all(finding.raw_data.get("sql") for finding in findings))

    def test_oracle_generic_template_item_owns_specific_query_evidence(self):
        policy = SimpleNamespace(
            id=1,
            policy_key="daily_standard",
            name="日常标准巡检",
            update_time=None,
            db_types_json=["oracle"],
            threshold_config_json={},
            action_config_json=[],
            report_config_json={},
            item_config_json=[
                {
                    "key": "performance.slow_sql",
                    "name": "性能与慢 SQL",
                    "enabled": True,
                }
            ],
        )
        finding = Finding(
            "SQL 性能",
            "Top SQL（逻辑读）",
            "normal",
            "oracle.performance.top_sql_buffer_gets",
            "0",
            "采集 SQL 成功执行",
            "查询完成",
            "保持观察",
            {
                "template_item_key": "oracle.performance.top_sql_buffer_gets",
                "sql": "SELECT 1 FROM dual",
                "rows": [],
                "row_count": 0,
            },
        )
        runner._attach_policy_context([finding], policy)
        self.assertEqual(
            finding.raw_data["template_item_key"],
            "performance.slow_sql",
        )
        self.assertEqual(
            finding.raw_data["template_source_key"],
            "performance.slow_sql",
        )


class InspectionTemplateRuntimePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        DBInspectionPolicy.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _payload(enabled: bool) -> dict:
        return {
            "policy_key": "runtime_draft",
            "name": "运行校验草稿",
            "db_types": ["mysql"],
            "enabled": enabled,
            "schedule": "manual",
            "categories": ["custom"],
            "item_config": [
                {
                    "key": "unknown.runtime.key",
                    "name": "未绑定采集器",
                    "enabled": True,
                }
            ],
        }

    def test_disabled_draft_can_be_saved_but_cannot_be_enabled(self):
        created = policy_service.create_policy(self.db, self._payload(False))
        self.assertFalse(created["enabled"])
        self.assertFalse(created["runtime_valid"])
        self.assertEqual(created["executable_item_count"], 0)

        model = policy_service.get_policy_model(self.db, created["id"])
        with self.assertRaises(template_runtime.TemplateRuntimeError):
            policy_service.update_policy(self.db, model, {"enabled": True})
        self.db.refresh(model)
        self.assertFalse(bool(model.enabled))

    def test_enabled_invalid_template_cannot_be_created(self):
        with self.assertRaises(template_runtime.TemplateRuntimeError):
            policy_service.create_policy(self.db, self._payload(True))


class InspectionTemplateRuntimeBoundaryTest(unittest.TestCase):
    def test_run_endpoint_blocks_corrupted_stored_template_before_collector(self):
        from app.api.v1.inspection import InspectionRunPayload, api_run_inspection

        instance = SimpleNamespace(id=7, db_type="mysql")
        policy = SimpleNamespace(
            name="损坏模板",
            report_config_json={},
            enabled=1,
            db_types_json=["mysql"],
            item_config_json=[
                {
                    "key": "unknown.runtime.key",
                    "name": "未绑定采集器",
                    "enabled": True,
                }
            ],
        )
        with (
            patch("app.api.v1.inspection._get_instance", return_value=instance),
            patch(
                "app.api.v1.inspection.policy_service.get_policy_model_by_key",
                return_value=policy,
            ),
            patch(
                "app.api.v1.inspection.runner.run_unified_inspection"
            ) as run_mock,
        ):
            with self.assertRaises(HTTPException) as raised:
                api_run_inspection(
                    "mysql",
                    7,
                    # The endpoint now also receives the authenticated Request (it records who
                    # asked for the run); this test drives the template gate, so no identity.
                    None,
                    InspectionRunPayload(policy_key="broken"),
                    db=MagicMock(),
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail["code"], "template_not_executable")
        run_mock.assert_not_called()

    def test_import_preview_rejects_template_without_runtime_binding(self):
        document = {
            "format": template_exchange.FORMAT_NAME,
            "format_version": template_exchange.FORMAT_VERSION,
            "template_id": "invalid_runtime_template",
            "alias": "无运行映射模板",
            "template": {
                "db_types": ["postgresql"],
                "schedule": "manual",
                "categories": ["custom"],
                "item_config": [
                    {
                        "key": "unknown.runtime.key",
                        "name": "未绑定采集器",
                        "enabled": True,
                    }
                ],
                "threshold_config": {},
                "action_config": [],
                "report_config": {},
                "remark": "",
            },
            "metadata": {
                "exported_at": "2026-07-29T17:00:00+08:00",
                "product_version": "1.0.0",
            },
        }
        preview = template_exchange.preview_uploads(
            [("invalid.json", json.dumps(document).encode("utf-8"))],
            set(),
        )
        self.assertEqual(preview[0]["status"], "invalid")
        self.assertIn("模板运行校验失败", preview[0]["reason"])

    def test_host_template_produces_one_result_per_enabled_item(self):
        template = next(
            item
            for item in policy_service.DEFAULT_POLICIES
            if item["policy_key"] == "host_resource"
        )
        overview = {
            "instance_id": 1,
            "source_db_type": "oracle",
            "ssh_enabled": True,
            "host_basic": {
                "os_name": "Linux",
                "os_version": "9",
                "kernel_version": "5.14",
                "cpu_count": 8,
                "cpu_model": "Test CPU",
                "memory_total_mb": 32768,
            },
            "resources": {
                "cpu_used_pct": 12,
                "memory_used_pct": 40,
                "swap_used_pct": 0,
                "swap_total_mb": 4096,
                "load_1m": 1,
            },
            "filesystems": [
                {
                    "filesystem": "/dev/sda1",
                    "mount_point": "/",
                    "used_pct": 42,
                }
            ],
            "processes": [
                {
                    "name": "Oracle Listener",
                    "status": "running",
                    "level": "normal",
                    "detail": "tnslsnr LISTENER",
                }
            ],
            "command_data": {
                "current_time": "2026-07-29 17:00:00 +0800",
                "hosts_file": "127.0.0.1 localhost",
                "meminfo": "MemTotal: 33554432 kB",
                "thp": "always [madvise] never",
                "df_inode": "/dev/sda1 100 10 90 10% /",
                "sysctl": "vm.swappiness = 10",
                "limits": "open files 65535",
                "crontab": "",
                "oracle_users": "oracle",
                "pmon": "oracle ora_pmon_orcl",
                "oracle_version": "SQL*Plus: Release 19.0",
                "db_patch": "RU 19.22",
                "gi_patch": "GI RU 19.22",
                "listener_sqlnet": "STATUS of the LISTENER",
                "rac_detect": "node1\nnode2",
                "rac_ssh_trust": "node1=node1\nnode2=node2",
                "rac_ocrcheck": "Device/File integrity check succeeded",
                "rac_votedisk": "ONLINE",
                "rac_ocrbackup": "backup available",
                "rac_oifcfg": "eth0 global public",
                "rac_crs_status": "ora.database ONLINE",
                "rac_nodes": "node1 1 Active\nnode2 2 Active",
            },
            "risks": [],
            "error": None,
        }
        checks = host_overview.build_template_checks(
            overview,
            template["item_config"],
        )
        self.assertEqual(len(checks), 37)
        self.assertEqual(len({item["key"] for item in checks}), 37)
        self.assertNotIn("unmapped", {item["metric_value"] for item in checks})

        instance = SimpleNamespace(id=1, db_type="host")
        policy = SimpleNamespace(item_config_json=template["item_config"])
        with patch(
            "app.services.host_overview.get_instance_host_overview",
            return_value=overview,
        ):
            findings = runner.collect_findings(MagicMock(), instance, policy)
        self.assertEqual(len(findings), 37)
        self.assertTrue(
            all(
                finding.raw_data.get("template_item_key")
                for finding in findings
            )
        )


class InspectionCustomTemplateLifecycleApiTest(unittest.TestCase):
    def setUp(self):
        from app.api.v1.inspection import router
        from app.core.database import get_db

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        DBInspectionPolicy.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_create_edit_clone_export_list_and_delete_custom_template(self):
        payload = {
            "policy_key": "lifecycle_mysql",
            "name": "MySQL 生命周期模板",
            "db_types": ["mysql"],
            "enabled": True,
            "schedule": "manual",
            "categories": ["connection"],
            "item_config": [
                {
                    "key": "company.mysql.connection",
                    "source_key": "mysql.connection.used_pct",
                    "name": "公司连接使用率",
                    "category": "connection",
                    "enabled": True,
                }
            ],
            "threshold_config": {},
            "action_config": ["export_report"],
            "report_config": {},
        }
        created = self.client.post(
            "/api/v1/inspection/policies",
            json=payload,
        )
        self.assertEqual(created.status_code, 200, created.text)
        created_row = created.json()
        self.assertTrue(created_row["runtime_valid"])
        self.assertEqual(created_row["executable_item_count"], 1)
        policy_id = created_row["id"]

        updated = self.client.put(
            f"/api/v1/inspection/policies/{policy_id}",
            json={
                "name": "MySQL 生命周期模板 V2",
                "item_config": [
                    {
                        **payload["item_config"][0],
                        "name": "连接使用率别名 V2",
                    }
                ],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["name"], "MySQL 生命周期模板 V2")
        self.assertEqual(
            updated.json()["item_config"][0]["source_key"],
            "mysql.connection.used_pct",
        )

        cloned = self.client.post(
            f"/api/v1/inspection/policies/{policy_id}/clone",
            json={
                "policy_key": "lifecycle_mysql_copy",
                "name": "MySQL 生命周期模板副本",
            },
        )
        self.assertEqual(cloned.status_code, 200, cloned.text)
        clone_row = cloned.json()
        self.assertTrue(clone_row["runtime_valid"])
        self.assertFalse(clone_row["read_only"])

        exported = self.client.get(
            f"/api/v1/inspection/policies/{clone_row['id']}/export"
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()["template_id"], "lifecycle_mysql_copy")

        listed = self.client.get("/api/v1/inspection/policies")
        self.assertEqual(listed.status_code, 200, listed.text)
        by_key = {
            item["policy_key"]: item
            for item in listed.json()["items"]
        }
        self.assertTrue(by_key["lifecycle_mysql"]["runtime_valid"])
        self.assertTrue(by_key["lifecycle_mysql_copy"]["runtime_valid"])

        deleted_clone = self.client.delete(
            f"/api/v1/inspection/policies/{clone_row['id']}"
        )
        deleted_source = self.client.delete(
            f"/api/v1/inspection/policies/{policy_id}"
        )
        self.assertEqual(deleted_clone.status_code, 200)
        self.assertEqual(deleted_source.status_code, 200)

    def test_api_returns_runtime_reason_for_invalid_enabled_template(self):
        response = self.client.post(
            "/api/v1/inspection/policies",
            json={
                "policy_key": "invalid_runtime_api",
                "name": "无映射模板",
                "db_types": ["postgresql"],
                "enabled": True,
                "item_config": [
                    {
                        "key": "unknown.runtime.key",
                        "name": "无采集器",
                        "enabled": True,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "template_not_executable")
        self.assertIn("PostgreSQL 没有可执行检查项", detail["message"])


if __name__ == "__main__":
    unittest.main()
