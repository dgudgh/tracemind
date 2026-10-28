from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, flatten_numbers, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


def _skywalking_probe(source: ObservabilitySource) -> dict[str, Any]:
    url = source.api_url or ""
    if url.rstrip("/").endswith("/graphql"):
        try:
            return http_request(source, method="POST", payload={"query": "query version { version }"})
        except Exception:
            return http_request(source)
    return http_request(source)


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    try:
        response = _skywalking_probe(source)
        body = response["body"]
        parsed = [(f"skywalking.{name}", value) for name, value in flatten_numbers(body, "", 80)] if isinstance(body, (dict, list)) else []
        metrics = [
            metric(source, "skywalking.api.up", 1),
            metric(source, "skywalking.api.response_time_ms", response["elapsed_ms"], "ms"),
        ]
        metrics.extend(metric(source, name, value) for name, value in parsed)
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"SkyWalking 只读采集完成，写入 {len(ingested)} 个指标", {"url": response["url"], "metric_count": len(parsed)})
        return {
            "status": "success",
            "message": "SkyWalking 只读采集完成",
            "source_id": source.id,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"SkyWalking 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
