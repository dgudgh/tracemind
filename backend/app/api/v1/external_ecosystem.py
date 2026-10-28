from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.external_ecosystem import (
    EcosystemConfigCreate,
    EcosystemConfigUpdate,
    EcosystemDispatchRequest,
    EcosystemEventCreate,
    EcosystemPlanRequest,
    GrafanaDashboardRequest,
)
from app.services import external_ecosystem

router = APIRouter(prefix="/ecosystem", tags=["外部生态适配"])


def _get_config_or_404(db: Session, config_id: int):
    row = external_ecosystem.get_config(db, config_id)
    if not row:
        raise HTTPException(status_code=404, detail="生态适配配置不存在")
    return row


def _moved_to_domain(task_code: str):
    deprecation = external_ecosystem.deprecation_for_task(task_code)
    raise HTTPException(status_code=409, detail=deprecation or {"deprecated": True, "message": "旧生态入口已只读"})


@router.get("/tasks")
def list_tasks():
    return {"items": external_ecosystem.list_tasks()}


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    return external_ecosystem.overview(db)


@router.get("/legacy-migration")
def legacy_migration_status(db: Session = Depends(get_db)):
    return external_ecosystem.legacy_migration_status(db)


@router.post("/legacy-migration")
def migrate_legacy_records(db: Session = Depends(get_db)):
    return external_ecosystem.migrate_legacy_records(db)


@router.get("/configs")
def list_configs(
    task_code: str | None = Query(None),
    integration_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return {"items": external_ecosystem.list_configs(db, task_code=task_code, integration_type=integration_type)}


@router.post("/configs")
def create_config(payload: EcosystemConfigCreate, db: Session = Depends(get_db)):
    if not external_ecosystem.get_task(payload.task_code):
        raise HTTPException(status_code=400, detail="未知任务编号")
    return external_ecosystem.create_config(db, payload.model_dump())


@router.put("/configs/{config_id}")
def update_config(config_id: int, payload: EcosystemConfigUpdate, db: Session = Depends(get_db)):
    row = _get_config_or_404(db, config_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("task_code") and not external_ecosystem.get_task(data["task_code"]):
        raise HTTPException(status_code=400, detail="未知任务编号")
    return external_ecosystem.update_config(db, row, data)


@router.delete("/configs/{config_id}")
def delete_config(config_id: int, db: Session = Depends(get_db)):
    row = _get_config_or_404(db, config_id)
    try:
        external_ecosystem.delete_config(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc),
            "deprecation": external_ecosystem.deprecation_for_task(row.task_code),
        }) from exc
    return {"detail": "生态适配配置已删除"}


@router.post("/configs/{config_id}/test")
def test_config(config_id: int, db: Session = Depends(get_db)):
    row = _get_config_or_404(db, config_id)
    return external_ecosystem.test_config(db, row)


@router.get("/events")
def list_events(
    task_code: str | None = Query(None),
    integration_type: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return {"items": external_ecosystem.list_events(db, task_code, integration_type, status, limit)}


@router.post("/events")
def create_event(payload: EcosystemEventCreate, db: Session = Depends(get_db)):
    if not external_ecosystem.get_task(payload.task_code):
        raise HTTPException(status_code=400, detail="未知任务编号")
    _moved_to_domain(payload.task_code)


@router.get("/plans")
def list_plans(
    task_code: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return {"items": external_ecosystem.list_plans(db, task_code=task_code, limit=limit)}


@router.post("/plans/{task_code}")
def build_plan(task_code: str, payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain(task_code)


@router.post("/notifications/dispatch")
def dispatch_notification(payload: EcosystemDispatchRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-051")


@router.post("/tickets/create")
def create_ticket(payload: EcosystemDispatchRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-052")


@router.post("/grafana/dashboard")
def build_grafana_dashboard(payload: GrafanaDashboardRequest):
    _moved_to_domain("T-055")


@router.post("/cmdb/sync-plan")
def build_cmdb_sync_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-053")


@router.post("/logs/query-plan")
def build_log_query_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-054")


@router.post("/backup/platform-plan")
def build_backup_platform_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-056")


@router.post("/object-storage/archive-plan")
def build_object_storage_archive_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-057")


@router.post("/automation/approval-plan")
def build_automation_approval_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-058")


@router.post("/identity/mapping-plan")
def build_identity_mapping_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-059")


@router.post("/apm/trace-plan")
def build_apm_trace_plan(payload: EcosystemPlanRequest, db: Session = Depends(get_db)):
    _moved_to_domain("T-060")
