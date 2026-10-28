"""复盘中心：复盘记录 + 沉淀回方法论库。"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.diagnosis_center import (
    DiagIncident,
    DiagRootCause,
    DiagReview,
)
from app.services.diagnosis import methodology_kb

logger = logging.getLogger("app.services.diagnosis.retrospective")


def create_review(db: Session, incident: DiagIncident, data: dict) -> DiagReview:
    """创建复盘记录，可选沉淀为方法论。"""
    review = DiagReview(
        incident_id=incident.id,
        root_cause=data.get("root_cause") or incident.root_cause_summary,
        process=data.get("process"),
        impact_scope=data.get("impact_scope"),
        recovery_minutes=data.get("recovery_minutes"),
        optimizations=data.get("optimizations"),
        reviewer=data.get("reviewer"),
    )
    db.add(review)
    db.flush()

    if data.get("sediment"):
        med = sediment_to_methodology(db, incident, review)
        if med:
            review.sediment_methodology_id = med.id

    # 标记事件已复盘
    incident.status = "reviewed"
    incident.reviewed_at = datetime.now()
    db.commit()
    db.refresh(review)
    return review


def sediment_to_methodology(db: Session, incident: DiagIncident, review: DiagReview):
    """把一次已解决事件沉淀为方法论库条目（source=sediment）。"""
    primary = (
        db.query(DiagRootCause)
        .filter(DiagRootCause.incident_id == incident.id)
        .order_by(DiagRootCause.is_primary.desc(), DiagRootCause.total_score.desc())
        .first()
    )
    opts = review.optimizations if isinstance(review.optimizations, list) else []
    data = {
        "title": f"[沉淀] {incident.title}",
        "symptom": incident.title,
        "layer": incident.primary_layer or (primary.layer if primary else None),
        "applicable_arch": [incident.arch_type] if incident.arch_type else None,
        "entry_point": review.root_cause or incident.root_cause_summary,
        "troubleshoot_path": opts or None,
        "judge_logic": primary.cause if primary else incident.root_cause_summary,
        "solution": review.process or (primary.suggestion if primary else None),
        "review_note": review.impact_scope,
        "keywords": [incident.primary_layer] if incident.primary_layer else None,
        "prior_prob": 0.5,
        "tags": ["复盘沉淀"],
        "source": "sediment",
    }
    try:
        return methodology_kb.create_methodology(db, data)
    except Exception as e:
        logger.warning("sediment to methodology failed: %s", e)
        return None


def to_dict(row: DiagReview) -> dict:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "root_cause": row.root_cause,
        "process": row.process,
        "impact_scope": row.impact_scope,
        "recovery_minutes": row.recovery_minutes,
        "optimizations": row.optimizations or [],
        "sediment_methodology_id": row.sediment_methodology_id,
        "reviewer": row.reviewer,
        "review_time": row.review_time.isoformat(sep=" ") if row.review_time else None,
    }
