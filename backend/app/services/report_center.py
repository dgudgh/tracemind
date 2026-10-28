from __future__ import annotations

import base64
import calendar
import hashlib
import html
import json
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, attributes, defer

from app.core.jwt_keys import key_id, signing_key
from app.models.db_instance import DBInstance
from app.models.report_center import (
    ReportArtifact,
    ReportArtifactAttachment,
    ReportSchedule,
    ReportScheduleRun,
    ReportSubscription,
    SchedulerDispatchRun,
    SchedulerLease,
)
from app.services.access_control import append_security_audit


REPORT_TYPES = {
    "all",
    "inspection",
    "awr",
    "performance",
    "slow_sql",
    "alert",
    "ticket",
    "backup",
    "governance",
    "ai",
    "rescue",
}
REPORT_FORMATS = {"markdown", "html", "json", "word"}
SCHEDULE_TYPES = {"manual", "interval", "cron", "daily", "workday", "weekly", "monthly", "quarterly"}
MISFIRE_POLICIES = {"skip", "fire_once", "catch_up"}
CATALOG_TIMEZONE = ZoneInfo("Asia/Shanghai")

# No durable per-run heartbeat exists yet. Expiry quarantines uncertain work;
# it never authorizes replay. Live calls in this process retain their ownership.
STALE_RUN_AFTER = timedelta(hours=24)
_ACTIVE_EXECUTIONS: set[tuple[str, int]] = set()
_EXECUTION_LOCK = threading.Lock()


@contextmanager
def _execution_guard(kind: str, identity: int):
    key = (kind, identity)
    with _EXECUTION_LOCK:
        acquired = key not in _ACTIVE_EXECUTIONS
        if acquired:
            _ACTIVE_EXECUTIONS.add(key)
    try:
        yield acquired
    finally:
        if acquired:
            with _EXECUTION_LOCK:
                _ACTIVE_EXECUTIONS.remove(key)


def _scheduler_recovery_allowed(db: Session, owner_id: str | None = None) -> bool:
    # Keep recovery inside the existing master leadership boundary. Standalone
    # callers without a configured lease retain the existing single-worker mode.
    lease = db.query(SchedulerLease).populate_existing().filter_by(lease_key="master-due-tasks").first()
    return lease is None or (
        lease.owner_id == (owner_id or worker_identity())
        and lease.lease_until is not None and lease.lease_until > utcnow()
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{__import__('os').getpid()}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _utc_naive(value: datetime) -> datetime:
    # Internal report/scheduler datetimes are UTC, even with a naive DB column.
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _set_artifact_times(artifact: ReportArtifact, **values: datetime) -> None:
    extra = dict(artifact.extra_json or {})
    fields = set(extra.get("application_utc_fields") or [])
    for name, value in values.items():
        setattr(artifact, name, _utc_naive(value))
        if name == "update_time":
            # Even same-tick updates must override the column's SQL NOW() fallback.
            attributes.flag_modified(artifact, name)
        fields.add(name)
    extra["application_utc_fields"] = sorted(fields)
    artifact.extra_json = extra


def _artifact_iso(artifact: ReportArtifact, name: str) -> str | None:
    value = getattr(artifact, name)
    if value and name in ((artifact.extra_json or {}).get("application_utc_fields") or []):
        # The catalog displays wall time verbatim; match its Shanghai schedule default.
        value = _utc_naive(value).replace(tzinfo=timezone.utc).astimezone(CATALOG_TIMEZONE)
    # Untagged historical values have unknown clock provenance; never shift them.
    return _iso(value)


def _canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _json_safe(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _representation_hashes(artifact: ReportArtifact) -> dict[str, str]:
    values = {
        "markdown": artifact.content_markdown.encode("utf-8") if artifact.content_markdown is not None else None,
        "html": artifact.content_html.encode("utf-8") if artifact.content_html is not None else None,
        "json": _canonical_json(artifact.content_json) if artifact.content_json is not None else None,
    }
    return {name: hashlib.sha256(value).hexdigest() for name, value in values.items() if value is not None}


def _content_bytes(artifact: ReportArtifact) -> bytes:
    if artifact.content_markdown is not None:
        return artifact.content_markdown.encode("utf-8")
    if artifact.content_html is not None:
        return artifact.content_html.encode("utf-8")
    return _canonical_json(artifact.content_json or {})


def _signature_payload(artifact: ReportArtifact) -> bytes:
    return _canonical_json({
        "artifact_uid": artifact.artifact_uid,
        "source_key": artifact.source_key,
        "content_sha256": artifact.content_sha256,
        "representation_hashes": (artifact.extra_json or {}).get("representation_hashes") or {},
        "watermark": artifact.watermark,
    })


def sign_artifact(artifact: ReportArtifact, *, now: datetime | None = None) -> ReportArtifact:
    artifact.content_sha256 = hashlib.sha256(_content_bytes(artifact)).hexdigest()
    extra = dict(artifact.extra_json or {})
    extra["representation_hashes"] = _representation_hashes(artifact)
    artifact.extra_json = extra
    artifact.signature_algorithm = "RS256"
    artifact.signing_key_id = key_id()
    current = _utc_naive(now or utcnow())
    _set_artifact_times(artifact, signed_at=current, update_time=current)
    signature = signing_key().sign(_signature_payload(artifact), padding.PKCS1v15(), hashes.SHA256())
    artifact.signature = base64.b64encode(signature).decode("ascii")
    return artifact


def verify_artifact(artifact: ReportArtifact) -> dict:
    actual_hash = hashlib.sha256(_content_bytes(artifact)).hexdigest()
    hash_valid = bool(artifact.content_sha256) and actual_hash == artifact.content_sha256
    expected_representations = dict((artifact.extra_json or {}).get("representation_hashes") or {})
    actual_representations = _representation_hashes(artifact)
    representations_valid = bool(expected_representations) and expected_representations == actual_representations
    signature_valid = False
    error = None
    if hash_valid and representations_valid and artifact.signature:
        try:
            signing_key().public_key().verify(
                base64.b64decode(artifact.signature),
                _signature_payload(artifact),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            signature_valid = True
        except Exception as exc:  # verification must return evidence, not mask the artifact
            error = str(exc) or exc.__class__.__name__
    elif not artifact.signature:
        error = "artifact is not signed"
    return {
        "artifact_id": artifact.id,
        "artifact_uid": artifact.artifact_uid,
        "valid": hash_valid and representations_valid and signature_valid,
        "hash_valid": hash_valid,
        "representations_valid": representations_valid,
        "representation_hashes": actual_representations,
        "signature_valid": signature_valid,
        "expected_sha256": artifact.content_sha256,
        "actual_sha256": actual_hash,
        "signature_algorithm": artifact.signature_algorithm,
        "signing_key_id": artifact.signing_key_id,
        "signed_at": _artifact_iso(artifact, "signed_at"),
        "error": error,
    }


def _source_markdown(item: dict, watermark: str | None = None) -> str:
    if item.get("report_type") == "ai" and (item.get("analysis_detail") or {}).get("kind") == "slow_sql_ai":
        from app.services.slow_sql_ai_report import source_markdown

        return source_markdown(item, watermark)
    if item.get("report_type") == "slow_sql":
        import re

        detail = item.get("slow_sql_detail") or {}
        metrics = detail.get("metrics") or {}

        def cell(value):
            value = "未采集" if value is None or value == "" else str(value)
            return "".join(f"&#{ord(char)};" if char in "\\`*_{}[]()#+-.!|><&\"'~" else
                           " " if char in "\r\n" else char for char in value)

        def fenced(value, language):
            value = str(value) if value is not None and value != "" else "未采集"
            fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
            return f"{fence}{language}\n{value}\n{fence}"

        values = [
            ("实例", item.get("instance_name")), ("数据库类型", item.get("db_type")),
            ("状态", item.get("status")), ("风险", item.get("risk_level")),
            ("SQL ID", detail.get("sql_id")), ("Schema", detail.get("schema_name")),
            ("采样时间（原记录）", detail.get("capture_time")), ("入库时间", detail.get("create_time")),
            ("执行次数（累计）", metrics.get("executions")), ("平均耗时（ms）", metrics.get("avg_elapsed_ms")),
            ("最大耗时（ms）", metrics.get("max_elapsed_ms")), ("总耗时（累计 ms）", metrics.get("total_elapsed_ms")),
            ("逻辑读（累计）", metrics.get("logical_reads")), ("物理读（累计）", metrics.get("physical_reads")),
            ("处理行数（累计）", metrics.get("rows_processed")),
            ("历史关联表", detail.get("involved_tables")), ("历史 MES 模块", detail.get("mes_module")),
        ]
        lines = [f"# {cell(item.get('title') or '慢 SQL 采样记录')}", "",
                 "本报告仅展示已保存采样数据，未执行 AI 或执行计划分析；历史备注不是已验证结论。", "",
                 "| 项目 | 内容 |", "| --- | --- |"]
        lines.extend(f"| {label} | {cell(value)} |" for label, value in values)
        lines.extend(["", "## 已保存 SQL 文本（可能截断，未验证全文）", "", fenced(detail.get("sql_text"), "sql"), "",
                      "## 数据边界", "",
                      "展示 DBSlowSQL 已保存 SQL 文本，不再次截断；采集链可能已截断（例如 1000 字符），未验证全文。",
                      "指标为旧表累计采样快照，不是最近 1 小时增量；累计起点、时间窗及采样时区未保存，不能据此还原窗口统计。",
                      "旧表没有独立 child_number、con_id、plan_hash_value 或执行计划证据列，不能还原子游标、容器或验证计划。",
                      "空值表示未采集，不代表 0。历史关联表、模块、AI 摘要和优化建议均未在本报告中重新验证。"])
        notes = detail.get("legacy_notes") or {}
        for key, title in (("ai_summary", "历史 AI 摘要（未验证）"), ("optimize_suggestion", "历史优化建议（未验证）")):
            lines.extend(["", f"## {title}", "", fenced(notes.get(key), "text")])
        if watermark:
            lines.extend(["", "---", "", f"水印：{cell(watermark)}"])
        return "\n".join(lines)
    lines = [
        f"# {item.get('title') or '运维报告'}",
        "",
        f"> 报告来源：{item.get('report_type') or '-'}",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 实例 | {item.get('instance_name') or '-'} |",
        f"| 数据库类型 | {item.get('db_type') or '-'} |",
        f"| 状态 | {item.get('status') or '-'} |",
        f"| 风险 | {item.get('risk_level') or '-'} |",
        f"| 评分 | {item.get('score') if item.get('score') is not None else '-'} |",
        f"| 生成时间 | {item.get('create_time') or '-'} |",
        "",
        "## 摘要",
        "",
        str(item.get("summary") or "暂无摘要。"),
    ]
    if watermark:
        lines.extend(["", "---", "", f"水印：{watermark}"])
    return "\n".join(lines)


def _source_html(item: dict, watermark: str | None = None) -> str:
    if item.get("report_type") == "ai" and (item.get("analysis_detail") or {}).get("kind") == "slow_sql_ai":
        from app.services.slow_sql_ai_report import source_html

        return source_html(item, watermark)
    if item.get("report_type") == "slow_sql":
        detail = item.get("slow_sql_detail") or {}
        metrics = detail.get("metrics") or {}

        def escaped(value):
            return html.escape("未采集" if value is None or value == "" else str(value))

        values = [
            ("实例", item.get("instance_name")), ("数据库类型", item.get("db_type")),
            ("状态", item.get("status")), ("风险", item.get("risk_level")),
            ("SQL ID", detail.get("sql_id")), ("Schema", detail.get("schema_name")),
            ("采样时间（原记录）", detail.get("capture_time")), ("入库时间", detail.get("create_time")),
            ("执行次数（累计）", metrics.get("executions")), ("平均耗时（ms）", metrics.get("avg_elapsed_ms")),
            ("最大耗时（ms）", metrics.get("max_elapsed_ms")), ("总耗时（累计 ms）", metrics.get("total_elapsed_ms")),
            ("逻辑读（累计）", metrics.get("logical_reads")), ("物理读（累计）", metrics.get("physical_reads")),
            ("处理行数（累计）", metrics.get("rows_processed")),
            ("历史关联表", detail.get("involved_tables")), ("历史 MES 模块", detail.get("mes_module")),
        ]
        rows = "".join(f"<tr><th>{label}</th><td>{escaped(value)}</td></tr>" for label, value in values)
        notes = detail.get("legacy_notes") or {}
        watermark_html = f'<footer>{escaped(watermark)}</footer>' if watermark else ""
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>慢 SQL 采样记录</title>'
            '<style>body{font:14px sans-serif;max-width:980px;margin:36px auto;padding:0 24px;overflow-wrap:anywhere}'
            'table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}footer{margin-top:32px}</style></head><body>'
            f'<h1>{escaped(item.get("title") or "慢 SQL 采样记录")}</h1>'
            '<p>本报告仅展示已保存采样数据，未执行 AI 或执行计划分析；历史备注不是已验证结论。</p>'
            f'<table>{rows}</table><h2>已保存 SQL 文本（可能截断，未验证全文）</h2><pre><code>{escaped(detail.get("sql_text"))}</code></pre>'
            '<h2>数据边界</h2><p>展示 DBSlowSQL 已保存 SQL 文本，不再次截断；采集链可能已截断（例如 1000 字符），未验证全文。</p>'
            '<p>指标为旧表累计采样快照，不是最近 1 小时增量；累计起点、时间窗及采样时区未保存，不能据此还原窗口统计。</p>'
            '<p>旧表没有独立 child_number、con_id、plan_hash_value 或执行计划证据列，不能还原子游标、容器或验证计划。</p>'
            '<p>空值表示未采集，不代表 0。历史关联表、模块、AI 摘要和优化建议均未在本报告中重新验证。</p>'
            f'<h2>历史 AI 摘要（未验证）</h2><pre>{escaped(notes.get("ai_summary"))}</pre>'
            f'<h2>历史优化建议（未验证）</h2><pre>{escaped(notes.get("optimize_suggestion"))}</pre>'
            f'{watermark_html}</body></html>'
        )
    values = [
        ("实例", item.get("instance_name")),
        ("数据库类型", item.get("db_type")),
        ("状态", item.get("status")),
        ("风险", item.get("risk_level")),
        ("评分", item.get("score")),
        ("生成时间", item.get("create_time")),
    ]
    rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value if value not in (None, '') else '-'))}</td></tr>"
        for label, value in values
    )
    watermark_html = f'<div class="watermark">{html.escape(watermark)}</div>' if watermark else ""
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>运维报告</title><style>body{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#1d1d1f;"
        "max-width:980px;margin:36px auto;padding:0 24px}h1{font-size:26px}table{border-collapse:collapse;width:100%;margin:24px 0}"
        "th,td{border:1px solid #d8d8dc;padding:10px 12px;text-align:left}th{width:180px;background:#f5f5f7}.summary{white-space:pre-wrap;line-height:1.7}"
        ".watermark{margin-top:40px;color:#86868b;border-top:1px solid #d8d8dc;padding-top:12px}</style></head><body>"
        f"<h1>{html.escape(str(item.get('title') or '运维报告'))}</h1><table>{rows}</table>"
        f"<h2>摘要</h2><div class=\"summary\">{html.escape(str(item.get('summary') or '暂无摘要。'))}</div>"
        f"{watermark_html}</body></html>"
    )


def _snapshot_markdown(title: str, items: list[dict], generated_at: datetime, watermark: str | None) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{_iso(generated_at)} UTC",
        f"- 报告数量：{len(items)}",
        "",
        "| 类型 | 标题 | 实例 | 数据库 | 状态 | 风险 | 时间 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        cells = [
            item.get("report_type"), item.get("title"), item.get("instance_name"), item.get("db_type"),
            item.get("status"), item.get("risk_level"), item.get("create_time"),
        ]
        lines.append("| " + " | ".join(str(value or "-").replace("|", "/").replace("\n", " ") for value in cells) + " |")
    if watermark:
        lines.extend(["", "---", "", f"水印：{watermark}"])
    return "\n".join(lines)


def _snapshot_html(title: str, items: list[dict], generated_at: datetime, watermark: str | None) -> str:
    rows = []
    for item in items:
        cells = [
            item.get("report_type"), item.get("title"), item.get("instance_name"), item.get("db_type"),
            item.get("status"), item.get("risk_level"), item.get("create_time"),
        ]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value or '-'))}</td>" for value in cells) + "</tr>")
    watermark_html = f'<footer>{html.escape(watermark)}</footer>' if watermark else ""
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>统一报告</title>"
        "<style>body{font:14px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#1d1d1f;max-width:1200px;margin:36px auto;padding:0 24px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d8d8dc;padding:9px;text-align:left}th{background:#f5f5f7}"
        "footer{margin-top:32px;color:#86868b;border-top:1px solid #ddd;padding-top:12px}</style></head><body>"
        f"<h1>{html.escape(title)}</h1><p>生成时间：{html.escape(_iso(generated_at) or '-')} UTC，共 {len(items)} 份报告。</p>"
        "<table><thead><tr><th>类型</th><th>标题</th><th>实例</th><th>数据库</th><th>状态</th><th>风险</th><th>时间</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{watermark_html}</body></html>"
    )


def sync_source_artifacts(
    db: Session,
    items: Iterable[dict],
    *,
    actor: str = "system",
    retention_days: int = 365,
    commit: bool = True,
) -> list[ReportArtifact]:
    """Synchronize existing source renderers; callers may own the publication transaction."""
    items = list(items)
    if not items:
        return []
    sources = []
    for source in items:
        source_id = str(source.get("source_id") or source.get("id") or uuid.uuid4().hex)
        source_type = str(source.get("report_type") or "unknown")
        sources.append((f"source:{source_type}:{source_id}", source_id, source_type, source))
    source_keys = list(dict.fromkeys(value[0] for value in sources))
    # Bound IN parameters for SQLite as well as production databases. Keep the
    # identity map populated before making changes so queries cannot autoflush
    # one update at a time.
    batch_size = 400
    existing = {}
    for offset in range(0, len(source_keys), batch_size):
        existing.update({row.source_key: row for row in db.query(ReportArtifact).filter(
            ReportArtifact.source_key.in_(source_keys[offset:offset + batch_size])
        ).all()})
    instance_ids = {int(item["instance_id"]) for item in items if item.get("instance_id") is not None}
    instances = {
        row.id: row for row in db.query(DBInstance.id, DBInstance.tenant_id, DBInstance.organization_id)
        .filter(DBInstance.id.in_(instance_ids)).all()
    } if instance_ids else {}
    rows: list[ReportArtifact] = []
    changed = False
    now = utcnow()
    for source_key, source_id, source_type, source in sources:
        row = existing.get(source_key)
        instance_id = int(source["instance_id"]) if source.get("instance_id") is not None else None
        instance = instances.get(instance_id)
        payload = _json_safe(dict(source))
        watermark = row.watermark if row else None
        tenant_id = getattr(instance, "tenant_id", None)
        organization_id = getattr(instance, "organization_id", None)
        if row is not None and row.content_json == payload and row.signature:
            # Preserve signed historical representations, even when renderers or
            # signing keys changed. Scope metadata may still change independently.
            if row.tenant_id != tenant_id or row.organization_id != organization_id:
                row.tenant_id = tenant_id
                row.organization_id = organization_id
                _set_artifact_times(row, update_time=now)
                changed = True
            rows.append(row)
            continue
        if row is None:
            row = ReportArtifact(
                artifact_uid=uuid.uuid4().hex,
                source_key=source_key,
                report_type=source_type,
                source_type=source_type,
                source_id=source_id,
                artifact_kind="source_index",
                title=str(source.get("title") or source_key)[:500],
                created_by=actor,
                retention_until=now + timedelta(days=max(1, retention_days)),
            )
            _set_artifact_times(row, create_time=now, update_time=now, retention_until=row.retention_until)
            db.add(row)
            existing[source_key] = row
        row.title = str(source.get("title") or source_key)[:500]
        row.summary = str(source.get("summary") or "") or None
        row.instance_id = instance_id
        row.instance_name = source.get("instance_name")
        row.db_type = source.get("db_type")
        row.status = str(source.get("status") or "generated")[:30]
        row.risk_level = source.get("risk_level")
        row.score = source.get("score")
        row.format_manifest = ["markdown", "html", "json"] + (["word"] if source_type == "inspection" else [])
        row.source_export_url = source.get("export_url")
        row.content_json = payload
        row.content_markdown = _source_markdown(payload, watermark)
        row.content_html = _source_html(payload, watermark)
        row.evidence_manifest = _source_evidence_manifest(source)
        row.tenant_id = tenant_id
        row.organization_id = organization_id
        row.extra_json = {**(row.extra_json or {}), "source_create_time": payload.get("create_time")}
        sign_artifact(row, now=now)
        changed = True
        rows.append(row)
    if changed:
        if not commit:
            db.flush()
            return rows
        db.commit()
        # Commit expires ORM rows by default. Refresh in batches, including
        # unchanged rows, so serialization never triggers per-row SELECTs.
        for offset in range(0, len(source_keys), batch_size):
            db.query(ReportArtifact).filter(
                ReportArtifact.source_key.in_(source_keys[offset:offset + batch_size])
            ).populate_existing().all()
    return rows


def _source_evidence_manifest(source: dict) -> list[dict]:
    manifest = []
    if source.get("export_url"):
        manifest.append({
            "key": "source_export",
            "name": "来源报告",
            "type": "source_link",
            "uri": source["export_url"],
        })
    return manifest


def create_snapshot_artifact(
    db: Session,
    items: list[dict],
    *,
    source_key: str,
    title: str,
    report_type: str = "all",
    actor: str = "system",
    watermark: str | None = None,
    retention_days: int = 365,
    tenant_id: int | None = None,
    organization_id: int | None = None,
    generated_at: datetime | None = None,
) -> ReportArtifact:
    existing = db.query(ReportArtifact).filter(ReportArtifact.source_key == source_key).first()
    if existing:
        return existing
    items = _json_safe(items)
    now = _utc_naive(generated_at or utcnow())
    payload = {
        "schema_version": "1.0",
        "generated_at": _iso(now.replace(tzinfo=timezone.utc)),
        "report_type": report_type,
        "title": title,
        "count": len(items),
        "items": items,
        "watermark": watermark,
    }
    scope_instance_ids = sorted({
        int(item["instance_id"])
        for item in items
        if item.get("instance_id") is not None and str(item.get("instance_id")).isdigit()
    })
    artifact = ReportArtifact(
        artifact_uid=uuid.uuid4().hex,
        source_key=source_key,
        report_type=report_type,
        source_type="scheduled_snapshot" if source_key.startswith("schedule:") else "manual_snapshot",
        source_id=source_key.rsplit(":", 1)[-1],
        artifact_kind="snapshot",
        title=title[:500],
        summary=f"统一归档 {len(items)} 份报告",
        status="generated",
        format_manifest=["markdown", "html", "json", "word"],
        content_markdown=_snapshot_markdown(title, items, now, watermark),
        content_html=_snapshot_html(title, items, now, watermark),
        content_json=payload,
        evidence_manifest=[{"key": str(item.get("id")), "name": item.get("title"), "type": "catalog_item"} for item in items],
        watermark=watermark,
        retention_until=now + timedelta(days=max(1, retention_days)),
        tenant_id=tenant_id,
        organization_id=organization_id,
        created_by=actor,
        extra_json={"scope_instance_ids": scope_instance_ids},
    )
    _set_artifact_times(artifact, create_time=now, update_time=now, retention_until=artifact.retention_until)
    sign_artifact(artifact, now=now)
    db.add(artifact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(ReportArtifact).filter(ReportArtifact.source_key == source_key).first()
        if existing:
            return existing
        raise
    db.refresh(artifact)
    return artifact


def create_document_artifact(
    db: Session,
    *,
    source_key: str,
    title: str,
    report_type: str,
    content_markdown: str,
    content_html: str,
    content_json: dict,
    evidence_manifest: list[dict],
    instance_id: int | None,
    instance_name: str | None,
    db_type: str | None,
    summary: str | None,
    risk_level: str | None,
    actor: str = "system",
    retention_days: int = 365,
    generated_at: datetime | None = None,
    commit: bool = True,
) -> ReportArtifact:
    """Persist one generated report document (markdown/html/json) with its evidence and signature."""
    existing = db.query(ReportArtifact).filter(ReportArtifact.source_key == source_key).first()
    if existing:
        return existing
    now = _utc_naive(generated_at or utcnow())
    artifact = ReportArtifact(
        artifact_uid=uuid.uuid4().hex,
        source_key=source_key,
        report_type=report_type,
        source_type="report_document",
        source_id=source_key.rsplit(":", 1)[-1],
        artifact_kind="document",
        title=title[:500],
        summary=summary,
        instance_id=instance_id,
        instance_name=instance_name,
        db_type=db_type,
        status="generated",
        risk_level=risk_level,
        format_manifest=["markdown", "html", "json"],
        content_markdown=content_markdown,
        content_html=content_html,
        content_json=content_json,
        evidence_manifest=evidence_manifest,
        retention_until=now + timedelta(days=max(1, retention_days)),
        created_by=actor,
        extra_json={"generated_by": content_json.get("analysis_source"), "schema": content_json.get("schema")},
    )
    _set_artifact_times(artifact, create_time=now, update_time=now, retention_until=artifact.retention_until)
    sign_artifact(artifact, now=now)
    db.add(artifact)
    if not commit:
        db.flush()
        return artifact
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(ReportArtifact).filter(ReportArtifact.source_key == source_key).first()
        if existing:
            return existing
        raise
    db.refresh(artifact)
    return artifact


def list_artifacts(
    db: Session,
    *,
    report_type: str | None = None,
    keyword: str | None = None,
    instance_id: int | None = None,
    archive_state: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    allowed_instance_ids: set[int] | frozenset[int] | None = None,
    limit: int = 200,
    metadata_only: bool = False,
) -> list[ReportArtifact]:
    query = db.query(ReportArtifact)
    if metadata_only:
        query = query.options(
            defer(ReportArtifact.content_markdown),
            defer(ReportArtifact.content_html),
            defer(ReportArtifact.content_json),
        )
    if report_type and report_type != "all":
        query = query.filter(ReportArtifact.report_type == report_type)
    if keyword:
        escaped = keyword.replace("%", "\\%").replace("_", "\\_")
        query = query.filter(or_(ReportArtifact.title.ilike(f"%{escaped}%", escape="\\"), ReportArtifact.summary.ilike(f"%{escaped}%", escape="\\")))
    if instance_id is not None:
        query = query.filter(ReportArtifact.instance_id == instance_id)
    if archive_state and archive_state != "all":
        query = query.filter(ReportArtifact.archive_state == archive_state)
    def boundaries(value):
        if value is None:
            return None, None
        local = value.astimezone(CATALOG_TIMEZONE) if value.tzinfo else value.replace(tzinfo=CATALOG_TIMEZONE)
        return local.replace(tzinfo=None), _utc_naive(local)

    from_wall, from_utc = boundaries(date_from)
    to_wall, to_utc = boundaries(date_to)
    # Coarse indexed bounds include both stored clock conventions; exact matching
    # below needs no dialect-specific JSON operators and never shifts old rows.
    if date_from is not None:
        query = query.filter(ReportArtifact.create_time >= min(from_wall, from_utc))
    if date_to is not None:
        query = query.filter(ReportArtifact.create_time <= max(to_wall, to_utc))
    requested_limit = max(1, min(limit, 1000))
    query = query.order_by(ReportArtifact.id.desc())
    if allowed_instance_ids is None and date_from is None and date_to is None:
        return query.limit(requested_limit).all()
    if allowed_instance_ids is not None and not allowed_instance_ids:
        return []
    # Multi-instance snapshots have no single instance_id. Keep their explicit
    # scope in extra_json and enforce it in Python for SQLite/MySQL/PostgreSQL
    # portability instead of relying on vendor-specific JSON operators.
    if allowed_instance_ids is not None:
        query = query.filter(or_(
            ReportArtifact.instance_id.in_(allowed_instance_ids),
            ReportArtifact.instance_id.is_(None),
        ))
    rows = []
    result = db.scalars(query.statement, execution_options={"yield_per": 200})
    try:
        for row in result:
            if allowed_instance_ids is not None and not _artifact_scope_allowed(row, allowed_instance_ids):
                continue
            known_utc = "create_time" in ((row.extra_json or {}).get("application_utc_fields") or [])
            lower, upper = (from_utc, to_utc) if known_utc else (from_wall, to_wall)
            if lower is not None and row.create_time < lower:
                continue
            if upper is not None and row.create_time > upper:
                continue
            rows.append(row)
            if len(rows) == requested_limit:
                break
    finally:
        # Drain/close MySQL's unbuffered cursor before this Session is reused.
        result.close()
    return rows


def artifact_instance_scope(row: ReportArtifact) -> set[int]:
    if row.instance_id is not None:
        return {int(row.instance_id)}
    values = (row.extra_json or {}).get("scope_instance_ids") or []
    return {int(value) for value in values if str(value).isdigit()}


def _artifact_scope_allowed(row: ReportArtifact, allowed_instance_ids: set[int] | frozenset[int]) -> bool:
    scope = artifact_instance_scope(row)
    return bool(scope) and scope.issubset(allowed_instance_ids)


def serialize_artifact(row: ReportArtifact, *, include_content: bool = False) -> dict:
    result = {
        "id": row.id,
        "artifact_uid": row.artifact_uid,
        "source_key": row.source_key,
        "report_type": row.report_type,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "artifact_kind": row.artifact_kind,
        "title": row.title,
        "summary": row.summary,
        "instance_id": row.instance_id,
        "instance_name": row.instance_name,
        "db_type": row.db_type,
        "status": row.status,
        "risk_level": row.risk_level,
        "score": row.score,
        "formats": row.format_manifest or [],
        "source_export_url": row.source_export_url,
        "evidence_manifest": row.evidence_manifest or [],
        "content_sha256": row.content_sha256,
        "signature_algorithm": row.signature_algorithm,
        "signing_key_id": row.signing_key_id,
        "signed_at": _artifact_iso(row, "signed_at"),
        "watermark": row.watermark,
        "archive_state": row.archive_state,
        "archive_uri": row.archive_uri,
        "archived_at": _artifact_iso(row, "archived_at"),
        "retention_until": _artifact_iso(row, "retention_until"),
        "legal_hold": bool(row.legal_hold),
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
        "scope_instance_ids": sorted(artifact_instance_scope(row)),
        "created_by": row.created_by,
        "create_time": _artifact_iso(row, "create_time"),
        "update_time": _artifact_iso(row, "update_time"),
    }
    if include_content:
        result.update({
            "content_markdown": row.content_markdown,
            "content_html": row.content_html,
            "content_json": row.content_json,
        })
    return result


def artifact_stats(rows: Iterable[ReportArtifact]) -> dict:
    values = list(rows)
    by_type: dict[str, int] = {}
    by_state: dict[str, int] = {}
    signed = 0
    for row in values:
        by_type[row.report_type] = by_type.get(row.report_type, 0) + 1
        by_state[row.archive_state] = by_state.get(row.archive_state, 0) + 1
        signed += int(bool(row.signature))
    return {"total": len(values), "signed": signed, "by_type": by_type, "by_archive_state": by_state}


def archive_artifact(db: Session, artifact: ReportArtifact, *, actor: str, archive_uri: str | None = None, reason: str | None = None) -> ReportArtifact:
    now = utcnow()
    artifact.archive_state = "archived"
    artifact.archived_at = now
    artifact.archive_uri = archive_uri or f"database://report-artifact/{artifact.artifact_uid}"
    extra = dict(artifact.extra_json or {})
    extra["archive_reason"] = reason
    extra["archived_by"] = actor
    artifact.extra_json = extra
    _set_artifact_times(artifact, archived_at=now, update_time=now)
    append_security_audit(
        db,
        category="report",
        action="archive",
        actor=actor,
        target_type="report_artifact",
        target_id=artifact.id,
        details={"reason": reason, "archive_uri": artifact.archive_uri},
    )
    db.commit()
    db.refresh(artifact)
    return artifact


def set_legal_hold(db: Session, artifact: ReportArtifact, *, enabled: bool, actor: str, reason: str | None = None) -> ReportArtifact:
    artifact.legal_hold = int(enabled)
    extra = dict(artifact.extra_json or {})
    extra["legal_hold_reason"] = reason
    extra["legal_hold_by"] = actor
    artifact.extra_json = extra
    _set_artifact_times(artifact, update_time=utcnow())
    append_security_audit(
        db,
        category="report",
        action="legal_hold_enabled" if enabled else "legal_hold_disabled",
        actor=actor,
        target_type="report_artifact",
        target_id=artifact.id,
        details={"reason": reason},
    )
    db.commit()
    db.refresh(artifact)
    return artifact


def apply_retention(db: Session, *, now: datetime | None = None) -> int:
    current = now or utcnow()
    rows = db.query(ReportArtifact).filter(
        ReportArtifact.archive_state == "active",
        ReportArtifact.legal_hold == 0,
        ReportArtifact.retention_until.isnot(None),
        ReportArtifact.retention_until <= current,
    ).all()
    for row in rows:
        row.archive_state = "archived"
        row.archived_at = current
        row.archive_uri = row.archive_uri or f"database://report-artifact/{row.artifact_uid}"
        _set_artifact_times(row, archived_at=current, update_time=current)
    db.commit()
    return len(rows)


def add_attachment(db: Session, artifact: ReportArtifact, values: dict) -> ReportArtifactAttachment:
    key = str(values.get("attachment_key") or uuid.uuid4().hex)[:160]
    row = db.query(ReportArtifactAttachment).filter(
        ReportArtifactAttachment.artifact_id == artifact.id,
        ReportArtifactAttachment.attachment_key == key,
    ).first()
    if row is None:
        row = ReportArtifactAttachment(artifact_id=artifact.id, attachment_key=key, file_name=str(values["file_name"])[:500])
        db.add(row)
    for field in ("media_type", "storage_uri", "content_sha256", "size_bytes", "classification", "extra_json"):
        if field in values:
            setattr(row, field, values[field])
    db.commit()
    db.refresh(row)
    return row


def serialize_attachment(row: ReportArtifactAttachment) -> dict:
    return {
        "id": row.id,
        "artifact_id": row.artifact_id,
        "attachment_key": row.attachment_key,
        "file_name": row.file_name,
        "media_type": row.media_type,
        "storage_uri": row.storage_uri,
        "content_sha256": row.content_sha256,
        "size_bytes": row.size_bytes,
        "classification": row.classification,
        "extra": row.extra_json or {},
        "create_time": _iso(row.create_time),
    }


def validate_schedule_values(values: dict) -> dict:
    result = dict(values)
    schedule_type = str(result.get("schedule_type") or "daily").lower()
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError(f"unsupported schedule_type: {schedule_type}")
    result["schedule_type"] = schedule_type
    report_type = str(result.get("report_type") or "all").lower()
    if report_type not in REPORT_TYPES:
        raise ValueError(f"unsupported report_type: {report_type}")
    result["report_type"] = report_type
    formats = [str(value).lower() for value in (result.get("output_formats") or ["markdown", "html", "json"])]
    unknown_formats = set(formats) - REPORT_FORMATS
    if unknown_formats:
        raise ValueError(f"unsupported output format: {', '.join(sorted(unknown_formats))}")
    result["output_formats"] = list(dict.fromkeys(formats))
    misfire = str(result.get("misfire_policy") or "fire_once").lower()
    if misfire not in MISFIRE_POLICIES:
        raise ValueError(f"unsupported misfire_policy: {misfire}")
    result["misfire_policy"] = misfire
    tz_name = str(result.get("timezone") or "Asia/Shanghai")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz_name}") from exc
    result["timezone"] = tz_name
    result["run_time"] = str(result.get("run_time") or "08:00")
    _parse_run_time(result["run_time"])
    if schedule_type == "interval" and int(result.get("interval_minutes") or 0) < 1:
        raise ValueError("interval_minutes must be greater than zero")
    if schedule_type == "cron":
        _cron_trigger(str(result.get("cron_expression") or ""), tz_name)
    weekdays = [int(value) for value in (result.get("weekdays") or [1, 2, 3, 4, 5])]
    if any(value < 1 or value > 7 for value in weekdays):
        raise ValueError("weekdays must use ISO values 1 through 7")
    result["weekdays"] = sorted(set(weekdays))
    day = int(result.get("day_of_month") or 1)
    if day < 1 or day > 31:
        raise ValueError("day_of_month must be between 1 and 31")
    result["day_of_month"] = day
    result["enabled"] = int(result.get("enabled", 1))
    result["filter_json"] = dict(result.get("filter_json") or {})
    result["retention_days"] = max(1, int(result.get("retention_days") or 365))
    result["misfire_grace_seconds"] = max(0, int(result.get("misfire_grace_seconds") or 0))
    result["max_catchup_runs"] = max(1, min(10, int(result.get("max_catchup_runs") or 1)))
    return result


def _parse_run_time(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (ValueError, TypeError) as exc:
        raise ValueError("run_time must use HH:MM") from exc


def _cron_trigger(expression: str, tz_name: str):
    if len(expression.split()) != 5:
        raise ValueError("cron_expression must contain five fields")
    try:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(expression, timezone=ZoneInfo(tz_name))
    except Exception as exc:
        raise ValueError(f"invalid cron_expression: {exc}") from exc


def calculate_next_run(schedule: ReportSchedule, *, after: datetime | None = None) -> datetime | None:
    if schedule.schedule_type == "manual" or not schedule.enabled:
        return None
    after_utc = (after or utcnow()).replace(tzinfo=timezone.utc)
    tz = ZoneInfo(schedule.timezone or "Asia/Shanghai")
    local_after = after_utc.astimezone(tz)
    if schedule.schedule_type == "interval":
        return (after_utc + timedelta(minutes=max(1, schedule.interval_minutes or 1))).replace(tzinfo=None)
    if schedule.schedule_type == "cron":
        next_value = _cron_trigger(schedule.cron_expression or "", schedule.timezone).get_next_fire_time(None, local_after)
        return next_value.astimezone(timezone.utc).replace(tzinfo=None) if next_value else None

    run_at = _parse_run_time(schedule.run_time or "08:00")
    weekdays = set(schedule.weekdays or [1, 2, 3, 4, 5])

    def candidate_for(day_value):
        return datetime.combine(day_value, run_at, tzinfo=tz)

    if schedule.schedule_type in {"daily", "workday", "weekly"}:
        for offset in range(0, 370):
            day_value = local_after.date() + timedelta(days=offset)
            candidate = candidate_for(day_value)
            if candidate <= local_after:
                continue
            if schedule.schedule_type in {"workday", "weekly"} and day_value.isoweekday() not in weekdays:
                continue
            return candidate.astimezone(timezone.utc).replace(tzinfo=None)

    month_cursor = local_after.year * 12 + (local_after.month - 1)
    allowed_months = {1, 4, 7, 10} if schedule.schedule_type == "quarterly" else set(range(1, 13))
    for month_offset in range(0, 48):
        value = month_cursor + month_offset
        year, month_zero = divmod(value, 12)
        month = month_zero + 1
        if month not in allowed_months:
            continue
        day = min(max(1, schedule.day_of_month or 1), calendar.monthrange(year, month)[1])
        candidate = datetime.combine(datetime(year, month, day).date(), run_at, tzinfo=tz)
        if candidate > local_after:
            return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError("unable to calculate next schedule occurrence")


def create_schedule(db: Session, values: dict, *, actor: str) -> ReportSchedule:
    payload = validate_schedule_values(values)
    row = ReportSchedule(schedule_uid=uuid.uuid4().hex, created_by=actor)
    for field, value in payload.items():
        if hasattr(row, field):
            setattr(row, field, value)
    row.next_run_at = calculate_next_run(row)
    db.add(row)
    append_security_audit(db, category="report", action="schedule_create", actor=actor, target_type="report_schedule", target_id=row.schedule_uid, details=payload)
    db.commit()
    db.refresh(row)
    return row


def update_schedule(db: Session, row: ReportSchedule, values: dict, *, actor: str) -> ReportSchedule:
    merged = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    merged.update(values)
    payload = validate_schedule_values(merged)
    for field in (
        "name", "report_type", "output_formats", "filter_json", "schedule_type", "interval_minutes",
        "cron_expression", "run_time", "weekdays", "day_of_month", "timezone", "misfire_policy",
        "misfire_grace_seconds", "max_catchup_runs", "enabled", "watermark", "retention_days",
        "tenant_id", "organization_id",
    ):
        if field in payload:
            setattr(row, field, payload[field])
    row.next_run_at = calculate_next_run(row)
    append_security_audit(db, category="report", action="schedule_update", actor=actor, target_type="report_schedule", target_id=row.id, details=values)
    db.commit()
    db.refresh(row)
    return row


def serialize_schedule(row: ReportSchedule) -> dict:
    return {
        "id": row.id,
        "schedule_uid": row.schedule_uid,
        "name": row.name,
        "report_type": row.report_type,
        "output_formats": row.output_formats or [],
        "filter": row.filter_json or {},
        "schedule_type": row.schedule_type,
        "interval_minutes": row.interval_minutes,
        "cron_expression": row.cron_expression,
        "run_time": row.run_time,
        "weekdays": row.weekdays or [],
        "day_of_month": row.day_of_month,
        "timezone": row.timezone,
        "misfire_policy": row.misfire_policy,
        "misfire_grace_seconds": row.misfire_grace_seconds,
        "max_catchup_runs": row.max_catchup_runs,
        "enabled": bool(row.enabled),
        "watermark": row.watermark,
        "retention_days": row.retention_days,
        "next_run_at": _iso(row.next_run_at),
        "last_planned_at": _iso(row.last_planned_at),
        "last_run_at": _iso(row.last_run_at),
        "last_status": row.last_status,
        "last_message": row.last_message,
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
        "created_by": row.created_by,
        "create_time": _iso(row.create_time),
        "update_time": _iso(row.update_time),
    }


def serialize_run(row: ReportScheduleRun) -> dict:
    return {
        "id": row.id,
        "run_uid": row.run_uid,
        "schedule_id": row.schedule_id,
        "idempotency_key": row.idempotency_key,
        "trigger_type": row.trigger_type,
        "planned_at": _iso(row.planned_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "status": row.status,
        "attempt_count": row.attempt_count,
        "artifact_id": row.artifact_id,
        "worker_id": row.worker_id,
        "message": row.message,
        "result": row.result_json or {},
        "create_time": _iso(row.create_time),
    }


def _default_source_loader(db: Session, report_type: str, limit: int) -> list[dict]:
    # The source adapter remains in the legacy report module so existing report
    # payloads and export links stay byte-for-byte compatible during migration.
    from app.api.v1.report import _collect_unified_reports

    return _collect_unified_reports(db, report_type, limit)


def execute_schedule(
    db: Session,
    schedule: ReportSchedule,
    *,
    planned_at: datetime | None = None,
    trigger_type: str = "manual",
    actor: str = "system",
    worker_id: str | None = None,
    source_loader: Callable[[Session, str, int], list[dict]] | None = None,
) -> ReportScheduleRun:
    with _execution_guard("report", schedule.id) as acquired:
        if not acquired:
            planned = planned_at or utcnow()
            key = f"report-schedule:{schedule.id}:{planned.isoformat(timespec='seconds')}"
            existing = db.query(ReportScheduleRun).filter_by(idempotency_key=key).first()
            if existing:
                return existing
            raise ValueError("Report schedule already has an active execution")
        return _execute_schedule(
            db, schedule, planned_at=planned_at, trigger_type=trigger_type,
            actor=actor, worker_id=worker_id, source_loader=source_loader,
        )


def _execute_schedule(
    db: Session,
    schedule: ReportSchedule,
    *,
    planned_at: datetime | None = None,
    trigger_type: str = "manual",
    actor: str = "system",
    worker_id: str | None = None,
    source_loader: Callable[[Session, str, int], list[dict]] | None = None,
) -> ReportScheduleRun:
    planned = planned_at or utcnow()
    idempotency_key = f"report-schedule:{schedule.id}:{planned.isoformat(timespec='seconds')}"
    existing = db.query(ReportScheduleRun).filter(ReportScheduleRun.idempotency_key == idempotency_key).first()
    if existing:
        return existing
    run = ReportScheduleRun(
        run_uid=uuid.uuid4().hex,
        schedule_id=schedule.id,
        idempotency_key=idempotency_key,
        trigger_type=trigger_type,
        planned_at=planned,
        started_at=utcnow(),
        status="running",
        attempt_count=1,
        worker_id=worker_id or worker_identity(),
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(ReportScheduleRun).filter(ReportScheduleRun.idempotency_key == idempotency_key).first()
        if existing:
            return existing
        raise
    db.refresh(run)
    run_id, started_at, owner = run.id, run.started_at, run.worker_id
    artifact_id = None

    def owned_run():
        return db.query(ReportScheduleRun).filter(
            ReportScheduleRun.id == run_id, ReportScheduleRun.status == "running",
            ReportScheduleRun.started_at == started_at, ReportScheduleRun.worker_id == owner,
        )

    try:
        loader = source_loader or _default_source_loader
        limit = int((schedule.filter_json or {}).get("limit") or 500)
        items = loader(db, schedule.report_type or "all", max(1, min(limit, 1000)))
        instance_ids = {int(value) for value in (schedule.filter_json or {}).get("instance_ids", []) if str(value).isdigit()}
        if instance_ids:
            items = [item for item in items if item.get("instance_id") in instance_ids]
        if not owned_run().update({ReportScheduleRun.status: "running"}, synchronize_session=False):
            db.rollback()
            db.refresh(run)
            return run
        source_key = f"schedule:{schedule.schedule_uid}:{planned.isoformat(timespec='seconds')}"
        artifact = create_snapshot_artifact(
            db,
            items,
            source_key=source_key,
            title=f"{schedule.name} · {_iso(planned)}",
            report_type=schedule.report_type,
            actor=actor,
            watermark=schedule.watermark,
            retention_days=schedule.retention_days,
            tenant_id=schedule.tenant_id,
            organization_id=schedule.organization_id,
            generated_at=utcnow(),
        )
        artifact_id = artifact.id
        status = "success"
        message = f"generated {len(items)} report items"
        result_json = {"artifact_uid": artifact.artifact_uid, "item_count": len(items), "formats": schedule.output_formats}
        # Subscription authorization resolves the schedule through this link.
        # Keep the run unfinished until delivery dispatch returns.
        if not owned_run().update({ReportScheduleRun.artifact_id: artifact_id}, synchronize_session=False):
            db.rollback()
            db.refresh(run)
            return run
        _dispatch_subscriptions(db, schedule, artifact)
    except Exception as exc:
        db.rollback()
        status = "failed"
        message = str(exc)[:2000]
        result_json = {"error_type": exc.__class__.__name__}
    finished_at = utcnow()
    changed = owned_run().update({
        ReportScheduleRun.status: status, ReportScheduleRun.message: message,
        ReportScheduleRun.result_json: result_json, ReportScheduleRun.artifact_id: artifact_id,
        ReportScheduleRun.finished_at: finished_at,
    }, synchronize_session=False)
    if not changed:
        db.rollback()
        db.refresh(run)
        return run
    schedule = db.query(ReportSchedule).filter(ReportSchedule.id == schedule.id).first()
    schedule.last_planned_at = planned
    schedule.last_run_at = finished_at
    schedule.last_status = status
    schedule.last_message = message
    db.commit()
    db.refresh(run)
    return run


def _dispatch_subscriptions(db: Session, schedule: ReportSchedule, artifact: ReportArtifact) -> None:
    subscriptions = db.query(ReportSubscription).filter(
        ReportSubscription.enabled == 1,
        or_(ReportSubscription.schedule_id == schedule.id, ReportSubscription.schedule_id.is_(None)),
    ).all()
    for subscription in subscriptions:
        if subscription.report_types and schedule.report_type not in subscription.report_types and "all" not in subscription.report_types:
            continue
        try:
            from app.notify.service import dispatch_report_subscription

            dispatch_report_subscription(db, subscription, schedule, artifact)
        except Exception as exc:
            db.rollback()
            extra = dict(artifact.extra_json or {})
            errors = list(extra.get("subscription_errors") or [])
            errors.append({"subscription_id": subscription.id, "error": str(exc)[:500]})
            extra["subscription_errors"] = errors[-20:]
            artifact.extra_json = extra
            _set_artifact_times(artifact, update_time=utcnow())
            db.commit()


def run_due_report_schedules(
    db: Session,
    *,
    now: datetime | None = None,
    worker_id: str | None = None,
    source_loader: Callable[[Session, str, int], list[dict]] | None = None,
) -> list[ReportScheduleRun]:
    current = now or utcnow()
    recovered = _recover_stale_report_runs(db, current, worker_id)
    schedules = db.query(ReportSchedule).filter(
        ReportSchedule.enabled == 1,
        ReportSchedule.next_run_at.isnot(None),
        ReportSchedule.next_run_at <= current,
    ).order_by(ReportSchedule.next_run_at.asc(), ReportSchedule.id.asc()).all()
    runs: list[ReportScheduleRun] = list(recovered)
    for schedule in schedules:
        planned = schedule.next_run_at or current
        run_limit = schedule.max_catchup_runs if schedule.misfire_policy == "catch_up" else 1
        completed = 0
        while planned and planned <= current and completed < max(1, run_limit or 1):
            delay = max(0.0, (current - planned).total_seconds())
            if schedule.misfire_policy == "skip" and delay > schedule.misfire_grace_seconds:
                idempotency_key = f"report-schedule:{schedule.id}:{planned.isoformat(timespec='seconds')}"
                run = db.query(ReportScheduleRun).filter(ReportScheduleRun.idempotency_key == idempotency_key).first()
                if run is None:
                    run = ReportScheduleRun(
                        run_uid=uuid.uuid4().hex,
                        schedule_id=schedule.id,
                        idempotency_key=idempotency_key,
                        trigger_type="misfire",
                        planned_at=planned,
                        started_at=current,
                        finished_at=current,
                        status="skipped",
                        worker_id=worker_id or worker_identity(),
                        message=f"misfire delay {int(delay)}s exceeded grace {schedule.misfire_grace_seconds}s",
                    )
                    db.add(run)
                    schedule.last_planned_at = planned
                    schedule.last_run_at = current
                    schedule.last_status = "skipped"
                    schedule.last_message = run.message
                    try:
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        run = db.query(ReportScheduleRun).filter_by(idempotency_key=idempotency_key).one()
                    db.refresh(run)
            else:
                run = execute_schedule(
                    db,
                    schedule,
                    planned_at=planned,
                    trigger_type="scheduled" if delay <= schedule.misfire_grace_seconds else "misfire_compensation",
                    actor="scheduler",
                    worker_id=worker_id,
                    source_loader=source_loader,
                )
            runs.append(run)
            # An idempotency hit is not completion. In particular, skip-misfire
            # must not hide a previously claimed but unfinished execution.
            if run.status not in {"success", "failed", "skipped"}:
                break
            completed += 1
            schedule = db.query(ReportSchedule).filter(ReportSchedule.id == schedule.id).first()
            base = planned if schedule.misfire_policy == "catch_up" else current
            next_run = calculate_next_run(schedule, after=base)
            advanced = db.query(ReportSchedule).filter(
                ReportSchedule.id == schedule.id,
                ReportSchedule.enabled == 1,
                ReportSchedule.next_run_at == planned,
            ).update({ReportSchedule.next_run_at: next_run}, synchronize_session=False)
            db.commit()
            db.refresh(schedule)
            if not advanced:
                break
            planned = schedule.next_run_at
            if schedule.misfire_policy != "catch_up":
                break
    apply_retention(db, now=current)
    return runs


def _recover_stale_report_runs(
    db: Session, current: datetime, owner_id: str | None,
) -> list[ReportScheduleRun]:
    from sqlalchemy import func

    cutoff = current - STALE_RUN_AFTER
    with _EXECUTION_LOCK:
        active_ids = [identity for kind, identity in _ACTIVE_EXECUTIONS if kind == "report"]
    current_owner = owner_id or worker_identity()
    live_owner = db.query(SchedulerLease.id).filter(
        SchedulerLease.owner_id == ReportScheduleRun.worker_id,
        SchedulerLease.owner_id != current_owner,
        SchedulerLease.lease_until > utcnow(),
    ).exists()
    candidates = db.query(ReportScheduleRun).filter(
        ReportScheduleRun.status == "running",
        func.coalesce(ReportScheduleRun.started_at, ReportScheduleRun.create_time) <= cutoff,
        ReportScheduleRun.schedule_id.notin_(active_ids),
        ~live_owner,
    ).order_by(ReportScheduleRun.id).limit(100).all()
    recovered = []
    for run in candidates:
        with _execution_guard("report", run.schedule_id) as acquired:
            if not acquired or not _scheduler_recovery_allowed(db, owner_id):
                continue
            if run.worker_id and run.worker_id != current_owner and db.query(SchedulerLease).filter(
                SchedulerLease.owner_id == run.worker_id,
                SchedulerLease.lease_until > utcnow(),
            ).first():
                continue
            message = "Execution interrupted; outcome unknown, pending reconciliation. Automatic replay disabled."
            changed = db.query(ReportScheduleRun).filter(
                ReportScheduleRun.id == run.id,
                ReportScheduleRun.status == "running",
                ReportScheduleRun.started_at == run.started_at,
                ReportScheduleRun.worker_id == run.worker_id,
                func.coalesce(ReportScheduleRun.started_at, ReportScheduleRun.create_time) <= cutoff,
            ).update({
                ReportScheduleRun.status: "interrupted",
                ReportScheduleRun.finished_at: current,
                ReportScheduleRun.message: message,
                ReportScheduleRun.result_json: {
                    **(run.result_json or {}), "outcome": "unknown",
                    "reconciliation_required": True, "automatic_replay": False,
                },
            }, synchronize_session=False)
            if changed:
                db.query(ReportSchedule).filter(ReportSchedule.id == run.schedule_id).update({
                    ReportSchedule.enabled: 0,
                    ReportSchedule.last_status: "interrupted",
                    ReportSchedule.last_message: message,
                }, synchronize_session=False)
            db.commit()
            db.refresh(run)
            if changed:
                recovered.append(run)
    return recovered


def create_subscription(db: Session, values: dict, *, actor: str) -> ReportSubscription:
    row = ReportSubscription(
        subscription_uid=uuid.uuid4().hex,
        name=str(values["name"])[:255],
        schedule_id=values.get("schedule_id"),
        report_types=values.get("report_types") or [],
        channels=values.get("channels") or [],
        recipients=values.get("recipients") or [],
        filter_json=values.get("filter_json") or {},
        enabled=int(values.get("enabled", 1)),
        tenant_id=values.get("tenant_id"),
        organization_id=values.get("organization_id"),
        created_by=actor,
    )
    db.add(row)
    append_security_audit(db, category="report", action="subscription_create", actor=actor, target_type="report_subscription", target_id=row.subscription_uid, details=values)
    db.commit()
    db.refresh(row)
    return row


def update_subscription(db: Session, row: ReportSubscription, values: dict, *, actor: str) -> ReportSubscription:
    for field in ("name", "schedule_id", "report_types", "channels", "recipients", "filter_json", "enabled", "tenant_id", "organization_id"):
        if field in values:
            setattr(row, field, values[field])
    append_security_audit(db, category="report", action="subscription_update", actor=actor, target_type="report_subscription", target_id=row.id, details=values)
    db.commit()
    db.refresh(row)
    return row


def serialize_subscription(row: ReportSubscription) -> dict:
    return {
        "id": row.id,
        "subscription_uid": row.subscription_uid,
        "name": row.name,
        "schedule_id": row.schedule_id,
        "report_types": row.report_types or [],
        "channels": row.channels or [],
        "recipients": row.recipients or [],
        "filter": row.filter_json or {},
        "enabled": bool(row.enabled),
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
        "created_by": row.created_by,
        "create_time": _iso(row.create_time),
        "update_time": _iso(row.update_time),
    }


def claim_scheduler_lease(
    db: Session,
    lease_key: str,
    owner_id: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> bool:
    return claim_scheduler_lease_token(
        db,
        lease_key,
        owner_id,
        now=now,
        ttl_seconds=ttl_seconds,
    ) is not None


def claim_scheduler_lease_token(
    db: Session,
    lease_key: str,
    owner_id: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> int | None:
    """Atomically claim a lease and return its fencing epoch.

    Every successful dispatch receives a new epoch.  Workers must present that
    epoch when renewing or validating the lease, so an expired worker cannot
    become current again after another node has taken over.
    """
    current = now or utcnow()
    until = current + timedelta(seconds=max(30, ttl_seconds))
    row = db.query(SchedulerLease).filter(SchedulerLease.lease_key == lease_key).first()
    if row is None:
        db.add(SchedulerLease(lease_key=lease_key, owner_id=None, lease_until=None, epoch=0))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    updated = db.query(SchedulerLease).filter(
        SchedulerLease.lease_key == lease_key,
        or_(SchedulerLease.owner_id == owner_id, SchedulerLease.lease_until.is_(None), SchedulerLease.lease_until <= current),
    ).update(
        {
            SchedulerLease.owner_id: owner_id,
            SchedulerLease.lease_until: until,
            SchedulerLease.heartbeat_at: current,
            SchedulerLease.epoch: SchedulerLease.epoch + 1,
        },
        synchronize_session=False,
    )
    db.commit()
    if updated != 1:
        return None
    epoch = db.query(SchedulerLease.epoch).filter(
        SchedulerLease.lease_key == lease_key,
        SchedulerLease.owner_id == owner_id,
        SchedulerLease.lease_until > current,
    ).scalar()
    return int(epoch) if epoch is not None else None


def heartbeat_scheduler_lease(
    db: Session,
    lease_key: str,
    owner_id: str,
    *,
    expected_epoch: int | None = None,
    ttl_seconds: int = 600,
) -> bool:
    current = utcnow()
    query = db.query(SchedulerLease).filter(
        SchedulerLease.lease_key == lease_key,
        SchedulerLease.owner_id == owner_id,
        SchedulerLease.lease_until > current,
    )
    if expected_epoch is not None:
        query = query.filter(SchedulerLease.epoch == expected_epoch)
    updated = query.update(
        {SchedulerLease.heartbeat_at: current, SchedulerLease.lease_until: current + timedelta(seconds=max(30, ttl_seconds))},
        synchronize_session=False,
    )
    db.commit()
    return updated == 1


def validate_scheduler_lease(
    db: Session,
    lease_key: str,
    owner_id: str,
    expected_epoch: int,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or utcnow()
    return db.query(SchedulerLease.id).filter(
        SchedulerLease.lease_key == lease_key,
        SchedulerLease.owner_id == owner_id,
        SchedulerLease.epoch == expected_epoch,
        SchedulerLease.lease_until > current,
    ).first() is not None


def release_scheduler_lease(
    db: Session,
    lease_key: str,
    owner_id: str,
    *,
    expected_epoch: int | None = None,
) -> bool:
    query = db.query(SchedulerLease).filter(
        SchedulerLease.lease_key == lease_key,
        SchedulerLease.owner_id == owner_id,
    )
    if expected_epoch is not None:
        query = query.filter(SchedulerLease.epoch == expected_epoch)
    updated = query.update(
        {SchedulerLease.owner_id: None, SchedulerLease.lease_until: utcnow(), SchedulerLease.heartbeat_at: utcnow()},
        synchronize_session=False,
    )
    db.commit()
    return updated == 1


def start_dispatch_run(
    db: Session,
    lease_key: str,
    owner_id: str,
    *,
    fencing_epoch: int = 0,
) -> SchedulerDispatchRun:
    row = SchedulerDispatchRun(
        dispatch_uid=uuid.uuid4().hex,
        lease_key=lease_key,
        owner_id=owner_id,
        fencing_epoch=fencing_epoch,
        started_at=utcnow(),
        status="running",
        job_results=[],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finish_dispatch_run(db: Session, row: SchedulerDispatchRun, job_results: list[dict]) -> SchedulerDispatchRun:
    from collections import Counter
    from app.tasks.master_scheduler import aggregate_business_status

    row.finished_at = utcnow()
    row.job_results = job_results
    statuses = [item.get("status", "unknown") for item in job_results]
    row.status = aggregate_business_status(statuses)
    completed = sum(item.get("dispatch_status") in {"success", "completed"} for item in job_results)
    counts = Counter(statuses)
    row.message = f"dispatch completed={completed}/{len(job_results)}; business " + ", ".join(
        f"{status}={counts[status]}" for status in sorted(counts)
    )
    db.commit()
    db.refresh(row)
    return row


def serialize_lease(row: SchedulerLease | None) -> dict | None:
    if row is None:
        return None
    return {
        "lease_key": row.lease_key,
        "owner_id": row.owner_id,
        "lease_until": _iso(row.lease_until),
        "heartbeat_at": _iso(row.heartbeat_at),
        "epoch": row.epoch,
        "active": bool(row.owner_id and row.lease_until and row.lease_until > utcnow()),
    }


def serialize_dispatch(row: SchedulerDispatchRun) -> dict:
    return {
        "id": row.id,
        "dispatch_uid": row.dispatch_uid,
        "lease_key": row.lease_key,
        "owner_id": row.owner_id,
        "fencing_epoch": row.fencing_epoch,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "status": row.status,
        "job_results": row.job_results or [],
        "message": row.message,
    }
