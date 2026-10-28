from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.observability import ObservabilityQueryAudit
from app.services.observability.log_adapters import MAX_LOG_EVENTS, fetch_log_payload, normalize_log_events


MAX_QUERY_WINDOW = timedelta(hours=24)
SOURCE_LABELS = {
    "database": "Database",
    "host": "Host",
    "alert": "Alert",
    "application_log": "Application log",
    "apm": "APM",
    "elasticsearch": "Application log",
    "opensearch": "Application log",
    "loki": "Application log",
    "graylog": "Application log",
}


def validate_time_window(start_time: datetime | str, end_time: datetime | str) -> tuple[datetime, datetime]:
    start = _to_utc(start_time)
    end = _to_utc(end_time)
    if end < start:
        raise ValueError("end time must not be earlier than start time")
    if end - start > MAX_QUERY_WINDOW:
        raise ValueError("time window must not exceed 24 hours")
    return start, end


def query_logs(source: Any, query: dict[str, Any], fetcher: Callable[[Any, dict[str, Any]], Any] | None = None) -> list[dict[str, Any]]:
    start, end = validate_time_window(query["start_time"], query["end_time"])
    normalized_query = {**query, "start_time": start, "end_time": end, "limit": min(int(query.get("limit") or MAX_LOG_EVENTS), MAX_LOG_EVENTS)}
    payload = (fetcher or fetch_log_payload)(source, normalized_query)
    return normalize_log_events(getattr(source, "source_type", ""), payload, normalized_query["limit"])


def build_fault_timeline(instance_id: int | None, start_time: datetime | str, end_time: datetime | str, events: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    start, end = validate_time_window(start_time, end_time)
    timeline = []
    for event in (events or [])[:MAX_LOG_EVENTS]:
        event_time = _to_utc(event.get("event_time"))
        if not start <= event_time <= end:
            continue
        source_type = str(event.get("source_type") or "application_log")
        timeline.append({
            **event,
            "instance_id": event.get("instance_id", instance_id),
            "event_time": event_time.isoformat(),
            "source_label": SOURCE_LABELS.get(source_type, source_type.replace("_", " ").title()),
        })
    return sorted(timeline, key=lambda item: item["event_time"])


def record_query_audit(db: Session, *, query_kind: str, source_id: int | None, instance_id: int | None, start_time: datetime | str | None, end_time: datetime | str | None, query_summary: str | None, result_count: int, status: str = "success", metadata: dict[str, Any] | None = None) -> None:
    db.add(ObservabilityQueryAudit(
        query_kind=query_kind,
        source_id=source_id,
        instance_id=instance_id,
        start_time=_to_utc(start_time) if start_time else None,
        end_time=_to_utc(end_time) if end_time else None,
        query_summary=(query_summary or "")[:500],
        result_count=min(max(result_count, 0), MAX_LOG_EVENTS),
        status=status,
        query_metadata=metadata or {},
    ))
    db.commit()


def _to_utc(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text) if text else datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
