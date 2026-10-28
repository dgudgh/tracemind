from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import generic_db_ops
from app.services import instances as instance_service
from app.services.inspection import dispatch as inspection_dispatch

router = APIRouter(prefix="/operations/postgresql", tags=["PostgreSQL 运维中心"])
inspect_router = APIRouter(prefix="/postgresql", tags=["PostgreSQL 巡检"])


def _get(db: Session, instance_id: int):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    if obj.db_type != "postgresql":
        raise HTTPException(status_code=400, detail="Instance is not PostgreSQL")
    return obj


def _section(db: Session, instance_id: int, section: str):
    return generic_db_ops.collect_section(_get(db, instance_id), section)


def _plan(db: Session, instance_id: int, plan_type: str, payload: dict | None = None):
    return generic_db_ops.build_plan(_get(db, instance_id), plan_type, payload)


@router.get("/modules")
def api_modules():
    return generic_db_ops.get_modules("postgresql")


@router.get("/{instance_id}/overview")
def api_overview(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "overview")


@router.get("/{instance_id}/parameters")
def api_parameters(instance_id: int, db: Session = Depends(get_db)):
    data = _section(db, instance_id, "wal")
    return {"items": data.get("items", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@router.get("/{instance_id}/roles")
def api_roles(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "roles")


@router.get("/{instance_id}/space/databases")
def api_space_databases(instance_id: int, db: Session = Depends(get_db)):
    data = _section(db, instance_id, "space")
    return {"items": data.get("databases", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@router.get("/{instance_id}/space/tables")
def api_space_tables(instance_id: int, db: Session = Depends(get_db)):
    data = _section(db, instance_id, "space")
    return {"items": data.get("items", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@router.get("/{instance_id}/space/indexes")
def api_space_indexes(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/space/bloat")
def api_space_bloat(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/wal/overview")
def api_wal_overview(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "wal")


@router.get("/{instance_id}/wal/archive-status")
def api_wal_archive_status(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "wal")


@router.get("/{instance_id}/wal/replication-slots")
def api_wal_replication_slots(instance_id: int, db: Session = Depends(get_db)):
    data = _section(db, instance_id, "wal")
    return {"items": data.get("replication_slots", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@router.get("/{instance_id}/sessions/active")
def api_sessions_active(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/locks")
def api_sessions_locks(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/blocking")
def api_sessions_blocking(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/long-transactions")
def api_sessions_long_transactions(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.post("/{instance_id}/sessions/kill-plan")
def api_sessions_kill_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "kill_session", payload)


@router.get("/{instance_id}/objects/table-stats")
def api_objects_table_stats(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/index-stats")
def api_objects_index_stats(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/unused-indexes")
def api_objects_unused_indexes(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/vacuum-status")
def api_objects_vacuum_status(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.post("/{instance_id}/objects/analyze-plan")
def api_objects_analyze_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "analyze", payload)


@router.post("/{instance_id}/objects/vacuum-plan")
def api_objects_vacuum_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "vacuum", payload)


@router.post("/{instance_id}/backup/pgdump-plan")
def api_backup_pgdump_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "pgdump", payload)


@router.post("/{instance_id}/backup/basebackup-plan")
def api_backup_basebackup_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "basebackup", payload)


@router.post("/{instance_id}/recovery/pitr-plan")
def api_recovery_pitr_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "pitr", payload)


@router.get("/{instance_id}/ha/replication-status")
def api_ha_replication_status(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "ha")


@router.get("/{instance_id}/ha/replication-lag")
def api_ha_replication_lag(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "ha")


@router.get("/{instance_id}/ha/patroni-status")
def api_ha_patroni_status(instance_id: int, db: Session = Depends(get_db)):
    data = _section(db, instance_id, "ha")
    data.setdefault("warnings", []).append("Patroni 状态需要配置 Patroni REST API 后采集。")
    return data


@router.post("/{instance_id}/ha/switchover-check-plan")
def api_ha_switchover_check_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "switchover_check", payload)


@router.get("/{instance_id}/governance/candidates")
def api_governance_candidates(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "governance")


@router.post("/{instance_id}/governance/partition-plan")
def api_governance_partition_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "partition", payload)


@router.post("/{instance_id}/governance/csv-export-plan")
def api_governance_csv_export_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "csv_export", payload)


@router.post("/{instance_id}/governance/archive-plan")
def api_governance_archive_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "archive", payload)


@inspect_router.post("/{instance_id}/inspect")
def api_inspect(instance_id: int, db: Session = Depends(get_db)):
    inst = _get(db, instance_id)
    return inspection_dispatch.run_through_queue(db, inst, trigger_type="manual")
