import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.models.suggestion import DBSuggestion
from app.models.sql_profile import DBSqlProfile
from app.services.sql_profile_query import with_sql_profile_issues
from app.models.index_finding import DBIndexFinding
from app.models.table_profile import DBTableProfile
from app.services import suggestion_aggregator as agg
from app.services import perf_profile_collector as ppc
from app.schemas.workload_report import WorkloadReportCollectRequest, WorkloadReportCompareRequest
from app.services import workload_report

logger = logging.getLogger("app.api.performance")
router = APIRouter(prefix="/performance", tags=["性能诊断"])


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("username") or auth.get("sub") or auth.get("user_id") or "system")


class SuggestionUpdate(BaseModel):
    status: Optional[str] = None
    problem_type: Optional[str] = None
    suggestion: Optional[str] = None
    script: Optional[str] = None


def _finish_object_profile_collection(db: Session, instance_id: int, result: dict):
    result["suggestions_refreshed"] = False
    if result["status"] == "success" and result["published"]:
        try:
            agg.rebuild_suggestions(db, instance_id)
            result["suggestions_refreshed"] = True
        except Exception:
            db.rollback()
            logger.exception("Object snapshot persisted but suggestion rebuild failed | instance=%s", instance_id)
            result["status"] = "partial"
            result["errors"].append("画像已保存，建议中心刷新失败")
            result["message"] = "画像已保存，建议中心刷新失败"
    if result["status"] != "success":
        # Existing clients treat every 2xx response as a completed refresh.
        return JSONResponse(
            status_code=422 if result["status"] == "unsupported" else 502,
            content={**result, "detail": result["message"]},
        )
    return result


# ---------------------------------------------------------------------------
# 性能基线
# ---------------------------------------------------------------------------

@router.post("/baseline/{instance_id}/collect")
def api_baseline_collect(instance_id: int, db: Session = Depends(get_db)):
    from app.services import perf_baseline
    inst = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
    return perf_baseline.collect_perf_metrics(db, inst)


@router.get("/baseline/{instance_id}")
def api_baseline(instance_id: int, db: Session = Depends(get_db)):
    from app.services import perf_baseline
    return perf_baseline.compute_baseline(db, instance_id)


def _serialize_suggestions(db: Session, rows: list[DBSuggestion]) -> list[dict]:
    proof = agg.inspection_suggestion_evidence(db, rows)
    return [_serialize(row, proof) for row in rows]


def _serialize(s: DBSuggestion, inspection_evidence: dict | None = None) -> dict:
    result = {
        "id": s.id,
        "instance_id": s.instance_id,
        "db_type": s.db_type,
        "source": s.source,
        "source_ref": s.source_ref,
        "problem_type": s.problem_type,
        "object_name": s.object_name,
        "severity": s.severity,
        "impact": s.impact,
        "suggestion": s.suggestion,
        "script": s.script,
        "status": s.status,
        "ticket_id": s.ticket_id,
        "extra": s.extra,
        "create_time": str(s.create_time) if s.create_time else "",
    }
    if s.source == "inspection":
        proof = (inspection_evidence or {}).get(s.id, {
            "execution_status": "unknown", "data_quality": "missing", "risk_level": "unknown",
            "evidence_basis": "source_unverified",
        })
        measured = proof["execution_status"] in {"success", "partial"}
        severity = {"critical": "high", "warning": "medium", "info": "low", "normal": "low"}.get(proof["risk_level"], "unknown") if measured else "unknown"
        state_fields = {
            "execution_status", "execution_status_source", "data_quality", "risk", "risk_level", "severity",
            "score", "overall_score", "evidence_score", "score_basis", "status", "state", "executed", "success",
            "source_evidence", "evidence_basis", "collection", "observed_score", "observed_risk_level",
            "observation_count", "observed_sources", "manifest",
        }
        extra = {key: value for key, value in s.extra.items() if key not in state_fields} if isinstance(s.extra, dict) else {}
        # Keep business references, but quarantine historical state from current proof.
        # The unchanged original remains auditable without becoming execution evidence.
        result.update(proof, severity=severity, historical_extra=s.extra,
                      extra={**extra, **proof, "source_evidence": dict(proof)})
    return result
