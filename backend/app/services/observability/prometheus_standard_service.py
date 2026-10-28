from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy.orm import Session

from app.core.http_security import require_http_url
from app.models.observability import ObservabilityCollectLog, ObservabilitySource
from app.services.observability.metric_mapping_service import ingest_metric


PROMETHEUS_STANDARD_QUERIES: dict[str, dict[str, str]] = {
    "host.cpu.used_pct": {
        "query": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "unit": "%",
    },
    "host.memory.used_pct": {
        "query": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
        "unit": "%",
    },
    "host.disk.used_pct": {
        "query": '100 * (1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|devtmpfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|devtmpfs"}))',
        "unit": "%",
    },
    "host.io.await_ms": {
        "query": "1000 * rate(node_disk_io_time_seconds_total[5m]) / clamp_min(rate(node_disk_reads_completed_total[5m]) + rate(node_disk_writes_completed_total[5m]), 1)",
        "unit": "ms",
    },
    "host.network.errors": {
        "query": "rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])",
        "unit": "count/s",
    },
    "host.network.retrans": {
        "query": "rate(node_netstat_Tcp_RetransSegs[5m])",
        "unit": "count/s",
    },
    "db.instance.up": {
        "query": 'up{job=~".*(oracle|mysql|postgres|postgresql|sqlserver|mssql|db).*"}',
        "unit": "bool",
    },
    "db.connection.used_pct": {
        "query": 'db_connection_used_pct or (100 * (oracledb_resource_current_utilization{resource_name="sessions"} / clamp_min(oracledb_resource_limit_value{resource_name="sessions"}, 1)))',
        "unit": "%",
    },
    "db.tablespace.used_pct": {
        "query": "db_tablespace_used_pct or oracledb_tablespace_used_percent",
        "unit": "%",
    },
    "db.replication.lag_seconds": {
        "query": "db_replication_lag_seconds",
        "unit": "s",
    },
    "oracle.fra.used_pct": {
        "query": "oracle_fra_used_pct or oracledb_fra_used_percent",
        "unit": "%",
    },
    "oracle.dg.lag_seconds": {
        "query": "oracle_dg_lag_seconds",
        "unit": "s",
    },
    "mysql.replication.lag_seconds": {
        "query": "mysql_replication_lag_seconds",
        "unit": "s",
    },
}


def sync_standard_metrics(db: Session, source: ObservabilitySource) -> dict[str, Any]:
    stored = 0
    failed: list[dict[str, str]] = []
    query_items = list(PROMETHEUS_STANDARD_QUERIES.items())
    for index, (standard_metric, spec) in enumerate(query_items):
        try:
            result = _query(source, spec["query"])
            stored += _store_query_result(db, source, standard_metric, spec["query"], spec.get("unit"), result)
        except Exception as exc:
            failed.append({"standard_metric": standard_metric, "message": str(exc)})
            if _is_source_unavailable(exc):
                failed.extend(
                    {
                        "standard_metric": skipped_metric,
                        "message": f"数据源不可达，已跳过：{exc}",
                    }
                    for skipped_metric, _ in query_items[index + 1 :]
                )
                break
    status = "success" if not failed else ("partial" if stored else "failed")
    message = (
        "Prometheus standard metric sync completed"
        if status != "failed"
        else failed[0]["message"]
    )
    db.add(
        ObservabilityCollectLog(
            source_id=source.id,
            log_level="info" if status == "success" else ("warning" if status == "partial" else "error"),
            log_type="prometheus_standard_metrics",
            message=f"Prometheus standard metric sync completed, stored={stored}, failed={len(failed)}",
            raw_data={"stored": stored, "failed": failed},
        )
    )
    db.commit()
    return {
        "status": status,
        "message": message,
        "stored_count": stored,
        "failed": failed,
    }


def _is_source_unavailable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, urllib.error.URLError),
    ) and not isinstance(exc, urllib.error.HTTPError)


def _query(source: ObservabilitySource, query: str) -> dict[str, Any]:
    if not source.api_url:
        raise ValueError("Prometheus api_url not configured")
    url = require_http_url(f"{source.api_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': query})}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=source.timeout_seconds or 10) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    if data.get("status") != "success":
        raise RuntimeError(data.get("error") or data.get("errorType") or "Prometheus query failed")
    return data


def _store_query_result(
    db: Session,
    source: ObservabilitySource,
    standard_metric: str,
    source_query: str,
    metric_unit: str | None,
    result: dict[str, Any],
) -> int:
    rows = ((result.get("data") or {}).get("result") or [])[:500]
    count = 0
    for row in rows:
        metric_labels = row.get("metric") or {}
        value_pair = row.get("value") or []
        if len(value_pair) != 2:
            continue
        ts, value = value_pair
        host_id = _host_id(metric_labels)
        ingest_metric(
            db,
            {
                "source_type": "prometheus",
                "source_id": source.id,
                "host_id": host_id,
                "labels": metric_labels,
                "source_metric": source_query,
                "standard_metric": standard_metric,
                "metric_value": value,
                "metric_unit": metric_unit,
                "metric_time": ts,
                "status": "ok",
            },
        )
        count += 1
    return count


def _host_id(metric_labels: dict[str, Any]) -> str | None:
    base = metric_labels.get("host") or metric_labels.get("instance") or metric_labels.get("job")
    dimensions = []
    for key in ("tablespace", "resource_name", "mountpoint", "device", "fstype"):
        value = metric_labels.get(key)
        if value:
            dimensions.append(f"{key}:{value}")
    if base and dimensions:
        return f"{base}:{':'.join(dimensions)}"
    return base or metric_labels.get("instance") or metric_labels.get("job")
