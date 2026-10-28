from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilitySource
from app.services.observability.http_probe import collect_log, flatten_numbers, http_request, metric
from app.services.observability.metric_mapping_service import ingest_metrics


PROM_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+]?\d+(?:\.\d+)?)")


def _parse_prometheus_text(text: str) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.startswith(("jvm_", "tomcat_", "process_", "system_")):
            rows.append((f"tomcat.{name}", float(match.group(2))))
        if len(rows) >= 80:
            break
    return rows


def collect(db: Session, source: ObservabilitySource, payload: dict | None = None) -> dict[str, Any]:
    try:
        response = http_request(source)
        body = response["body"]
        parsed = (
            [(f"tomcat.{name}", value) for name, value in flatten_numbers(body, "", 80)]
            if isinstance(body, (dict, list))
            else _parse_prometheus_text(response["text"])
        )
        metrics = [
            metric(source, "tomcat.jvm.up", 1),
            metric(source, "tomcat.jvm.response_time_ms", response["elapsed_ms"], "ms"),
        ]
        metrics.extend(metric(source, name, value) for name, value in parsed)
        ingested = ingest_metrics(db, metrics)
        collect_log(db, source, "info", f"Tomcat/JVM 只读采集完成，写入 {len(ingested)} 个指标", {"url": response["url"], "metric_count": len(parsed)})
        return {
            "status": "success",
            "message": "Tomcat/JVM 只读采集完成",
            "source_id": source.id,
            "metric_count": len(ingested),
            "metrics": ingested,
        }
    except Exception as exc:
        collect_log(db, source, "error", f"Tomcat/JVM 只读采集失败：{exc}")
        return {"status": "failed", "message": str(exc), "source_id": source.id}
