import io
import unittest
import zipfile
from types import SimpleNamespace

from app.analyzers.rules import Finding
from app.api.v1.report import (
    _build_inspection_docx,
    _build_inspection_html,
    _build_inspection_report_detail,
)
from app.services.inspection.report_builder import _finding_evidence
from app.services.inspection.result_writer import inspection_template_summary
from app.services.inspection.script_db_checks import _mysql_performance_warnings
from app.services.inspection.runner import (
    _apply_policy_thresholds,
    _attach_oracle_query_evidence,
    _attach_policy_context,
)


def _result_payload():
    return {
        "task": {
            "task_id": "task-evidence-1",
            "instance_id": 7,
            "db_type": "oracle",
            "task_status": "success",
            "overall_score": 100,
            "risk_level": "normal",
            "normal_count": 1,
            "info_count": 0,
            "warning_count": 0,
            "critical_count": 0,
            "template_key": "oracle_standard",
            "template_name": "Oracle 默认巡检模板",
        },
        "modules": [
            {
                "module_key": "DBCheck SQL",
                "module_name": "DBCheck SQL",
                "score": 100,
                "risk_level": "normal",
                "summary": "未发现明显风险",
                "evidence": {"counts": {"normal": 1}},
            }
        ],
        "findings": [
            {
                "id": 1,
                "module_key": "DBCheck SQL",
                "check_item": "Tablespace usage",
                "severity": "normal",
                "finding": "表空间使用率正常",
                "suggestions": [],
                "evidence": {
                    "status": "normal",
                    "line_number": 88,
                    "sql": "SELECT tablespace_name, used_pct FROM dba_tablespace_usage_metrics",
                    "columns": ["TABLESPACE_NAME", "USED_PCT"],
                    "rows": [{"TABLESPACE_NAME": "USERS", "USED_PCT": 12.5}],
                    "row_count": 1,
                    "truncated": False,
                },
            }
        ],
        "report": {
            "instance": {
                "id": 7,
                "name": "mesdb",
                "host": "198.51.100.200",
                "port": 1521,
                "db_type": "oracle",
            },
            "overall_conclusion": "巡检正常",
        },
    }


class InspectionReportEvidenceTest(unittest.TestCase):
    def test_oracle_analyzer_finding_keeps_executed_query_and_rows(self):
        finding = Finding(
            check_category="表空间状态",
            check_item="SYSAUX",
            check_status="normal",
            metric_name="used_pct",
            metric_value="12.5%",
            threshold_value="warning>85%",
            finding="表空间使用率正常",
            suggestion="",
            raw_data={"tablespace_name": "SYSAUX", "used_pct": 12.5},
        )

        _attach_oracle_query_evidence(
            [finding],
            {
                "_query_evidence": {
                    "tablespace_usage": {
                        "status": "normal",
                        "sql": "SELECT tablespace_name, used_pct FROM dba_tablespace_usage_metrics",
                        "columns": ["tablespace_name", "used_pct"],
                        "rows": [
                            {"tablespace_name": "SYSAUX", "used_pct": 12.5},
                            {"tablespace_name": "SYSTEM", "used_pct": 18.0},
                        ],
                        "row_count": 2,
                    }
                }
            },
        )

        self.assertIn("SELECT tablespace_name", finding.raw_data["sql"])
        self.assertEqual(finding.raw_data["row_count"], 2)
        self.assertEqual(len(finding.raw_data["rows"]), 2)
        self.assertEqual(finding.raw_data["rows"][0]["tablespace_name"], "SYSAUX")

    def test_legacy_oracle_finding_does_not_invent_query_evidence(self):
        row = SimpleNamespace(
            db_type="oracle",
            check_category="表空间状态",
            check_status="normal",
            metric_name="used_pct",
            metric_value="12.5%",
            threshold_value="warning>85%",
            raw_data={"tablespace_name": "SYSAUX", "used_pct": 12.5},
        )

        evidence = _finding_evidence(row)

        self.assertNotIn("sql", evidence)
        self.assertNotIn("row_count", evidence)
        self.assertNotIn("rows", evidence)
        self.assertEqual(evidence["tablespace_name"], "SYSAUX")
        self.assertEqual(evidence["execution_status"], "unknown")

    def test_markdown_contains_executed_sql_and_result_rows(self):
        detail = _build_inspection_report_detail(_result_payload())

        markdown = detail["markdown"]

        self.assertIn("## SQL 与查询结果", markdown)
        self.assertIn("SELECT tablespace_name", markdown)
        self.assertIn("| TABLESPACE_NAME | USED_PCT |", markdown)
        self.assertIn("| USERS | 12.5 |", markdown)
        self.assertIn("返回行数: 1", markdown)
        self.assertIn("检查结论: 表空间使用率正常", markdown)

    def test_word_contains_executed_sql_and_result_rows(self):
        stream = _build_inspection_docx(_result_payload())

        with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")

        self.assertIn("SELECT tablespace_name", document_xml)
        self.assertIn("TABLESPACE_NAME", document_xml)
        self.assertIn("USERS", document_xml)
        self.assertIn("Oracle 默认巡检模板", document_xml)
        self.assertIn("数据库健康巡检报告", document_xml)
        self.assertIn("执行摘要", document_xml)

    def test_html_is_a_structured_report_instead_of_a_markdown_pre_block(self):
        detail = _build_inspection_report_detail(_result_payload())

        report_html = _build_inspection_html(detail)

        self.assertIn('<section class="metrics">', report_html)
        self.assertIn('<section class="evidence-list">', report_html)
        self.assertIn("<table", report_html)
        self.assertIn("SELECT tablespace_name", report_html)
        self.assertIn("USERS", report_html)
        self.assertIn("表空间使用率正常", report_html)
        self.assertNotIn("<main><pre># TraceMind AI", report_html)

    def test_report_labels_rule_analysis_transparently(self):
        detail = _build_inspection_report_detail(_result_payload())

        self.assertIn("## 综合分析（规则引擎）", detail["markdown"])
        self.assertNotIn("AI 综合分析", detail["markdown"])
        self.assertEqual(detail["ai_result"]["analysis_source"], "rule_engine")

    def test_execution_status_is_independent_of_final_health_severity(self):
        payload = _result_payload()
        payload["findings"][0]["severity"] = "normal"
        payload["findings"][0]["evidence"]["status"] = "warning"
        payload["findings"][0]["evidence"]["execution_status"] = "failed"

        markdown = _build_inspection_report_detail(payload)["markdown"]

        self.assertIn("- 执行状态: 采集失败", markdown)
        self.assertNotIn("- 执行状态: 健康", markdown)

    def test_selected_template_is_attached_to_saved_evidence(self):
        finding = Finding(
            check_category="basic",
            check_item="connection",
            check_status="normal",
            metric_name="connected",
            metric_value="true",
            threshold_value="true",
            finding="ok",
            suggestion="",
            raw_data={"sql": "SELECT 1 FROM dual"},
        )
        policy = SimpleNamespace(id=3, policy_key="oracle_standard", name="Oracle 默认巡检模板")

        _attach_policy_context([finding], policy)
        row = SimpleNamespace(raw_data=finding.raw_data)

        self.assertEqual(
            inspection_template_summary([row]),
            {
                "template_id": 3,
                "template_key": "oracle_standard",
                "template_name": "Oracle 默认巡检模板",
                "template_revision": finding.raw_data["inspection_template"]["revision"],
                "template_updated_at": None,
                "template_item_count": 0,
            },
        )

    def test_policy_evidence_is_grouped_under_its_template_item(self):
        finding = Finding(
            check_category="12. Tablespace Usage",
            check_item="Tablespace Usage",
            check_status="normal",
            metric_name="oracle.tablespace.used_pct",
            metric_value="3 rows",
            threshold_value="只读 SQL 成功",
            finding="ok",
            suggestion="",
            raw_data={
                "template_item_key": "oracle.tablespace.used_pct",
                "sql": "SELECT tablespace_name FROM dba_tablespaces",
            },
        )
        policy = SimpleNamespace(
            id=3,
            policy_key="oracle_standard",
            name="Oracle 默认巡检模板",
            item_config_json=[
                {
                    "key": "oracle.tablespace.used_pct",
                    "name": "表空间使用率",
                    "category": "space",
                }
            ],
        )

        _attach_policy_context([finding], policy)

        self.assertEqual(finding.check_category, "表空间使用率")
        self.assertEqual(
            finding.raw_data["inspection_item"],
            {
                "key": "oracle.tablespace.used_pct",
                "source_key": "oracle.tablespace.used_pct",
                "name": "表空间使用率",
                "category": "space",
            },
        )

    def test_policy_alias_uses_stable_source_key_for_runtime_evidence(self):
        finding = Finding(
            check_category="12. Tablespace Usage",
            check_item="Tablespace Usage",
            check_status="normal",
            metric_name="oracle.tablespace.used_pct",
            metric_value="42%",
            threshold_value="< 85%",
            finding="ok",
            suggestion="",
            raw_data={"template_item_key": "oracle.tablespace.used_pct"},
        )
        policy = SimpleNamespace(
            id=8,
            policy_key="custom_oracle",
            name="自定义 Oracle 模板",
            item_config_json=[
                {
                    "key": "mes.core_tablespace",
                    "source_key": "oracle.tablespace.used_pct",
                    "name": "MES 核心表空间",
                    "category": "space",
                }
            ],
        )

        _attach_policy_context([finding], policy)

        self.assertEqual(finding.check_category, "MES 核心表空间")
        self.assertEqual(finding.raw_data["template_item_key"], "mes.core_tablespace")
        self.assertEqual(
            finding.raw_data["template_source_key"],
            "oracle.tablespace.used_pct",
        )

    def test_edited_template_thresholds_are_applied_to_the_next_run(self):
        def policy(warning: float, critical: float):
            return SimpleNamespace(
                item_config_json=[
                    {
                        "key": "mes.core_tablespace",
                        "source_key": "oracle.tablespace.used_pct",
                        "name": "MES 核心表空间",
                        "normal_range": f"< {warning}%",
                        "warning_range": f"{warning}% - {critical}%",
                        "critical_range": f">= {critical}%",
                    }
                ],
                # A stale template-wide fallback must not override the edited row.
                threshold_config_json={
                    "space_usage_pct": 80,
                    "critical_space_usage_pct": 90,
                },
            )

        first = Finding(
            check_category="space",
            check_item="Tablespace Usage",
            check_status="normal",
            metric_name="used_pct",
            metric_value="92%",
            threshold_value="",
            finding="",
            suggestion="",
            raw_data={"template_item_key": "mes.core_tablespace"},
        )
        after_edit = Finding(
            check_category="space",
            check_item="Tablespace Usage",
            check_status="critical",
            metric_name="used_pct",
            metric_value="92%",
            threshold_value="",
            finding="",
            suggestion="",
            raw_data={"template_item_key": "oracle.tablespace.used_pct"},
        )

        _apply_policy_thresholds([first], policy(80, 90))
        _apply_policy_thresholds([after_edit], policy(95, 99))

        self.assertEqual(first.check_status, "critical")
        self.assertEqual(after_edit.check_status, "normal")
        self.assertEqual(
            after_edit.raw_data["template_threshold"]["source_key"],
            "oracle.tablespace.used_pct",
        )

    def test_lower_is_worse_range_uses_upper_warning_boundary(self):
        policy = SimpleNamespace(
            item_config_json=[
                {
                    "key": "mysql.innodb_buffer_pool.hit_pct",
                    "name": "Buffer Pool 命中率",
                    "normal_range": ">= 99%",
                    "warning_range": "95% - 99%",
                    "critical_range": "< 95%",
                    "unit": "%",
                }
            ],
            threshold_config_json={},
        )

        def finding(value: float) -> Finding:
            return Finding(
                check_category="performance",
                check_item="性能指标巡检",
                check_status="normal",
                metric_name="mysql.innodb_buffer_pool.hit_pct",
                metric_value=str(value),
                threshold_value="",
                finding="旧结论",
                suggestion="",
                raw_data={
                    "template_item_key": "mysql.innodb_buffer_pool.hit_pct",
                    "template_threshold_eligible": True,
                },
            )

        critical = finding(94.99)
        warning_at_boundary = finding(95)
        warning = finding(96.15)
        normal_at_boundary = finding(99)

        _apply_policy_thresholds(
            [critical, warning_at_boundary, warning, normal_at_boundary],
            policy,
        )

        self.assertEqual(critical.check_status, "critical")
        self.assertEqual(warning_at_boundary.check_status, "warning")
        self.assertEqual(warning.check_status, "warning")
        self.assertEqual(normal_at_boundary.check_status, "normal")
        self.assertIn("96.15%", warning.finding)
        self.assertIn("95% - 99%", warning.finding)

    def test_slow_query_warning_uses_rate_instead_of_cumulative_count(self):
        values = {"slow_queries": 24, "innodb_log_waits": 0}

        low_rate = _mysql_performance_warnings(
            values,
            hit_pct=99.5,
            slow_queries_per_min=0.0123,
            tmp_disk_pct=0,
        )
        high_rate = _mysql_performance_warnings(
            values,
            hit_pct=99.5,
            slow_queries_per_min=5.1,
            tmp_disk_pct=0,
        )

        self.assertFalse(any("慢查询速率" in item for item in low_rate))
        self.assertTrue(any("5.1 次/分钟" in item for item in high_rate))


if __name__ == "__main__":
    unittest.main()
