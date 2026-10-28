from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.automation.models import ApprovalRequest, AutomationPlan
from app.models.config_governance import (
    DBConfigBaseline,
    DBConfigBaselineItem,
    DBConfigDrift,
    DBConfigGovernanceAudit,
    DBConfigRemediation,
    DBConfigSnapshot,
)
from app.models.db_instance import DBInstance
from app.schemas.config_governance import (
    ConfigBaselineCreate,
    ConfigBaselineUpdate,
    ConfigDriftAcceptRequest,
    ConfigEvaluationRequest,
    ConfigRemediationApplyRequest,
    ConfigRemediationCreateRequest,
    ConfigRemediationSubmitRequest,
    ConfigRemediationVerifyRequest,
)
from app.services import config_governance
from app.services.access_control import resolve_scope


router = APIRouter(prefix="/operations/config-governance", tags=["配置基线与漂移治理"])


def _auth(request: Request) -> tuple[int, str, bool, str]:
    info = getattr(request.state, "auth", {}) or {}
    actor = str(info.get("username") or info.get("sub") or info.get("user_id") or "system")
    return int(info.get("user_id") or 0), actor, info.get("role") == "admin", str(info.get("source") or "")


def _allowed_instance_ids(request: Request, db: Session, minimum_access: str = "read") -> set[int] | None:
    user_id, _, is_admin, source = _auth(request)
    if is_admin or not user_id or source not in {"db", "identity"}:
        return None
    scope = resolve_scope(db, user_id, minimum_access=minimum_access)
    return set(scope.native_instance_ids) if scope.restricted else None


def _instance_or_404(
    request: Request,
    db: Session,
    instance_id: int,
    minimum_access: str = "read",
) -> DBInstance:
    allowed = _allowed_instance_ids(request, db, minimum_access)
    if allowed is not None and instance_id not in allowed:
        raise HTTPException(403, "实例不在当前用户的数据范围内")
    instance = db.get(DBInstance, instance_id)
    if instance is None:
        raise HTTPException(404, "数据库实例不存在")
    return instance


def _baseline_or_404(db: Session, baseline_id: int) -> DBConfigBaseline:
    baseline = db.get(DBConfigBaseline, baseline_id)
    if baseline is None:
        raise HTTPException(404, "配置基线不存在")
    return baseline


def _drift_or_404(db: Session, drift_id: int) -> DBConfigDrift:
    drift = db.get(DBConfigDrift, drift_id)
    if drift is None:
        raise HTTPException(404, "配置漂移不存在")
    return drift


def _remediation_or_404(db: Session, remediation_id: int) -> DBConfigRemediation:
    remediation = db.get(DBConfigRemediation, remediation_id)
    if remediation is None:
        raise HTTPException(404, "整改方案不存在")
    return remediation


def _validate_scope_payload(scope_type: str, scope_id: int | None) -> None:
    if scope_type == "global" and scope_id is not None:
        raise HTTPException(422, "全局基线不能指定 scope_id")
    if scope_type != "global" and scope_id is None:
        raise HTTPException(422, "环境或实例基线必须指定 scope_id")


def _require_baseline_write_scope(
    request: Request,
    db: Session,
    scope_type: str,
    scope_id: int | None,
) -> None:
    user_id, _, is_admin, source = _auth(request)
    if is_admin or not user_id or source not in {"db", "identity"}:
        return
    if scope_type != "instance" or scope_id is None:
        raise HTTPException(403, "非管理员只能维护实例范围配置基线")
    _instance_or_404(request, db, scope_id, "write")


def _decorate_snapshot(row: DBConfigSnapshot, instances: dict[int, DBInstance], baselines: dict[int, DBConfigBaseline]) -> dict:
    data = config_governance.serialize_model(row)
    instance = instances.get(row.instance_id)
    baseline = baselines.get(row.baseline_id)
    data["instance_name"] = instance.instance_name if instance else str(row.instance_id)
    data["db_type"] = instance.db_type if instance else None
    data["baseline_name"] = baseline.name if baseline else str(row.baseline_id)
    return data


def _decorate_drift(row: DBConfigDrift, instances: dict[int, DBInstance], baselines: dict[int, DBConfigBaseline]) -> dict:
    data = config_governance.serialize_model(row)
    instance = instances.get(row.instance_id)
    baseline = baselines.get(row.baseline_id)
    data["instance_name"] = instance.instance_name if instance else str(row.instance_id)
    data["db_type"] = instance.db_type if instance else None
    data["baseline_name"] = baseline.name if baseline else str(row.baseline_id)
    return data


def _decorate_remediation(row: DBConfigRemediation, instances: dict[int, DBInstance], baselines: dict[int, DBConfigBaseline]) -> dict:
    data = config_governance.serialize_model(row)
    instance = instances.get(row.instance_id)
    baseline = baselines.get(row.baseline_id)
    data["instance_name"] = instance.instance_name if instance else str(row.instance_id)
    data["db_type"] = instance.db_type if instance else None
    data["baseline_name"] = baseline.name if baseline else str(row.baseline_id)
    return data


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db)):
    return config_governance.summary(db, allowed_instance_ids=_allowed_instance_ids(request, db))


@router.get("/baselines")
def list_baselines(
    db_type: str | None = Query(None, pattern="^(oracle|mysql|postgresql|sqlserver)$"),
    status: str | None = Query(None, pattern="^(draft|active|archived)$"),
    db: Session = Depends(get_db),
):
    query = db.query(DBConfigBaseline)
    if db_type:
        query = query.filter(DBConfigBaseline.db_type == db_type)
    if status:
        query = query.filter(DBConfigBaseline.status == status)
    rows = query.order_by(DBConfigBaseline.status, DBConfigBaseline.db_type, DBConfigBaseline.id.desc()).all()
    return {"items": [config_governance.baseline_detail(db, row) for row in rows], "total": len(rows)}


@router.get("/baselines/{baseline_id}")
def get_baseline(baseline_id: int, db: Session = Depends(get_db)):
    return config_governance.baseline_detail(db, _baseline_or_404(db, baseline_id))


@router.post("/baselines", status_code=201)
def create_baseline(
    payload: ConfigBaselineCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_baseline_write_scope(request, db, payload.scope_type, payload.scope_id)
    if db.query(DBConfigBaseline).filter(DBConfigBaseline.name == payload.name).first():
        raise HTTPException(409, "配置基线名称已存在")
    _, actor, _, _ = _auth(request)
    values = payload.model_dump(exclude={"items"})
    baseline = DBConfigBaseline(**values, created_by=actor, updated_by=actor)
    db.add(baseline)
    db.flush()
    try:
        config_governance.replace_baseline_items(db, baseline, payload.items)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    config_governance.normalize_default_baseline(db, baseline)
    config_governance.audit(
        db,
        "baseline",
        baseline.id,
        "created",
        actor,
        after={"name": baseline.name, "db_type": baseline.db_type, "item_count": len(payload.items)},
        message="配置基线已创建",
    )
    db.commit()
    db.refresh(baseline)
    return config_governance.baseline_detail(db, baseline)


@router.put("/baselines/{baseline_id}")
def update_baseline(
    baseline_id: int,
    payload: ConfigBaselineUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    baseline = _baseline_or_404(db, baseline_id)
    values = payload.model_dump(exclude_unset=True)
    scope_type = str(values.get("scope_type", baseline.scope_type))
    scope_id = values.get("scope_id", baseline.scope_id)
    _validate_scope_payload(scope_type, scope_id)
    _require_baseline_write_scope(request, db, scope_type, scope_id)
    if "name" in values:
        duplicate = db.query(DBConfigBaseline).filter(
            DBConfigBaseline.name == values["name"], DBConfigBaseline.id != baseline.id
        ).first()
        if duplicate:
            raise HTTPException(409, "配置基线名称已存在")
    _, actor, _, _ = _auth(request)
    before = config_governance.baseline_detail(db, baseline)
    items = values.pop("items", None)
    for key, value in values.items():
        setattr(baseline, key, value)
    if items is not None:
        try:
            config_governance.replace_baseline_items(db, baseline, items)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
    baseline.version_no = int(baseline.version_no or 0) + 1
    baseline.updated_by = actor
    config_governance.normalize_default_baseline(db, baseline)
    db.flush()
    after = config_governance.baseline_detail(db, baseline)
    config_governance.audit(db, "baseline", baseline.id, "updated", actor, before=before, after=after, message="基线版本已更新")
    db.commit()
    db.refresh(baseline)
    return config_governance.baseline_detail(db, baseline)


@router.delete("/baselines/{baseline_id}")
def delete_baseline(baseline_id: int, request: Request, db: Session = Depends(get_db)):
    baseline = _baseline_or_404(db, baseline_id)
    _require_baseline_write_scope(request, db, baseline.scope_type, baseline.scope_id)
    _, actor, _, _ = _auth(request)
    snapshot_count = db.query(DBConfigSnapshot).filter(DBConfigSnapshot.baseline_id == baseline.id).count()
    if snapshot_count:
        baseline.status = "archived"
        baseline.is_default = 0
        baseline.updated_by = actor
        config_governance.audit(db, "baseline", baseline.id, "archived", actor, message="基线已有历史快照，已归档而非物理删除")
        db.commit()
        return {"ok": True, "action": "archived"}
    config_governance.audit(db, "baseline", baseline.id, "deleted", actor, before=config_governance.baseline_detail(db, baseline))
    db.query(DBConfigBaselineItem).filter(DBConfigBaselineItem.baseline_id == baseline.id).delete()
    db.delete(baseline)
    db.commit()
    return {"ok": True, "action": "deleted"}


@router.post("/baselines/{baseline_id}/evaluate")
def evaluate_baseline(
    baseline_id: int,
    payload: ConfigEvaluationRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    baseline = _baseline_or_404(db, baseline_id)
    instance = _instance_or_404(request, db, payload.instance_id, "write")
    _, actor, _, _ = _auth(request)
    try:
        snapshot = config_governance.evaluate_baseline(
            db,
            baseline,
            instance,
            actor=actor,
            source=payload.source,
            observed_parameters=payload.observed_parameters,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _decorate_snapshot(snapshot, {instance.id: instance}, {baseline.id: baseline})


@router.get("/snapshots")
def list_snapshots(
    request: Request,
    instance_id: int | None = Query(None, ge=1),
    baseline_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    allowed = _allowed_instance_ids(request, db)
    query = db.query(DBConfigSnapshot)
    if allowed is not None:
        query = query.filter(DBConfigSnapshot.instance_id.in_(allowed or {-1}))
    if instance_id:
        query = query.filter(DBConfigSnapshot.instance_id == instance_id)
    if baseline_id:
        query = query.filter(DBConfigSnapshot.baseline_id == baseline_id)
    rows = query.order_by(DBConfigSnapshot.collected_at.desc(), DBConfigSnapshot.id.desc()).limit(limit).all()
    instance_ids = {row.instance_id for row in rows}
    baseline_ids = {row.baseline_id for row in rows}
    instances = {row.id: row for row in db.query(DBInstance).filter(DBInstance.id.in_(instance_ids or {-1})).all()}
    baselines = {row.id: row for row in db.query(DBConfigBaseline).filter(DBConfigBaseline.id.in_(baseline_ids or {-1})).all()}
    return {"items": [_decorate_snapshot(row, instances, baselines) for row in rows], "total": len(rows)}


@router.get("/drifts")
def list_drifts(
    request: Request,
    instance_id: int | None = Query(None, ge=1),
    status: str | None = Query(None),
    limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    allowed = _allowed_instance_ids(request, db)
    query = db.query(DBConfigDrift)
    if allowed is not None:
        query = query.filter(DBConfigDrift.instance_id.in_(allowed or {-1}))
    if instance_id:
        query = query.filter(DBConfigDrift.instance_id == instance_id)
    if status == "active":
        query = query.filter(DBConfigDrift.status.in_(config_governance.ACTIVE_DRIFT_STATUSES))
    elif status:
        query = query.filter(DBConfigDrift.status == status)
    rows = query.order_by(DBConfigDrift.last_detected_at.desc(), DBConfigDrift.id.desc()).limit(limit).all()
    instance_ids = {row.instance_id for row in rows}
    baseline_ids = {row.baseline_id for row in rows}
    instances = {row.id: row for row in db.query(DBInstance).filter(DBInstance.id.in_(instance_ids or {-1})).all()}
    baselines = {row.id: row for row in db.query(DBConfigBaseline).filter(DBConfigBaseline.id.in_(baseline_ids or {-1})).all()}
    return {"items": [_decorate_drift(row, instances, baselines) for row in rows], "total": len(rows)}


@router.post("/drifts/{drift_id}/accept")
def accept_drift(
    drift_id: int,
    payload: ConfigDriftAcceptRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    drift = _drift_or_404(db, drift_id)
    _instance_or_404(request, db, drift.instance_id, "write")
    _, actor, _, _ = _auth(request)
    try:
        row = config_governance.accept_drift(db, drift, actor=actor, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return config_governance.serialize_model(row)


@router.get("/remediations")
def list_remediations(
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    allowed = _allowed_instance_ids(request, db)
    query = db.query(DBConfigRemediation)
    if allowed is not None:
        query = query.filter(DBConfigRemediation.instance_id.in_(allowed or {-1}))
    if status:
        query = query.filter(DBConfigRemediation.status == status)
    rows = query.order_by(DBConfigRemediation.id.desc()).limit(limit).all()
    instance_ids = {row.instance_id for row in rows}
    baseline_ids = {row.baseline_id for row in rows}
    instances = {row.id: row for row in db.query(DBInstance).filter(DBInstance.id.in_(instance_ids or {-1})).all()}
    baselines = {row.id: row for row in db.query(DBConfigBaseline).filter(DBConfigBaseline.id.in_(baseline_ids or {-1})).all()}
    request_ids = {row.approval_request_id for row in rows if row.approval_request_id}
    plan_ids = {row.automation_plan_id for row in rows if row.automation_plan_id}
    requests = {
        row.id: row
        for row in db.query(ApprovalRequest).filter(ApprovalRequest.id.in_(request_ids or {-1})).all()
    }
    plans = {
        row.id: row
        for row in db.query(AutomationPlan).filter(AutomationPlan.id.in_(plan_ids or {-1})).all()
    }
    items = []
    for row in rows:
        data = _decorate_remediation(row, instances, baselines)
        approval = requests.get(row.approval_request_id)
        plan = plans.get(row.automation_plan_id)
        data["approval_status"] = approval.status if approval else None
        data["automation_status"] = plan.status if plan else None
        data["can_apply"] = bool(approval and plan and approval.status == "approved" and plan.status == "approved")
        items.append(data)
    return {"items": items, "total": len(items)}


@router.post("/remediations", status_code=201)
def create_remediation(
    payload: ConfigRemediationCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    instance = _instance_or_404(request, db, payload.instance_id, "write")
    baseline = _baseline_or_404(db, payload.baseline_id)
    _, actor, _, _ = _auth(request)
    try:
        row = config_governance.create_remediation(
            db, baseline, instance, actor=actor, drift_ids=payload.drift_ids
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _decorate_remediation(row, {instance.id: instance}, {baseline.id: baseline})


@router.post("/remediations/{remediation_id}/submit")
def submit_remediation(
    remediation_id: int,
    payload: ConfigRemediationSubmitRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    remediation = _remediation_or_404(db, remediation_id)
    instance = _instance_or_404(request, db, remediation.instance_id, "write")
    _, actor, _, _ = _auth(request)
    try:
        row = config_governance.submit_remediation(
            db,
            remediation,
            instance,
            actor=actor,
            **payload.model_dump(),
        )
    except ValueError as exc:
        remediation.status = "approval_failed"
        remediation.apply_message = str(exc)
        db.commit()
        raise HTTPException(422, str(exc)) from exc
    return config_governance.serialize_model(row)


@router.post("/remediations/{remediation_id}/apply")
def apply_remediation(
    remediation_id: int,
    payload: ConfigRemediationApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    current = _remediation_or_404(db, remediation_id)
    instance = _instance_or_404(request, db, current.instance_id, "execute")
    _, actor, _, _ = _auth(request)
    try:
        row = config_governance.apply_remediation(
            db, remediation_id, instance, actor=actor, confirm=payload.confirm
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return config_governance.serialize_model(row)


@router.post("/remediations/{remediation_id}/verify")
def verify_remediation(
    remediation_id: int,
    payload: ConfigRemediationVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    remediation = _remediation_or_404(db, remediation_id)
    instance = _instance_or_404(request, db, remediation.instance_id, "write")
    baseline = _baseline_or_404(db, remediation.baseline_id)
    _, actor, _, _ = _auth(request)
    try:
        row, snapshot = config_governance.verify_remediation(
            db,
            remediation,
            baseline,
            instance,
            actor=actor,
            observed_parameters=payload.observed_parameters,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "remediation": config_governance.serialize_model(row),
        "snapshot": config_governance.serialize_model(snapshot),
    }


@router.get("/audits")
def list_audits(
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    rows = db.query(DBConfigGovernanceAudit).order_by(DBConfigGovernanceAudit.id.desc()).limit(limit).all()
    return {"items": [config_governance.serialize_model(row) for row in rows], "total": len(rows)}
