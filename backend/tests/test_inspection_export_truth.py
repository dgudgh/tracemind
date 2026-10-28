from copy import deepcopy
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from app.api.v1 import report


def payload(execution="failed", task_status="failed"):
    return {
        "task": {"task_id": "offline-export", "task_status": task_status,
                 "overall_score": 100, "risk_level": "normal", "normal_count": 1},
        "modules": [{"module_key": "probe", "module_name": "Probe", "score": 100,
                     "task_status": task_status, "risk_level": "normal"}],
        "findings": [{
            "check_item": "Probe", "module_key": "probe", "severity": "normal",
            "finding": "fixture observation",
            "evidence": {"execution_status": execution, "sql": "SELECT name FROM probe"},
        }],
        "report": {"instance": {"name": "offline-fixture", "db_type": "oracle"},
                   "overall_conclusion": "未发现明显风险"},
    }


def rendered(data, format):
    detail = report._build_inspection_report_detail(data)
    if format == "markdown":
        return detail["markdown"]
    if format == "html":
        return report._build_inspection_html(detail)
    with ZipFile(BytesIO(report._build_inspection_docx(data).getvalue())) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join(node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
@pytest.mark.parametrize("execution,label", [
    ("failed", "采集失败"), ("partial", "部分完成"),
    ("skipped", "未执行"), ("unknown", "未确认"),
])
def test_export_execution_state_never_comes_from_severity(format, execution, label):
    data = payload(execution)
    output = rendered(data, format)
    assert label in output
    assert "SQL 执行成功，返回 0 行" not in output
    assert "未记录" in output


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
@pytest.mark.parametrize("state", ["failed", "partial", None])
def test_incomplete_export_never_keeps_stale_health_score(format, state):
    data = payload(task_status=state)
    original = deepcopy(data)
    detail = report._build_inspection_report_detail(data)
    assert detail["summary"]["score"] is None
    assert detail["category_summary"][0]["score"] is None
    output = rendered(data, format)
    assert "100/100" not in output
    assert "未发现明显风险" not in output
    assert "未发现严重或警告风险" not in output
    assert data == original


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
def test_confirmed_zero_rows_can_succeed_despite_critical_health_risk(format):
    data = payload("success", "success")
    data["findings"][0]["severity"] = "critical"
    data["findings"][0]["evidence"].update({"rows": [], "row_count": 0, "columns": ["NAME"]})
    output = rendered(data, format)
    assert "执行成功" in output
    assert "SQL 执行成功，返回 0 行" in output


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
def test_missing_result_rows_are_not_reported_as_zero(format):
    data = payload("success", "success")
    data["findings"][0]["evidence"]["row_count"] = 12
    output = rendered(data, format)
    assert "返回 12 行，未保存可展示明细" in output
    assert "返回 0 行" not in output


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
def test_missing_sql_and_cursor_evidence_are_not_fabricated(format):
    data = payload("unknown", "success")
    data["findings"][0]["evidence"] = {"execution_status": "unknown", "metric": "cpu", "value": 12}
    output = rendered(data, format)
    assert "未确认" in output
    assert "未记录" in output
    assert "SELECT" not in output
    assert "返回 0 行" not in output


def test_export_table_keeps_recorded_rows_but_does_not_infer_total_from_preview():
    data = payload("success", "success")
    evidence = data["findings"][0]["evidence"]
    evidence.update({"rows": [{"NAME": "LAB"}], "columns": ["NAME"], "truncated": True})
    output = rendered(data, "markdown")
    assert "| LAB |" in output
    assert "返回行数: 未记录" in output


@pytest.mark.parametrize("format", ["markdown", "html", "docx"])
def test_empty_saved_rows_without_recorded_count_are_not_claimed_as_zero(format):
    data = payload("success", "success")
    data["findings"][0]["evidence"]["rows"] = []
    output = rendered(data, format)
    assert "返回 0 行" not in output
    assert "未记录" in output


@pytest.mark.parametrize("raw,expected", [
    ({"execution_status": "unknown", "error": "permission denied"}, "failed"),
    ({"execution_status": "success", "state": "not_applicable"}, "skipped"),
    ({"execution_status": "partial", "state": "collected"}, "partial"),
    ({"sql": "SELECT 1", "evidence_source": "legacy_saved_metric", "status": "normal"}, "unknown"),
    ({"sql": "SELECT 1", "status": "warning", "message": "ORA-01031"}, "failed"),
])
def test_export_status_precedence_matches_inspection_evidence(raw, expected):
    assert report._export_execution_status({"severity": "normal"}, raw) == expected


def test_docx_sql_keeps_chinese_font_when_latin_text_is_monospace():
    data = payload("success", "success")
    data["findings"][0]["evidence"].update({
        "sql": "SELECT '中文结果' AS name FROM dual -- 中文注释",
        "rows": [{"NAME": "中文结果"}], "row_count": 1,
    })
    with ZipFile(BytesIO(report._build_inspection_docx(data).getvalue())) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    sql_runs = [run for run in root.findall(".//w:r", ns)
                if "SELECT" in "".join(run.itertext())]
    assert sql_runs
    for run in sql_runs:
        fonts = run.find("w:rPr/w:rFonts", ns)
        assert fonts is not None
        assert fonts.get("{%s}ascii" % ns["w"]) == "Consolas"
        assert fonts.get("{%s}eastAsia" % ns["w"]) in {"Arial Unicode MS", "Microsoft YaHei"}
