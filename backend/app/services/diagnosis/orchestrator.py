"""诊断主编排器：发现 → 证据 → 根因 → AI → 建议 的端到端流水线。"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.core.database_options import redact_database_error
from app.models.diagnosis_center import (
    DiagArchProfile,
    DiagEvidence,
    DiagIncident,
    DiagRootCause,
    DiagTimelineEvent,
)
from app.services.inspection import dispatch as inspection_dispatch
from app.services.rule_engine import build_rule_hits
from app.services.ai_evidence import build_local_conclusion, evidence_state
from app.services.ai_diagnosis_contract import inspection_row_to_dict
from app.services.diagnosis import (
    ai_orchestrator,
    arch_profiler,
    collectors_runner,
    evidence_chain,
    rca_engine,
    timeline_builder,
    topology_builder,
)
from app.services.diagnosis import constants as C

logger = logging.getLogger("app.services.diagnosis.orchestrator")


def _evidence_truth(evidences: list, rows: list | None = None, failures: list | None = None) -> dict:
    items = [evidence_chain.to_dict(row) for row in evidences if row.source != "manual_symptom"]
    items.extend(inspection_row_to_dict(row) for row in rows or [])
    items.extend({"check_status": "failed", "raw_data": {"execution_status": "failed"}} for _ in failures or [])
    return build_local_conclusion({"sections": [{"key": "inspection", "title": "诊断证据", "items": items}]})


def _severity_from_evidences(evidences: list) -> str:
    return _evidence_truth(evidences)["risk_level"]


def _score_from_evidences(evidences: list, truth: dict | None = None) -> float | None:
    truth = truth or _evidence_truth(evidences)
    if truth["data_quality"] != "good":
        return None
    states = [evidence_state(evidence_chain.to_dict(row))[0] for row in evidences if row.source != "manual_symptom"]
    crit, warn = states.count("critical"), states.count("warning")
    score = max(0.0, round(100.0 - 15 * crit - 5 * warn, 1))
    ceiling = {"critical": 85.0, "warning": 95.0}.get(truth["risk_level"], 100.0)
    return min(score, ceiling)


def _symptom_layer(symptom: str) -> str:
    text = symptom.lower()
    if any(token in text for token in ("锁", "row lock", "enq: tx", "阻塞", "sessions", "processes", "ora-00018", "连接池")):
        return C.LAYER_DB
    if any(token in text for token in ("awr", "sql", "执行计划", "db file sequential read", "慢", "耗时")):
        return C.LAYER_SQL
    if any(token in text for token in ("nginx", "499", "网络", "gateway")):
        return C.LAYER_NETWORK
    if any(token in text for token in ("timeout", "超时", "应用", "接口", "submitorder", "登录失败")):
        return C.LAYER_APP
    return C.LAYER_BUSINESS


def _symptom_severity(symptom: str) -> str:
    text = symptom.lower()
    if any(token in text for token in ("失败", "不可用", "hang", "卡住", "超时", "timeout", "ora-", "499", "上限", "critical")):
        return "critical"
    if any(token in text for token in ("慢", "等待", "异常", "warning", "warn", "抖动")):
        return "warning"
    return "info"


def _add_manual_symptom_evidence(
    db: Session,
    incident_id: int,
    symptom: str | None,
    intent_summary: str | None,
    intent_signals: list[str] | None,
    recommended_cards: list[str] | None,
) -> DiagEvidence | None:
    symptom_text = (symptom or "").strip()
    if not symptom_text:
        return None
    signal_text = "、".join((intent_signals or [])[:8]) or "待后续证据确认"
    card_text = "、".join((recommended_cards or [])[:8]) or "摘要、时间线、根因候选"
    row = DiagEvidence(
        incident_id=incident_id,
        layer=_symptom_layer(symptom_text),
        source="manual_symptom",
        phenomenon=symptom_text[:2000],
        evidence=f"客户现场描述：{symptom_text[:2000]}",
        judgement=intent_summary or "根据客户描述进入自适应诊断编排",
        impact="该描述作为卡片推荐、证据缺口和根因排序的上下文信号，不单独作为最终根因。",
        suggestion=f"优先打开：{card_text}；已识别信号：{signal_text}。",
        metric_name="customer_symptom",
        metric_value=signal_text,
        weight=0.55,
        confidence=65,
        severity=_symptom_severity(symptom_text),
        raw={
            "symptom": symptom_text,
            "intent_summary": intent_summary,
            "intent_signals": intent_signals or [],
            "recommended_cards": recommended_cards or [],
        },
    )
    db.add(row)
    db.flush()
    return row


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception as rollback_error:
        logger.warning("diagnosis rollback failed: %s", rollback_error)


def _reload_incident(db: Session, snapshot: dict) -> DiagIncident:
    incident = db.query(DiagIncident).filter(DiagIncident.incident_no == snapshot["incident_no"]).first()
    if incident is None:
        incident = DiagIncident(**snapshot)
        db.add(incident)
        db.flush()
    return incident


def _record_failed_diagnosis(
    db: Session,
    snapshot: dict,
    incident_id: int | None,
    exc: Exception,
) -> dict:
    _rollback_quietly(db)
    message = str(exc) or exc.__class__.__name__
    incident = None
    if incident_id:
        try:
            incident = db.query(DiagIncident).filter(
                DiagIncident.id == incident_id, DiagIncident.incident_no == snapshot["incident_no"],
            ).first()
        except Exception:
            _rollback_quietly(db)

    if incident is None:
        incident = DiagIncident(**snapshot)
        db.add(incident)

    incident.status = "failed"
    incident.severity = "unknown"
    incident.score = None
    incident.root_cause_summary = f"诊断流程异常：{message[:1800]}"
    incident.evidence_count = 0
    incident.root_cause_count = 0
    incident.ai_result = {"status": "failed", "execution_status": "failed", "data_quality": "missing",
                          "conclusion_status": "not_available", "error": message[:1800]}
    incident.root_caused_at = None
    db.commit()
    db.refresh(incident)
    return get_incident_detail(db, incident.id)


def run_diagnosis(db: Session, instance_id: int, title: str | None = None,
                  symptom: str | None = None,
                  intent_summary: str | None = None,
                  intent_signals: list[str] | None = None,
                  recommended_cards: list[str] | None = None,
                  trigger_source: str = "manual") -> dict:
    """对单实例执行端到端诊断，落 incident 及全部子表，返回详情 dict。"""
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        raise ValueError("数据库实例不存在")

    instance_id_value = instance.id
    instance_name = instance.instance_name
    db_type = instance.db_type
    started_at = datetime.now()
    incident_snapshot = {
        "incident_no": f"INC-{started_at.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}",
        "instance_id": instance_id_value,
        "db_type": db_type,
        "instance_name": instance_name,
        "title": title or f"{instance_name} 端到端诊断",
        "trigger_source": trigger_source,
        "status": "diagnosing",
        "started_at": started_at,
    }
    incident = DiagIncident(**incident_snapshot)
    db.add(incident)
    db.flush()  # 拿到 incident.id
    incident_id = incident.id
    collection_failures = []

    try:
        # 1) 架构画像
        arch_row = arch_profiler.profile_architecture(db, instance, refresh=True)
        arch = arch_profiler.to_dict(arch_row)

        # 2) 巡检 → 规则命中（复用现有引擎）
        try:
            _task_id, rows = inspection_dispatch.run_collecting_rows(
                db, instance, trigger_type="diagnosis",
            )
            if not rows:
                collection_failures.append({"source": "inspection", "reason": "巡检没有返回检查项"})
        except Exception as e:
            _rollback_quietly(db)
            logger.warning("inspection failed for %s: %s", instance_id_value, e)
            collection_failures.append({"source": "inspection", "reason": redact_database_error(e)})
            incident = _reload_incident(db, incident_snapshot)
            incident_id = incident.id
            instance = db.query(DBInstance).filter(DBInstance.id == instance_id_value).first() or instance
            arch_row = (
                db.query(DiagArchProfile)
                .filter(DiagArchProfile.instance_id == instance_id_value)
                .order_by(DiagArchProfile.id.desc())
                .first()
            ) or arch_row
            rows = []
        supporting_rows = [row for row in rows if evidence_state(inspection_row_to_dict(row))[1] == "success"]
        hits = build_rule_hits(instance, supporting_rows) if supporting_rows else []

        # 2.5) 多源采集器（中间件/APM/OS/存储）—— 未配置数据源时为空，无副作用
        try:
            signals = collectors_runner.run_collectors(db, instance)
        except Exception as e:
            _rollback_quietly(db)
            logger.warning("collectors failed for %s: %s", instance_id_value, e)
            collection_failures.append({"source": "collectors", "reason": redact_database_error(e)})
            incident = _reload_incident(db, incident_snapshot)
            incident_id = incident.id
            instance = db.query(DBInstance).filter(DBInstance.id == instance_id_value).first() or instance
            signals = []

        # 3) 多源证据链（规则命中 + AWR + 告警 + 采集信号）
        hardware_profile = (arch.get("details") or {}).get("hardware_profile")
        evidences = evidence_chain.build_evidence_chain(
            db,
            incident_id,
            instance,
            hits,
            collector_signals=signals,
            hardware_profile=hardware_profile,
            as_of=started_at,
        )
        manual_evidence = _add_manual_symptom_evidence(
            db,
            incident_id,
            symptom,
            intent_summary,
            intent_signals,
            recommended_cards,
        )
        if manual_evidence:
            evidences.append(manual_evidence)

        # 整体风险/评分基于完整证据集（含 AWR 等待事件、告警、采集器），而非仅巡检命中
        truth = _evidence_truth(evidences, rows, collection_failures)
        severity = truth["risk_level"]
        score = _score_from_evidences(evidences, truth)

        # 4) 根因排序（RCA + 方法论）
        root_causes = rca_engine.rank_root_causes(db, incident_id, evidences, arch_row)

        # 5) 时间线 + 服务拓扑
        timeline_builder.build_timeline(db, incident_id, instance, evidences, started_at, signals=signals)
        try:
            topology_builder.build_topology(db, incident_id, instance, signals)
        except Exception as e:
            if not db.is_active:
                raise
            logger.warning("topology build failed: %s", e)

        # 6) AI 三层编排
        ev_dicts = [evidence_chain.to_dict(e) for e in evidences]
        rc_dicts = [rca_engine.to_dict(c) for c in root_causes]
        ai = ai_orchestrator.orchestrate(db, instance, arch, ev_dicts, rc_dicts, severity,
                                         collection_incomplete=truth["data_quality"] != "good")
        ai.update(execution_status=truth["execution_status"], data_quality=truth["data_quality"],
                  conclusion_status="hypotheses_require_validation" if root_causes else "not_available",
                  limitations=truth["limitations"])
        ai["collection_failures"] = collection_failures

        # 7) 回填 incident
        primary = root_causes[0] if root_causes else None
        incident = db.query(DiagIncident).filter(DiagIncident.id == incident_id).first() or incident
        incident.arch_type = arch.get("arch_type")
        incident.primary_layer = primary.layer if primary else None
        incident.root_cause_summary = primary.cause if primary else (
            "证据不足，不能确认根因" if truth["data_quality"] != "good" else "已提供检查项未见明确根因"
        )
        incident.severity = severity
        incident.score = score
        incident.evidence_count = len(evidences)
        incident.root_cause_count = len(root_causes)
        incident.ai_summary = ai.get("summary")
        incident.ai_provider = ai.get("provider")
        incident.ai_tier = ai.get("tier")
        incident.ai_result = ai
        incident.status = "root_caused" if primary else "diagnosing"
        incident.root_caused_at = datetime.now() if primary else None
        db.commit()
        db.refresh(incident)

        # 8) 刷新统一建议（容错，不阻断主流程）
        try:
            from app.services.suggestion_aggregator import rebuild_suggestions
            rebuild_suggestions(db, instance_id_value)
        except Exception as e:
            _rollback_quietly(db)
            logger.warning("rebuild suggestions failed: %s", e)

        return get_incident_detail(db, incident_id)
    except Exception as exc:
        logger.exception("diagnosis failed | incident_id=%s | instance_id=%s", incident_id, instance_id_value)
        return _record_failed_diagnosis(db, incident_snapshot, incident_id, exc)


def incident_summary(row: DiagIncident) -> dict:
    outcome = row.ai_result if isinstance(row.ai_result, dict) else {}
    return {
        "id": row.id,
        "incident_no": row.incident_no,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "instance_name": row.instance_name,
        "title": row.title,
        "trigger_source": row.trigger_source,
        "status": row.status,
        "severity": row.severity,
        "score": row.score,
        "arch_type": row.arch_type,
        "primary_layer": row.primary_layer,
        "root_cause_summary": row.root_cause_summary,
        "ai_tier": row.ai_tier,
        "ai_provider": row.ai_provider,
        "ticket_id": row.ticket_id,
        "evidence_count": row.evidence_count,
        "root_cause_count": row.root_cause_count,
        "execution_status": outcome.get("execution_status", "unknown"),
        "data_quality": outcome.get("data_quality", "unknown"),
        "conclusion_status": outcome.get("conclusion_status", "unknown"),
        "create_time": row.create_time.isoformat(sep=" ") if row.create_time else None,
    }


def list_incidents(db: Session, instance_id: int | None = None, status: str | None = None,
                   page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    q = db.query(DiagIncident)
    if instance_id:
        q = q.filter(DiagIncident.instance_id == instance_id)
    if status:
        q = q.filter(DiagIncident.status == status)
    total = q.count()
    rows = q.order_by(DiagIncident.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return [incident_summary(r) for r in rows], total


def get_incident_detail(db: Session, incident_id: int) -> dict:
    incident = db.query(DiagIncident).filter(DiagIncident.id == incident_id).first()
    if not incident:
        raise ValueError("诊断事件不存在")

    evidences = (
        db.query(DiagEvidence).filter(DiagEvidence.incident_id == incident_id)
        .order_by(DiagEvidence.severity.desc(), DiagEvidence.id).all()
    )
    root_causes = (
        db.query(DiagRootCause).filter(DiagRootCause.incident_id == incident_id)
        .order_by(DiagRootCause.rank).all()
    )
    timeline = (
        db.query(DiagTimelineEvent).filter(DiagTimelineEvent.incident_id == incident_id)
        .order_by(DiagTimelineEvent.event_time, DiagTimelineEvent.id).all()
    )
    arch = db.query(DiagArchProfile).filter(DiagArchProfile.instance_id == incident.instance_id).first()

    from app.models.diagnosis_center import DiagTopologyEdge, DiagTopologyNode
    topo_nodes = (
        db.query(DiagTopologyNode).filter(DiagTopologyNode.incident_id == incident_id)
        .order_by(DiagTopologyNode.order_index).all()
    )
    topo_edges = db.query(DiagTopologyEdge).filter(DiagTopologyEdge.incident_id == incident_id).all()

    # 证据链按七层分组
    evidence_by_layer: dict[str, list[dict]] = {layer: [] for layer in C.LAYERS}
    for e in evidences:
        evidence_by_layer.setdefault(e.layer or C.LAYER_DB, []).append(evidence_chain.to_dict(e))

    detail = incident_summary(incident)
    detail.update({
        "ai_summary": incident.ai_summary,
        "ai_result": incident.ai_result,
        "arch_profile": arch_profiler.to_dict(arch) if arch else None,
        "evidence_by_layer": {k: v for k, v in evidence_by_layer.items() if v},
        "evidences": [evidence_chain.to_dict(e) for e in evidences],
        "root_causes": [rca_engine.to_dict(c) for c in root_causes],
        "timeline": [timeline_builder.to_dict(t) for t in timeline],
        "topology": {
            "nodes": [topology_builder.node_to_dict(n) for n in topo_nodes],
            "edges": [topology_builder.edge_to_dict(e) for e in topo_edges],
        },
        "started_at": incident.started_at.isoformat(sep=" ") if incident.started_at else None,
        "root_caused_at": incident.root_caused_at.isoformat(sep=" ") if incident.root_caused_at else None,
        "resolved_at": incident.resolved_at.isoformat(sep=" ") if incident.resolved_at else None,
        "reviewed_at": incident.reviewed_at.isoformat(sep=" ") if incident.reviewed_at else None,
    })
    return detail
