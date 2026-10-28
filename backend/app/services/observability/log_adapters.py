from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url


MAX_LOG_EVENTS = 2000
MAX_MESSAGE_LENGTH = 4000


def normalize_log_events(source_type: str, payload: Any, limit: int = MAX_LOG_EVENTS) -> list[dict[str, Any]]:
    """Convert supported read-only log responses into the timeline contract."""
    normalized_source = (source_type or "").lower()
    limit = min(max(int(limit or MAX_LOG_EVENTS), 1), MAX_LOG_EVENTS)
    if normalized_source in {"elasticsearch", "opensearch"}:
        events = _normalize_elasticsearch(payload)
    elif normalized_source == "loki":
        events = _normalize_loki(payload)
    elif normalized_source == "graylog":
        events = _normalize_graylog(payload)
    else:
        raise ValueError(f"unsupported log source type: {source_type}")
    return [event for event in events if event is not None][:limit]


def fetch_log_payload(source: Any, query: dict[str, Any]) -> Any:
    source_type = (getattr(source, "source_type", "") or "").lower()
    if source_type in {"elasticsearch", "opensearch"}:
        return _post_json(source, "/_search", _elasticsearch_query(query))
    if source_type == "loki":
        params = {
            "query": query.get("query") or "{}",
            "start": str(_epoch_nanoseconds(query.get("start_time"))),
            "end": str(_epoch_nanoseconds(query.get("end_time"))),
            "limit": min(int(query.get("limit") or MAX_LOG_EVENTS), MAX_LOG_EVENTS),
            "direction": "FORWARD",
        }
        return _get_json(source, f"/loki/api/v1/query_range?{urllib.parse.urlencode(params)}")
    if source_type == "graylog":
        params = {
            "query": query.get("query") or "*",
            "from": _to_utc(query.get("start_time")).isoformat(),
            "to": _to_utc(query.get("end_time")).isoformat(),
            "limit": min(int(query.get("limit") or MAX_LOG_EVENTS), MAX_LOG_EVENTS),
        }
        return _get_json(source, f"/api/search/universal/absolute?{urllib.parse.urlencode(params)}")
    raise ValueError(f"unsupported log source type: {source_type}")


def _normalize_elasticsearch(payload: Any) -> list[dict[str, Any]]:
    hits = ((payload or {}).get("hits") or {}).get("hits") or []
    events = []
    for index, hit in enumerate(hits[:MAX_LOG_EVENTS]):
        document = hit.get("_source") or {}
        events.append(_event(
            event_time=document.get("@timestamp") or document.get("timestamp"),
            source_type="elasticsearch",
            host=_nested(document, "host", "name") or document.get("host"),
            application=_nested(document, "service", "name") or document.get("application") or document.get("app"),
            severity=_nested(document, "log", "level") or document.get("level"),
            trace_id=_nested(document, "trace", "id") or document.get("trace_id"),
            message=document.get("message") or document.get("log_message") or "",
            raw_ref=str(hit.get("_id") or f"elasticsearch:{index}"),
        ))
    return events


def _normalize_loki(payload: Any) -> list[dict[str, Any]]:
    streams = ((payload or {}).get("data") or {}).get("result") or []
    events = []
    for stream_index, stream in enumerate(streams):
        labels = stream.get("stream") or {}
        for value_index, value in enumerate(stream.get("values") or []):
            if len(events) >= MAX_LOG_EVENTS:
                return events
            timestamp, message = (value + [""])[:2] if isinstance(value, list) else (None, "")
            events.append(_event(
                event_time=timestamp,
                source_type="loki",
                host=labels.get("host") or labels.get("hostname") or labels.get("instance"),
                application=labels.get("application") or labels.get("app") or labels.get("service"),
                severity=labels.get("level") or labels.get("severity"),
                trace_id=labels.get("trace_id") or labels.get("traceid"),
                message=message,
                raw_ref=f"loki:{stream_index}:{value_index}",
            ))
    return events


def _normalize_graylog(payload: Any) -> list[dict[str, Any]]:
    messages = (payload or {}).get("messages") or []
    events = []
    for index, item in enumerate(messages[:MAX_LOG_EVENTS]):
        document = item.get("message") if isinstance(item, dict) else {}
        document = document or {}
        events.append(_event(
            event_time=document.get("timestamp"),
            source_type="graylog",
            host=document.get("source") or document.get("host"),
            application=document.get("application_name") or document.get("application") or document.get("facility"),
            severity=document.get("level") or document.get("severity"),
            trace_id=document.get("trace_id") or document.get("traceid"),
            message=document.get("full_message") or document.get("message") or "",
            raw_ref=str(document.get("_id") or f"graylog:{index}"),
        ))
    return events


def _event(*, event_time: Any, source_type: str, host: Any, application: Any, severity: Any, trace_id: Any, message: Any, raw_ref: str) -> dict[str, Any] | None:
    if event_time in (None, ""):
        return None
    try:
        normalized_time = _to_utc(event_time).isoformat()
    except (OverflowError, TypeError, ValueError, OSError):
        return None
    return {
        "event_time": normalized_time,
        "source_type": source_type,
        "host": str(host or ""),
        "application": str(application or ""),
        "severity": _severity(severity),
        "trace_id": str(trace_id or ""),
        "message": str(message or "")[:MAX_MESSAGE_LENGTH],
        "raw_ref": raw_ref,
    }


def _severity(value: Any) -> str:
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return {0: "critical", 1: "critical", 2: "critical", 3: "error", 4: "warning", 5: "info", 6: "info", 7: "debug"}.get(int(value), "info")
    text = str(value or "info").lower()
    if text in {"fatal", "critical", "crit", "emergency", "alert"}:
        return "critical"
    if text in {"error", "err"}:
        return "error"
    if text in {"warn", "warning"}:
        return "warning"
    if text in {"debug", "trace"}:
        return "debug"
    return "info"


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _to_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        numeric = int(value)
        seconds = numeric / (1_000_000_000 if numeric > 10_000_000_000_000 else 1000 if numeric > 10_000_000_000 else 1)
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text) if text else datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _epoch_nanoseconds(value: Any) -> int:
    return int(_to_utc(value).timestamp() * 1_000_000_000)


def _headers(source: Any) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    auth_type = (getattr(source, "auth_type", "") or "").lower()
    token = decrypt_password(source.token_encrypted) if getattr(source, "token_encrypted", None) else None
    if token and auth_type in {"bearer", "token", "api_key"}:
        headers["Authorization"] = f"Bearer {token}"
    if auth_type == "basic" and getattr(source, "username", None) and getattr(source, "password_encrypted", None):
        import base64
        password = decrypt_password(source.password_encrypted)
        encoded = base64.b64encode(f"{source.username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def _get_json(source: Any, path: str) -> Any:
    url = require_http_url(f"{str(source.api_url).rstrip('/')}{path}")
    request = urllib.request.Request(url, headers=_headers(source), method="GET")
    with urllib.request.urlopen(request, timeout=_timeout(source)) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _post_json(source: Any, path: str, payload: dict[str, Any]) -> Any:
    url = require_http_url(f"{str(source.api_url).rstrip('/')}{path}")
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={**_headers(source), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=_timeout(source)) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _timeout(source: Any) -> float:
    return min(max(float(getattr(source, "timeout_seconds", 10) or 10), 1), 30)


def _elasticsearch_query(query: dict[str, Any]) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": _to_utc(query.get("start_time")).isoformat(), "lte": _to_utc(query.get("end_time")).isoformat()}}}]
    for field in ("host", "application", "trace_id"):
        if query.get(field):
            clauses.append({"term": {field: str(query[field])}})
    if query.get("severity"):
        clauses.append({"term": {"log.level": str(query["severity"])}})
    if query.get("keyword") or query.get("query"):
        clauses.append({"simple_query_string": {"query": str(query.get("keyword") or query.get("query")), "fields": ["message", "log_message"]}})
    return {"size": min(int(query.get("limit") or MAX_LOG_EVENTS), MAX_LOG_EVENTS), "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": clauses}}}
