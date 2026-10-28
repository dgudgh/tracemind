from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


def _parse_stub_status(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    active = re.search(r"Active connections:\s*(\d+)", text)
    if active:
        data["nginx.active_connections"] = int(active.group(1))
    accepts = re.search(r"server\s+accepts\s+handled\s+requests\s*\n\s*(\d+)\s+(\d+)\s+(\d+)", text, re.I)
    if accepts:
        data["nginx.accepts_total"] = int(accepts.group(1))
        data["nginx.handled_total"] = int(accepts.group(2))
        data["nginx.requests_total"] = int(accepts.group(3))
    rw = re.search(r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)", text)
    if rw:
        data["nginx.reading"] = int(rw.group(1))
        data["nginx.writing"] = int(rw.group(2))
        data["nginx.waiting"] = int(rw.group(3))
    return data


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    try:
        response = http_request(source)
        parsed = _parse_stub_status(response["text"])
        metrics = [
            metric(source, "nginx.up", 1),
            metric(source, "nginx.response_time_ms", response["elapsed_ms"], "ms"),
        ]
        metrics.extend(metric(source, name, value) for name, value in parsed.items())
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"Nginx 只读采集完成，写入 {len(ingested)} 个指标", {"url": response["url"], "metrics": parsed})
        return {
            "status": "success",
            "message": "Nginx 只读采集完成",
            "source_id": source.id,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"Nginx 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
