from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url
from app.models.alert_event import DBAlertEvent
from app.models.db_instance import DBInstance
from app.models.external_monitoring import PrometheusAlertCache, ZabbixHostMapping, ZabbixProblemCache
from app.models.observability import (
    ObservabilityAlertEvent,
    ObservabilityCurrentMetric,
    ObservabilityInstanceBinding,
    ObservabilityMetricHistory,
    ObservabilitySource,
)
from app.services.observability.binding_service import binding_to_dict


SOURCE_TYPES = ("zabbix", "prometheus", "alertmanager")


def get_linkage_health(
    db: Session,
    *,
    instance_id: int,
    probe_sources: bool = False,
    probe_timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        raise ValueError("database instance not found")

    bindings = (
        db.query(ObservabilityInstanceBinding)
        .filter(ObservabilityInstanceBinding.instance_id == instance.id, ObservabilityInstanceBinding.enabled == 1)
        .order_by(ObservabilityInstanceBinding.confidence.desc(), ObservabilityInstanceBinding.id.desc())
        .all()
    )
    legacy_zabbix = (
        db.query(ZabbixHostMapping)
        .filter(ZabbixHostMapping.db_instance_id == instance.id, ZabbixHostMapping.enabled == 1)
        .order_by(ZabbixHostMapping.id.desc())
        .all()
    )
    sources = {
        source_type: db.query(ObservabilitySource)
        .filter(ObservabilitySource.source_type == source_type, ObservabilitySource.enabled == 1)
        .order_by(ObservabilitySource.id.desc())
        .all()
        for source_type in SOURCE_TYPES
    }
    evidence = _recent_evidence(db, instance, bindings, legacy_zabbix)

    checks = [
        _binding_check(bindings),
        _source_check("zabbix_source", "Zabbix 数据源", sources["zabbix"]),
        _source_check("prometheus_source", "Prometheus 数据源", sources["prometheus"]),
        _source_check("alertmanager_source", "Alertmanager 数据源", sources["alertmanager"]),
        _zabbix_mapping_check(bindings, legacy_zabbix),
        _prometheus_mapping_check(bindings),
        _recent_evidence_check(evidence),
    ]

    source_probes = []
    if probe_sources:
        timeout = max(0.5, min(float(probe_timeout_seconds or 2.0), 5.0))
        for rows in sources.values():
            source_probes.extend(_probe_source(row, timeout) for row in rows)
        checks.append(_source_probe_check(source_probes))

    recommendations = _recommendations(checks, source_probes)
    overall_status = _overall_status(checks)

    return {
        "instance": _instance_payload(instance),
        "overall_status": overall_status,
        "summary": _summary(overall_status, checks, source_probes),
        "checks": checks,
        "recommendations": recommendations,
        "bindings": [binding_to_dict(row) for row in bindings],
        "legacy_zabbix_mappings": [_legacy_mapping_payload(row) for row in legacy_zabbix],
        "sources": {key: [_source_payload(row) for row in rows] for key, rows in sources.items()},
        "source_probes": source_probes,
        "recent_evidence": evidence,
        "probe_sources": bool(probe_sources),
        "probe_timeout_seconds": probe_timeout_seconds,
        "checked_at": datetime.utcnow(),
    }


def _instance_payload(instance: DBInstance) -> dict[str, Any]:
    return {
        "id": instance.id,
        "instance_name": instance.instance_name,
        "db_type": instance.db_type,
        "host": instance.host,
        "port": instance.port,
        "system_name": instance.system_name,
        "ssh_host": instance.ssh_host,
        "enabled": instance.enabled,
    }


def _source_payload(row: ObservabilitySource) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_name": row.source_name,
        "source_type": row.source_type,
        "api_url": row.api_url,
        "auth_type": row.auth_type,
        "enabled": row.enabled,
        "timeout_seconds": row.timeout_seconds,
        "collect_interval_seconds": row.collect_interval_seconds,
        "has_password": bool(row.password_encrypted),
        "has_token": bool(row.token_encrypted),
        "remark": row.remark,
        "update_time": row.update_time,
    }


def _legacy_mapping_payload(row: ZabbixHostMapping) -> dict[str, Any]:
    return {
        "id": row.id,
        "zabbix_config_id": row.zabbix_config_id,
        "zabbix_hostid": row.zabbix_hostid,
        "zabbix_host_name": row.zabbix_host_name,
        "visible_name": row.visible_name,
        "ip_address": row.ip_address,
        "db_instance_id": row.db_instance_id,
        "enabled": row.enabled,
    }


def _binding_check(bindings: list[ObservabilityInstanceBinding]) -> dict[str, Any]:
    if not bindings:
        return _check("binding", "实例绑定", "failed", "未找到启用的实例绑定", {"binding_count": 0})
    precise = [
        row
        for row in bindings
        if row.zabbix_hostid or row.zabbix_host_name or row.prometheus_target or row.zabbix_source_id or row.prometheus_source_id
    ]
    if precise:
        return _check("binding", "实例绑定", "ok", f"已配置 {len(bindings)} 条启用绑定", {"binding_count": len(bindings)})
    return _check("binding", "实例绑定", "warning", "只有主机/IP 级绑定，缺少 Zabbix Host 或 Prometheus Target", {"binding_count": len(bindings)})


def _source_check(key: str, label: str, sources: list[ObservabilitySource]) -> dict[str, Any]:
    if not sources:
        return _check(key, label, "warning", "未配置启用的数据源", {"source_count": 0})
    with_url = [row for row in sources if row.api_url]
    if with_url:
        return _check(key, label, "ok", f"已配置 {len(sources)} 个启用数据源", {"source_count": len(sources)})
    return _check(key, label, "warning", "数据源已登记，但缺少访问地址", {"source_count": len(sources)})


def _zabbix_mapping_check(bindings: list[ObservabilityInstanceBinding], legacy: list[ZabbixHostMapping]) -> dict[str, Any]:
    mapped = [row for row in bindings if row.zabbix_hostid or row.zabbix_host_name]
    if mapped or legacy:
        return _check(
            "zabbix_mapping",
            "Zabbix 绑定",
            "ok",
            f"已找到 {len(mapped) + len(legacy)} 条 Zabbix Host 映射",
            {"binding_mapping_count": len(mapped), "legacy_mapping_count": len(legacy)},
        )
    return _check(
        "zabbix_mapping",
        "Zabbix 绑定",
        "warning",
        "未配置 Zabbix HostID / 主机名映射",
        {"binding_mapping_count": 0, "legacy_mapping_count": 0},
    )


def _prometheus_mapping_check(bindings: list[ObservabilityInstanceBinding]) -> dict[str, Any]:
    mapped = [row for row in bindings if row.prometheus_target]
    if mapped:
        return _check(
            "prometheus_mapping",
            "Prometheus Target",
            "ok",
            f"已找到 {len(mapped)} 条 Prometheus Target 映射",
            {"target_count": len(mapped), "targets": [row.prometheus_target for row in mapped]},
        )
    return _check(
        "prometheus_mapping",
        "Prometheus Target",
        "warning",
        "未配置 Prometheus Target 映射",
        {"target_count": 0},
    )


def _recent_evidence_check(evidence: dict[str, Any]) -> dict[str, Any]:
    total = int(evidence.get("total_count") or 0)
    if total > 0:
        return _check("recent_evidence", "最近证据", "ok", f"最近 24 小时有 {total} 条告警/指标证据", evidence)
    return _check("recent_evidence", "最近证据", "warning", "最近 24 小时未发现该实例的外部告警或指标证据", evidence)


def _source_probe_check(source_probes: list[dict[str, Any]]) -> dict[str, Any]:
    if not source_probes:
        return _check("source_probe", "源连通性", "warning", "没有可探测的数据源", {"probe_count": 0})
    failed = [item for item in source_probes if item.get("status") == "failed"]
    if failed:
        return _check("source_probe", "源连通性", "failed", f"{len(failed)} 个数据源连通性异常", {"failed_count": len(failed)})
    unknown = [item for item in source_probes if item.get("status") != "success"]
    if unknown:
        return _check("source_probe", "源连通性", "warning", f"{len(unknown)} 个数据源未完成探测", {"unknown_count": len(unknown)})
    return _check("source_probe", "源连通性", "ok", "所有数据源连通性正常", {"probe_count": len(source_probes)})


def _recent_evidence(
    db: Session,
    instance: DBInstance,
    bindings: list[ObservabilityInstanceBinding],
    legacy_zabbix: list[ZabbixHostMapping],
) -> dict[str, Any]:
    since = datetime.utcnow() - timedelta(hours=24)
    zabbix_hostids = {row.zabbix_hostid for row in bindings if row.zabbix_hostid}
    zabbix_hostids.update(row.zabbix_hostid for row in legacy_zabbix if row.zabbix_hostid)
    prometheus_targets = {row.prometheus_target for row in bindings if row.prometheus_target}
    host_values = {instance.host, instance.ssh_host, instance.instance_name, instance.system_name}
    host_values.update(zabbix_hostids)
    host_values.update(prometheus_targets)
    host_values = {str(item) for item in host_values if item}

    obs_alert_count = _count(
        db.query(ObservabilityAlertEvent).filter(
            ObservabilityAlertEvent.instance_id == instance.id,
            ObservabilityAlertEvent.last_time >= since,
        )
    )
    unified_alert_count = _count(
        db.query(DBAlertEvent).filter(
            DBAlertEvent.instance_id == instance.id,
            DBAlertEvent.last_seen >= since,
            DBAlertEvent.source.in_(["zabbix", "prometheus", "alertmanager", "observability"]),
        )
    )
    current_metric_count = _count(
        db.query(ObservabilityCurrentMetric).filter(ObservabilityCurrentMetric.instance_id == instance.id)
    )
    history_metric_count = _count(
        db.query(ObservabilityMetricHistory).filter(
            ObservabilityMetricHistory.instance_id == instance.id,
            ObservabilityMetricHistory.metric_time >= since,
        )
    )
    prom_alert_count = _count(
        db.query(PrometheusAlertCache).filter(
            or_(
                PrometheusAlertCache.db_instance_id == instance.id,
                PrometheusAlertCache.instance_name.in_(host_values) if host_values else False,
            ),
            PrometheusAlertCache.update_time >= since,
        )
    )
    zbx_problem_count = 0
    if zabbix_hostids:
        zbx_problem_count = _count(
            db.query(ZabbixProblemCache).filter(
                ZabbixProblemCache.hostid.in_(zabbix_hostids),
                ZabbixProblemCache.sync_time >= since,
            )
        )
    last_times = [
        _max_time(db, ObservabilityAlertEvent.last_time, ObservabilityAlertEvent.instance_id == instance.id),
        _max_time(db, DBAlertEvent.last_seen, DBAlertEvent.instance_id == instance.id),
        _max_time(db, ObservabilityMetricHistory.metric_time, ObservabilityMetricHistory.instance_id == instance.id),
    ]
    if zabbix_hostids:
        last_times.append(_max_time(db, ZabbixProblemCache.sync_time, ZabbixProblemCache.hostid.in_(zabbix_hostids)))
    if host_values:
        last_times.append(_max_time(db, PrometheusAlertCache.update_time, PrometheusAlertCache.instance_name.in_(host_values)))
    last_time = max([item for item in last_times if item], default=None)

    counts = {
        "observability_alerts": obs_alert_count,
        "unified_alerts": unified_alert_count,
        "current_metrics": current_metric_count,
        "history_metrics_24h": history_metric_count,
        "prometheus_alert_cache_24h": prom_alert_count,
        "zabbix_problem_cache_24h": zbx_problem_count,
    }
    return {
        "window_hours": 24,
        "counts": counts,
        "total_count": sum(counts.values()),
        "last_time": last_time,
        "matched_hosts": sorted(host_values),
    }


def _probe_source(source: ObservabilitySource, timeout_seconds: float) -> dict[str, Any]:
    started = datetime.utcnow()
    if not source.api_url:
        return {
            "source_id": source.id,
            "source_name": source.source_name,
            "source_type": source.source_type,
            "status": "unknown",
            "message": "未配置访问地址",
        }
    url = source.api_url
    headers = {"Accept": "application/json"}
    headers.update(_auth_headers(source))
    payload = None
    method = "GET"
    if source.source_type == "zabbix":
        payload = json.dumps({"jsonrpc": "2.0", "method": "apiinfo.version", "params": {}, "id": 1}).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    try:
        url = _probe_url(source)
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read(512).decode("utf-8", errors="ignore")
            elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
            return {
                "source_id": source.id,
                "source_name": source.source_name,
                "source_type": source.source_type,
                "api_url": source.api_url,
                "probe_url": url,
                "status": "success",
                "message": f"HTTP {resp.status}",
                "http_status": resp.status,
                "elapsed_ms": elapsed,
                "sample": body[:200],
            }
    except Exception as exc:
        elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
        return {
            "source_id": source.id,
            "source_name": source.source_name,
            "source_type": source.source_type,
            "api_url": source.api_url,
            "probe_url": url,
            "status": "failed",
            "message": str(exc),
            "elapsed_ms": elapsed,
        }


def _probe_url(source: ObservabilitySource) -> str:
    base = (source.api_url or "").rstrip("/")
    if source.source_type == "prometheus":
        return require_http_url(f"{base}/-/ready")
    if source.source_type == "alertmanager":
        return require_http_url(f"{base}/-/ready")
    return require_http_url(base)


def _auth_headers(source: ObservabilitySource) -> dict[str, str]:
    auth_type = (source.auth_type or "").lower()
    headers: dict[str, str] = {}
    token = decrypt_password(source.token_encrypted) if source.token_encrypted else None
    if token and auth_type in {"bearer", "token", "api_key"}:
        headers["Authorization"] = f"Bearer {token}"
    if auth_type == "basic" and source.username and source.password_encrypted:
        password = decrypt_password(source.password_encrypted)
        encoded = base64.b64encode(f"{source.username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def _recommendations(checks: list[dict[str, Any]], source_probes: list[dict[str, Any]]) -> list[str]:
    by_key = {item["key"]: item for item in checks}
    recommendations: list[str] = []
    if by_key.get("binding", {}).get("status") == "failed":
        recommendations.append("先在实例绑定中补齐数据库实例与主机 IP/主机名的关系。")
    if by_key.get("zabbix_mapping", {}).get("status") != "ok":
        recommendations.append("补齐 Zabbix HostID 或 Zabbix 主机名映射，确保 Problem 能关联到数据库实例。")
    if by_key.get("prometheus_mapping", {}).get("status") != "ok":
        recommendations.append("补齐 Prometheus Target，例如 node_exporter 或数据库 exporter 的 host:port。")
    if by_key.get("recent_evidence", {}).get("status") != "ok":
        recommendations.append("同步或接入最近的 Zabbix/Prometheus 告警与指标，否则联动分析只能用静态绑定判断。")
    failed_probes = [item for item in source_probes if item.get("status") == "failed"]
    if failed_probes:
        names = "、".join(f"{item.get('source_name')}({item.get('source_type')})" for item in failed_probes[:4])
        recommendations.append(f"检查外部监控 HTTP 服务连通性：{names}。")
    if not recommendations:
        recommendations.append("当前联动配置可用，建议定期保留真实告警与指标样本用于诊断校验。")
    return recommendations


def _summary(overall_status: str, checks: list[dict[str, Any]], source_probes: list[dict[str, Any]]) -> str:
    failed = [item for item in checks if item.get("status") == "failed"]
    warnings = [item for item in checks if item.get("status") == "warning"]
    if overall_status == "failed":
        return f"联动异常：{len(failed)} 项失败，优先处理源连通性或实例绑定。"
    if warnings:
        return f"联动可识别但不完整：{len(warnings)} 项需要补齐。"
    if source_probes:
        return "联动配置、证据和源连通性均正常。"
    return "联动配置和证据检查正常，源连通性尚未主动探测。"


def _overall_status(checks: list[dict[str, Any]]) -> str:
    if any(item.get("status") == "failed" for item in checks):
        return "failed"
    if any(item.get("status") == "warning" for item in checks):
        return "warning"
    return "ok"


def _check(key: str, label: str, status: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "message": message,
        "details": details or {},
    }


def _count(query) -> int:
    return int(query.count() or 0)


def _max_time(db: Session, column, *filters) -> datetime | None:
    query = db.query(func.max(column))
    for item in filters:
        query = query.filter(item)
    return query.scalar()
