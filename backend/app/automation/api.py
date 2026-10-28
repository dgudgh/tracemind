from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.automation.schemas import (
    ApprovalDecision,
    ApprovalSubmit,
    AutomationPlanCreate,
    ExecutionRequest,
    RollbackPlanCreate,
)
from app.automation.service import AutomationService
from app.core.database import get_db

router = APIRouter(prefix="/automation", tags=["自动化审批与执行"])


def _service(db: Session) -> AutomationService:
    return AutomationService(db)


def _value_error(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc))


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", None) or {}
    username = str(auth.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=401, detail="无法识别当前登录用户")
    return username


@router.get("/capabilities")
def capabilities(db: Session = Depends(get_db)):
    service = _service(db)
    return {
        "execution_enabled": service.execution_enabled,
        "server_whitelist_configured": bool(service.allowed_hosts),
        "allowed_host_count": len(service.allowed_hosts),
        "executors": sorted(service.executors),
        "safety_gates": [
            "script_hash",
            "server_host_whitelist",
            "maintenance_window",
            "target_identity",
            "dual_approval_for_high_risk",
            "target_concurrency_lock",
            "idempotency",
            "timeout_and_retry",
            "kill_switch",
            "output_redaction",
            "rollback_plan",
            "audit_evidence",
        ],
    }


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    return {"items": _service(db).list_plans()}


@router.post("/plans")
def create_plan(payload: AutomationPlanCreate, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).create_plan(actor=_actor(request), **payload.model_dump())
    except ValueError as exc:
        _value_error(exc)


@router.post("/plans/{plan_id}/submit")
def submit_plan(plan_id: int, payload: ApprovalSubmit, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).submit_plan(plan_id, requester_id=_actor(request), expires_at=payload.expires_at)
    except ValueError as exc:
        _value_error(exc)


@router.get("/approvals")
def list_approvals(db: Session = Depends(get_db)):
    return {"items": _service(db).list_requests()}


@router.post("/approvals/{request_id}/approve")
def approve(request_id: int, payload: ApprovalDecision, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).approve(request_id, _actor(request), payload.reason)
    except ValueError as exc:
        _value_error(exc)


@router.post("/approvals/{request_id}/reject")
def reject(request_id: int, payload: ApprovalDecision, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).reject(request_id, _actor(request), payload.reason or "rejected")
    except ValueError as exc:
        _value_error(exc)


@router.post("/approvals/{request_id}/execute")
def execute(request_id: int, payload: ExecutionRequest, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).execute(request_id, _actor(request), payload.idempotency_key)
    except ValueError as exc:
        _value_error(exc)


@router.post("/approvals/{request_id}/cancel")
def cancel(request_id: int, payload: ApprovalDecision, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).cancel(
            request_id,
            _actor(request),
            payload.reason or "管理员取消自动化执行",
        )
    except ValueError as exc:
        _value_error(exc)


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    return {"items": _service(db).list_runs()}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    try:
        run, output = _service(db).get_run_output(run_id)
        return {
            "run": {column.name: getattr(run, column.name) for column in run.__table__.columns},
            "output": None if output is None else {
                column.name: getattr(output, column.name) for column in output.__table__.columns
            },
        }
    except ValueError as exc:
        _value_error(exc)


@router.get("/audits")
def list_audits(limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    return {"items": _service(db).list_audits(limit)}


@router.get("/rollbacks")
def list_rollbacks(db: Session = Depends(get_db)):
    return {"items": _service(db).list_rollbacks()}


@router.post("/plans/{plan_id}/rollback")
def create_rollback_plan(
    plan_id: int,
    payload: RollbackPlanCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return _service(db).create_rollback_plan(
            plan_id,
            actor=_actor(request),
            **payload.model_dump(),
        )
    except ValueError as exc:
        _value_error(exc)


@router.post("/rollbacks/{rollback_id}/prepare")
def prepare_rollback(rollback_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        return _service(db).prepare_rollback(rollback_id, _actor(request))
    except ValueError as exc:
        _value_error(exc)
