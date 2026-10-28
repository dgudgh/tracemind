from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url
from app.services.observability.apm_adapters import normalize_trace
from app.services.observability.log_query import validate_time_window


MIN_SQL_MATCH_CONFIDENCE = 60
MAX_SQL_FINGERPRINT_LENGTH = 4000


def query_trace(source: Any, trace_id: str, fetcher: Callable[[Any, str], Any] | None = None) -> dict[str, Any]:
    if not trace_id.strip():
        raise ValueError("trace id is required")
    payload = (fetcher or fetch_trace_payload)(source, trace_id.strip())
    return normalize_trace(getattr(source, "source_type", ""), payload)


def fetch_trace_payload(source: Any, trace_id: str) -> Any:
    source_type = (getattr(source, "source_type", "") or "").lower()
    trace_path_segment = quote(trace_id, safe="")
    if source_type == "jaeger":
        path = f"/api/traces/{trace_path_segment}"
    elif source_type == "zipkin":
        path = f"/api/v2/trace/{trace_path_segment}"
    elif source_type == "skywalking":
        raise ValueError("SkyWalking trace reads require a configured GraphQL adapter")
    elif source_type in {"otel", "opentelemetry"}:
        raise ValueError("OpenTelemetry collectors do not expose a standard trace-query endpoint")
    elif source_type == "pinpoint":
        raise ValueError("Pinpoint adapter is planned until a fixture-backed adapter is available")
    else:
        raise ValueError(f"unsupported APM source type: {source_type}")
    request = urllib.request.Request(require_http_url(f"{str(source.api_url).rstrip('/')}{path}"), headers=_headers(source), method="GET")
    with urllib.request.urlopen(request, timeout=min(max(float(getattr(source, "timeout_seconds", 10) or 10), 1), 30)) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def link_sql(trace: dict[str, Any], instances: list[Any]) -> list[dict[str, Any]]:
    evidence = []
    for span in trace.get("spans") or []:
        tags = span.get("tags") or {}
        sql = tags.get("db.statement") or tags.get("sql") or tags.get("db.sql.text")
        if not sql:
            continue
        candidate, confidence = _match_instance(tags, instances)
        matched = candidate if confidence >= MIN_SQL_MATCH_CONFIDENCE else None
        evidence.append({
            "source": "apm",
            "trace_id": trace.get("trace_id"),
            "span_id": span.get("span_id"),
            "service": span.get("service"),
            "endpoint": span.get("endpoint"),
            "duration_ms": span.get("duration_ms"),
            "span_status": span.get("status"),
            "event_time": span.get("event_time"),
            "sql_fingerprint": sql_fingerprint(str(sql)),
            "instance_id": _field(matched, "id") if matched is not None else None,
            "match_confidence": confidence,
            "unmatched_reason": None if matched is not None else _unmatched_reason(tags, confidence),
        })
    return evidence


def build_apm_evidence(instance_id: int, window: tuple[datetime | str, datetime | str], traces: list[dict[str, Any]] | None = None, instances: list[Any] | None = None) -> list[dict[str, Any]]:
    start, end = validate_time_window(window[0], window[1])
    result = []
    for trace in traces or []:
        for item in link_sql(trace, instances or []):
            event_time = _to_datetime(item.get("event_time"))
            if item["instance_id"] == instance_id and start <= event_time <= end:
                result.append({**item, "time_range": {"start": start.isoformat(), "end": end.isoformat()}})
    return result


def sql_fingerprint(sql: str) -> str:
    compact = re.sub(r"\s+", " ", sql[:MAX_SQL_FINGERPRINT_LENGTH]).strip()
    compact = re.sub(r"'(?:''|[^'])*'", "?", compact)
    compact = re.sub(r'"(?:""|[^"])*"', "?", compact)
    return re.sub(r"\b\d+(?:\.\d+)?\b", "?", compact)


def _match_instance(tags: dict[str, Any], instances: list[Any]) -> tuple[Any | None, int]:
    peer = str(tags.get("peer.address") or tags.get("server.address") or tags.get("net.peer.name") or "").strip().lower()
    peer_port = str(tags.get("peer.port") or tags.get("server.port") or "").strip()
    best: tuple[Any | None, int] = (None, 0)
    for instance in instances:
        host = str(_field(instance, "host") or "").strip().lower()
        port = str(_field(instance, "port") or "").strip()
        if not peer or peer != host:
            continue
        confidence = 100 if peer_port and peer_port == port else 70
        if confidence > best[1]:
            best = (instance, confidence)
    return best


def _unmatched_reason(tags: dict[str, Any], confidence: int) -> str:
    if not (tags.get("peer.address") or tags.get("server.address") or tags.get("net.peer.name")):
        return "trace span has no database peer address"
    return f"instance match confidence {confidence} is below {MIN_SQL_MATCH_CONFIDENCE}"


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _to_datetime(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _headers(source: Any) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if getattr(source, "token_encrypted", None):
        headers["Authorization"] = f"Bearer {decrypt_password(source.token_encrypted)}"
    return headers
