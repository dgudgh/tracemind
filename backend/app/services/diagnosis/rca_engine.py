"""根因分析引擎：按层聚合证据 → 评分排序。

total = (w1·evidence_score + w2·prior_prob + w3·methodology_match) · priority_weight(layer)
其中 priority_weight 来自架构画像（默认 网络>存储>内存>CPU；RAC 网络>CacheFusion>存储>内存>CPU）。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.diagnosis_center import DiagArchProfile, DiagEvidence, DiagRootCause
from app.services.diagnosis import constants as C
from app.services.diagnosis import methodology_kb
from app.services.ai_evidence import is_supporting_evidence

logger = logging.getLogger("app.services.diagnosis.rca_engine")

W_EVIDENCE = 0.5
W_PRIOR = 0.2
W_METHODOLOGY = 0.3
CONTEXT_ONLY_SOURCES = {"manual_symptom"}
HARDWARE_CONTEXT_SOURCES = {"hardware_profile"}
HARDWARE_CONTEXT_MAX_PRIOR = 0.45
HARDWARE_CONTEXT_MAX_METHODOLOGY_MATCH = 0.35


def _diagnostic_items(items: list[DiagEvidence]) -> list[DiagEvidence]:
    """Return evidence that can independently support a root-cause score."""
    return [e for e in items if is_supporting_evidence({
        key: getattr(e, key, None) for key in (
            "source", "severity", "raw", "evidence", "phenomenon", "metric_name", "metric_value",
        )
    })]


def _diagnostic_sources(items: list[DiagEvidence]) -> set[str]:
    return {e.source for e in _diagnostic_items(items) if e.source and e.source not in CONTEXT_ONLY_SOURCES}


def _is_weak_hardware_context(items: list[DiagEvidence], sources: set[str]) -> bool:
    """A single non-critical hardware profile signal is context, not a primary RCA by itself."""
    if sources != HARDWARE_CONTEXT_SOURCES:
        return False
    return all(C.severity_score(e.severity) < 1.0 for e in _diagnostic_items(items))


def rank_root_causes(
    db: Session,
    incident_id: int,
    evidences: list[DiagEvidence],
    arch_profile: DiagArchProfile | None,
) -> list[DiagRootCause]:
    """对证据按层评分排序，落 diag_root_cause。返回已落库候选。"""
    if not evidences:
        return []

    priority = (arch_profile.priority_template or {}).get("order") if arch_profile else None
    if not priority:
        priority = C.DEFAULT_PRIORITY

    by_layer: dict[str, list[DiagEvidence]] = defaultdict(list)
    for e in evidences:
        by_layer[e.layer or C.LAYER_DB].append(e)

    candidates = []
    for layer, items in by_layer.items():
        diagnostic_items = _diagnostic_items(items)
        if not diagnostic_items:
            continue
        # 证据分三要素：
        #  base        —— 本层最强证据（避免单条 critical 被平均稀释）
        #  count_bonus —— 证据条数递减加成
        #  corroboration —— 跨源互证：同一层被多个独立来源（巡检/AWR/告警/采集器）共同指证时显著加权
        base = max((e.weight or 1.0) * C.severity_score(e.severity) for e in diagnostic_items)
        count_bonus = min(0.15, 0.04 * len(diagnostic_items))
        sources = _diagnostic_sources(items)
        n_sources = len(sources)
        corroboration = min(0.25, 0.12 * (n_sources - 1))
        evidence_score = min(1.0, round(0.65 * base + count_bonus + corroboration, 3))

        # 方法论匹配（纳入证据明细，提升对具体指标如 stale_stats 的命中精度）
        text = " ".join(filter(None, [
            *(e.phenomenon or "" for e in diagnostic_items),
            *(e.judgement or "" for e in diagnostic_items),
            *(e.evidence or "" for e in diagnostic_items),
            *(e.metric_name or "" for e in diagnostic_items),
        ]))
        med, match = methodology_kb.match_methodology(db, text, layer=layer)
        prior = float(med.prior_prob) if med else 0.4
        if _is_weak_hardware_context(items, sources):
            prior = min(prior, HARDWARE_CONTEXT_MAX_PRIOR)
            match = min(match, HARDWARE_CONTEXT_MAX_METHODOLOGY_MATCH)

        pw = C.priority_weight(layer, priority)
        total = (W_EVIDENCE * evidence_score + W_PRIOR * prior + W_METHODOLOGY * match) * pw

        # 根因文本：优先用方法论判断逻辑，否则用最强证据的判断
        strongest = max(diagnostic_items, key=lambda e: (e.weight or 1.0) * C.severity_score(e.severity))
        if med:
            cause = f"[{layer}] {med.title}：{med.judge_logic or med.symptom or ''}".strip()
            suggestion = med.solution or strongest.suggestion
        else:
            cause = f"[{layer}] {strongest.judgement or strongest.phenomenon}"
            suggestion = strongest.suggestion
        # 跨源互证标注：多来源共同指证时提示，增强可信度说明
        if n_sources > 1:
            cause = f"{cause}（{n_sources} 个来源互证：{'、'.join(sorted(sources))}）"

        candidates.append({
            "layer": layer,
            "cause": cause,
            "evidence_score": round(evidence_score, 3),
            "prior_prob": round(prior, 3),
            "methodology_match": round(match, 3),
            "total_score": round(total, 4),
            "confidence": min(95, int(50 + 45 * total)),
            "methodology_id": med.id if med else None,
            "suggestion": suggestion,
        })

    candidates.sort(key=lambda c: c["total_score"], reverse=True)

    saved = []
    for idx, c in enumerate(candidates):
        row = DiagRootCause(
            incident_id=incident_id,
            layer=c["layer"],
            cause=c["cause"],
            evidence_score=c["evidence_score"],
            prior_prob=c["prior_prob"],
            methodology_match=c["methodology_match"],
            total_score=c["total_score"],
            rank=idx + 1,
            is_primary=1 if idx == 0 else 0,
            confidence=c["confidence"],
            methodology_id=c["methodology_id"],
            suggestion=c["suggestion"],
        )
        db.add(row)
        saved.append(row)
    db.flush()
    return saved


def to_dict(row: DiagRootCause) -> dict:
    return {
        "id": row.id,
        "layer": row.layer,
        "cause": row.cause,
        "evidence_score": row.evidence_score,
        "prior_prob": row.prior_prob,
        "methodology_match": row.methodology_match,
        "total_score": row.total_score,
        "rank": row.rank,
        "is_primary": row.is_primary,
        "confidence": row.confidence,
        "methodology_id": row.methodology_id,
        "suggestion": row.suggestion,
    }
