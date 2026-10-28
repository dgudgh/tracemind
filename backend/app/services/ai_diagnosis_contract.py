"""Shared inspection evidence and model-output gates for diagnosis entrypoints."""
from __future__ import annotations

import json

from app.analyzers.ai_diagnosis import metric_rows
from app.services import ai_config
from app.services.ai_evidence import evidence_state


def inspection_row_to_dict(row) -> dict:
    return {key: getattr(row, key, None) for key in (
        "check_category", "check_item", "check_status", "metric_name", "metric_value",
        "threshold_value", "finding", "suggestion", "raw_data",
    )}


def metric_evidence(metrics: dict) -> dict:
    return {f"metric-{index}": {"group": key, "row": row}
            for index, (key, _, row) in enumerate(metric_rows(metrics))
            if isinstance(row, dict) and evidence_state(row)[1] == "success"}


def model_diagnosis(config, prompt: str, evidence: dict, local_result: dict):
    prompt += ai_config.DIAGNOSIS_CONTRACT + "\nSupporting evidence by ID:\n" + json.dumps(evidence, ensure_ascii=False)
    metadata = {**ai_config.inference_metadata(config, prompt), "evidence_manifest": evidence,
                "response_schema": "diagnosis-v1", "validation_status": "failed",
                "validation_limitations": ["References checked; semantic entailment and freshness not verified."]}
    if not evidence:
        return None, "no_supporting_evidence", metadata
    try:
        text = ai_config.call_ai_provider(config, prompt)
        metadata.update(getattr(text, "metadata", {}))
        ai_config.validate_diagnosis(text, set(evidence), risk_floor=local_result.get("risk_level", "unknown"),
                                     incomplete=local_result.get("data_quality", "missing") != "good")
        metadata["validation_status"] = "structure_and_references_validated"
        return str(text), None, metadata
    except Exception as exc:
        metadata.update(getattr(exc, "metadata", {}))
        metadata["validation_status"] = "failed"
        return None, getattr(exc, "code", type(exc).__name__), metadata
