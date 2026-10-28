"""自动诊断触发：告警/定时 → 自动建 incident → 端到端诊断（无人值守闭环）。

由 master_scheduler 每分钟调用 run_due_auto_diagnosis()，按 diag_auto_policy 评估触发。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagAutoPolicy
from app.models.capacity_policy import DBCapacityAlert
from app.models.external_monitoring import (
    PrometheusAlertCache,
    ZabbixHostMapping,
    ZabbixProblemCache,
)

logger = logging.getLogger("app.services.diagnosis.auto_trigger")


def get_or_create_policy(db: Session, instance_id: int) -> DiagAutoPolicy:
    policy = db.query(DiagAutoPolicy).filter(DiagAutoPolicy.instance_id == instance_id).first()
    if policy:
        return policy
    policy = DiagAutoPolicy(instance_id=instance_id)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def update_policy(db: Session, instance_id: int, data: dict) -> DiagAutoPolicy:
    policy = get_or_create_policy(db, instance_id)
    for k in ("enabled", "mode", "interval_minutes", "cooldown_minutes", "min_severity", "auto_ticket", "auto_notify"):
        if k in data and data[k] is not None:
            setattr(policy, k, data[k])
    # 调整后立即可评估
    policy.next_run_time = None
    db.commit()
    db.refresh(policy)
    return policy


def policy_to_dict(p: DiagAutoPolicy) -> dict:
    return {
        "instance_id": p.instance_id,
        "enabled": p.enabled,
        "mode": p.mode,
        "interval_minutes": p.interval_minutes,
        "cooldown_minutes": p.cooldown_minutes,
        "min_severity": p.min_severity,
        "auto_ticket": p.auto_ticket,
        "auto_notify": p.auto_notify,
        "next_run_time": p.next_run_time.isoformat(sep=" ") if p.next_run_time else None,
        "last_run_at": p.last_run_at.isoformat(sep=" ") if p.last_run_at else None,
        "last_incident_id": p.last_incident_id,
        "last_trigger": p.last_trigger,
    }


def _active_alert_count(db: Session, instance_id: int, min_severity: str) -> int:
    """该实例当前活跃告警数（Prometheus + Zabbix + 容量），按阈值过滤。"""
    crit_only = min_severity == "critical"
    count = 0

    prom_q = db.query(PrometheusAlertCache).filter(
        PrometheusAlertCache.db_instance_id == instance_id,
        PrometheusAlertCache.alert_status == "firing",
    )
    if crit_only:
        prom_q = prom_q.filter(PrometheusAlertCache.severity.in_(["critical", "high", "严重"]))
    count += prom_q.count()

    hostids = [m.zabbix_hostid for m in
               db.query(ZabbixHostMapping).filter(ZabbixHostMapping.db_instance_id == instance_id).all()]
    if hostids:
        zbx_q = db.query(ZabbixProblemCache).filter(
            ZabbixProblemCache.hostid.in_(hostids),
            ZabbixProblemCache.status.in_(["firing", "active", "problem"]),
        )
        if crit_only:
            zbx_q = zbx_q.filter(ZabbixProblemCache.severity >= 4)
        count += zbx_q.count()

    cap_q = db.query(DBCapacityAlert).filter(
        DBCapacityAlert.instance_id == instance_id,
        DBCapacityAlert.status == "open",
    )
    if crit_only:
        cap_q = cap_q.filter(DBCapacityAlert.level == "critical")
    count += cap_q.count()
    return count


def _in_cooldown(policy: DiagAutoPolicy) -> bool:
    """基于策略自身的 last_run_at（Python 本地时间，跨库一致）判断冷却。

    刻意不比较 incident.create_time —— 它由 DB func.now() 生成，时区可能与应用本地不一致。
    """
    if policy.cooldown_minutes <= 0 or not policy.last_run_at:
        return False
    return policy.last_run_at >= datetime.now() - timedelta(minutes=policy.cooldown_minutes)


def _evaluate(db: Session, policy: DiagAutoPolicy) -> tuple[bool, str]:
    """返回 (是否触发, 触发原因)。"""
    if _in_cooldown(policy):
        return False, "cooldown"
    if policy.mode in ("alert", "both"):
        n = _active_alert_count(db, policy.instance_id, policy.min_severity)
        if n > 0:
            return True, f"alert({n})"
        if policy.mode == "alert":
            return False, "no_alert"
    if policy.mode in ("scheduled", "both"):
        return True, "scheduled"
    return False, "idle"


def run_due_auto_diagnosis(db: Session | None = None) -> int:
    """评估所有到期的自动诊断策略，触发诊断。返回触发次数。"""
    from app.core.database import SessionLocal
    from app.services.diagnosis.orchestrator import run_diagnosis

    own = db is None
    db = db or SessionLocal()
    triggered = 0
    try:
        now = datetime.now()
        policies = (
            db.query(DiagAutoPolicy)
            .filter(DiagAutoPolicy.enabled == 1)
            .filter((DiagAutoPolicy.next_run_time == None) | (DiagAutoPolicy.next_run_time <= now))  # noqa: E711
            .all()
        )
        for policy in policies:
            inst = db.query(DBInstance).filter(DBInstance.id == policy.instance_id, DBInstance.enabled == 1).first()
            # 推进下次评估时间（无论是否触发）
            policy.next_run_time = now + timedelta(minutes=max(1, policy.interval_minutes))
            if not inst:
                db.commit()
                continue
            should, reason = _evaluate(db, policy)
            if not should:
                db.commit()
                continue
            try:
                detail = run_diagnosis(db, inst.id, title=f"[自动] {inst.instance_name} 触发：{reason}",
                                       trigger_source="alert" if reason.startswith("alert") else "scheduled")
                policy.last_run_at = datetime.now()
                policy.last_incident_id = detail.get("id")
                policy.last_trigger = reason
                db.commit()
                triggered += 1
                # 按策略自动转工单 / 通知（容错）
                _post_actions(db, detail.get("id"), policy)
            except Exception as e:
                logger.error("auto diagnosis failed for instance %s: %s", policy.instance_id, e)
                db.rollback()
        return triggered
    finally:
        if own:
            db.close()


def _post_actions(db: Session, incident_id: int | None, policy: DiagAutoPolicy) -> None:
    if not incident_id:
        return
    if policy.auto_ticket:
        try:
            from app.api.v1.diagnosis_center import _create_ticket_for_incident
            from app.schemas.diagnosis import TicketFromIncidentRequest
            _create_ticket_for_incident(db, incident_id, TicketFromIncidentRequest())
        except Exception as e:
            logger.warning("auto ticket failed: %s", e)
    if policy.auto_notify:
        try:
            from app.api.v1.diagnosis_center import _notify_incident
            _notify_incident(db, incident_id)
        except Exception as e:
            logger.warning("auto notify failed: %s", e)
