from __future__ import annotations

import re
from typing import Any

STANDARD_METRICS = [
    "host.cpu.used_pct",
    "host.memory.used_pct",
    "host.disk.used_pct",
    "host.io.await_ms",
    "host.network.errors",
    "host.network.retrans",
    "db.instance.up",
    "db.connection.used_pct",
    "db.tablespace.used_pct",
    "db.replication.lag_seconds",
    "oracle.fra.used_pct",
    "oracle.dg.lag_seconds",
    "mysql.replication.lag_seconds",
]


DEFAULT_MAPPINGS = [
    # Host CPU
    ("zabbix", "system.cpu.util", "host.cpu.used_pct", "%", "Zabbix CPU utilization"),
    ("zabbix", "system.cpu.util[*]", "host.cpu.used_pct", "%", "Zabbix CPU utilization with params"),
    ("zabbix", "system.cpu.load[*]", "host.cpu.used_pct", "%", "Zabbix CPU load proxy"),
    ("prometheus", "node_cpu_seconds_total", "host.cpu.used_pct", "%", "Prometheus node exporter CPU counter"),
    ("prometheus", "host_cpu_used_pct", "host.cpu.used_pct", "%", "Precomputed CPU used percent"),

    # Host memory
    ("zabbix", "vm.memory.size[pused]", "host.memory.used_pct", "%", "Zabbix memory used percent"),
    ("zabbix", "vm.memory.size[pavailable]", "host.memory.used_pct", "%", "Zabbix memory available percent, inverted"),
    ("prometheus", "node_memory_MemAvailable_bytes", "host.memory.used_pct", "%", "Prometheus available memory, combined with total"),
    ("prometheus", "node_memory_MemTotal_bytes", "host.memory.used_pct", "%", "Prometheus memory total"),
    ("prometheus", "host_memory_used_pct", "host.memory.used_pct", "%", "Precomputed memory used percent"),

    # Host disk / IO
    ("zabbix", "vfs.fs.size[*]", "host.disk.used_pct", "%", "Zabbix filesystem size key"),
    ("zabbix", "vfs.fs.size[*,pused]", "host.disk.used_pct", "%", "Zabbix filesystem used percent"),
    ("zabbix", "vfs.dev.await[*]", "host.io.await_ms", "ms", "Zabbix disk await"),
    ("zabbix", "vfs.dev.read.await[*]", "host.io.await_ms", "ms", "Zabbix disk read await"),
    ("zabbix", "vfs.dev.write.await[*]", "host.io.await_ms", "ms", "Zabbix disk write await"),
    ("prometheus", "node_filesystem_avail_bytes", "host.disk.used_pct", "%", "Prometheus filesystem available bytes"),
    ("prometheus", "node_filesystem_size_bytes", "host.disk.used_pct", "%", "Prometheus filesystem size bytes"),
    ("prometheus", "node_disk_io_time_seconds_total", "host.io.await_ms", "ms", "Prometheus disk IO time"),
    ("prometheus", "host_disk_used_pct", "host.disk.used_pct", "%", "Precomputed disk used percent"),
    ("prometheus", "host_io_await_ms", "host.io.await_ms", "ms", "Precomputed IO await"),

    # Host network
    ("zabbix", "net.if.in[*,errors]", "host.network.errors", "count", "Zabbix network input errors"),
    ("zabbix", "net.if.out[*,errors]", "host.network.errors", "count", "Zabbix network output errors"),
    ("zabbix", "net.tcp.service.perf[*]", "host.network.retrans", "count", "Zabbix TCP service signal"),
    ("prometheus", "node_network_receive_errs_total", "host.network.errors", "count", "Prometheus RX errors"),
    ("prometheus", "node_network_transmit_errs_total", "host.network.errors", "count", "Prometheus TX errors"),
    ("prometheus", "node_netstat_Tcp_RetransSegs", "host.network.retrans", "count", "Prometheus TCP retransmits"),

    # Database
    ("zabbix", "zabbix.host.up", "db.instance.up", "bool", "Zabbix host up proxy"),
    ("prometheus", "up", "db.instance.up", "bool", "Prometheus target up"),
    ("prometheus", "db_instance_up", "db.instance.up", "bool", "Database exporter instance up"),
    ("zabbix", "db.connections.used_pct", "db.connection.used_pct", "%", "Database connection used percent"),
    ("prometheus", "db_connection_used_pct", "db.connection.used_pct", "%", "Database connection used percent"),
    ("zabbix", "db.tablespace.used_pct", "db.tablespace.used_pct", "%", "Database tablespace used percent"),
    ("prometheus", "db_tablespace_used_pct", "db.tablespace.used_pct", "%", "Database tablespace used percent"),
    ("zabbix", "db.replication.lag_seconds", "db.replication.lag_seconds", "s", "Database replication lag"),
    ("prometheus", "db_replication_lag_seconds", "db.replication.lag_seconds", "s", "Database replication lag"),
    ("zabbix", "oracle.fra.used_pct", "oracle.fra.used_pct", "%", "Oracle FRA used percent"),
    ("prometheus", "oracle_fra_used_pct", "oracle.fra.used_pct", "%", "Oracle FRA used percent"),
    ("zabbix", "oracle.dg.lag_seconds", "oracle.dg.lag_seconds", "s", "Oracle Data Guard lag"),
    ("prometheus", "oracle_dg_lag_seconds", "oracle.dg.lag_seconds", "s", "Oracle Data Guard lag"),
    ("zabbix", "mysql.replication.lag_seconds", "mysql.replication.lag_seconds", "s", "MySQL replication lag"),
    ("prometheus", "mysql_replication_lag_seconds", "mysql.replication.lag_seconds", "s", "MySQL replication lag"),
]

DEFAULT_MAPPING_BY_SOURCE_METRIC = {row[1]: row[2] for row in DEFAULT_MAPPINGS}


def normalize_metric_name(source_metric: str | None, mapping_rows: list[Any] | None = None) -> str:
    metric = str(source_metric or "").strip()
    if not metric:
        return "unknown.metric"
    standard_rows = [
        row for row in mapping_rows or []
        if str(getattr(row, "standard_metric", "") or "") in STANDARD_METRICS
    ]
    for row in standard_rows:
        if not getattr(row, "enabled", 1):
            continue
        pattern = str(getattr(row, "source_metric", "")).strip()
        if _metric_matches(metric, pattern):
            return str(getattr(row, "standard_metric", "") or metric)
    for _source_type, pattern, standard_metric, _unit, _description in DEFAULT_MAPPINGS:
        if _metric_matches(metric, pattern):
            return standard_metric
    for row in mapping_rows or []:
        if not getattr(row, "enabled", 1):
            continue
        pattern = str(getattr(row, "source_metric", "")).strip()
        if _metric_matches(metric, pattern):
            return str(getattr(row, "standard_metric", "") or metric)
    return DEFAULT_MAPPING_BY_SOURCE_METRIC.get(metric, metric.replace("_", "."))


def normalize_metric_unit(source_metric: str | None, standard_metric: str | None = None, mapping_rows: list[Any] | None = None) -> str | None:
    metric = str(source_metric or "").strip()
    standard_rows = [
        row for row in mapping_rows or []
        if str(getattr(row, "standard_metric", "") or "") in STANDARD_METRICS
    ]
    for row in standard_rows:
        if not getattr(row, "enabled", 1):
            continue
        if _metric_matches(metric, str(getattr(row, "source_metric", "")).strip()):
            return getattr(row, "metric_unit", None)
    for _source_type, pattern, mapped_metric, unit, _description in DEFAULT_MAPPINGS:
        if mapped_metric == standard_metric or _metric_matches(metric, pattern):
            return unit
    for row in mapping_rows or []:
        if not getattr(row, "enabled", 1):
            continue
        if _metric_matches(metric, str(getattr(row, "source_metric", "")).strip()):
            return getattr(row, "metric_unit", None)
    return None


def transform_metric_value(source_metric: str | None, standard_metric: str, value: Any) -> Any:
    metric = str(source_metric or "").strip()
    if value is None or value == "":
        return value
    try:
        number = float(value)
    except Exception:
        return value
    if metric.startswith("system.cpu.util[") and ",idle" in metric and standard_metric == "host.cpu.used_pct":
        return max(0.0, min(100.0, 100.0 - number))
    if metric == "vm.memory.size[pavailable]" and standard_metric == "host.memory.used_pct":
        return max(0.0, min(100.0, 100.0 - number))
    if metric.startswith("vfs.fs.size[") and metric.endswith(",pfree]") and standard_metric == "host.disk.used_pct":
        return max(0.0, min(100.0, 100.0 - number))
    return value


def infer_metric_from_text(*parts: Any) -> str | None:
    text = " ".join(str(part or "") for part in parts).lower()
    if not text.strip():
        return None
    if "fra" in text or "flash recovery" in text or "fast recovery" in text:
        return "oracle.fra.used_pct"
    if "data guard" in text or "dataguard" in text or "dg lag" in text:
        return "oracle.dg.lag_seconds"
    if "mysql" in text and ("replication" in text or "slave" in text or "replica" in text or "lag" in text):
        return "mysql.replication.lag_seconds"
    if "replication" in text or "replica" in text or "archive gap" in text:
        return "db.replication.lag_seconds"
    if "tablespace" in text or "表空间" in text:
        return "db.tablespace.used_pct"
    if "connection" in text or "connections" in text or "连接" in text:
        return "db.connection.used_pct"
    if "iowait" in text or "io wait" in text or "await" in text or "disk io" in text:
        return "host.io.await_ms"
    if "disk" in text or "filesystem" in text or "fs " in text or "磁盘" in text:
        return "host.disk.used_pct"
    if "memory" in text or "mem " in text or "内存" in text:
        return "host.memory.used_pct"
    if "cpu" in text or "processor" in text:
        return "host.cpu.used_pct"
    if "retrans" in text:
        return "host.network.retrans"
    if "network" in text or "interface" in text or "errors" in text or "网络" in text:
        return "host.network.errors"
    if " down" in text or "unavailable" in text or "up{" in text:
        return "db.instance.up"
    return None


def _metric_matches(metric: str, pattern: str) -> bool:
    if not pattern:
        return False
    if metric == pattern:
        return True
    if "*" not in pattern and "?" not in pattern:
        return False
    regex = "^" + re.escape(pattern).replace("\\*", ".*").replace("\\?", ".") + "$"
    return re.match(regex, metric) is not None


def seed_default_mappings(db) -> int:
    from app.models.observability import ObservabilityMetricMapping

    created = 0
    for source_type, source_metric, standard_metric, metric_unit, description in DEFAULT_MAPPINGS:
        exists = db.query(ObservabilityMetricMapping).filter(
            ObservabilityMetricMapping.source_type == source_type,
            ObservabilityMetricMapping.source_metric == source_metric,
            ObservabilityMetricMapping.standard_metric == standard_metric,
        ).first()
        if exists:
            continue
        db.add(
            ObservabilityMetricMapping(
                source_type=source_type,
                source_metric=source_metric,
                standard_metric=standard_metric,
                metric_unit=metric_unit,
                description=description or "TraceMind AI 智能运维平台 default observability metric mapping",
                enabled=1,
            )
        )
        created += 1
    if created:
        db.commit()
    return created
