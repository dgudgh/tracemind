"""Stable plan-to-incident identity for the root-cause capability effect."""
from __future__ import annotations

import hashlib


ROOT_CAUSE_RESULT_SCHEMA = "diagnosis.root_cause_result/v1"
COLLECTION_MODE = "current_snapshot"


def trigger_source_for_execution(execution_id: str) -> str:
    """Fit an unguessable plan execution identity into DiagIncident.trigger_source."""
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
    return f"capability:{digest}"


def customer_context_summary(time_window: str, impact_scope: str) -> str:
    return (
        f"客户报告时间窗：{time_window}；影响范围：{impact_scope}。"
        "本次执行采集当前快照，不代表对客户报告时间窗进行了历史回放。"
    )


def customer_context_signals() -> list[str]:
    """Keep the indexed metric value bounded; full customer text lives in raw context."""
    return [
        "requested_time_window:supplied",
        "impact_scope:supplied",
        f"collection_mode:{COLLECTION_MODE}",
    ]
