"""Isolated query budgets for catalog sync, including serialization after commit."""

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from app.api.v1 import report_center as api
from app.api.v1.report import _collect_unified_reports
from app.models.db_instance import DBInstance
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL
from app.services import report_center as service


NOW = datetime(2026, 9, 10, 2, 34, 17)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def db(monkeypatch, signing_key):
    monkeypatch.setattr(service, "signing_key", lambda: signing_key)
    monkeypatch.setattr(service, "key_id", lambda: "isolated-performance-key")
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    engine = create_engine("sqlite://")
    for model in (DBInstance, DBSlowSQL, ReportArtifact):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            DBInstance(id=i, instance_name=f"fixture-{i}", db_type="oracle", host="192.0.2.1",
                       port=1521, username="fixture", password_cipher="unused", enabled=1)
            for i in (1, 2)
        ])
        session.commit()
        yield session
    engine.dispose()


@contextmanager
def query_counts(db):
    counts = Counter()
    statements = []

    def before_execute(_conn, _cursor, statement, _params, _context, _many):
        counts[statement.split()[0].upper()] += 1
        statements.append(statement)

    def committed(_session):
        counts["COMMIT"] += 1

    event.listen(db.get_bind(), "before_cursor_execute", before_execute)
    event.listen(db, "after_commit", committed)
    try:
        yield counts, statements
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", before_execute)
        event.remove(db, "after_commit", committed)


def sources(count):
    return [dict(source_id=i + 1, report_type="inspection", instance_id=1, title=f"fixture-{i}",
                 summary="unchanged", create_time="2026-09-10 18:34:17") for i in range(count)]


@pytest.mark.parametrize("count", [1, 200, 801])
def test_sync_query_budget_and_unchanged_rows_are_never_rewritten(db, monkeypatch, count):
    items = sources(count)
    batches = (count + 399) // 400
    with query_counts(db) as (counts, _):
        rows = service.sync_source_artifacts(db, items)
        before = [deepcopy(service.serialize_artifact(row, include_content=True)) for row in rows]
        signatures = [row.signature for row in rows]
    assert counts["SELECT"] == 1 + 2 * batches
    assert counts["COMMIT"] == 1
    assert counts["UPDATE"] == 0
    db.expunge_all()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Unchanged sources must not render or sign again")

    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(days=1))
    monkeypatch.setattr(service, "sign_artifact", unexpected)
    monkeypatch.setattr(service, "_source_markdown", unexpected)
    monkeypatch.setattr(service, "_source_html", unexpected)
    with query_counts(db) as (counts, _):
        rows = service.sync_source_artifacts(db, items, actor="different-actor", retention_days=1)
        after = [service.serialize_artifact(row, include_content=True) for row in rows]
        assert [row.signature for row in rows] == signatures
        assert after == before
    assert counts["SELECT"] == 1 + batches
    assert not any(counts[k] for k in ("INSERT", "UPDATE", "DELETE", "COMMIT"))


def test_mixed_sync_signs_only_changed_and_new_sources_and_preserves_lifecycle(db, monkeypatch):
    items = sources(200)
    rows = service.sync_source_artifacts(db, items)
    changed = rows[0]
    changed.archive_state = "archived"
    changed.archive_uri = "fixture://historical"
    changed.legal_hold = 1
    changed.watermark = "fixture-watermark"
    service.sign_artifact(changed)
    db.commit()
    before = deepcopy(service.serialize_artifact(changed, include_content=True))
    signatures = {row.source_key: row.signature for row in rows}
    original_sign = service.sign_artifact
    signed = []

    def counted_sign(row, **kwargs):
        signed.append(row.source_key)
        return original_sign(row, **kwargs)

    monkeypatch.setattr(service, "sign_artifact", counted_sign)
    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(hours=1))
    items[0]["summary"] = "changed"
    items.append(dict(items[1], source_id=201))
    with query_counts(db) as (counts, _):
        result = service.sync_source_artifacts(db, items)
        serialized = [service.serialize_artifact(row) for row in result]
    assert counts["SELECT"] == 3
    assert counts["COMMIT"] == counts["UPDATE"] == 1
    assert signed == ["source:inspection:1", "source:inspection:201"]
    assert len(serialized) == 201
    for row in result:
        assert service.verify_artifact(row)["valid"]
        if row.source_key not in signed:
            assert row.signature == signatures[row.source_key]
    for field in ("create_time", "retention_until", "archive_state", "archive_uri", "legal_hold", "watermark"):
        assert serialized[0][field] == before[field]
    assert result[0].extra_json["source_create_time"] == items[0]["create_time"]
    assert result[0].signed_at == NOW + timedelta(hours=1)


def test_legacy_unchanged_signed_representations_and_clock_tags_survive_sync(db):
    item = sources(1)[0]
    row = service.sync_source_artifacts(db, [item])[0]
    row.content_markdown = "historical renderer output"
    row.content_html = "<p>historical renderer output</p>"
    service.sign_artifact(row)
    row.extra_json = {key: value for key, value in row.extra_json.items() if key != "application_utc_fields"}
    db.commit()
    before = deepcopy(service.serialize_artifact(row, include_content=True))
    extra = deepcopy(row.extra_json)
    signature = row.signature
    with query_counts(db) as (counts, _):
        again = service.sync_source_artifacts(db, [item])[0]
    assert counts["UPDATE"] == counts["COMMIT"] == 0
    assert service.serialize_artifact(again, include_content=True) == before
    assert again.extra_json == extra
    assert again.signature == signature
    assert service.verify_artifact(again)["valid"]


def test_scope_metadata_updates_without_rewriting_or_resigning_payload(db, monkeypatch):
    item = sources(1)[0]
    row = service.sync_source_artifacts(db, [item])[0]
    signature, signed_at = row.signature, row.signed_at
    db.get(DBInstance, 1).organization_id = 42
    db.commit()
    monkeypatch.setattr(service, "utcnow", lambda: NOW + timedelta(hours=1))
    with query_counts(db) as (counts, _):
        row = service.sync_source_artifacts(db, [item])[0]
    assert counts["UPDATE"] == counts["COMMIT"] == 1
    assert row.organization_id == 42
    assert row.signature == signature and row.signed_at == signed_at
    assert row.update_time == NOW + timedelta(hours=1)
    assert service.verify_artifact(row)["valid"]


def test_duplicate_sources_share_one_insert_and_empty_sync_has_no_transaction(db):
    item = sources(1)[0]
    with query_counts(db) as (counts, _):
        rows = service.sync_source_artifacts(db, [item, item, dict(item, summary="last")])
        assert rows[0].summary == "last"
    assert rows[0] is rows[1] is rows[2]
    assert counts["INSERT"] == counts["COMMIT"] == 1
    with query_counts(db) as (counts, _):
        assert service.sync_source_artifacts(db, []) == []
    assert not counts


@pytest.mark.parametrize("scoped", [False, True])
def test_catalog_metadata_projection_has_no_serialization_n_plus_one(db, scoped):
    service.sync_source_artifacts(db, sources(200))
    db.expunge_all()
    with query_counts(db) as (counts, statements):
        rows = service.list_artifacts(db, metadata_only=True, limit=200,
                                      allowed_instance_ids={1} if scoped else None,
                                      date_from=NOW if scoped else None)
        assert len(rows) == 200
        assert {"content_json", "content_markdown", "content_html"}.issubset(inspect(rows[0]).unloaded)
        assert len([service.serialize_artifact(row) for row in rows]) == 200
        assert service.artifact_stats(rows)["signed"] == 200
    assert counts["SELECT"] == 1
    for field in ("content_json", "content_markdown", "content_html"):
        assert f"report_artifact.{field}" not in statements[0]


@pytest.mark.parametrize("count", [1, 200])
def test_real_source_collection_and_catalog_api_keep_refresh_and_scope(db, monkeypatch, count):
    db.add_all([DBSlowSQL(instance_id=1 + (i % 2), db_type="oracle", sql_id=f"fixture-{i}",
                         sql_text="select 1 from dual", capture_time=NOW) for i in range(count)])
    db.commit()
    with query_counts(db) as (counts, _):
        collected = _collect_unified_reports(db, "slow_sql", count)
    assert len(collected) == count
    assert counts["SELECT"] == 2
    request = SimpleNamespace(state=SimpleNamespace(auth={"role": "admin", "username": "fixture"}))
    monkeypatch.setattr(api, "_allowed_instance_ids", lambda *_args: {1})

    def catalog(refresh=True):
        return api.list_report_catalog(request, report_type="slow_sql", keyword=None, instance_id=None,
                                       archive_state="all", date_from=None, date_to=None,
                                       refresh_sources=refresh, limit=200, db=db)

    with query_counts(db) as (counts, _):
        first = catalog()
    assert len(first["items"]) == (count + 1) // 2
    assert all(row["instance_id"] == 1 for row in first["items"])
    assert counts["SELECT"] == 6
    with query_counts(db) as (counts, _):
        assert catalog() == first
    assert counts["SELECT"] == 5
    assert counts["UPDATE"] == counts["COMMIT"] == 0
    db.add(DBSlowSQL(instance_id=1, db_type="oracle", sql_id="new-fixture", sql_text="select 2", capture_time=NOW))
    db.commit()
    with query_counts(db) as (counts, _):
        assert catalog(False) == first
    assert counts["SELECT"] == 1
    assert any("new-fixture" in row["title"] for row in catalog()["items"])


@pytest.mark.parametrize("representation", ["markdown", "html"])
def test_slow_sql_ai_format_entry_delegates_without_owning_renderer(monkeypatch, representation):
    calls = []

    def render(source, watermark=None):
        calls.append((source, watermark))
        return "delegated-full-report"

    monkeypatch.setitem(sys.modules, "app.services.slow_sql_ai_report", SimpleNamespace(
        source_markdown=render, source_html=render,
    ))
    source = {"report_type": "ai", "analysis_detail": {"kind": "slow_sql_ai", "model_result": "full fixture"}}
    render_source = getattr(service, f"_source_{representation}")
    assert render_source(source, "fixture-watermark") == "delegated-full-report"
    assert calls == [(source, "fixture-watermark")]
    assert render_source({"report_type": "ai", "title": "legacy AI"}) != "delegated-full-report"
    assert len(calls) == 1
