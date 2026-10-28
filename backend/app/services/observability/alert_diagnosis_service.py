from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.alert_event import DBAlertActionLog, DBAlertEvent
from app.models.db_instance import DBInstance
from app.models.observability import ObservabilityMetricHistory
from app.models.oracle_awr import AwrReport


CORE_METRICS = [
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


def metric_trends(db: Session, event: DBAlertEvent, window_minutes: int = 120) -> dict[str, Any]:
    start, end = _event_window(event, window_minutes)
    metrics = [event.metric_name] if event.metric_name else []
    metrics += [item for item in CORE_METRICS if item not in metrics]
    query = db.query(ObservabilityMetricHistory).filter(
        ObservabilityMetricHistory.metric_time >= start,
        ObservabilityMetricHistory.metric_time <= end,
        ObservabilityMetricHistory.standard_metric.in_(metrics),
    )
    if event.instance_id:
        query = query.filter(ObservabilityMetricHistory.instance_id == event.instance_id)
    elif event.object_name:
        query = query.filter(ObservabilityMetricHistory.host_id == event.object_name)
    rows = query.order_by(ObservabilityMetricHistory.metric_time).limit(2000).all()
    series: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        series.setdefault(row.standard_metric, []).append(
            {
                "time": row.metric_time,
                "value": _float(row.metric_value),
                "unit": row.metric_unit,
                "source_type": row.source_type,
                "host_id": row.host_id,
                "instance_id": row.instance_id,
            }
        )
    return {
        "event_id": event.id,
        "window": {"start": start, "end": end, "minutes": window_minutes},
        "series": series,
        "series_count": len(series),
        "point_count": sum(len(items) for items in series.values()),
    }


def build_evidence_chain(db: Session, event: DBAlertEvent, window_minutes: int = 120) -> dict[str, Any]:
    trend = metric_trends(db, event, window_minutes)
    instance = db.query(DBInstance).filter(DBInstance.id == event.instance_id).first() if event.instance_id else None
    related_alerts = _related_alerts(db, event, window_minutes)
    awr = _latest_awr_evidence(db, event.instance_id) if event.instance_id else []
    metric_evidences = _metric_evidences(trend["series"])
    root_cause = _infer_root_cause(event, metric_evidences, awr)
    evidence = [
        {
            "source": event.source,
            "layer": _metric_layer(event.metric_name),
            "severity": event.severity,
            "phenomenon": event.title,
            "evidence": event.message or event.title,
            "metric_name": event.metric_name,
            "metric_value": _float(event.metric_value),
            "time": event.last_seen,
        },
        *metric_evidences,
        *awr,
        *related_alerts,
    ]
    return {
        "event_id": event.id,
        "event_uid": event.event_uid,
        "instance": {
            "id": instance.id,
            "name": instance.instance_name,
            "db_type": instance.db_type,
            "host": instance.host,
            "system_name": instance.system_name,
        } if instance else None,
        "window": trend["window"],
        "evidence": evidence,
        "root_cause": root_cause,
        "summary": _summary(event, instance, root_cause, evidence),
        "trend": trend,
    }


def ticket_description(db: Session, event: DBAlertEvent, window_minutes: int = 120) -> dict[str, Any]:
    chain = build_evidence_chain(db, event, window_minutes)
    instance = chain.get("instance") or {}
    lines = [
        f"告警标题：{event.title}",
        f"告警来源：{event.source}",
        f"严重级别：{event.severity}",
        f"状态：{event.status}",
        f"实例：{instance.get('name') or event.instance_id or '-'}",
        f"数据库类型：{instance.get('db_type') or event.db_type or '-'}",
        f"主机/对象：{event.object_name or '-'}",
        f"指标：{event.metric_name or '-'}",
        f"当前值：{event.metric_value or '-'}",
        f"发生时间：{event.first_seen}",
        f"最近时间：{event.last_seen}",
        "",
        "初步根因判断：",
        chain["root_cause"]["summary"],
        "",
        "证据链：",
    ]
    for index, item in enumerate(chain["evidence"][:12], 1):
        lines.append(f"{index}. [{item.get('source')}/{item.get('layer')}] {item.get('phenomenon')}")
        if item.get("evidence"):
            lines.append(f"   证据：{item.get('evidence')}")
    lines.extend([
        "",
        "建议动作：",
        chain["root_cause"]["suggestion"],
        "",
        "执行策略：plan_only，需人工确认后执行变更。",
    ])
    return {
        "event_id": event.id,
        "title": f"[{event.severity}] {event.title}",
        "description": "\n".join(lines),
        "priority": "高" if event.severity in {"critical", "high"} else "中" if event.severity == "warning" else "低",
        "plan_only": True,
    }


def run_diagnosis_from_event(db: Session, event: DBAlertEvent) -> dict[str, Any]:
    if not event.instance_id:
        raise ValueError("告警未绑定数据库实例，无法触发诊断中心")
    from app.services.diagnosis import orchestrator

    detail = orchestrator.run_diagnosis(
        db,
        event.instance_id,
        title=f"告警触发诊断：{event.title}",
        trigger_source=f"alert_event:{event.id}",
    )
    db.add(
        DBAlertActionLog(
            event_id=event.id,
            action_type="diagnosis",
            action_status="success",
            target="diagnosis-center",
            request_json=json.dumps({"event_uid": event.event_uid, "instance_id": event.instance_id}, ensure_ascii=False),
            response_json=json.dumps({"incident_id": detail.get("id"), "incident_no": detail.get("incident_no")}, ensure_ascii=False),
        )
    )
    db.commit()
    return detail


def _event_window(event: DBAlertEvent, window_minutes: int) -> tuple[datetime, datetime]:
    center = event.last_seen or event.first_seen or datetime.utcnow()
    return center - timedelta(minutes=window_minutes), center + timedelta(minutes=max(30, window_minutes // 2))


def _related_alerts(db: Session, event: DBAlertEvent, window_minutes: int) -> list[dict[str, Any]]:
    start, end = _event_window(event, window_minutes)
    query = db.query(DBAlertEvent).filter(
        DBAlertEvent.id != event.id,
        DBAlertEvent.last_seen >= start,
        DBAlertEvent.last_seen <= end,
    )
    if event.instance_id:
        query = query.filter(DBAlertEvent.instance_id == event.instance_id)
    elif event.object_name:
        query = query.filter(DBAlertEvent.object_name == event.object_name)
    rows = query.order_by(DBAlertEvent.last_seen.desc()).limit(20).all()
    return [
        {
            "source": row.source,
            "layer": _metric_layer(row.metric_name),
            "severity": row.severity,
            "phenomenon": f"同时间窗关联告警：{row.title}",
            "evidence": row.message,
            "metric_name": row.metric_name,
            "metric_value": _float(row.metric_value),
            "time": row.last_seen,
        }
        for row in rows
    ]


def _latest_awr_evidence(db: Session, instance_id: int | None) -> list[dict[str, Any]]:
    if not instance_id:
        return []
    report = (
        db.query(AwrReport)
        .filter(AwrReport.instance_id == instance_id, AwrReport.analysis_results.isnot(None))
        .order_by(AwrReport.id.desc())
        .first()
    )
    if not report or not isinstance(report.analysis_results, dict):
        return []
    top_events = ((report.analysis_results.get("wait_analysis") or {}).get("top_events") or [])[:5]
    result = []
    for item in top_events:
        if not isinstance(item, dict):
            continue
        name = item.get("event_name")
        if not name:
            continue
        pct = _float(item.get("db_time_pct")) or 0
        result.append({
            "source": "awr",
            "layer": "database",
            "severity": "critical" if pct >= 30 else "warning" if pct >= 15 else "info",
            "phenomenon": f"AWR 等待事件：{name}",
            "evidence": f"DB Time 占比 {pct:.1f}%，AWR 报告 #{report.id}",
            "metric_name": name,
            "metric_value": pct,
            "time": report.create_time,
        })
    return result


def _metric_evidences(series: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for metric, points in series.items():
        values = [item["value"] for item in points if item.get("value") is not None]
        if not values:
            continue
        latest = values[-1]
        peak = max(values)
        result.append({
            "source": "metric_history",
            "layer": _metric_layer(metric),
            "severity": _metric_severity(metric, peak),
            "phenomenon": f"指标趋势：{metric}",
            "evidence": f"点数 {len(values)}，最新 {latest:.2f}，峰值 {peak:.2f}",
            "metric_name": metric,
            "metric_value": latest,
        })
    return result


def _infer_root_cause(event: DBAlertEvent, evidences: list[dict[str, Any]], awr: list[dict[str, Any]]) -> dict[str, str]:
    event_text = f"{event.title} {event.metric_name or ''} {event.message or ''}".lower()
    primary = _classify_root_cause(event_text)
    if primary:
        return primary

    abnormal_text = " ".join(
        str(item.get("metric_name") or item.get("phenomenon") or item.get("evidence") or "").lower()
        for item in evidences + awr
        if item.get("severity") in {"critical", "warning"}
    )
    contextual = _classify_root_cause(abnormal_text)
    if contextual:
        return contextual

    names = " ".join(str(item.get("metric_name") or item.get("phenomenon") or "").lower() for item in evidences + awr)
    text = f"{event_text} {names}"
    fallback = _classify_root_cause(text)
    if fallback:
        return fallback
    return {
        "category": "unknown",
        "summary": "当前证据不足以唯一定位根因，建议结合指标趋势、同时间窗告警和数据库诊断结果继续排查。",
        "suggestion": "优先确认实例绑定是否准确，再补采主机指标、数据库指标和 AWR/慢 SQL 证据。",
    }


def _classify_root_cause(text: str) -> dict[str, str] | None:
    if not text.strip():
        return None
    if any(key in text for key in ("oracle.fra", " fra", "flash recovery", "fast recovery", "recovery area", "db_recovery_file_dest", "归档区", "恢复区")):
        return {
            "category": "oracle_fra",
            "summary": "Oracle FRA/归档恢复区容量压力较高，可能影响归档、备份、闪回和 Data Guard 日志传输。",
            "suggestion": "检查 V$RECOVERY_FILE_DEST、归档生成速率、备份保留策略和可清理归档；必要时扩容 db_recovery_file_dest_size。",
        }
    if any(key in text for key in ("tablespace", "db.tablespace", "表空间", "datafile", "数据文件")):
        return {
            "category": "tablespace_capacity",
            "summary": "数据库表空间容量压力较高，可能导致对象扩展失败或业务写入异常。",
            "suggestion": "检查表空间使用率、数据文件 autoextend、剩余磁盘空间和 Top Segment 增长；按变更流程扩容或清理。",
        }
    if any(key in text for key in ("oracle.dg", "data guard", "dataguard", "archive gap", "replication", "replica", " lag", "lag_seconds")):
        return {
            "category": "replication",
            "summary": "复制/同步延迟可能性较高，需要检查主备链路、归档传输和应用进程。",
            "suggestion": "检查 Data Guard/MRP、MySQL replication、网络延迟、归档 gap 和从库应用速率。",
        }
    if any(key in text for key in ("connection", "connections", "db.connection", "processes", "sessions", "连接")):
        return {
            "category": "connection_pool",
            "summary": "数据库连接或会话使用率偏高，可能由连接池泄漏、突增并发或阻塞堆积引起。",
            "suggestion": "检查连接池配置、活跃会话、阻塞链和异常应用来源，必要时先限流再释放异常连接。",
        }
    if any(key in text for key in ("io.await", "disk", "i/o", "log file sync", "redo")):
        return {
            "category": "storage_io",
            "summary": "主机磁盘 IO 或 Redo 写入延迟可能性较高，需要结合 AWR 等待事件和磁盘 await 继续定位。",
            "suggestion": "检查存储延迟、Redo 日志盘、归档/FRA 空间、数据库 log file sync/log file parallel write 等等待事件。",
        }
    if any(key in text for key in ("cpu", "load")):
        return {
            "category": "cpu",
            "summary": "CPU 资源紧张可能性较高，需要定位高 CPU SQL、并发任务和主机负载。",
            "suggestion": "查看 CPU 趋势、AWR DB CPU、Top SQL、操作系统进程和批任务窗口。",
        }
    if any(key in text for key in ("memory", "swap")):
        return {
            "category": "memory",
            "summary": "内存压力可能性较高，需要确认可用内存、Swap、数据库连接数和 PGA/Buffer 使用。",
            "suggestion": "检查内存使用率、Swap in/out、数据库连接池和异常大 SQL 工作区。",
        }
    return None


def _summary(event: DBAlertEvent, instance: DBInstance | None, root_cause: dict[str, str], evidence: list[dict[str, Any]]) -> str:
    instance_name = instance.instance_name if instance else "未绑定实例"
    return f"{event.source} 告警关联到 {instance_name}，证据 {len(evidence)} 条。{root_cause['summary']}"


def _metric_layer(metric_name: str | None) -> str:
    metric = str(metric_name or "").lower()
    if metric.startswith("host.network"):
        return "network"
    if metric.startswith("host."):
        return "os"
    if metric.startswith("db.") or metric.startswith("oracle.") or metric.startswith("mysql."):
        return "database"
    return "observability"


def _metric_severity(metric: str, value: float) -> str:
    if metric.endswith("used_pct"):
        if value >= 90:
            return "critical"
        if value >= 80:
            return "warning"
    if metric.endswith("await_ms") and value >= 50:
        return "critical"
    if metric.endswith("await_ms") and value >= 20:
        return "warning"
    if metric.endswith("lag_seconds") and value >= 300:
        return "critical"
    if metric.endswith("lag_seconds") and value >= 60:
        return "warning"
    return "info"


def _float(value: Any) -> float | None:
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return None
