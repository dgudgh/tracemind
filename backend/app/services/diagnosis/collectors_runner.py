"""采集器调度：按 diag_data_source 运行匹配采集器，聚合 Signal。"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagDataSource
from app.collectors.diag.base import CollectContext, Signal, LAYER_APP
from app.collectors.diag import (
    kafka as kafka_collector,
    nginx as nginx_collector,
    os_metrics as os_collector,
    redis as redis_collector,
    skywalking as sw_collector,
    storage as storage_collector,
    tomcat as tomcat_collector,
)

logger = logging.getLogger("app.services.diagnosis.collectors_runner")

REGISTRY = {
    "nginx": nginx_collector.collect,
    "tomcat": tomcat_collector.collect,
    "redis": redis_collector.collect,
    "kafka": kafka_collector.collect,
    "skywalking": sw_collector.collect,
    "os": os_collector.collect,
    "storage": storage_collector.collect,
}


def _decrypt_auth(row: DiagDataSource) -> dict:
    if not row.auth_cipher:
        return {}
    try:
        return json.loads(decrypt_password(row.auth_cipher)) or {}
    except Exception as e:
        logger.info("decrypt auth failed for ds %s: %s", row.id, e)
        return {}


def _context(row: DiagDataSource, instance: DBInstance | None) -> CollectContext:
    return CollectContext(
        name=row.name,
        source_type=row.source_type,
        endpoint=row.endpoint,
        auth=_decrypt_auth(row),
        config=row.config_json or {},
        instance=instance,
    )


def list_sources(db: Session, instance_id: int) -> list[DiagDataSource]:
    """该实例绑定的 + 未绑定（全局）的启用数据源。"""
    return (
        db.query(DiagDataSource)
        .filter(
            DiagDataSource.enabled == 1,
            (DiagDataSource.instance_id == instance_id) | (DiagDataSource.instance_id.is_(None)),
        )
        .all()
    )


def run_collectors(db: Session, instance: DBInstance) -> list[Signal]:
    """运行该实例所有数据源采集器，聚合 Signal。逐源容错。"""
    signals: list[Signal] = []
    for row in list_sources(db, instance.id):
        fn = REGISTRY.get(row.source_type)
        if not fn:
            continue
        try:
            sigs = fn(_context(row, instance)) or []
            for s in sigs:
                s.detail = {**(s.detail or {}), "data_source": row.name, "data_source_id": row.id}
            signals.extend(sigs)
        except Exception as e:
            logger.warning("collector %s (%s) failed: %s", row.source_type, row.name, e)
            signals.append(Signal(
                LAYER_APP,
                row.source_type,
                "collector_error",
                0,
                status="warn",
                node_type=row.source_type,
                phenomenon=f"{row.source_type} 采集器异常：{row.name}",
                judgement="采集器执行失败，当前数据源无法提供端到端诊断证据。",
                impact="证据链会缺少该中间件或主机层信号，可能降低根因定位准确度。",
                suggestion="检查数据源 endpoint、认证信息、网络连通性和采集器配置。",
                detail={"data_source": row.name, "data_source_id": row.id, "error": str(e)},
            ))
    return signals


def test_data_source(db: Session, row: DiagDataSource) -> dict:
    """连通性测试：运行一次采集，返回结果概要。"""
    instance = None
    if row.instance_id:
        instance = db.query(DBInstance).filter(DBInstance.id == row.instance_id).first()
    fn = REGISTRY.get(row.source_type)
    if not fn:
        return {"ok": False, "message": f"不支持的数据源类型：{row.source_type}"}
    try:
        sigs = fn(_context(row, instance)) or []
        reach = [s for s in sigs if s.metric == "reachable"]
        ok = not any(s.status == "warn" and s.metric == "reachable" for s in reach) and bool(sigs)
        return {
            "ok": ok,
            "message": "采集成功" if ok else "采集为空或不可达",
            "signal_count": len(sigs),
            "signals": [{"metric": s.metric, "value": s.value, "status": s.status, "phenomenon": s.phenomenon} for s in sigs[:10]],
        }
    except Exception as e:
        return {"ok": False, "message": f"采集异常：{e}"}
