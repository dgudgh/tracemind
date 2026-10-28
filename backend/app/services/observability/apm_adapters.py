from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


SENSITIVE_TAG_PARTS = ("password", "passwd", "secret", "token", "authorization", "credential")
MAX_TRACE_SPANS = 2000
MAX_TRACE_TAGS = 128
MAX_TAG_VALUE_LENGTH = 4000


def normalize_trace(source_type: str, payload: Any) -> dict[str, Any]:
    source_type = (source_type or "").lower().replace("opentelemetry", "otel")
    if source_type == "skywalking":
        return _skywalking(payload)
    if source_type in {"jaeger", "zipkin"}:
        return _jaeger(payload) if source_type == "jaeger" else _zipkin(payload)
    if source_type == "otel":
        return _otel(payload)
    if source_type == "pinpoint":
        raise ValueError("Pinpoint adapter is planned until a fixture-backed adapter is available")
    raise ValueError(f"unsupported APM source type: {source_type}")


def _skywalking(payload: Any) -> dict[str, Any]:
    trace = (((payload or {}).get("data") or {}).get("trace") or (payload or {}).get("trace") or {})
    spans = []
    for span in (trace.get("spans") or [])[:MAX_TRACE_SPANS]:
        start = _millis(span.get("startTime"))
        end = _millis(span.get("endTime"))
        if start is None or end is None:
            continue
        spans.append(_span(
            span_id=span.get("spanId"), parent_span_id=span.get("parentSpanId"), service=span.get("serviceCode") or span.get("serviceName"), endpoint=span.get("endpointName") or span.get("operationName"), start=start, end=end,
            status="error" if span.get("isError") else "ok", tags=_tag_list(span.get("tags")),
        ))
    return {"trace_id": str(trace.get("traceId") or trace.get("trace_id") or ""), "spans": spans}


def _jaeger(payload: Any) -> dict[str, Any]:
    trace = ((payload or {}).get("data") or [{}])[0] if isinstance(payload, dict) else {}
    spans = []
    for span in (trace.get("spans") or [])[:MAX_TRACE_SPANS]:
        process = span.get("process") or {}
        tags = _tag_list(span.get("tags"))
        start = _micros(span.get("startTime"))
        duration = _duration_millis(span.get("duration"))
        if start is None or duration is None:
            continue
        end = start + timedelta(milliseconds=duration)
        spans.append(_span(span.get("spanID"), span.get("references", [{}])[0].get("spanID") if span.get("references") else None, process.get("serviceName"), span.get("operationName"), start, end, _status_from_tags(tags), tags))
    return {"trace_id": str(trace.get("traceID") or trace.get("traceId") or ""), "spans": spans}


def _zipkin(payload: Any) -> dict[str, Any]:
    spans = []
    values = payload if isinstance(payload, list) else (payload or {}).get("data") or []
    trace_id = ""
    for span in values:
        if len(spans) >= MAX_TRACE_SPANS:
            break
        trace_id = trace_id or str(span.get("traceId") or "")
        tags = _tag_list(span.get("tags"))
        start = _micros(span.get("timestamp"))
        duration = _duration_millis(span.get("duration"))
        if start is None or duration is None:
            continue
        end = start + timedelta(milliseconds=duration)
        endpoint = span.get("localEndpoint") or {}
        spans.append(_span(span.get("id"), span.get("parentId"), endpoint.get("serviceName"), span.get("name"), start, end, _status_from_tags(tags), tags))
    return {"trace_id": trace_id, "spans": spans}


def _otel(payload: Any) -> dict[str, Any]:
    spans = []
    trace_id = ""
    for resource_span in (payload or {}).get("resourceSpans") or []:
        resource = _tag_list((resource_span.get("resource") or {}).get("attributes"))
        service = resource.get("service.name", "")
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                if len(spans) >= MAX_TRACE_SPANS:
                    break
                trace_id = trace_id or str(span.get("traceId") or "")
                tags = _tag_list(span.get("attributes"))
                status_code = ((span.get("status") or {}).get("code") or "").lower()
                start = _nanos(span.get("startTimeUnixNano"))
                end = _nanos(span.get("endTimeUnixNano"))
                if start is None or end is None:
                    continue
                spans.append(_span(span.get("spanId"), span.get("parentSpanId"), service, span.get("name"), start, end, "error" if status_code in {"error", "2"} else "ok", tags))
    return {"trace_id": trace_id, "spans": spans}


def _span(span_id: Any, parent_span_id: Any, service: Any, endpoint: Any, start: datetime, end: datetime, status: str, tags: dict[str, Any]) -> dict[str, Any]:
    return {"span_id": str(span_id or "")[:256], "parent_span_id": str(parent_span_id or "")[:256], "service": str(service or "")[:500], "endpoint": str(endpoint or "")[:1000], "event_time": start.isoformat(), "duration_ms": max(int((end - start).total_seconds() * 1000), 0), "status": status, "tags": tags}


def _tag_list(values: Any) -> dict[str, Any]:
    if isinstance(values, dict):
        items = values.items()
    else:
        items = []
        for item in values or []:
            key = item.get("key") if isinstance(item, dict) else None
            value = item.get("value") if isinstance(item, dict) else None
            if isinstance(value, dict):
                value = next((v for v in value.values() if v is not None), None)
            items.append((key, value))
    result = {}
    for key, value in items:
        normalized_key = str(key or "")[:256]
        if not normalized_key or any(part in normalized_key.lower() for part in SENSITIVE_TAG_PARTS):
            continue
        result[normalized_key] = str(value)[:MAX_TAG_VALUE_LENGTH] if value is not None else None
        if len(result) >= MAX_TRACE_TAGS:
            break
    return result


def _status_from_tags(tags: dict[str, Any]) -> str:
    status = str(tags.get("error") or tags.get("http.status_code") or "").lower()
    return "error" if status in {"true", "error"} or status.startswith("5") else "ok"


def _timestamp(value: Any, divisor: int) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / divisor, tz=timezone.utc)
    except (OverflowError, TypeError, ValueError, OSError):
        return None


def _millis(value: Any) -> datetime | None:
    return _timestamp(value, 1000)


def _micros(value: Any) -> datetime | None:
    return _timestamp(value, 1_000_000)


def _nanos(value: Any) -> datetime | None:
    return _timestamp(value, 1_000_000_000)


def _duration_millis(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(float(value) / 1000, 0)
    except (TypeError, ValueError, OverflowError):
        return None
