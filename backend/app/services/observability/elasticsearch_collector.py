from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, flatten_numbers, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


STATUS_VALUE = {"green": 1, "yellow": 0.5, "red": 0}


def _health_path(source: ObservabilitySource) -> str:
    url = source.api_url or ""
    return "" if "_cluster/health" in url else "/_cluster/health"


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    try:
        response = http_request(source, _health_path(source))
        body = response["body"]
        if not isinstance(body, dict):
            raise ValueError("Elasticsearch health endpoint did not return JSON")
        status = str(body.get("status") or "unknown").lower()
        metrics = [
            metric(source, "elasticsearch.up", 1),
            metric(source, "elasticsearch.response_time_ms", response["elapsed_ms"], "ms"),
            metric(source, "elasticsearch.cluster.health_status", STATUS_VALUE.get(status, -1), status=status if status in STATUS_VALUE else "warning"),
        ]
        for key, value in flatten_numbers(body, "elasticsearch.cluster", 80):
            metrics.append(metric(source, key, value))
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"Elasticsearch 只读采集完成，集群状态 {status}，写入 {len(ingested)} 个指标", {"url": response["url"], "status": status})
        return {
            "status": "success",
            "message": f"Elasticsearch 只读采集完成，集群状态 {status}",
            "source_id": source.id,
            "cluster_status": status,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"Elasticsearch 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
