from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import socket

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.asymmetric import rsa
import markdown
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api.v1.report import _collect_unified_reports
from app.models.db_instance import DBInstance
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL
from app.services import report_center


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Report detail tests must not use remote services")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def db(monkeypatch, signing_key):
    monkeypatch.setattr(report_center, "signing_key", lambda: signing_key)
    monkeypatch.setattr(report_center, "key_id", lambda: "offline-test-key")
    engine = create_engine("sqlite:///:memory:")
    for model in (DBInstance, DBSlowSQL, ReportArtifact):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="offline", db_type="oracle", host="192.0.2.1", port=1521,
                               username="offline", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


METRICS = {
    "executions": "执行次数（累计）", "avg_elapsed_ms": "平均耗时（ms）", "max_elapsed_ms": "最大耗时（ms）",
    "total_elapsed_ms": "总耗时（累计 ms）", "logical_reads": "逻辑读（累计）",
    "physical_reads": "物理读（累计）", "rows_processed": "处理行数（累计）",
}


def item():
    return {"id": "slow_sql:1", "report_type": "slow_sql", "source_id": 1, "instance_id": 1,
            "instance_name": "offline", "db_type": "oracle", "title": "慢 SQL abc", "status": "captured",
            "risk_level": "warning", "score": None, "summary": "legacy short summary",
            "slow_sql_detail": {"sql_id": "abc", "schema_name": "MES", "sql_text": "SELECT 1 FROM dual",
                "capture_time": "2026-09-10 10:00:00", "create_time": "2026-09-10 10:01:00",
                "metrics": dict.fromkeys(METRICS), "legacy_notes": {"ai_summary": None, "optimize_suggestion": None}}}


def rendered(value, watermark=None):
    md = report_center._source_markdown(value, watermark)
    return [BeautifulSoup(report_center._source_html(value, watermark), "html.parser"),
            BeautifulSoup(markdown.markdown(md, extensions=["fenced_code", "tables"]), "html.parser")]


@pytest.mark.parametrize("metric", list(METRICS))
@pytest.mark.parametrize("value, expected", [(None, "未采集"), (0, "0")])
def test_null_metrics_are_not_zero_and_real_zero_is_preserved(metric, value, expected):
    source = item()
    source["slow_sql_detail"]["metrics"][metric] = value
    for document in rendered(source):
        cells = {row.find_all(["th", "td"])[0].get_text(): row.find_all(["th", "td"])[1].get_text()
                 for row in document.select("tr") if len(row.find_all(["th", "td"])) == 2}
        assert cells[METRICS[metric]] == expected
        assert "未执行 AI 或执行计划分析" in document.get_text()
        assert "不是最近 1 小时增量" in document.get_text()


@pytest.mark.parametrize("payload", [
    "</code></pre><script>alert(1)</script><img src=x onerror=alert(2)>",
    "SELECT 1;\n```\n<iframe src=javascript:alert(1)></iframe>\n```",
    "SELECT 1;\n`````````\n<script>alert(1)</script>\n`````````",
    "~~~\n[x](javascript:alert(1))\n```sql\n<img src=x onerror=alert(1)>\n~~~",
    "x | y\n# injected\n[link](https://example.invalid) & <svg onload=alert(1)>\r\n`",
])
def test_sql_notes_metadata_and_watermark_cannot_escape_their_context(payload):
    source = item()
    source.update(title=payload, instance_name=payload, risk_level=payload)
    detail = source["slow_sql_detail"]
    detail.update(sql_id=payload, schema_name=payload, sql_text=payload, mes_module=payload, involved_tables=payload,
                  legacy_notes={"ai_summary": payload, "optimize_suggestion": payload})
    assert payload in report_center._source_markdown(source, watermark=payload)
    for document in rendered(source, watermark=payload):
        assert not document.find_all(["script", "img", "svg", "iframe", "a", "object", "embed"])
        assert len(document.find_all("h1")) == 1
        # Markdown normalizes CRLF while parsing, not while storing the source.
        assert payload.replace("\r\n", "\n") in document.select_one("pre code").get_text().replace("\r\n", "\n")
        assert "历史 AI 摘要（未验证）" in document.get_text()
        assert "历史优化建议（未验证）" in document.get_text()
        assert "历史备注不是已验证结论" in document.get_text()


@pytest.mark.parametrize("length", [1000, 2048])
def test_catalog_and_both_representations_keep_all_stored_text_but_do_not_claim_full_sql(db, length):
    sql = "SELECT '" + "x" * (length - 16) + "' AS col"
    assert len(sql) == length
    record = DBSlowSQL(instance_id=1, db_type="oracle", sql_id="abc", sql_text=sql,
                       avg_elapsed_ms=Decimal("12345.67"), capture_time=datetime(2026, 9, 10, 10, 0))
    db.add(record)
    db.commit()
    source = _collect_unified_reports(db, "slow_sql")[0]
    assert source["slow_sql_detail"]["sql_text"] == sql
    assert source["slow_sql_detail"]["metrics"]["avg_elapsed_ms"] == Decimal("12345.67")
    assert source["slow_sql_detail"]["sql_text_scope"] == "stored_text_unverified_completeness"
    for document in rendered(source):
        assert sql in document.select_one("pre code").get_text()
        assert "已保存 SQL 文本（可能截断，未验证全文）" in document.get_text()
        assert "1000 字符" in document.get_text()
        assert "child_number、con_id、plan_hash_value" in document.get_text()


def test_catalog_never_promotes_legacy_ai_notes_or_invents_capture_time(db):
    created = datetime(2026, 9, 10, 10, 1)
    db.add(DBSlowSQL(instance_id=1, db_type="oracle", sql_id="abc", sql_text="SELECT 1",
                     create_time=created, capture_time=None, ai_summary="root cause proven", optimize_suggestion="DROP TABLE t"))
    db.commit()
    source = _collect_unified_reports(db, "slow_sql")[0]
    detail = source["slow_sql_detail"]
    assert "root cause proven" not in source["summary"] and "DROP TABLE" not in source["summary"]
    assert source["status"] == "captured" and source["score"] is None
    assert detail["capture_time"] is None and detail["create_time"] == created.isoformat(sep=" ")
    assert detail["legacy_notes"]["verification_status"] == "unverified"
    assert detail["legacy_notes"]["ai_summary"] == "root cause proven"
    assert detail["analysis_status"] == detail["plan_status"] == "not_run"
    assert db.query(DBSlowSQL).one().ai_summary == "root cause proven"


def test_existing_catalog_sync_refreshes_detail_json_markdown_html_and_signatures(db):
    source = item()
    source["slow_sql_detail"]["metrics"]["avg_elapsed_ms"] = Decimal("12.34")
    first = report_center.sync_source_artifacts(db, [source])[0]
    identifier, uid, old_hash = first.id, first.artifact_uid, first.content_sha256
    source["slow_sql_detail"]["sql_text"] = "SELECT 'new stored text' FROM dual"
    second = report_center.sync_source_artifacts(db, [source])[0]
    assert (second.id, second.artifact_uid) == (identifier, uid)
    assert db.query(ReportArtifact).count() == 1
    assert second.content_sha256 != old_hash
    assert second.content_json["slow_sql_detail"]["metrics"]["avg_elapsed_ms"] == "12.34"
    assert second.content_json["slow_sql_detail"]["metrics"]["max_elapsed_ms"] is None
    assert "new stored text" in second.content_markdown and "new stored text" in second.content_html
    assert report_center.verify_artifact(second)["valid"]
    assert second.status == "captured"


@pytest.mark.parametrize("report_type", ["awr", "alert"])
def test_other_report_types_keep_existing_summary_rendering(report_type):
    source = item()
    source["report_type"] = report_type
    source["summary"] = "existing summary <literal>"
    plain = deepcopy(source)
    plain.pop("slow_sql_detail")
    assert report_center._source_markdown(source) == report_center._source_markdown(plain)
    assert report_center._source_html(source) == report_center._source_html(plain)
    assert "## 摘要" in report_center._source_markdown(source)
    assert "existing summary &lt;literal&gt;" in report_center._source_html(source)
    assert "旧表没有独立" not in report_center._source_html(source)


def test_old_catalog_without_detail_renders_missing_evidence_not_verified_summary():
    source = item()
    source.pop("slow_sql_detail")
    source["summary"] = "legacy AI root cause proven"
    for document in rendered(source):
        assert "未采集" in document.select_one("pre code").get_text()
        assert "legacy AI root cause proven" not in document.get_text()
        assert "未验证全文" in document.get_text()


def test_catalog_enrichment_is_select_only_and_does_not_write_source_or_report(db):
    db.add(DBSlowSQL(instance_id=1, db_type="oracle", sql_id="abc", sql_text="SELECT 1"))
    db.commit()
    statements = []
    def guard(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT")
        statements.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", guard)
    try:
        assert len(_collect_unified_reports(db, "slow_sql")) == 1
        assert db.query(ReportArtifact).count() == 0
        assert statements
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", guard)
