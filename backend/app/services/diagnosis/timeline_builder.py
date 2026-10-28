"""故障时间线：把诊断过程与多源事件按时间归并。

Phase A：从诊断关键节点 + 活跃告警生成时间线。
Phase B：接入 AWR 快照、Trace、采集器时序事件。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagEvidence, DiagTimelineEvent
from app.models.external_monitoring import (
    PrometheusAlertCache,
    ZabbixHostMapping,
    ZabbixProblemCache,
)
from app.services.diagnosis import constants as C
from app.services.diagnosis.evidence_chain import _external_alert_profile, _external_metric

logger = logging.getLogger("app.services.diagnosis.timeline_builder")


def build_timeline(
    db: Session,
    incident_id: int,
    instance: DBInstance,
    evidences: list[DiagEvidence],
    started_at: datetime,
    signals: list | None = None,
) -> list[DiagTimelineEvent]:
    """生成时间线事件并持久化。"""
    events: list[dict] = []

    events.append({
        "event_time": started_at,
        "layer": C.LAYER_DB,
        "source": "diagnosis",
        "event_type": "start",
        "title": "诊断启动：采集多源证据",
        "severity": "info",
    })

    # 采集器异常信号
    for s in signals or []:
        if getattr(s, "abnormal", False):
            events.append({
                "event_time": getattr(s, "event_time", None) or started_at,
                "layer": s.layer,
                "source": s.source,
                "event_type": "metric",
                "title": s.phenomenon or f"{s.source} {s.metric}",
                "severity": s.severity,
                "detail": {"data_source": (s.detail or {}).get("data_source")},
            })

    # 告警事件（带真实时间）
    prom = (
        db.query(PrometheusAlertCache)
        .filter(PrometheusAlertCache.db_instance_id == instance.id,
                PrometheusAlertCache.alert_status == "firing")
        .limit(30).all()
    )
    for a in prom:
        labels = a.labels_json if isinstance(a.labels_json, dict) else {}
        annotations = a.annotations_json if isinstance(a.annotations_json, dict) else {}
        source_metric = labels.get("metric") or labels.get("__name__") or labels.get("alertname") or a.alert_name
        description = annotations.get("description") or annotations.get("summary")
        metric = _external_metric(source_metric, a.alert_name, description, labels, annotations)
        profile = _external_alert_profile(metric, a.alert_name, description, labels, annotations)
        events.append({
            "event_time": getattr(a, "starts_at", None) or started_at,
            "layer": profile["layer"],
            "source": "prometheus",
            "event_type": "alert",
            "title": f"Prometheus 告警：{a.alert_name}",
            "severity": a.severity or "warning",
            "detail": {"instance_name": a.instance_name, "metric_name": metric},
        })

    hostids = [m.zabbix_hostid for m in
               db.query(ZabbixHostMapping).filter(ZabbixHostMapping.db_instance_id == instance.id).all()]
    if hostids:
        zbx = (
            db.query(ZabbixProblemCache)
            .filter(ZabbixProblemCache.hostid.in_(hostids),
                    ZabbixProblemCache.status.in_(["firing", "active", "problem"]))
            .limit(30).all()
        )
        for z in zbx:
            raw = z.raw_data if isinstance(z.raw_data, dict) else {}
            source_metric = raw.get("item_key") or raw.get("metric_name") or raw.get("key") or raw.get("key_") or z.objectid
            metric = _external_metric(source_metric, z.problem_name, raw.get("opdata"), raw.get("name"), raw)
            profile = _external_alert_profile(metric, z.problem_name, raw.get("opdata"), raw.get("name"), raw)
            events.append({
                "event_time": started_at,
                "layer": profile["layer"],
                "source": "zabbix",
                "event_type": "alert",
                "title": f"Zabbix 问题：{z.problem_name}",
                "severity": "critical" if (z.severity or 0) >= 4 else "warning",
                "detail": {"host_name": z.host_name, "metric_name": metric},
            })

    # 关键证据落点（critical/warning）
    for e in evidences:
        if e.severity in ("critical", "warning"):
            events.append({
                "event_time": e.create_time or started_at,
                "layer": e.layer,
                "source": e.source,
                "event_type": "evidence",
                "title": e.phenomenon or "证据",
                "severity": e.severity,
            })

    events.sort(key=lambda x: x["event_time"] or started_at)

    saved = []
    for ev in events:
        row = DiagTimelineEvent(
            incident_id=incident_id,
            event_time=ev.get("event_time"),
            layer=ev.get("layer"),
            source=ev.get("source"),
            event_type=ev.get("event_type"),
            title=ev["title"],
            detail=ev.get("detail"),
            severity=ev.get("severity", "info"),
        )
        db.add(row)
        saved.append(row)
    db.flush()
    return saved


def to_dict(row: DiagTimelineEvent) -> dict:
    return {
        "id": row.id,
        "event_time": row.event_time.isoformat(sep=" ") if row.event_time else None,
        "layer": row.layer,
        "source": row.source,
        "event_type": row.event_type,
        "title": row.title,
        "detail": row.detail,
        "severity": row.severity,
    }
