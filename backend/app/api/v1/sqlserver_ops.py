from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import generic_db_ops
from app.services import instances as instance_service
from app.services.inspection import dispatch as inspection_dispatch

router = APIRouter(prefix="/operations/sqlserver", tags=["SQL Server 运维中心"])
inspect_router = APIRouter(prefix="/sqlserver", tags=["SQL Server 巡检"])


def _get(db: Session, instance_id: int):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    if obj.db_type != "sqlserver":
        raise HTTPException(status_code=400, detail="Instance is not SQL Server")
    return obj


def _section(db: Session, instance_id: int, section: str):
    return generic_db_ops.collect_section(_get(db, instance_id), section)


def _plan(db: Session, instance_id: int, plan_type: str, payload: dict | None = None):
    return generic_db_ops.build_plan(_get(db, instance_id), plan_type, payload)


@router.get("/modules")
def api_modules():
    return generic_db_ops.get_modules("sqlserver")


@router.get("/{instance_id}/overview")
def api_overview(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "overview")


@router.get("/{instance_id}/configuration")
def api_configuration(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "overview")


@router.get("/{instance_id}/logins")
def api_logins(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "logins")


@router.get("/{instance_id}/space/databases")
def api_space_databases(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "space")


@router.get("/{instance_id}/space/files")
def api_space_files(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "space")


@router.get("/{instance_id}/space/tempdb")
def api_space_tempdb(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "space")


@router.post("/{instance_id}/space/growth-plan")
def api_space_growth_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "growth", payload)


@router.get("/{instance_id}/logs/error-log")
def api_logs_error_log(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "logs")


@router.get("/{instance_id}/logs/transaction-log")
def api_logs_transaction_log(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "logs")


@router.get("/{instance_id}/logs/vlf")
def api_logs_vlf(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "logs")


@router.post("/{instance_id}/logs/backup-log-plan")
def api_logs_backup_log_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "log_backup", payload)


@router.get("/{instance_id}/sessions/active")
def api_sessions_active(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/blocking")
def api_sessions_blocking(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/locks")
def api_sessions_locks(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.get("/{instance_id}/sessions/long-transactions")
def api_sessions_long_transactions(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "sessionTransaction")


@router.post("/{instance_id}/sessions/kill-plan")
def api_sessions_kill_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "kill_session", payload)


@router.get("/{instance_id}/objects/table-size")
def api_objects_table_size(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/index-fragmentation")
def api_objects_index_fragmentation(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/missing-indexes")
def api_objects_missing_indexes(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/unused-indexes")
def api_objects_unused_indexes(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.get("/{instance_id}/objects/stale-stats")
def api_objects_stale_stats(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "objectStats")


@router.post("/{instance_id}/objects/rebuild-index-plan")
def api_objects_rebuild_index_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "rebuild_index", payload)


@router.post("/{instance_id}/objects/update-stats-plan")
def api_objects_update_stats_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "update_stats", payload)


@router.get("/{instance_id}/backup/history")
def api_backup_history(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "backupRecovery")


@router.get("/{instance_id}/backup/chain")
def api_backup_chain(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "backupRecovery")


@router.post("/{instance_id}/backup/full-plan")
def api_backup_full_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "full", payload)


@router.post("/{instance_id}/backup/diff-plan")
def api_backup_diff_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "diff", payload)


@router.post("/{instance_id}/backup/log-plan")
def api_backup_log_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "log_backup", payload)


@router.post("/{instance_id}/recovery/restore-plan")
def api_recovery_restore_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "restore", payload)


@router.get("/{instance_id}/ha/ag-status")
def api_ha_ag_status(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "ha")


@router.get("/{instance_id}/ha/replicas")
def api_ha_replicas(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "ha")


@router.get("/{instance_id}/ha/databases")
def api_ha_databases(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "ha")


@router.post("/{instance_id}/ha/failover-check-plan")
def api_ha_failover_check_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "failover_check", payload)


@router.get("/{instance_id}/governance/candidates")
def api_governance_candidates(instance_id: int, db: Session = Depends(get_db)):
    return _section(db, instance_id, "governance")


@router.post("/{instance_id}/governance/partition-plan")
def api_governance_partition_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "partition", payload)


@router.post("/{instance_id}/governance/bcp-export-plan")
def api_governance_bcp_export_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "bcp_export", payload)


@router.post("/{instance_id}/governance/archive-plan")
def api_governance_archive_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    return _plan(db, instance_id, "archive", payload)


@inspect_router.post("/{instance_id}/inspect")
def api_inspect(instance_id: int, db: Session = Depends(get_db)):
    inst = _get(db, instance_id)
    return inspection_dispatch.run_through_queue(db, inst, trigger_type="manual")
