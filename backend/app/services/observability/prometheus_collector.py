from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    try:
        path = payload.get("path") if payload else None
        response = http_request(source, path or "/api/v1/status/runtimeinfo")
        metrics = [
            metric(source, "prometheus.up", 1),
            metric(source, "prometheus.response_time_ms", response["elapsed_ms"], "ms"),
        ]
        body = response["body"]
        if isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            series = data.get("seriesCountByMetricName") if isinstance(data, dict) else None
            if isinstance(series, int):
                metrics.append(metric(source, "prometheus.series_count_by_metric_name", series))
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"Prometheus 只读采集完成，写入 {len(ingested)} 个指标", {"url": response["url"]})
        return {
            "status": "success",
            "message": "Prometheus 只读采集完成",
            "source_id": source.id,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"Prometheus 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
