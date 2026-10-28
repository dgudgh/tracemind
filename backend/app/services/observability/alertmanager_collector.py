from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, flatten_numbers, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    if not source.api_url:
        collect_log(db, source, "info", "Alertmanager 接收型数据源已登记，无主动采集地址")
        return {"status": "success", "message": "Alertmanager 接收型数据源已登记，无主动采集地址", "source_id": source.id}
    try:
        response = http_request(source, "/api/v2/status" if "/api/v2/status" not in (source.api_url or "") else "")
        metrics = [
            metric(source, "alertmanager.up", 1),
            metric(source, "alertmanager.response_time_ms", response["elapsed_ms"], "ms"),
        ]
        body = response["body"]
        if isinstance(body, (dict, list)):
            metrics.extend(metric(source, f"alertmanager.{name}", value) for name, value in flatten_numbers(body, "", 40))
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"Alertmanager 只读采集完成，写入 {len(ingested)} 个指标", {"url": response["url"]})
        return {
            "status": "success",
            "message": "Alertmanager 只读采集完成",
            "source_id": source.id,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"Alertmanager 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
