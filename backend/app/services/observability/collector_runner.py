from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilityCollectLog, ObservabilityCollectTask, ObservabilitySource
from app.services.observability.source_service import test_source
from app.services.observability import (
    alertmanager_collector,
    elasticsearch_collector,
    nginx_collector,
    prometheus_collector,
    prometheus_standard_service,
    skywalking_collector,
    tomcat_collector,
    zabbix_collector,
)


def run_collect_task(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    source_id = payload.get("source_id")
    source = db.query(ObservabilitySource).filter(ObservabilitySource.id == source_id).first() if source_id else None
    task = ObservabilityCollectTask(
        policy_id=payload.get("policy_id"),
        source_id=source_id,
        task_name=payload.get("task_name") or "手动采集任务",
        collect_type=payload.get("collect_type") or "manual",
        task_status="running",
        start_time=datetime.utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    try:
        if not source:
            raise ValueError("未指定有效数据源，当前任务只记录运行框架。")
        result = _collect_source(db, source, payload)
        ok = result.get("status") in {"success", "partial"}
        task.success_count = 1 if ok else 0
        task.fail_count = 0 if ok else 1
        task.task_status = "success" if ok else "failed"
        task.error_message = None if task.task_status == "success" else result.get("message")
        db.add(
            ObservabilityCollectLog(
                task_id=task.id,
                source_id=source_id,
                log_level="info" if task.task_status == "success" else "error",
                log_type="manual_run",
                message=f"采集任务完成：{task.task_status}",
                raw_data=_json_safe(result),
            )
        )
    except Exception as exc:
        task.task_status = "failed"
        task.fail_count = 1
        task.error_message = str(exc)
        db.add(
            ObservabilityCollectLog(
                task_id=task.id,
                source_id=source_id,
                log_level="error",
                log_type="manual_run",
                message=f"采集任务失败：{exc}",
            )
        )
    finally:
        task.end_time = datetime.utcnow()
        task.duration_ms = int((task.end_time - task.start_time).total_seconds() * 1000) if task.start_time else None
        db.commit()
        db.refresh(task)
    return {
        "id": task.id,
        "task_status": task.task_status,
        "success_count": task.success_count,
        "fail_count": task.fail_count,
        "error_message": task.error_message,
        "duration_ms": task.duration_ms,
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _collect_source(db: Session, source: ObservabilitySource, payload: dict[str, Any]) -> dict[str, Any]:
    collect_type = payload.get("collect_type") or "manual"
    if source.source_type == "zabbix":
        if collect_type == "zabbix_hosts":
            return zabbix_collector.sync_hosts(db, source)
        if collect_type == "zabbix_problems":
            return zabbix_collector.sync_problems(db, source, limit=int(payload.get("limit") or 100))
        if collect_type == "zabbix_metrics":
            return zabbix_collector.sync_metrics(
                db,
                source,
                hostid=payload.get("hostid"),
                limit=int(payload.get("limit") or 100),
            )
        return test_source(db, source)
    if source.source_type == "prometheus":
        if collect_type == "prometheus_standard_metrics":
            return prometheus_standard_service.sync_standard_metrics(db, source)
        return prometheus_collector.collect(db, source, payload)
    if source.source_type == "alertmanager":
        return alertmanager_collector.collect(db, source, payload)
    if source.source_type == "nginx":
        return nginx_collector.collect(db, source, payload)
    if source.source_type == "tomcat_jvm":
        return tomcat_collector.collect(db, source, payload)
    if source.source_type == "skywalking":
        return skywalking_collector.collect(db, source, payload)
    if source.source_type == "elasticsearch":
        return elasticsearch_collector.collect(db, source, payload)
    if source.source_type == "webhook" and not source.api_url:
        return {"status": "success", "message": "Webhook/Alertmanager 接收型数据源已登记，无需主动采集。"}
    return test_source(db, source)
